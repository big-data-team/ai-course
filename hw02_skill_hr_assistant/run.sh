#!/usr/bin/env bash
# One isolated non-interactive run of prompt.txt in Claude Code, Codex or OpenCode: see ./run.sh --help.
set -u
set -o pipefail

STARTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROMPT_FILE="$STARTER_DIR/prompt.txt"
STAGES="noskill draft skill calib"
AGENTS="claude codex opencode"
SKILL_NAME="hr-assistant"
# the last month of the course dataset and of its copy cut by make_calib_data.py
MAIN_LAST_MONTH="2026-11"
CALIB_LAST_MONTH="2024-12"
BUDGET_USD="2.00"
RUNS_ROOT_ARG="${HW02_RUNS_DIR:-$HOME/hw02_runs}"
# Codex and OpenCode have no budget cap of their own: a run that hangs is stopped after this time
TIMEOUT_SECONDS="${HW02_TIMEOUT_SECONDS:-900}"
# the versions run.sh was tested with: another version may rename a flag
CODEX_TESTED="0.153.0-alpha.5"
OPENCODE_TESTED="1.18.30"
CODEX_APP_BINARY="/Applications/Codex.app/Contents/Resources/codex"

usage() {
    cat <<EOF
Usage: ./run.sh <stage> <model> <number> <skill_dir | none> <data_dir> [--agent NAME] [--out DIR]

One isolated non-interactive run of prompt.txt in Claude Code (claude -p), Codex (codex exec)
or OpenCode (opencode run).

  stage      noskill (step 1), draft (step 2), skill (step 3) or calib (calibration)
  model      Claude Code: a short name (haiku, sonnet, opus) or a full model id;
             Codex: a full model id, such as gpt-5.6-luna;
             OpenCode: provider/model, such as anthropic/claude-haiku-4-5
  number     the run number: 1, 2, 3, ...
  skill_dir  your skill directory: named $SKILL_NAME, with SKILL.md; none for noskill
  data_dir   the directory with hn_whoishiring_*.json

Options:
  --agent NAME  claude (the default), codex or opencode
  --out DIR     where the files of the run are copied, default ./runs
  -h, --help    show this help

Run it from your solution folder, the one with skill/ and runs/. The run works in
  ${RUNS_ROOT_ARG}/<stage>_<model>_r<number>
(set HW02_RUNS_DIR to change it; outside any git repository): its own copy of the data and
of the skill, no MCP and no web search. The skill goes where the agent looks for it:
  claude    .claude/skills/$SKILL_NAME/    sandbox on, project settings only, budget \$$BUDGET_USD
  codex     .agents/skills/$SKILL_NAME/    sandbox: writes only into the run, no network
  opencode  .opencode/skills/$SKILL_NAME/  opencode.json of the run; its bash has no sandbox
Codex and OpenCode have no budget cap: a run is stopped after HW02_TIMEOUT_SECONDS
(default 900) seconds. The model in the name is lowercased and every character other
than a-z 0-9 . - becomes "-" (anthropic/claude-haiku-4-5 -> anthropic-claude-haiku-4-5).
After the run log.jsonl, stderr.txt, wall_seconds.txt and, for Codex and OpenCode,
agent.json are copied to <out>/<stage>_<model>_r<number>/. Do not start runs in parallel.
Codex $CODEX_TESTED and OpenCode $OPENCODE_TESTED were tested on macOS only.

Examples:
  ./run.sh noskill haiku 1 none data/
  ./run.sh draft haiku 1 my_draft/$SKILL_NAME data/
  ./run.sh skill haiku 1 skill/$SKILL_NAME data/
  ./run.sh calib haiku 1 skill/$SKILL_NAME data_2024/
  ./run.sh skill gpt-5.6-luna 1 skill/$SKILL_NAME data/ --agent codex
  ./run.sh noskill anthropic/claude-haiku-4-5 1 none data/ --agent opencode
EOF
}

die() {
    echo "error: $*" >&2
    exit 2
}

warn() {
    echo "warning: $*" >&2
}

# Path checks, the settings of the run, the agent run itself, agent.json and the summary after the run.
# Python handles the paths: they may hold spaces and quotes, and JSON and TOML must be written by code.
IFS= read -r -d '' HELPER <<'PY'
import json
import os
import shutil
import signal
import stat
import subprocess
import sys

SKILL_NAME = "hr-assistant"
WITH_SKILL = ("draft", "skill", "calib")
WITH_SCRIPT = ("skill", "calib")
# On macOS /tmp and /var are symbolic links: a deny rule must catch both spellings of a path under them
ALIASED_ROOTS = ("/tmp", "/var")
# Codex has no skill tool: a command inside the skill directory uses the skill; no trailing slash, so
# "cd .agents/skills/hr-assistant && python3 scripts/x.py" counts too
SKILL_DIR_MARKER = f"skills/{SKILL_NAME}"
SCRIPT_MARKER = "scripts/"
# the limit of the Grader for one file of the archive
MAX_FILE_BYTES = 4 * 1024 * 1024
TIMEOUT_EXIT = 124
INTERRUPT_EXIT = 130
STOP_GRACE_SECONDS = 10
CODEX_DISABLED = ("plugins", "apps", "hooks", "memories", "browser_use", "computer_use", "image_generation",
                  "multi_agent")
