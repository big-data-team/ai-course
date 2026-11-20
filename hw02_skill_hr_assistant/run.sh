#!/usr/bin/env bash
# One isolated non-interactive run of prompt.txt in Claude Code: see ./run.sh --help.
set -u
set -o pipefail

STARTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROMPT_FILE="$STARTER_DIR/prompt.txt"
STAGES="noskill draft skill calib"
SKILL_NAME="hr-assistant"
# the last month of the course dataset and of its copy cut by make_calib_data.py
MAIN_LAST_MONTH="2026-11"
CALIB_LAST_MONTH="2024-12"
BUDGET_USD="2.00"
RUNS_ROOT_ARG="${HW02_RUNS_DIR:-$HOME/hw02_runs}"

usage() {
    cat <<EOF
Usage: ./run.sh <stage> <model> <number> <skill_dir | none> <data_dir> [--out DIR]

One isolated non-interactive run of prompt.txt in Claude Code (claude -p).

  stage      noskill (step 1), draft (step 2), skill (step 3) or calib (calibration)
  model      a short name (haiku, sonnet, opus) or a full model id, passed to claude --model
  number     the run number: 1, 2, 3, ...
  skill_dir  your skill directory: named $SKILL_NAME, with SKILL.md; none for noskill
  data_dir   the directory with hn_whoishiring_*.json

Options:
  --out DIR     where the three files of the run are copied, default ./runs
  --agent NAME  claude, the default; support for Codex and OpenCode is being added
  -h, --help    show this help

Run it from your solution folder, the one with skill/ and runs/. The run works in
  ${RUNS_ROOT_ARG}/<stage>_<model>_r<number>
(set HW02_RUNS_DIR to change it; outside any git repository): its own copy of the data,
the skill in .claude/skills/$SKILL_NAME/, the sandbox on, project settings only, no MCP and
no web search. The model in the name is lowercased and every character other than
a-z 0-9 . - becomes "-". After the run log.jsonl, stderr.txt and wall_seconds.txt are
copied to <out>/<stage>_<model>_r<number>/. Do not start runs in parallel.

Examples:
  ./run.sh noskill haiku 1 none data/
  ./run.sh draft haiku 1 my_draft/$SKILL_NAME data/
  ./run.sh skill haiku 1 skill/$SKILL_NAME data/
  ./run.sh calib haiku 1 skill/$SKILL_NAME data_2024/
EOF
}

die() {
    echo "error: $*" >&2
    exit 2
}

warn() {
    echo "warning: $*" >&2
}

# Path checks, settings.json and the summary after the run. Python handles the paths: they may hold
# spaces and quotes, and JSON must be written by a JSON library.
IFS= read -r -d '' HELPER <<'PY'
import json
import os
import sys

SKILL_NAME = "hr-assistant"
WITH_SKILL = ("draft", "skill", "calib")
WITH_SCRIPT = ("skill", "calib")
# On macOS /tmp and /var are symbolic links: a deny rule must catch both spellings of a path under them
ALIASED_ROOTS = ("/tmp", "/var")


def inside(path, parent):
    """True when path is parent itself or lies under it, both resolved."""
    real_path = os.path.realpath(path)
    real_parent = os.path.realpath(parent)
    prefix = real_parent.rstrip("/") + "/"
    within = real_path == real_parent or real_path.startswith(prefix)
    return within


def ancestors(path):
    """The resolved path and every directory above it, up to /."""
    current = os.path.realpath(path)
    chain = [current]
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return chain
        chain.append(parent)
        current = parent


def check(run_dir, blocked):
    """Stop when the run cannot be isolated; warn about files Claude Code may load into it."""
    runs_root = os.path.dirname(run_dir)
    chain = ancestors(runs_root)
    for directory in chain:
        git_path = os.path.join(directory, ".git")
        if os.path.exists(git_path):
            sys.exit(f"error: the runs directory {runs_root} is inside the git repository {directory}, "
                     f"Claude Code would load its .claude/ and CLAUDE.md: set HW02_RUNS_DIR outside any repository")
    for path in blocked:
        if inside(run_dir, path):
            sys.exit(f"error: the run directory {run_dir} would lie inside {path}, which the run must not read: "
                     f"set HW02_RUNS_DIR elsewhere or run run.sh from your solution folder")
    # ~/.claude holds the user settings, which --setting-sources project leaves out
    home = os.path.expanduser("~")
    home_claude = os.path.join(home, ".claude")
    real_home_claude = os.path.realpath(home_claude)
    for directory in chain:
        for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude"):
            found = os.path.join(directory, name)
            real_found = os.path.realpath(found)
            if os.path.exists(found) and real_found != real_home_claude:
                print(f"warning: {found} lies above the run directory and may get into the run: "
                      f"move it or set HW02_RUNS_DIR elsewhere", file=sys.stderr)


