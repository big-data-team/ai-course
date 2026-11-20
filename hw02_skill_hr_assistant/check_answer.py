#!/usr/bin/env python3
"""Check the form of the final answer in a run log: the contract of the prompt and the invariants.

There is no etalon here: OK says the answer is well formed, not that its numbers are right.
How right they are shows your own eval on the calibration data (3.2.8 of the assignment).
Step 1 (noskill) is not graded on its answer: there OK says the run is valid, the form is for information.

The final JSON is the last ```json block of the agent's final answer; without such a block, the whole
answer if it is one JSON object. The Grader takes it the same way. The final answer of Claude Code is its
result event, of Codex its last agent_message, of OpenCode the text of its last step that stopped. Codex
and OpenCode name no model in the log: agent.json next to the log does (run.sh writes it).
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
AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"
AGENT_OPENCODE = "opencode"
AGENTS = (AGENT_CLAUDE, AGENT_CODEX, AGENT_OPENCODE)
AGENT_FILENAME = "agent.json"
MAX_AGENT_FILE_BYTES = 4 * 1024
RUN_SUCCESS = "success"
# Codex has no skill tool: a command inside the skill directory uses the skill; no trailing slash, so
# "cd .agents/skills/hr-assistant && python3 scripts/x.py" counts too
SKILL_DIR_MARKER = f"skills/{SKILL_NAME}"
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
NO_JSON_WARNING = "warning: the final message has no JSON: this run cannot be the speedup base"


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


# ---------------------------------------------------------------- the agent of the log

def is_claude_event(event):
    marked = event.get("type") == "system" and event.get("subtype") == "init"
    return marked


def is_codex_event(event):
    marked = event.get("type") == "thread.started"
    return marked


def is_opencode_event(event):
    """Every OpenCode event carries sessionID and a part; its error event carries sessionID and no part."""
    if "sessionID" not in event:
        return False
    marked = "part" in event or event.get("type") == "error"
    return marked


AGENT_MARKS = ((AGENT_CLAUDE, is_claude_event), (AGENT_CODEX, is_codex_event), (AGENT_OPENCODE, is_opencode_event))


def log_agent(events):
    """The agent of a log by the marks of its format, Claude Code without any; ValueError for two agents."""
    agents = [agent for agent, recognize in AGENT_MARKS if any(recognize(event) for event in events)]
    if len(agents) > 1:
        joined = ", ".join(agents)
        raise ValueError(f"events of several agents in one log: {joined}")
    agent = agents[0] if agents else AGENT_CLAUDE
    return agent


def agent_key_problem(raw, key, kind):
    """What breaks one required string key of agent.json, None when it holds: the reason names the key."""
    if key not in raw:
        return f'no "{key}"'
    value = raw[key]
    if not isinstance(value, str):
        return f'"{key}" must be {kind}'
    if not is_clean(value):
        return f'"{key}" holds a lone surrogate escape'
    if key == "model" and value.strip() == "":
        return f'"{key}" is empty'
    return None


def agent_file_fields(content):
    """(the fields of agent.json, None) or (None, why the Grader cannot read the run), in the order of the Grader."""
    bad = f"bad {AGENT_FILENAME}, the Grader cannot read this run"
    if len(content) > MAX_AGENT_FILE_BYTES:
        return None, f"{bad}: over {MAX_AGENT_FILE_BYTES // 1024} KB"
    try:
        text = content.decode("utf-8")
        raw = json.loads(text)
    except (ValueError, RecursionError):
        return None, f"{bad}: not valid JSON"
    if not isinstance(raw, dict):
        return None, f"{bad}: not a JSON object"
    supported = ", ".join(AGENTS)
    agent_problem = agent_key_problem(raw, "agent", f"one of {supported}")
    if agent_problem:
        return None, f"{bad}: {agent_problem}"
    agent = raw["agent"]
    if agent not in AGENTS:
        return None, f"{AGENT_FILENAME} names an unknown agent {agent!r}, supported: {supported}"
    for field in ("model", "agent_version"):
        field_problem = agent_key_problem(raw, field, "a string")
        if field_problem:
            return None, f"{bad}: {field_problem}"
    fields = {"agent": agent, "model": raw["model"], "version": raw["agent_version"]}
    return fields, None


def read_agent_file(log_fpath):
    """(agent.json next to the log or None, the problem or None): the rules of the Grader for the file."""
    log_dir = os.path.dirname(os.path.abspath(log_fpath))
    fpath = os.path.join(log_dir, AGENT_FILENAME)
    if not os.path.exists(fpath):
        return None, None
    try:
        with open(fpath, "rb") as fin:
            content = fin.read(MAX_AGENT_FILE_BYTES + 1)
    except OSError:
        return None, f"bad {AGENT_FILENAME}, the Grader cannot read this run: cannot be read"
    agent_file, problem = agent_file_fields(content)
    return agent_file, problem


def dict_of(value):
    found = value if isinstance(value, dict) else {}
    return found


def session_ids(events, key):
    """The session ids of a log under one key: session_id of Claude Code, sessionID of OpenCode."""
    sessions = set()
    for event in events:
        session = event.get(key)
        if isinstance(session, str) and session:
            sessions.add(session)
    return sessions


def claude_run(events):
    """A claude -p log: the result event is the final answer, the init event names the model and the skills."""
    results = [event for event in events if event.get("type") == "result"]
    result = results[-1] if results else {}
    raw_text = result.get("result")
    inits = [event for event in events if is_claude_event(event)]
    init = inits[0] if inits else {}
    raw_skills = init.get("skills")
    skills = raw_skills if isinstance(raw_skills, list) else []
    sessions = session_ids(events, "session_id")
    run = {"finished": bool(results), "subtype": result.get("subtype"),
           "final_text": raw_text if isinstance(raw_text, str) else "", "model": init.get("model"),
           "session_count": len(sessions), "skill_available": SKILL_NAME in skills, "skill_used": False}
    return run


def codex_run(events):
    """A codex exec --json log: the last agent_message is the final answer, turn.failed fails the run.

    The log names no model; a command inside the skill directory uses the skill.
    """
    items = []
    for event in events:
        if event.get("type") == "item.completed":
            raw_item = event.get("item")
            item = dict_of(raw_item)
            items.append(item)
    messages = [item for item in items if item.get("type") == "agent_message"]
    raw_text = messages[-1].get("text") if messages else ""
    used = False
    for item in items:
        if item.get("type") == "command_execution":
            raw_command = item.get("command", "")
            command = str(raw_command)
            used = used or SKILL_DIR_MARKER in command
    failed = any(event.get("type") == "turn.failed" for event in events)
    completed = any(event.get("type") == "turn.completed" for event in events)
    if failed:
        subtype = "turn_failed"
    elif not completed:
        subtype = "no_turn_completed"
    else:
        subtype = RUN_SUCCESS
    threads = [event for event in events if is_codex_event(event)]
    run = {"finished": failed or completed, "subtype": subtype,
           "final_text": raw_text if isinstance(raw_text, str) else "", "model": None,
           "session_count": len(threads), "skill_available": None, "skill_used": used}
    return run


def opencode_run(events):
    """An opencode run --format json log: the final answer is the text of the last step that stopped.

    The log names no model; the skill tool or a command inside the skill directory uses the skill.
    """
    finishes = []
    for event in events:
        if event.get("type") == "step_finish":
            raw_part = event.get("part")
            part = dict_of(raw_part)
            finishes.append(part)
    stops = [part for part in finishes if part.get("reason") == "stop"]
    stop_message = stops[-1].get("messageID") if stops else None
    texts, used = [], False
    for event in events:
        raw_part = event.get("part")
        part = dict_of(raw_part)
        if event.get("type") == "text" and stop_message is not None and part.get("messageID") == stop_message:
            text = part.get("text")
            kept_text = text if isinstance(text, str) else ""
            texts.append(kept_text)
        if event.get("type") != "tool_use":
            continue
        raw_state = part.get("state")
        state = dict_of(raw_state)
        raw_input = state.get("input")
        tool_input = dict_of(raw_input)
        raw_command = tool_input.get("command", "")
        command = str(raw_command)
        skill_names = (tool_input.get("name"), tool_input.get("id"))
        called = part.get("tool") == "skill" and SKILL_NAME in skill_names
        ran = part.get("tool") == "bash" and SKILL_DIR_MARKER in command
        used = used or called or ran
    failed = any(event.get("type") == "error" for event in events)
    stopped = bool(finishes) and finishes[-1].get("reason") == "stop"
    if failed:
        subtype = "error"
    elif not stopped:
        subtype = "not_stopped"
    else:
        subtype = RUN_SUCCESS
    sessions = session_ids(events, "sessionID")
    final_text = "".join(texts)
    run = {"finished": failed or bool(finishes), "subtype": subtype, "final_text": final_text, "model": None,
           "session_count": len(sessions), "skill_available": None, "skill_used": used}
    return run


AGENT_RUNS = {AGENT_CLAUDE: claude_run, AGENT_CODEX: codex_run, AGENT_OPENCODE: opencode_run}


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


def answer_state(answer, source):
    """Where the final JSON was found; a ```json block that gives no object says so instead."""
    if source is None:
        return "no JSON object found"
    if answer is None:
        return f"{source} is not valid JSON"
    if not isinstance(answer, dict):
        return f"{source} is not a JSON object"
    return source


def prompt_contract_problems(answer, stage, source=None):
    """The form the prompt asks for, the same for every stage."""
    if not isinstance(answer, dict) and source is not None:
        state = answer_state(answer, source)
        return [state]
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


def agent_problems(agent, agent_file, agent_problem):
    """Why the Grader cannot read the run at all: a bad agent.json or one of another agent."""
    if agent_problem:
        return [agent_problem]
    if agent_file is not None and agent_file["agent"] != agent:
        mismatch = f"a {agent} log, but {AGENT_FILENAME} names {agent_file['agent']}: the Grader cannot read this run"
        return [mismatch]
    return []


def run_model(run, agent, agent_file):
    """The model of the run: the init event of Claude Code, agent.json of Codex and OpenCode; None without it."""
    model = run["model"] if agent == AGENT_CLAUDE else (agent_file or {}).get("model")
    if not isinstance(model, str) or not model:
        return None
    return model


def step1_reasons(run, agent, agent_file):
    """Why a run of step 1 does not count for the process, by the rules of the Grader for a valid run.

    The Grader also drops a run whose log is a copy of another run: pack.py --check compares the runs.
    """
    reasons = []
    if run["subtype"] != RUN_SUCCESS:
        reasons.append("the run did not finish with success")
    session_count = run["session_count"]
    if session_count != 1:
        reasons.append(f"{session_count} session ids in one log, expected 1")
    model = run_model(run, agent, agent_file)
    if model is None and agent == AGENT_CLAUDE:
        reasons.append("no model id in the log")
    elif model is None:
        reasons.append(f"no model id: no {AGENT_FILENAME} next to the log")
    if run["skill_available"]:
        reasons.append(f"the skill {SKILL_NAME} is available in a run without a skill")
    if agent != AGENT_CLAUDE and run["skill_used"]:
        reasons.append(f"the skill {SKILL_NAME} was used in a run without a skill")
    return reasons


def agent_line(run, agent, agent_file, agent_problem):
    """The agent and the model of the run for the report."""
    model = run_model(run, agent, agent_file)
    if agent == AGENT_CLAUDE:
        line = f"{agent}, model {model or 'none'}"
        return line
    if agent_problem:
        line = f"{agent}, no model: {AGENT_FILENAME} is not readable, see the problems"
        return line
    if agent_file is None or model is None:
        line = f"{agent}, no model: no {AGENT_FILENAME} next to the log"
        return line
    if agent_file["agent"] != agent:
        line = f"{agent}, but {AGENT_FILENAME} names {agent_file['agent']}"
        return line
    line = f"{agent}, model {model} ({AGENT_FILENAME}, version {agent_file['version']})"
    return line


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
        agent = log_agent(events)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        sys.exit(f"error: cannot read {args.log}: {error}")
    agent_file, agent_problem = read_agent_file(args.log)
    read_agent_run = AGENT_RUNS[agent]
    run = read_agent_run(events)
    if not run["finished"]:
        missing = "no result event" if agent == AGENT_CLAUDE else f"no final event of {agent}"
        print(f"{args.log}: {missing}, the run did not finish")
        print("FAIL")
        return 1
    answer, source = extract_answer(run["final_text"])
    shown_agent = agent_line(run, agent, agent_file, agent_problem)
    print(f"log:    {args.log}")
    print(f"agent:  {shown_agent}")
    print(f"stage:  {stage}")
    print(f"run:    {run['subtype']}")
    shown_answer = answer_state(answer, source)
    print(f"answer: {shown_answer}")

    unreadable = agent_problems(agent, agent_file, agent_problem)
    problems = prompt_contract_problems(answer, stage, source)
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
        code = report_step1(run, agent, agent_file, unreadable, answer, problems)
        return code
    problems = unreadable + problems
    if problems:
        print("problems:")
        for problem in problems:
            print(f"  - {problem}")
        print("FAIL")
        return 1
    print("OK")
    return 0


def report_step1(run, agent, agent_file, unreadable, answer, problems):
    """Step 1 is judged by the run, not by the form of its answer: the form is only for your information."""
    if problems:
        print(f"form of the answer (informational: {NOSKILL_NOTE}):")
        for problem in problems:
            print(f"  - {problem}")
    reasons = unreadable or step1_reasons(run, agent, agent_file)
    if reasons:
        print("step 1: not a valid run, it does not count for the process and the speedup base:")
        for reason in reasons:
            print(f"  - {reason}")
        print("FAIL")
        return 1
    if isinstance(answer, dict):
        print("step 1: a valid run with a JSON object: it counts for the process and the speedup base")
    else:
        print(NO_JSON_WARNING)
        print("step 1: a valid run: it counts for the process, not for the speedup base")
    print("OK")
    return 0


if __name__ == "__main__":
    code = main()
    sys.exit(code)