# Keys and tokens in the home directory that a Codex run must not read; only the existing ones are denied
HOME_SECRETS = (".ssh", ".aws", ".gnupg", ".config", os.path.join(".local", "share", "opencode"))
# OpenCode looks for these up to /; Claude Code for CLAUDE.md and .claude/ above the run
FILES_ABOVE = {"claude": ("CLAUDE.md", "CLAUDE.local.md", ".claude"),
               "opencode": ("AGENTS.md", "opencode.json", "opencode.jsonc", ".opencode")}


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


def codex_skill_warnings():
    """run.sh switches off only the bundled skills of Codex: the personal ones are another matter."""
    home = os.path.expanduser("~")
    agents_skills = os.path.join(home, ".agents", "skills")
    if os.path.isdir(agents_skills):
        print(f"warning: {agents_skills} exists: run.sh switches off only the bundled skills of Codex, "
              f"the skills there may get into the run", file=sys.stderr)
    codex_skills = os.path.join(home, ".codex", "skills")
    listed = os.listdir(codex_skills) if os.path.isdir(codex_skills) else []
    personal = [entry for entry in listed if entry != ".system"]
    if personal:
        print(f"warning: {codex_skills} holds skills besides .system: run.sh switches off only the bundled "
              f"skills of Codex, these may get into the run", file=sys.stderr)


def check(run_dir, agent, blocked):
    """Stop when the run cannot be isolated; warn about files the agent may load into it."""
    runs_root = os.path.dirname(run_dir)
    chain = ancestors(runs_root)
    for directory in chain:
        git_path = os.path.join(directory, ".git")
        if os.path.exists(git_path):
            sys.exit(f"error: the runs directory {runs_root} is inside the git repository {directory}, the agent "
                     f"would load its project files: set HW02_RUNS_DIR outside any repository")
    for path in blocked:
        if inside(run_dir, path):
            sys.exit(f"error: the run directory {run_dir} would lie inside {path}, which the run must not read: "
                     f"set HW02_RUNS_DIR elsewhere or run run.sh from your solution folder")
    # ~/.claude holds the user settings, which --setting-sources project leaves out
    home = os.path.expanduser("~")
    home_claude = os.path.join(home, ".claude")
    real_home_claude = os.path.realpath(home_claude)
    names = FILES_ABOVE.get(agent, ())
    for directory in chain:
        for name in names:
            found = os.path.join(directory, name)
            real_found = os.path.realpath(found)
            if os.path.exists(found) and real_found != real_home_claude:
                print(f"warning: {found} lies above the run directory and may get into the run: "
                      f"move it or set HW02_RUNS_DIR elsewhere", file=sys.stderr)
    if agent == "codex":
        codex_skill_warnings()


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


def write_opencode_config(run_dir, with_skill):
    """opencode.json of the run: no plugins, MCP, web or subagents; only hr-assistant among the skills."""
    skill_rules = {"*": "deny", SKILL_NAME: "allow"} if with_skill == "1" else {"*": "deny"}
    config = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False, "share": "disabled", "snapshot": False, "lsp": False, "formatter": False,
        "mcp": {}, "plugin": [],
        "permission": {"external_directory": "deny", "webfetch": "deny", "websearch": "deny", "task": "deny",
                       "skill": skill_rules},
    }
    config_fpath = os.path.join(run_dir, "opencode.json")
    with open(config_fpath, "w", encoding="utf-8") as fout:
        json.dump(config, fout, indent=2)
        fout.write("\n")
    print(f"config: {config_fpath}")


def toml_string(value):
    """A TOML basic string: quotes, backslashes and control characters escaped."""
    escaped = []
    for char in value:
        code = ord(char)
        if char in '"\\':
            escaped.append("\\" + char)
        elif code < 0x20 or code == 0x7F:
            escaped.append(f"\\u{code:04x}")
        else:
            escaped.append(char)
    joined = "".join(escaped)
    return f'"{joined}"'


def codex_filesystem(run_dir, runs_root, blocked):
    """The filesystem table of the permissions profile: deny the secrets, the sources and other runs.

    Reads elsewhere stay allowed: node needs the home directory, and denying all of it breaks node.
    The run itself is the only place to write; it lies inside the denied runs root, the nearer rule wins.
    """
    home = os.path.expanduser("~")
    secrets = [os.path.join(home, name) for name in HOME_SECRETS]
    present = [path for path in secrets if os.path.exists(path)]
    denied = [os.path.join(home, ".codex"), os.path.join(home, ".claude"), runs_root, "/tmp"] + present + blocked
    if sys.platform == "darwin":
        denied.append("/private/tmp")
    access = {}
    for path in denied:
        spellings = spellings_of(path)
        # the path as given too: /tmp itself resolves to /private/tmp on macOS
        for spelling in [path] + spellings:
            access[spelling] = "deny"
    run_spellings = spellings_of(run_dir)
    for spelling in [run_dir] + run_spellings:
        access[spelling] = "write"
    entries = [f"{toml_string(path)}={toml_string(rule)}" for path, rule in access.items()]
    table = ", ".join(entries)
    return f"{{{table}}}"