def spellings_of(path):
    """The resolved path and, under a root that is a symbolic link, the same path through that root."""
    resolved = os.path.realpath(path)
    real = resolved.rstrip("/") or "/"
    spellings = [real]
    for root in ALIASED_ROOTS:
        real_root = os.path.realpath(root)
        if real_root != root and real.startswith(real_root + "/"):
            prefix_length = len(real_root)
            alias = root + real[prefix_length:]
            spellings.append(alias)
    return spellings


def deny_rules(tool, path, is_dir):
    """Rules with absolute paths in the //path syntax, //path/** for a directory."""
    spellings = spellings_of(path)
    rules = []
    for spelling in spellings:
        pattern = f"{spelling}/**" if is_dir else spelling
        rules.append(f"{tool}(/{pattern})")
    return rules


def write_settings(run_dir, blocked):
    """Sandbox on, Bash allowed only inside it; no access to other runs, to the sources and to ~/.claude."""
    run_name = os.path.basename(run_dir)
    runs_root = os.path.dirname(run_dir)
    deny = ["Read(~/.claude/**)", "Edit(~/.claude/**)"]
    # the default temporary directory of Claude Code: the run writes into its own .tmp instead
    deny += deny_rules("Edit", "/tmp/claude", True)
    listed = os.listdir(runs_root)
    neighbours = [os.path.join(runs_root, entry) for entry in sorted(listed) if entry != run_name]
    targets = neighbours + blocked
    for target in targets:
        # a directory that does not exist yet, such as the --out directory, is still a directory
        is_dir = os.path.isdir(target) or not os.path.exists(target)
        for tool in ("Read", "Edit"):
            deny += deny_rules(tool, target, is_dir)
    ordered = dict.fromkeys(deny)
    unique = list(ordered)
    settings = {
        "sandbox": {"enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True,
                    "allowUnsandboxedCommands": False},
        "permissions": {"deny": unique},
    }
    settings_fpath = os.path.join(run_dir, ".claude", "settings.json")
    with open(settings_fpath, "w", encoding="utf-8") as fout:
        json.dump(settings, fout, indent=2)
        fout.write("\n")
    rule_count = len(unique)
    print(f"settings: {settings_fpath}, {rule_count} deny rules")


def read_events(log_fpath):
    events = []
    with open(log_fpath, encoding="utf-8") as fin:
        for line in fin:
            if not line.strip():
                continue
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
    return events


def tool_inputs(events, tool_name):
    """The input objects of every call of one tool by the agent, in log order."""
    inputs = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != tool_name:
                continue
            tool_input = block.get("input")
            if isinstance(tool_input, dict):
                inputs.append(tool_input)
    return inputs


