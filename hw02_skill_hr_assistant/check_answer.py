#!/usr/bin/env python3
"""Check the form of the final answer in a run log: the contract of the prompt and the invariants.

There is no etalon here: OK says the answer is well formed, not that its numbers are right.
How right they are shows your own eval on the calibration data (3.2.8 of the assignment).
Step 1 (noskill) is not graded on its answer: there OK says the run is valid, the form is for information.

The final JSON is the last ```json block of the agent's final answer (the result event of the log);
without such a block, the whole answer if it is one JSON object. The Grader takes it the same way.
"""
import argparse
import collections
import json
import math
import os
import re
import sys

STAGES = ("noskill", "draft", "skill", "calib")
WITH_SKILL = ("draft", "skill", "calib")
SKILL_NAME = "hr-assistant"
RUN_DIR_RE = re.compile(r"^(?P<stage>[^_]+)_(?P<model>.+)_r(?P<number>[0-9]+)$")
JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.S)
DATA_FILE_RE = re.compile(r"^hn_whoishiring_(?P<month>\d{4}-\d{2})\.json$")
ANSWER_KEYS = ("relevant_vacancies", "top_skills_for_study", "series", "available_for_me_ids")
SERIES_ROWS = ("total_vacancies", "python_vacancies", "available_for_me")
TOP_SKILLS = 20
WINDOW_MONTHS = 12
# longer skill names and month keys are off the contract: the Grader keeps its records small
MAX_NAME_CHARS = 100
NOSKILL_NOTE = ("step 1 is not graded on quality; the run counts for the process and the speedup base "
                "if the agent finished and returned a JSON object")


# ---------------------------------------------------------------- the log