def codex_command(binary, run_dir, model, prompt, runs_root, blocked):
    """codex exec without the user config, rules, history, web, plugins and bundled skills, in a sandbox.

    The trust of the run directory is given inline: otherwise Codex writes it into ~/.codex/config.toml.
    """
    filesystem = codex_filesystem(run_dir, runs_root, blocked)
    run_path = toml_string(run_dir)
    command = [binary, "exec", "--json", "--ignore-user-config", "--ignore-rules", "--ephemeral",
               "--skip-git-repo-check", "-C", run_dir, "-m", model,
               "-c", 'approval_policy="never"', "-c", f'projects={{{run_path}={{trust_level="trusted"}}}}',
               "-c", 'web_search="disabled"', "-c", 'history.persistence="none"', "-c", "project_doc_max_bytes=0",
               "-c", "skills.bundled.enabled=false"]
    for feature in CODEX_DISABLED:
        command += ["--disable", feature]
    command += ["-c", 'default_permissions="hw02"', "-c", 'permissions.hw02.extends=":workspace"',
                "-c", f"permissions.hw02.filesystem={filesystem}", prompt]
    tmp_dir = os.path.join(run_dir, ".tmp")
    env = dict(os.environ, TMPDIR=tmp_dir)
    return command, env


def opencode_env(run_dir):
    """No ~/.claude, no external skills, no ~/.config/opencode, the session in a database of the run."""
    config_dir = os.path.join(run_dir, ".xdg_config")
    tmp_dir = os.path.join(run_dir, ".tmp")
    database = os.path.join(run_dir, ".opencode.db")
    env = dict(os.environ, OPENCODE_DISABLE_CLAUDE_CODE="1", OPENCODE_DISABLE_EXTERNAL_SKILLS="1",
               XDG_CONFIG_HOME=config_dir, TMPDIR=tmp_dir, OPENCODE_DB=database)
    return env


def opencode_command(binary, run_dir, model, prompt):
    command = [binary, "run", "--format", "json", "--pure", "--model", model, "--dir", run_dir, prompt]
    env = opencode_env(run_dir)
    return command, env