def number_or_zero(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def summary(stage, log_fpath, shown_log):
    """What to look at in the log right away: the model, the skill and its script, as the Grader reads them."""
    print(f"log:              {shown_log}")
    try:
        events = read_events(log_fpath)
    except (OSError, ValueError) as error:
        print(f"warning: the log cannot be read ({error.__class__.__name__})", file=sys.stderr)
        return
    inits = [event for event in events if event.get("type") == "system" and event.get("subtype") == "init"]
    results = [event for event in events if event.get("type") == "result"]
    if not inits or not results:
        print("warning: the log has no init or no result event: the run did not finish", file=sys.stderr)
        return
    init, result = inits[0], results[-1]
    raw_skills = init.get("skills")
    skills = raw_skills if isinstance(raw_skills, list) else []
    available = SKILL_NAME in skills
    skill_inputs = tool_inputs(events, "Skill")
    skill_called = any(tool_input.get("skill") == SKILL_NAME for tool_input in skill_inputs)
    bash_inputs = tool_inputs(events, "Bash")
    commands = [tool_input.get("command") for tool_input in bash_inputs]
    script_run = any(isinstance(command, str) and "scripts/" in command for command in commands)
    raw_duration = result.get("duration_ms")
    duration_ms = number_or_zero(raw_duration)
    seconds = duration_ms / 1000
    subtype = result.get("subtype")
    model = init.get("model")
    raw_cost = result.get("total_cost_usd")
    cost = number_or_zero(raw_cost)
    turns = result.get("num_turns")
    print(f"model:            {model}")
    print(f"result:           {subtype}, {seconds:.1f} s, ${cost:.4f}, {turns} turns")
    print(f"skill available:  {'yes' if available else 'no'}")
    if stage in WITH_SKILL:
        print(f"Skill called:     {'yes' if skill_called else 'no'}")
    if stage in WITH_SCRIPT:
        print(f"scripts/ run:     {'yes' if script_run else 'no'}")
    if init.get("mcp_servers"):
        print("warning: MCP servers got into the run", file=sys.stderr)
    if subtype != "success":
        print("warning: the run did not finish with success", file=sys.stderr)
    if stage == "noskill" and available:
        print(f"warning: {SKILL_NAME} is available in a run without a skill: not a valid step 1 run", file=sys.stderr)
    if stage in WITH_SKILL and not skill_called:
        print(f'warning: no call of Skill "{SKILL_NAME}": the run says nothing about your skill', file=sys.stderr)
    if stage in WITH_SCRIPT and not script_run:
        print("warning: no command with scripts/ ran: the Grader zeroes step 3 for that", file=sys.stderr)


def main():
    command = sys.argv[1]
    if command == "check":
        check(sys.argv[2], sys.argv[3:])
    elif command == "settings":
        write_settings(sys.argv[2], sys.argv[3:])
    elif command == "summary":
        summary(sys.argv[2], sys.argv[3], sys.argv[4])


main()
PY

# ---------------------------------------------------------------- arguments
OUT=""
AGENT="claude"
N_POS=0
STAGE="" MODEL="" NUMBER="" SKILL_ARG="" DATA_ARG=""
add_positional() {
    N_POS=$((N_POS + 1))
    case "$N_POS" in
        1) STAGE="$1" ;;
        2) MODEL="$1" ;;
        3) NUMBER="$1" ;;
        4) SKILL_ARG="$1" ;;
        5) DATA_ARG="$1" ;;
    esac
}
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --out) [ $# -ge 2 ] || die "--out needs a directory"; OUT="$2"; shift 2 ;;
        --out=*) OUT="${1#--out=}"; shift ;;
        --agent) [ $# -ge 2 ] || die "--agent needs a name"; AGENT="$2"; shift 2 ;;
        --agent=*) AGENT="${1#--agent=}"; shift ;;
        -*) die "unknown option $1, see ./run.sh --help" ;;
        *) add_positional "$1"; shift ;;
    esac
done

case "$AGENT" in
    claude) ;;
    codex|opencode)
        echo "run.sh runs Claude Code only for now; support for Codex and OpenCode is being added:" \
             "write to the course channel if you work in them." >&2
        exit 2 ;;
    *) die "unknown agent $AGENT: run.sh runs Claude Code" ;;
esac

if [ "$N_POS" -ne 5 ]; then
    usage >&2
    exit 2
fi
case " $STAGES " in
    *" $STAGE "*) ;;
    *) die "stage must be one of: $STAGES; got \"$STAGE\"" ;;
esac
case "$NUMBER" in
    ''|*[!0-9]*|0*) die "the run number must be a positive integer, got \"$NUMBER\"" ;;
esac
MODEL_LOWER="$(printf '%s' "$MODEL" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
MODEL_LABEL="$(printf '%s' "$MODEL_LOWER" | LC_ALL=C sed 's/[^a-z0-9.-]/-/g')"
[ -n "$MODEL_LABEL" ] || die "the model is empty"

SKILL_SRC=""
if [ "$STAGE" = "noskill" ]; then
    [ "$SKILL_ARG" = "none" ] || die "stage noskill runs without a skill: pass none instead of \"$SKILL_ARG\""