def read_events(log_fpath):
    """The events of a stream-json log; a broken line raises ValueError with its number."""
    events = []
    with open(log_fpath, encoding="utf-8") as fin:
        for number, line in enumerate(fin, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError as error:
                raise ValueError(f"line {number} is not valid JSON") from error
            if not isinstance(event, dict):
                raise ValueError(f"line {number} is not a JSON object")
            if not is_clean(event):
                raise ValueError(f"line {number} holds a lone surrogate escape")
            events.append(event)
    return events


def is_clean(value):
    """A value without a lone surrogate, which a JSON escape such as \\ud800 gives: the Grader refuses it."""
    try:
        dumped = json.dumps(value, ensure_ascii=False)
        dumped.encode("utf-8")
    except (ValueError, RecursionError, UnicodeEncodeError):
        return False
    return True


def extract_answer(result_text):
    """(answer, source): the last ```json block, else the whole text if it is one JSON object."""
    blocks = JSON_BLOCK_RE.findall(result_text)
    candidate = blocks[-1] if blocks else result_text
    try:
        answer = json.loads(candidate)
    except (ValueError, RecursionError):
        answer = None
    if answer is not None and not is_clean(answer):
        answer = None
    if blocks:
        source = "the last ```json block"
    elif isinstance(answer, dict):
        source = "the whole answer"
    else:
        answer, source = None, None
    return answer, source


# ---------------------------------------------------------------- the contract of the prompt

def is_number(value):
    """A finite JSON number; bool is an int in Python but not a number in an answer."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    finite = math.isfinite(value)
    return finite


def is_integer_id(value):
    integer = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return integer


def long_keys(mapping):
    keys = [key for key in mapping if len(key) > MAX_NAME_CHARS]
    return keys


def prompt_contract_problems(answer, stage):
    """The form the prompt asks for, the same for every stage."""
    if not isinstance(answer, dict):
        return ["no JSON object in the final answer: put it into a ```json block at the end"]
    problems = []
    missing = [key for key in ANSWER_KEYS if key not in answer]
    if missing:
        joined = ", ".join(missing)
        problems.append(f"missing keys: {joined}")
    if "relevant_vacancies" in answer and not is_number(answer["relevant_vacancies"]):
        problems.append("relevant_vacancies must be a number")
    skills = answer.get("top_skills_for_study")
    if "top_skills_for_study" in answer:
        if not isinstance(skills, dict):
            problems.append("top_skills_for_study must be an object {skill: percent}")
        else:
            skill_count = len(skills)
            if skill_count != TOP_SKILLS:
                problems.append(f"top_skills_for_study: {skill_count} skills instead of {TOP_SKILLS}")
            bad_values = [name for name, value in skills.items() if not is_number(value) or not 0 <= value <= 100]
            if bad_values:
                problems.append(f"top_skills_for_study: values must be numbers from 0 to 100, see {bad_values[:3]}")
            long_names = long_keys(skills)
            if long_names:
                long_count = len(long_names)
                problems.append(f"top_skills_for_study: {long_count} names longer than {MAX_NAME_CHARS} characters")
    series = answer.get("series")
    if "series" in answer:
        row_values = [series.get(row) for row in SERIES_ROWS] if isinstance(series, dict) else []
        rows_ok = len(row_values) == len(SERIES_ROWS) and all(isinstance(value, dict) for value in row_values)
        if not rows_ok:
            row_names = ", ".join(SERIES_ROWS)
            problems.append(f"series must hold three monthly objects: {row_names}")
        else:
            for row in SERIES_ROWS:
                bad_months = [month for month, value in series[row].items() if not is_number(value) or value < 0]
                if bad_months:
                    problems.append(f"series.{row}: values must be non-negative numbers, see {bad_months[:3]}")
                long_months = long_keys(series[row])
                if long_months:
                    long_count = len(long_months)
                    problems.append(f"series.{row}: {long_count} keys longer than {MAX_NAME_CHARS} characters")
    ids = answer.get("available_for_me_ids")
    if "available_for_me_ids" in answer:
        if not isinstance(ids, list):
            problems.append("available_for_me_ids must be a list of post ids")
        else:
            not_integers = [post_id for post_id in ids if not is_integer_id(post_id)]
            if not_integers:
                bad_count = len(not_integers)
                problems.append(f"available_for_me_ids: {bad_count} items are not integer ids, "
                                f"for example {not_integers[:3]} (an id is a number, not a string)")
            elif stage in WITH_SKILL:
                unique = set(ids)
                repeated = len(ids) - len(unique)
                if repeated:
                    problems.append(f"available_for_me_ids: {repeated} ids repeat")
    return problems


# ---------------------------------------------------------------- the data and the window

def month_after(month, step):
    year, number = int(month[:4]), int(month[5:7])
    index = year * 12 + number - 1 + step
    shifted = f"{index // 12:04d}-{index % 12 + 1:02d}"
    return shifted


def data_months(data_dir):
    """{month: file path} of the hn_whoishiring_YYYY-MM.json files of a directory."""
    months = {}
    if not data_dir or not os.path.isdir(data_dir):
        return months
    names = os.listdir(data_dir)
    for name in sorted(names):
        matched = DATA_FILE_RE.match(name)
        if matched:
            months[matched["month"]] = os.path.join(data_dir, name)
    return months


def window_of(months):
    """The 12 calendar months that end with the last month of the data."""
    last = max(months)
    window = []
    for step in range(1 - WINDOW_MONTHS, 1):
        month = month_after(last, step)
        window.append(month)
    return window


def post_months(months, window):
    """{post id: month} over the window: a post is a top-level element of children with a non-empty text."""
    found = {}
    for month in window:
        fpath = months.get(month)
        if fpath is None:
            continue
        with open(fpath, encoding="utf-8") as fin:
            thread = json.load(fin)
        children = thread.get("children") or []
        for child in children:
            if not isinstance(child, dict) or not child.get("text"):
                continue
            post_id = child.get("id")
            if is_integer_id(post_id):
                found[post_id] = month
    return found


def find_data(log_fpath, data_arg):
    """--data, else data/ next to the log, else the run directory of run.sh under HW02_RUNS_DIR."""
    if data_arg:
        return data_arg
    log_fpath_abs = os.path.abspath(log_fpath)
    log_dir = os.path.dirname(log_fpath_abs)
    run_name = os.path.basename(log_dir)
    default_root = os.path.expanduser("~/hw02_runs")
    runs_root = os.environ.get("HW02_RUNS_DIR") or default_root
    candidates = [os.path.join(log_dir, "data"), os.path.join(runs_root, run_name, "data")]
    for candidate in candidates:
        months = data_months(candidate)
        if months:
            return candidate
    return None


def skill_problems(answer, window, posts):
    """The extras of a run with the skill: the window, the invariants and the ids against the posts."""
    series = answer["series"]
    period = f"{window[0]}..{window[-1]}"
    problems = []
    for row in SERIES_ROWS:
        months = sorted(series[row])
        if months != window:
            month_count = len(months)
            problems.append(f"series.{row}: the months must be exactly the 12 months of the window {period}, "
                            f"got {month_count} months")
    if problems:
        return problems
    for month in window:
        available = series["available_for_me"][month]
        python = series["python_vacancies"][month]
        total = series["total_vacancies"][month]
        if not available <= python <= total:
            problems.append(f"{month}: available_for_me <= python_vacancies <= total_vacancies does not hold "
                            f"({available}, {python}, {total})")
    python_sum = sum(series["python_vacancies"][month] for month in window)
    if abs(answer["relevant_vacancies"] - python_sum) > 0.5:
        problems.append(f"relevant_vacancies is {answer['relevant_vacancies']}, "
                        f"but python_vacancies sums to {python_sum} over the window")
    ids = answer["available_for_me_ids"]
    outside = [post_id for post_id in ids if post_id not in posts]
    if outside:
        outside_count = len(outside)
        problems.append(f"available_for_me_ids: {outside_count} ids are not posts of the window {period}, "
                        f"for example {outside[:3]}")
    months_of_ids = [posts[post_id] for post_id in ids if post_id in posts]
    by_month = collections.Counter(months_of_ids)
    for month in window:
        expected = series["available_for_me"][month]
        found = by_month.get(month, 0)
        if found != expected:
            problems.append(f"{month}: {found} ids in available_for_me_ids, but available_for_me is {expected}")
    return problems


def invariant_problems(answer):
    """The invariants that need no data: per month and the sum, over the months the answer has."""
    series = answer["series"]
    problems = []
    common = set(series["total_vacancies"]) & set(series["python_vacancies"]) & set(series["available_for_me"])
    for month in sorted(common):
        available = series["available_for_me"][month]
        python = series["python_vacancies"][month]
        total = series["total_vacancies"][month]
        if not available <= python <= total:
            problems.append(f"{month}: available_for_me <= python_vacancies <= total_vacancies does not hold "
                            f"({available}, {python}, {total})")
    python_values = series["python_vacancies"].values()
    python_sum = sum(python_values)
    if abs(answer["relevant_vacancies"] - python_sum) > 0.5:
        problems.append(f"relevant_vacancies is {answer['relevant_vacancies']}, "
                        f"but python_vacancies sums to {python_sum}")
    available_values = series["available_for_me"].values()
    available_sum = sum(available_values)
    id_count = len(answer["available_for_me_ids"])
    if id_count != available_sum:
        problems.append(f"available_for_me_ids has {id_count} ids, but available_for_me sums to {available_sum}")
    return problems


# ---------------------------------------------------------------- the report

def print_series(answer, posts):
    series = answer.get("series") if isinstance(answer, dict) else None
    if not isinstance(series, dict):
        return
    rows = {}
    for row in SERIES_ROWS:
        values = series.get(row)
        rows[row] = values if isinstance(values, dict) else {}
    months = set()
    for row in SERIES_ROWS:
        months.update(rows[row])
    ids = answer.get("available_for_me_ids")
    id_list = ids if isinstance(ids, list) else []
    months_of_ids = [posts[post_id] for post_id in id_list if is_integer_id(post_id) and post_id in posts]
    by_month = collections.Counter(months_of_ids)
    print("series:")
    header = f"  {'month':10} {'total':>7} {'python':>7} {'available':>9}"
    if posts:
        header += f" {'ids':>5}"
    print(header)
    for month in sorted(months, key=str):
        values = [rows[row].get(month, "-") for row in SERIES_ROWS]
        line = f"  {str(month)[:10]:10} {str(values[0]):>7} {str(values[1]):>7} {str(values[2]):>9}"
        if posts:
            line += f" {by_month.get(month, 0):>5}"
        print(line)


def print_skills(answer):
    skills = answer.get("top_skills_for_study") if isinstance(answer, dict) else None
    if not isinstance(skills, dict):
        return
    numeric = {name: value for name, value in skills.items() if is_number(value)}
    ordered = sorted(numeric, key=lambda name: (-numeric[name], name))
    print("top_skills_for_study:")
    for name in ordered:
        print(f"  {numeric[name]:6.1f}%  {name}")
    others = [name for name in skills if name not in numeric]
    for name in others:
        print(f"  {'?':>7}  {name}: {skills[name]!r}")


def step1_reasons(events, result):
    """Why a run of step 1 does not count for the process, by the rules of the Grader for a valid run.

    The Grader also drops a run whose log is a copy of another run: pack.py --check compares the runs.
    """
    reasons = []
    if result.get("subtype") != "success":
        reasons.append("the run did not finish with success")
    sessions = set()
    for event in events:
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            sessions.add(session_id)
    session_count = len(sessions)
    if session_count != 1:
        reasons.append(f"{session_count} session ids in one log, expected 1")
    inits = [event for event in events if event.get("type") == "system" and event.get("subtype") == "init"]
    init = inits[0] if inits else {}
    model = init.get("model")
    if not isinstance(model, str) or not model:
        reasons.append("no model id in the log")
    raw_skills = init.get("skills")
    skills = raw_skills if isinstance(raw_skills, list) else []
    if SKILL_NAME in skills:
        reasons.append(f"the skill {SKILL_NAME} is available in a run without a skill")
    return reasons


def stage_from_path(log_fpath):
    log_fpath_abs = os.path.abspath(log_fpath)
    log_dir = os.path.dirname(log_fpath_abs)
    run_name = os.path.basename(log_dir)
    matched = RUN_DIR_RE.match(run_name)
    if matched and matched["stage"] in STAGES:
        return matched["stage"]
    return None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Check the form of the final answer in a run log: the contract of prompt.txt for every "
                    "stage, plus the window, the invariants and the ids for the stages with the skill. "
                    "No etalon: OK means well formed, not right. For noskill OK means a valid run of step 1, "
                    "the form of its answer is for information.",
        epilog="The stage comes from the run directory <stage>_<model>_r<k>; draft is checked as skill. "
               "The data default to data/ next to the log, then to ${HW02_RUNS_DIR:-~/hw02_runs}/<run>/data; "
               "without data the window checks are skipped.")
    parser.add_argument("log", help="log.jsonl of a run, for example runs/skill_haiku_r1/log.jsonl")
    parser.add_argument("--stage", choices=STAGES, help="the stage when the directory name does not tell it")
    parser.add_argument("--data", help="the data directory of the run, with hn_whoishiring_*.json")
    args = parser.parse_args()
    return args


def main():
    args = parse_args()
    stage = args.stage or stage_from_path(args.log)
    if stage is None:
        sys.exit(f"error: cannot tell the stage from the directory of {args.log}: pass --stage")
    try:
        events = read_events(args.log)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        sys.exit(f"error: cannot read {args.log}: {error}")
    results = [event for event in events if event.get("type") == "result"]
    if not results:
        print(f"{args.log}: no result event, the run did not finish")
        print("FAIL")
        return 1
    result = results[-1]
    raw_text = result.get("result")
    result_text = raw_text if isinstance(raw_text, str) else ""
    answer, source = extract_answer(result_text)
    subtype = result.get("subtype")
    print(f"log:    {args.log}")
    print(f"stage:  {stage}")
    print(f"run:    {subtype}")
    print(f"answer: {source or 'no JSON object found'}")

    problems = prompt_contract_problems(answer, stage)
    posts, notes = {}, []
    if stage in WITH_SKILL and not problems:
        data_dir = find_data(args.log, args.data)
        months = data_months(data_dir)
        if months:
            window = window_of(months)
            posts = post_months(months, window)
            print(f"data:   {data_dir}, window {window[0]}..{window[-1]}")
            window_problems = skill_problems(answer, window, posts)
            problems.extend(window_problems)
        else:
            notes.append("no data found: the window and the ids against the posts are not checked (pass --data)")
            print("data:   not found")
            invariants = invariant_problems(answer)
            problems.extend(invariants)

    print_series(answer, posts)
    print_skills(answer)
    for note in notes:
        print(f"note: {note}")
    if stage == "noskill":
        code = report_step1(events, result, answer, problems)
        return code
    if problems:
        print("problems:")
        for problem in problems:
            print(f"  - {problem}")
        print("FAIL")
        return 1
    print("OK")
    return 0


def report_step1(events, result, answer, problems):
    """Step 1 is judged by the run, not by the form of its answer: the form is only for your information."""
    if problems:
        print(f"form of the answer (informational: {NOSKILL_NOTE}):")
        for problem in problems:
            print(f"  - {problem}")
    reasons = step1_reasons(events, result)
    if reasons:
        print("step 1: not a valid run, it does not count for the process and the speedup base:")
        for reason in reasons:
            print(f"  - {reason}")
        print("FAIL")
        return 1
    if isinstance(answer, dict):
        print("step 1: a valid run with a JSON object: it counts for the process and the speedup base")
    else:
        print("step 1: a valid run without a JSON object: it counts for the process, not for the speedup base")
    print("OK")
    return 0


if __name__ == "__main__":
    code = main()
    sys.exit(code)