def stop(process):
    """SIGTERM to the process group of the agent, the commands it started included; SIGKILL if it lingers."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def append_note(fpath, note):
    """Append the note about a stopped run to stderr.txt, never through a link the agent may have put there.

    O_NONBLOCK keeps a planted FIFO from hanging run.sh; a regular file ignores it.
    """
    try:
        descriptor = os.open(fpath, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        print(f"warning: {fpath} is not a plain file ({error.strerror}): the note about the stop is not in it",
              file=sys.stderr)
        return
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        print(f"warning: {fpath} is not a regular file: the note about the stop is not in it", file=sys.stderr)
        return
    with os.fdopen(descriptor, "a", encoding="utf-8") as fappend:
        fappend.write(f"\n{note}\n")


def run_agent(agent, run_dir, timeout, binary, model, prompt, runs_root, blocked):
    """Run the agent in the run directory, its output into log.jsonl and stderr.txt; stop it at the timeout.

    Its own process group lets the timeout stop the commands of the agent too. A stopped run keeps a line
    about it at the end of stderr.txt, which goes into the submission.
    """
    if agent == "codex":
        command, env = codex_command(binary, run_dir, model, prompt, runs_root, blocked)
    else:
        command, env = opencode_command(binary, run_dir, model, prompt)
    log_fpath = os.path.join(run_dir, "log.jsonl")
    stderr_fpath = os.path.join(run_dir, "stderr.txt")
    limit = int(timeout)
    note = None
    with open(os.devnull, "rb") as fin, open(log_fpath, "wb") as fout, open(stderr_fpath, "wb") as ferr:
        try:
            process = subprocess.Popen(command, cwd=run_dir, env=env, stdin=fin, stdout=fout, stderr=ferr,
                                       start_new_session=True)
        except OSError as error:
            print(f"error: cannot start {binary}: {error}", file=sys.stderr)
            return 127
        try:
            code = process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            stop(process)
            code = TIMEOUT_EXIT
            note = f"run.sh: stopped after {limit} s (HW02_TIMEOUT_SECONDS), the run did not finish"
        except KeyboardInterrupt:
            stop(process)
            code = INTERRUPT_EXIT
            note = "run.sh: interrupted, the run did not finish"
    if note:
        append_note(stderr_fpath, note)
        print(f"warning: {note}", file=sys.stderr)
    return code


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


def first_session(log_fpath):
    """The sessionID of an OpenCode log, for opencode export; nothing when there is none."""
    try:
        events = read_events(log_fpath)
    except (OSError, ValueError):
        return
    for event in events:
        session = event.get("sessionID")
        if isinstance(session, str) and session:
            print(session)
            return


def export_model(export_fpath):
    """provider/model of the session from opencode export, None when the export has no model."""
    try:
        with open(export_fpath, encoding="utf-8") as fin:
            exported = json.load(fin)
    except (OSError, ValueError):
        return None
    info = exported.get("info") if isinstance(exported, dict) else None
    model = info.get("model") if isinstance(info, dict) else None
    if not isinstance(model, dict):
        return None
    provider, model_id = model.get("providerID"), model.get("id")
    if not isinstance(provider, str) or not isinstance(model_id, str) or not provider or not model_id:
        return None
    return f"{provider}/{model_id}"


def write_fresh(fpath, text):
    """Write a new file in place of whatever the agent left under this name, a symbolic link included.

    The agent could have put a link there to make run.sh write outside the run; O_EXCL never follows one.
    """
    try:
        os.unlink(fpath)
    except FileNotFoundError:
        pass
    descriptor = os.open(fpath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as fout:
        fout.write(text)


def write_wall_seconds(run_dir, start, end):
    """wall_seconds.txt of the run, rounded to 0.1 s; the seconds go to stdout for the summary."""
    start_seconds = float(start)
    end_seconds = float(end)
    seconds = round(end_seconds - start_seconds, 1)
    fpath = os.path.join(run_dir, "wall_seconds.txt")
    try:
        write_fresh(fpath, f"{seconds}\n")
    except OSError as error:
        print(f"warning: cannot write {fpath}: {error.strerror}", file=sys.stderr)
    print(seconds)


def write_agent_file(run_dir, agent, model, version, export_fpath):
    """agent.json of a Codex or OpenCode run: their logs name no model, the Grader takes it from here.

    Codex runs the model given with -m. OpenCode names its model in the export of the session; without
    it the model given with --model stands in.
    """
    if agent == "opencode":
        exported = export_model(export_fpath)
        if exported is None:
            print(f"warning: opencode export gave no model, agent.json takes --model {model}", file=sys.stderr)
        else:
            model = exported
    fpath = os.path.join(run_dir, "agent.json")
    content = json.dumps({"agent": agent, "model": model, "agent_version": version})
    write_fresh(fpath, content + "\n")


def open_run_file(fpath):
    """(a descriptor of the regular file, None) or (None, what is wrong): a link is never followed."""
    if os.path.islink(fpath):
        return None, "is a symbolic link"
    if not os.path.lexists(fpath):
        return None, "is missing"
    try:
        descriptor = os.open(fpath, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        return None, f"cannot be opened ({error.strerror})"
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        return None, "is not a regular file"
    return descriptor, None


def copy_run_files(out_run, paths):
    """Copy the run files into a new out_run, all or nothing.

    The agent could have put a link in place of a run file to get a file from outside the run into the
    submission: every file is opened without following links before anything is copied.
    """
    opened = []
    for fpath in paths:
        descriptor, problem = open_run_file(fpath)
        if problem:
            for _, other in opened:
                os.close(other)
            sys.exit(f"error: {fpath} {problem}: run.sh copies only regular files, nothing is copied to "
                     f"{out_run}; look at the run directory, the agent may have replaced the file")
        opened.append((fpath, descriptor))
    out_dir = os.path.dirname(out_run)
    try:
        os.makedirs(out_dir, exist_ok=True)
        os.mkdir(out_run)
    except OSError as error:
        for _, descriptor in opened:
            os.close(descriptor)
        run_dir = os.path.dirname(paths[0])
        print(f"warning: cannot copy the run files to {out_run} ({error.strerror}): take them from {run_dir}",
              file=sys.stderr)
        return
    for fpath, descriptor in opened:
        name = os.path.basename(fpath)
        target = os.path.join(out_run, name)
        with os.fdopen(descriptor, "rb") as fin, open(target, "xb") as fout:
            shutil.copyfileobj(fin, fout)


def tool_inputs(events, tool_name):
    """The input objects of every call of one tool by Claude Code, in log order."""
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


def dict_of(value):
    found = value if isinstance(value, dict) else {}
    return found


def final_answer_text(raw_text):
    text = raw_text if isinstance(raw_text, str) else ""
    return text


def claude_facts(events, _wall_seconds):
    """The run as the Grader reads a claude -p log; None without the init or the result event."""
    inits = [event for event in events if event.get("type") == "system" and event.get("subtype") == "init"]
    results = [event for event in events if event.get("type") == "result"]
    if not inits or not results:
        return None
    init, result = inits[0], results[-1]
    raw_skills = init.get("skills")
    skills = raw_skills if isinstance(raw_skills, list) else []
    skill_inputs = tool_inputs(events, "Skill")
    skill_called = any(tool_input.get("skill") == SKILL_NAME for tool_input in skill_inputs)
    bash_inputs = tool_inputs(events, "Bash")
    commands = []
    for tool_input in bash_inputs:
        raw_command = tool_input.get("command", "")
        command = str(raw_command)
        commands.append(command)
    raw_duration = result.get("duration_ms")
    duration_ms = number_or_zero(raw_duration)
    raw_cost = result.get("total_cost_usd")
    cost = number_or_zero(raw_cost)
    raw_text = result.get("result")
    final_text = final_answer_text(raw_text)
    mcp_servers = init.get("mcp_servers")
    facts = {"model": init.get("model"), "subtype": result.get("subtype"), "seconds": duration_ms / 1000,
             "cost": cost, "turns": result.get("num_turns"), "skill_available": SKILL_NAME in skills,
             "skill_called": skill_called, "commands": commands, "final_text": final_text, "mcp": bool(mcp_servers)}
    return facts


def codex_facts(events, wall_seconds):
    """The run as the Grader reads a codex exec --json log: no time and no cost in it."""
    items = []
    for event in events:
        if event.get("type") == "item.completed":
            raw_item = event.get("item")
            item = dict_of(raw_item)
            items.append(item)
    messages = [item for item in items if item.get("type") == "agent_message"]
    commands = []
    for item in items:
        if item.get("type") == "command_execution":
            raw_command = item.get("command", "")
            command = str(raw_command)
            commands.append(command)
    raw_text = messages[-1].get("text") if messages else ""
    final_text = final_answer_text(raw_text)
    failed = any(event.get("type") == "turn.failed" for event in events)
    completed = any(event.get("type") == "turn.completed" for event in events)
    if failed:
        subtype = "turn_failed"
    elif not completed:
        subtype = "no_turn_completed"
    else:
        subtype = "success"
    turns = [item for item in items if item.get("type") in ("command_execution", "file_change", "agent_message")]
    turn_count = len(turns)
    skill_called = any(SKILL_DIR_MARKER in command for command in commands)
    facts = {"model": None, "subtype": subtype, "seconds": wall_seconds, "cost": None, "turns": turn_count,
             "skill_available": None, "skill_called": skill_called, "commands": commands, "final_text": final_text,
             "mcp": False}
    return facts


def opencode_facts(events, wall_seconds):
    """The run as the Grader reads an opencode run --format json log: the time is the span of its events."""
    finishes = []
    for event in events:
        if event.get("type") == "step_finish":
            raw_part = event.get("part")
            part = dict_of(raw_part)
            finishes.append(part)
    stops = [part for part in finishes if part.get("reason") == "stop"]
    stop_message = stops[-1].get("messageID") if stops else None
    texts, commands, skill_calls, stamps = [], [], [], []
    for event in events:
        stamp = event.get("timestamp")
        if isinstance(stamp, (int, float)):
            stamps.append(stamp)
        raw_part = event.get("part")
        part = dict_of(raw_part)
        if event.get("type") == "text" and stop_message is not None and part.get("messageID") == stop_message:
            raw_text = part.get("text")
            text = final_answer_text(raw_text)
            texts.append(text)
        if event.get("type") != "tool_use":
            continue
        raw_state = part.get("state")
        state = dict_of(raw_state)
        raw_input = state.get("input")
        tool_input = dict_of(raw_input)
        if part.get("tool") == "skill":
            skill = tool_input.get("name")
            if skill is None:
                skill = tool_input.get("id")
            skill_calls.append(skill)
        elif part.get("tool") == "bash":
            raw_command = tool_input.get("command", "")
            command = str(raw_command)
            commands.append(command)
    failed = any(event.get("type") == "error" for event in events)
    stopped = bool(finishes) and finishes[-1].get("reason") == "stop"
    if failed:
        subtype = "error"
    elif not stopped:
        subtype = "not_stopped"
    else:
        subtype = "success"
    span = (stamps[-1] - stamps[0]) / 1000 if len(stamps) >= 2 else 0
    seconds = span if span > 0 else wall_seconds
    costs = []
    for part in finishes:
        raw_cost = part.get("cost")
        step_cost = number_or_zero(raw_cost)
        costs.append(step_cost)
    cost = sum(costs)
    final_text = "".join(texts)
    turn_count = len(finishes)
    facts = {"model": None, "subtype": subtype, "seconds": seconds, "cost": cost, "turns": turn_count,
             "skill_available": None, "skill_called": SKILL_NAME in skill_calls, "commands": commands,
             "final_text": final_text, "mcp": False}
    return facts


AGENT_FACTS = {"claude": claude_facts, "codex": codex_facts, "opencode": opencode_facts}


def has_json_object(text):
    """The rule of the Grader: the last ```json block, else the whole text, must be one JSON object."""
    blocks, position = [], 0
    while True:
        opening = text.find("```json", position)
        if opening < 0:
            break
        closing = text.find("```", opening + len("```json"))
        if closing < 0:
            break
        blocks.append(text[opening + len("```json"):closing])
        position = closing + len("```")
    candidate = blocks[-1] if blocks else text
    try:
        answer = json.loads(candidate)
    except (ValueError, RecursionError):
        return False
    found = isinstance(answer, dict)
    return found