else
    [ "$SKILL_ARG" != "none" ] || die "stage $STAGE needs a skill directory, see ./run.sh --help"
    [ -d "$SKILL_ARG" ] || die "no skill directory $SKILL_ARG"
    SKILL_LOGICAL="$(cd "$SKILL_ARG" && pwd)"
    SKILL_DIR_NAME="$(basename "$SKILL_LOGICAL")"
    [ "$SKILL_DIR_NAME" = "$SKILL_NAME" ] || die "the skill directory must be named $SKILL_NAME, got $SKILL_DIR_NAME"
    SKILL_SRC="$(cd "$SKILL_ARG" && pwd -P)"
    [ -f "$SKILL_SRC/SKILL.md" ] || die "no SKILL.md in $SKILL_ARG"
fi

[ -d "$DATA_ARG" ] || die "no data directory $DATA_ARG"
DATA_SRC="$(cd "$DATA_ARG" && pwd -P)"
LAST_FILE="$(ls "$DATA_SRC"/hn_whoishiring_*.json 2>/dev/null | sort | tail -n 1)"
[ -n "$LAST_FILE" ] || die "no hn_whoishiring_*.json in $DATA_ARG: unzip the dataset there (README, Datasets)"
LAST_MONTH="$(basename "$LAST_FILE" .json)"
LAST_MONTH="${LAST_MONTH#hn_whoishiring_}"
[ -f "$PROMPT_FILE" ] || die "no prompt.txt next to run.sh"

# ---------------------------------------------------------------- environment
command -v claude >/dev/null 2>&1 || die "claude is not in PATH: install Claude Code"
command -v python3 >/dev/null 2>&1 || die "python3 is not in PATH"
OS_NAME="$(uname -s)"
case "$OS_NAME" in
    Linux)
        for tool in bwrap socat; do
            command -v "$tool" >/dev/null 2>&1 || \
                die "the Claude Code sandbox on Linux needs the packages bubblewrap and socat: $tool is missing"
        done ;;
    MINGW*|MSYS*|CYGWIN*) die "run.sh needs WSL on Windows for now: the Claude Code sandbox does not run on native Windows" ;;
esac