def summary(stage, agent, log_fpath, shown_log, wall_seconds_text):
    """What to look at in the log right away: the agent, the model, the skill and its script, as the Grader."""
    size = os.path.getsize(log_fpath) if os.path.exists(log_fpath) else 0
    print(f"log:              {shown_log}, {size} bytes")
    if size > MAX_FILE_BYTES:
        print(f"warning: log.jsonl is over {MAX_FILE_BYTES} bytes (4 MB), the limit of the Grader for a file: "
              f"this run cannot be submitted", file=sys.stderr)
    try:
        events = read_events(log_fpath)
        wall_seconds = float(wall_seconds_text)
    except (OSError, ValueError) as error:
        print(f"warning: the log cannot be read ({error.__class__.__name__})", file=sys.stderr)
        return
    read_facts = AGENT_FACTS[agent]
    facts = read_facts(events, wall_seconds)
    if facts is None:
        print("warning: the log has no init or no result event: the run did not finish", file=sys.stderr)
        return
    model = facts["model"]
    version = ""
    log_dir = os.path.dirname(log_fpath)
    agent_fpath = os.path.join(log_dir, "agent.json")
    if agent != "claude" and os.path.exists(agent_fpath):
        with open(agent_fpath, encoding="utf-8") as fin:
            agent_file = json.load(fin)
        model = agent_file.get("model")
        agent_version = agent_file.get("agent_version")
        version = f" ({agent_version})"
    cost = "cost n/a" if facts["cost"] is None else f"${facts['cost']:.4f}"
    commands = facts["commands"]
    script_run = any(SCRIPT_MARKER in command for command in commands)
    skill_used = facts["skill_called"] or any(SKILL_DIR_MARKER in command for command in commands)
    print(f"agent:            {agent}{version}")
    print(f"model:            {model}")
    print(f"result:           {facts['subtype']}, {facts['seconds']:.1f} s, {cost}, {facts['turns']} turns")
    if agent == "claude":
        print(f"skill available:  {'yes' if facts['skill_available'] else 'no'}")
    # Codex has no skill tool: a command in the skill directory is its use, as the Grader counts it
    if stage in WITH_SKILL and agent == "codex":
        print(f"skill used:       {'yes' if facts['skill_called'] else 'no'}")
    elif stage in WITH_SKILL:
        print(f"Skill called:     {'yes' if facts['skill_called'] else 'no'}")
    if stage in WITH_SCRIPT:
        print(f"scripts/ run:     {'yes' if script_run else 'no'}")
    if facts["mcp"]:
        print("warning: MCP servers got into the run", file=sys.stderr)
    if facts["subtype"] != "success":
        print("warning: the run did not finish with success", file=sys.stderr)
    if stage == "noskill" and facts["skill_available"]:
        print(f"warning: {SKILL_NAME} is available in a run without a skill: not a valid step 1 run", file=sys.stderr)
    if stage == "noskill" and agent != "claude" and skill_used:
        print(f"warning: {SKILL_NAME} was used in a run without a skill: not a valid step 1 run", file=sys.stderr)
    if stage == "noskill" and not has_json_object(facts["final_text"]):
        print("warning: the final message has no JSON: this run cannot be the speedup base", file=sys.stderr)
    if stage in WITH_SKILL and not facts["skill_called"]:
        if agent == "codex":
            no_use = f"no command in {SKILL_DIR_MARKER}, the skill was not used"
        else:
            no_use = f'no call of Skill "{SKILL_NAME}"'
        print(f"warning: {no_use}: the run says nothing about your skill", file=sys.stderr)
    if stage in WITH_SCRIPT and not script_run:
        print("warning: no command with scripts/ ran: the Grader zeroes step 3 for that", file=sys.stderr)


def main():
    command = sys.argv[1]
    if command == "check":
        check(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif command == "settings":
        write_settings(sys.argv[2], sys.argv[3:])
    elif command == "opencode-config":
        write_opencode_config(sys.argv[2], sys.argv[3])
    elif command == "run":
        agent, run_dir, timeout, binary, model, prompt, runs_root = sys.argv[2:9]
        blocked = sys.argv[9:]
        code = run_agent(agent, run_dir, timeout, binary, model, prompt, runs_root, blocked)
        sys.exit(code)
    elif command == "session":
        first_session(sys.argv[2])
    elif command == "copy-run":
        copy_run_files(sys.argv[2], sys.argv[3:])
    elif command == "wall-seconds":
        write_wall_seconds(sys.argv[2], sys.argv[3], sys.argv[4])
    elif command == "agent-file":
        run_dir, agent, model, version, export_fpath = sys.argv[2:7]
        write_agent_file(run_dir, agent, model, version, export_fpath)
    elif command == "summary":
        stage, agent, log_fpath, shown_log, wall_seconds_text = sys.argv[2:7]
        summary(stage, agent, log_fpath, shown_log, wall_seconds_text)


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

case " $AGENTS " in
    *" $AGENT "*) ;;
    *) die "unknown agent $AGENT: run.sh runs one of $AGENTS" ;;
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
case "$AGENT:$MODEL" in
    codex:haiku|codex:sonnet|codex:opus|codex:*/*)
        die "Codex needs a full model id of its own, such as gpt-5.6-luna; got \"$MODEL\"" ;;
    opencode:*/*) ;;
    opencode:*) die "OpenCode needs the model as provider/model, such as anthropic/claude-haiku-4-5; got \"$MODEL\"" ;;