# ---------------------------------------------------------------- paths
CWD="$(pwd -P)"
case "$OUT" in
    "") OUT_DIR="$CWD/runs" ;;
    /*) OUT_DIR="$OUT" ;;
    *) OUT_DIR="$CWD/$OUT" ;;
esac
case "$RUNS_ROOT_ARG" in
    /*) RUNS_ROOT_ABS="$RUNS_ROOT_ARG" ;;
    *) RUNS_ROOT_ABS="$CWD/$RUNS_ROOT_ARG" ;;
esac
RUN_PREFIX="${STAGE}_${MODEL_LABEL}_r"
RUN_NAME="${RUN_PREFIX}${NUMBER}"
OUT_RUN="$OUT_DIR/$RUN_NAME"

next_number() {
    candidate=$((NUMBER + 1))
    while [ -e "$OUT_DIR/$RUN_PREFIX$candidate" ] || [ -e "$RUNS_ROOT_ABS/$RUN_PREFIX$candidate" ]; do
        candidate=$((candidate + 1))
    done
    echo "$candidate"
}

# both checks come before the agent starts: a run directory is never reused
if [ -e "$OUT_RUN" ]; then
    die "$OUT_RUN already exists: take the next number, $(next_number)"
fi
if [ -e "$RUNS_ROOT_ABS/$RUN_NAME" ]; then
    die "$RUNS_ROOT_ABS/$RUN_NAME already exists: take the next number, $(next_number)"
fi
# The run reads only its own copies: not the source skill, the starter, the source data, the copies
# of past runs in --out, the solution folder it was started from or ~/.claude.
set -- "$STARTER_DIR" "$DATA_SRC" "$OUT_DIR" "$CWD" "$HOME/.claude"
if [ -n "$SKILL_SRC" ]; then
    set -- "$@" "$SKILL_SRC"
fi
python3 -I -c "$HELPER" check "$RUNS_ROOT_ABS/$RUN_NAME" "$@" || exit 2

case "$STAGE" in
    calib) EXPECTED_LAST_MONTH="$CALIB_LAST_MONTH" ;;
    *) EXPECTED_LAST_MONTH="$MAIN_LAST_MONTH" ;;
esac
if [ "$LAST_MONTH" != "$EXPECTED_LAST_MONTH" ]; then
    warn "the last month in $DATA_ARG is $LAST_MONTH, stage $STAGE expects $EXPECTED_LAST_MONTH:" \
         "Grader checks the snapshot from the course dataset"
fi

# ---------------------------------------------------------------- the run directory
mkdir -p "$RUNS_ROOT_ABS" || die "cannot create $RUNS_ROOT_ABS"
RUNS_ROOT="$(cd "$RUNS_ROOT_ABS" && pwd -P)"
RUN_DIR="$RUNS_ROOT/$RUN_NAME"
mkdir "$RUN_DIR" || die "cannot create $RUN_DIR"
mkdir -p "$RUN_DIR/data" "$RUN_DIR/.tmp" "$RUN_DIR/.claude/skills" || die "cannot fill $RUN_DIR"

# a clone costs no disk space where the file system supports it, otherwise a plain copy
if [ "$OS_NAME" = "Darwin" ]; then
    cp -c "$DATA_SRC"/hn_whoishiring_*.json "$RUN_DIR/data/" 2>/dev/null || \
        cp "$DATA_SRC"/hn_whoishiring_*.json "$RUN_DIR/data/" || die "cannot copy the data"
else
    cp --reflink=auto "$DATA_SRC"/hn_whoishiring_*.json "$RUN_DIR/data/" 2>/dev/null || \
        cp "$DATA_SRC"/hn_whoishiring_*.json "$RUN_DIR/data/" || die "cannot copy the data"
fi
# the agent reads this copy of the skill: the source directory is closed to it
if [ -n "$SKILL_SRC" ]; then
    mkdir "$RUN_DIR/.claude/skills/$SKILL_NAME" || die "cannot create the skill directory of the run"
    cp -R "$SKILL_SRC"/. "$RUN_DIR/.claude/skills/$SKILL_NAME"/ || die "cannot copy the skill"
fi
python3 -I -c "$HELPER" settings "$RUN_DIR" "$@" || die "cannot write the settings of the run"

# ---------------------------------------------------------------- the run
echo "run $RUN_NAME in $RUN_DIR: model $MODEL, data up to $LAST_MONTH"
case "$STAGE" in
    noskill|draft) echo "claude -p is working: without a tool the agent writes the analysis itself, it takes minutes" ;;
    *) echo "claude -p is working" ;;
esac
PROMPT="$(cat "$PROMPT_FILE")"
START="$(python3 -I -c 'import time; now = time.time(); print(now)')"
(
    cd "$RUN_DIR" || exit 1
    TMPDIR="$RUN_DIR/.tmp" CLAUDE_CODE_TMPDIR="$RUN_DIR/.tmp" claude -p "$PROMPT" --model "$MODEL" \
        --output-format stream-json --verbose --no-session-persistence --setting-sources project \
        --strict-mcp-config --max-budget-usd "$BUDGET_USD" \
        --allowedTools Bash Read Write Edit Glob Grep Skill --disallowedTools WebSearch WebFetch \
        < /dev/null > log.jsonl 2> stderr.txt
)
CODE=$?
END="$(python3 -I -c 'import time; now = time.time(); print(now)')"
python3 -I -c 'import sys; start, end = float(sys.argv[1]), float(sys.argv[2]); seconds = round(end - start, 1); print(seconds)' \
    "$START" "$END" > "$RUN_DIR/wall_seconds.txt"

# ---------------------------------------------------------------- the three files of the submission
mkdir -p "$OUT_DIR" && mkdir "$OUT_RUN" && \
    cp "$RUN_DIR/log.jsonl" "$RUN_DIR/stderr.txt" "$RUN_DIR/wall_seconds.txt" "$OUT_RUN/" || \
    warn "cannot copy the run files to $OUT_RUN: take them from $RUN_DIR"
echo "claude exit code: $CODE, wall time: $(cat "$RUN_DIR/wall_seconds.txt") s"
python3 -I -c "$HELPER" summary "$STAGE" "$RUN_DIR/log.jsonl" "$OUT_RUN/log.jsonl"
echo "check the answer: python3 \"$STARTER_DIR/check_answer.py\" \"$OUT_RUN/log.jsonl\""
exit "$CODE"