esac
case "$TIMEOUT_SECONDS" in
    ''|*[!0-9]*|0*) die "HW02_TIMEOUT_SECONDS must be a positive number of seconds, got \"$TIMEOUT_SECONDS\"" ;;
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
command -v python3 >/dev/null 2>&1 || die "python3 is not in PATH"
OS_NAME="$(uname -s)"
case "$OS_NAME" in
    MINGW*|MSYS*|CYGWIN*) die "run.sh needs WSL on Windows: the agents and their sandboxes do not run on native Windows" ;;
esac
AGENT_BIN=""
case "$AGENT" in
    claude)
        command -v claude >/dev/null 2>&1 || die "claude is not in PATH: install Claude Code"
        if [ "$OS_NAME" = "Linux" ]; then
            for tool in bwrap socat; do
                command -v "$tool" >/dev/null 2>&1 || \
                    die "the Claude Code sandbox on Linux needs the packages bubblewrap and socat: $tool is missing"
            done
        fi ;;
    codex)
        if command -v codex >/dev/null 2>&1; then
            AGENT_BIN="$(command -v codex)"
        elif [ "$OS_NAME" = "Darwin" ] && [ -x "$CODEX_APP_BINARY" ]; then
            AGENT_BIN="$CODEX_APP_BINARY"
        else
            die "codex is not in PATH: install the Codex CLI"
        fi ;;
    opencode)
        command -v opencode >/dev/null 2>&1 || die "opencode is not in PATH: install OpenCode"
        AGENT_BIN="$(command -v opencode)" ;;
esac
if [ "$AGENT" != "claude" ] && [ "$OS_NAME" != "Darwin" ]; then
    warn "run.sh runs $AGENT here, but it is not tested on this OS ($OS_NAME): it was tested on macOS only"
fi

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
python3 -I -c "$HELPER" check "$RUNS_ROOT_ABS/$RUN_NAME" "$AGENT" "$@" || exit 2

case "$STAGE" in
    calib) EXPECTED_LAST_MONTH="$CALIB_LAST_MONTH" ;;
    *) EXPECTED_LAST_MONTH="$MAIN_LAST_MONTH" ;;
esac
if [ "$LAST_MONTH" != "$EXPECTED_LAST_MONTH" ]; then
    warn "the last month in $DATA_ARG is $LAST_MONTH, stage $STAGE expects $EXPECTED_LAST_MONTH:" \
         "Grader checks the snapshot from the course dataset"
fi

# ---------------------------------------------------------------- the run directory
case "$AGENT" in
    claude) SKILLS_SUBDIR=".claude/skills" ;;
    codex) SKILLS_SUBDIR=".agents/skills" ;;
    opencode) SKILLS_SUBDIR=".opencode/skills" ;;
esac
mkdir -p "$RUNS_ROOT_ABS" || die "cannot create $RUNS_ROOT_ABS"
RUNS_ROOT="$(cd "$RUNS_ROOT_ABS" && pwd -P)"
RUN_DIR="$RUNS_ROOT/$RUN_NAME"
mkdir "$RUN_DIR" || die "cannot create $RUN_DIR"
mkdir -p "$RUN_DIR/data" "$RUN_DIR/.tmp" || die "cannot fill $RUN_DIR"
if [ "$AGENT" = "claude" ] || [ -n "$SKILL_SRC" ]; then
    mkdir -p "$RUN_DIR/$SKILLS_SUBDIR" || die "cannot fill $RUN_DIR"
fi

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
    mkdir "$RUN_DIR/$SKILLS_SUBDIR/$SKILL_NAME" || die "cannot create the skill directory of the run"
    cp -R "$SKILL_SRC"/. "$RUN_DIR/$SKILLS_SUBDIR/$SKILL_NAME"/ || die "cannot copy the skill"
fi
case "$AGENT" in
    claude)
        python3 -I -c "$HELPER" settings "$RUN_DIR" "$@" || die "cannot write the settings of the run" ;;
    opencode)
        WITH_SKILL=0
        [ -n "$SKILL_SRC" ] && WITH_SKILL=1
        mkdir -p "$RUN_DIR/.xdg_config" || die "cannot fill $RUN_DIR"
        python3 -I -c "$HELPER" opencode-config "$RUN_DIR" "$WITH_SKILL" || die "cannot write opencode.json"
        warn "the bash tool of OpenCode has no OS sandbox: it can read files outside the run and reach the" \
             "network, and it sees your environment variables, an API key included" ;;
esac

# ---------------------------------------------------------------- the run
echo "run $RUN_NAME in $RUN_DIR: $AGENT, model $MODEL, data up to $LAST_MONTH"
if [ "$AGENT" != "claude" ]; then
    echo "no budget cap for this agent: the run is stopped after $TIMEOUT_SECONDS s (HW02_TIMEOUT_SECONDS)"
fi
case "$STAGE" in
    noskill|draft) echo "$AGENT is working: without a tool the agent writes the analysis itself, it takes minutes" ;;
    *) echo "$AGENT is working" ;;
esac
PROMPT="$(cat "$PROMPT_FILE")"
START="$(python3 -I -c 'import time; now = time.time(); print(now)')"
if [ "$AGENT" = "claude" ]; then
    (
        cd "$RUN_DIR" || exit 1
        TMPDIR="$RUN_DIR/.tmp" CLAUDE_CODE_TMPDIR="$RUN_DIR/.tmp" claude -p "$PROMPT" --model "$MODEL" \
            --output-format stream-json --verbose --no-session-persistence --setting-sources project \
            --strict-mcp-config --max-budget-usd "$BUDGET_USD" \
            --allowedTools Bash Read Write Edit Glob Grep Skill --disallowedTools WebSearch WebFetch \
            < /dev/null > log.jsonl 2> stderr.txt
    )
    CODE=$?
else
    python3 -I -c "$HELPER" run "$AGENT" "$RUN_DIR" "$TIMEOUT_SECONDS" "$AGENT_BIN" "$MODEL" "$PROMPT" \
        "$RUNS_ROOT" "$@"
    CODE=$?
fi
END="$(python3 -I -c 'import time; now = time.time(); print(now)')"
WALL_SECONDS="$(python3 -I -c "$HELPER" wall-seconds "$RUN_DIR" "$START" "$END")"

# ---------------------------------------------------------------- agent.json: the model, after the time is taken
if [ "$AGENT" != "claude" ]; then
    # OpenCode keeps its own config out of ~/.config/opencode here too
    VERSION="$(env XDG_CONFIG_HOME="$RUN_DIR/.xdg_config" "$AGENT_BIN" --version 2>/dev/null < /dev/null | head -n 1)"
    EXPORT_FPATH="$RUN_DIR/.tmp/opencode_export.json"
    if [ "$AGENT" = "opencode" ]; then
        SESSION="$(python3 -I -c "$HELPER" session "$RUN_DIR/log.jsonl")"
        if [ -n "$SESSION" ]; then
            (
                cd "$RUN_DIR" || exit 1
                env OPENCODE_DISABLE_CLAUDE_CODE=1 OPENCODE_DISABLE_EXTERNAL_SKILLS=1 \
                    XDG_CONFIG_HOME="$RUN_DIR/.xdg_config" TMPDIR="$RUN_DIR/.tmp" OPENCODE_DB="$RUN_DIR/.opencode.db" \
                    "$AGENT_BIN" export "$SESSION" < /dev/null > "$EXPORT_FPATH" 2> "$RUN_DIR/.tmp/opencode_export.err"
            )
        fi
    fi
    python3 -I -c "$HELPER" agent-file "$RUN_DIR" "$AGENT" "$MODEL" "$VERSION" "$EXPORT_FPATH" || \
        warn "cannot write agent.json: the Grader takes the model of $AGENT from it"
    # an agent that exits at once with nothing in its log has most likely refused a flag of another version
    if [ "$CODE" -ne 0 ] && [ "$CODE" -ne 124 ] && [ "$CODE" -ne 130 ] && [ ! -s "$RUN_DIR/log.jsonl" ]; then
        case "$AGENT" in
            codex) TESTED="$CODEX_TESTED" ;;
            *) TESTED="$OPENCODE_TESTED" ;;
        esac
        tail -n 5 "$RUN_DIR/stderr.txt" >&2
        warn "$AGENT wrote no log: if it refused a flag of run.sh, compare the versions: run.sh was tested" \
             "with $AGENT $TESTED, yours is ${VERSION:-unknown}"
    fi
fi

# ---------------------------------------------------------------- the files of the submission
set -- "$RUN_DIR/log.jsonl" "$RUN_DIR/stderr.txt" "$RUN_DIR/wall_seconds.txt"
# a link or another odd file in place of agent.json is refused, not skipped
if [ "$AGENT" != "claude" ] && { [ -e "$RUN_DIR/agent.json" ] || [ -L "$RUN_DIR/agent.json" ]; }; then
    set -- "$@" "$RUN_DIR/agent.json"
fi
python3 -I -c "$HELPER" copy-run "$OUT_RUN" "$@" || exit 2
echo "$AGENT exit code: $CODE, wall time: $WALL_SECONDS s"
python3 -I -c "$HELPER" summary "$STAGE" "$AGENT" "$RUN_DIR/log.jsonl" "$OUT_RUN/log.jsonl" "$WALL_SECONDS"
echo "check the answer: python3 \"$STARTER_DIR/check_answer.py\" \"$OUT_RUN/log.jsonl\""
exit "$CODE"
