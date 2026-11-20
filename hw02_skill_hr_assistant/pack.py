#!/usr/bin/env python3
"""Build HW02_<Surname>_<Name>.zip and check an archive the way the Grader does. Optional.

Build:  python3 pack.py <Surname> <Name> [--root .] [--runs-dir ./runs]
        skill/hr-assistant/ and the runs noskill_*, skill_*, calib_* with their log.jsonl, stderr.txt and
        wall_seconds.txt; draft_* and service files stay out. The built archive is checked right away.
Check:  python3 pack.py --check HW02_<Surname>_<Name>.zip
        an archive built by hand, with zip or "Compressed (zipped) folder" on Windows.

The check reads the archive without unpacking it and reports on three levels:
- errors: the Grader rejects the whole archive, the score is 0;
- zeroed: the archive is accepted, but Skills, Remote and the speedup get 0;
- warnings: the archive is accepted, a part of the score may suffer.
SKILL.md is checked against the standard by skills-ref, the validator of agentskills.io, when it is installed:
only SKILL.md is copied into a temporary directory for it. Without skills-ref that check is skipped.
It knows nothing about the quality of the answers: check_answer.py and your eval do.
"""
import argparse
import collections
import io
import json
import math
import os
import posixpath
import re
import shutil
import statistics
import stat
import subprocess
import sys
import tempfile
import zipfile
import zlib

SKILL_DIR = "skill"
RUNS_DIR = "runs"
SKILL_NAME = "hr-assistant"
SKILL_MD = "SKILL.md"
SCRIPTS_DIR = "scripts"
ASSETS_DIR = "assets"
LOG_FILENAME = "log.jsonl"
WALL_SECONDS_FILENAME = "wall_seconds.txt"
RUN_FILES = (LOG_FILENAME, "stderr.txt", WALL_SECONDS_FILENAME)
STAGE_NO_SKILL = "noskill"
STAGE_SKILL = "skill"
STAGE_CALIBRATION = "calib"
KNOWN_STAGES = (STAGE_NO_SKILL, STAGE_SKILL, STAGE_CALIBRATION)
DRAFT_PREFIX = "draft_"
RUN_DIR_RE = re.compile(r"^(?P<stage>[^_]+)_(?P<model>.+)_r(?P<number>[0-9]+)$")
NAME_RE = re.compile(r"^[^\W_]+(?:-[^\W_]+)*$")

# the limits of the Grader
MAX_ARCHIVE_BYTES = 3 * 1024 * 1024
MAX_UNPACKED_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 400
MAX_DEPTH = 8
# a longer file or directory name cannot be written on the file system of the Grader
MAX_NAME_BYTES = 255
ALLOWED_EXTENSIONS = (".md", ".py", ".json", ".jsonl", ".txt", ".yaml", ".yml", ".csv", ".sh")
SERVICE_DIRS = ("__MACOSX", "__pycache__")
SERVICE_FILES = (".DS_Store",)
SERVICE_PREFIX = "._"
MAX_WALL_SECONDS_BYTES = 100

# the rules of the score
RUNS_PER_STAGE = 3
# the Grader reads up to 6 runs of step 1 on the model of step 3 and up to 6 of the other models;
# without a single model of step 3, one budget of 12 for all of them
MAX_BASE_STEP1_RUNS = 6
MAX_OTHER_STEP1_RUNS = 6
SPEEDUP_MIN = 3.0
SCRIPT_MARKER = "scripts/"

# Agent Skills: the standard check of SKILL.md is skills-ref, the reference validator of agentskills.io, at the
# commit the Grader follows. It is run as a program: its YAML parser is not in the standard library.
SKILLS_REF = "skills-ref"
SKILLS_REF_INSTALL = ('uv tool install "git+https://github.com/agentskills/agentskills'
                      '@83021e2be457a9b6679148719c1f6074f0ad975e#subdirectory=skills-ref"')
SKILLS_REF_TIMEOUT_SECONDS = 30
SKILLS_REF_FAILED = "Validation failed"
SKILLS_REF_ERROR_PREFIX = "  - "
SKILLS_REF_RECURSION = "RecursionError"
FRONTMATTER_DELIMITER = "---"
# the Grader refuses a longer frontmatter before parsing it: the parser is slow on a large one
MAX_FRONTMATTER_LINES = 200
MAX_FRONTMATTER_BYTES = 16 * 1024
# remarks only: the standard recommends a shorter SKILL.md, and skills-ref checks neither this nor the files
MAX_SKILL_MD_LINES = 500
NAME_FIELD_RE = re.compile(r"^name:(.*)$")
QUOTED_SCALAR_RE = re.compile(r"""^(["'])(.*?)\1\s*(?:#.*)?$""")
BYTE_ORDER_MARK = "\ufeff"
# a path of the skill, not a part of a URL such as https://github.com/org/repo/scripts/x.py
SKILL_FILE_REF_RE = re.compile(r"(?<![\w/])((?:scripts|references|assets)/[\w./-]+)")
# HN item ids of the dataset are 8-digit numbers from 3xxxxxxx (2018) to 5xxxxxxx (2026)
HN_ID_RE = re.compile(r"(?<!\d)[3-5]\d{7}(?!\d)")
JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.S)
MAX_LISTED = 10


class Report:
    """Problems of an archive by level, in the order they were found, and the lines of its composition."""

    def __init__(self):
        self.errors = []
        self.zeroed = []
        self.warnings = []
        self.notes = []
        self.lines = []
        # why the standard check of SKILL.md did not run, None when skills-ref ran
        self.skipped_standard = None


def listed(names):
    shown = [str(name) for name in names[:MAX_LISTED]]
    text = ", ".join(shown)
    hidden = len(names) - MAX_LISTED
    if hidden > 0:
        text += f" and {hidden} more"
    return text


# ---------------------------------------------------------------- the central directory

def path_problem(name):
    """Why a member path is unsafe to unpack, None when it is safe."""
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return "an absolute path"
    if "\\" in name:
        return "a backslash in the path"
    stripped = name.rstrip("/")
    parts = stripped.split("/")
    if ".." in parts:
        return "a path outside the archive"
    if any(part in ("", ".") for part in parts):
        return "an empty path part"
    return None


def split_parts(name):
    stripped = name.rstrip("/")
    parts = stripped.split("/")
    return parts


def is_service(parts):
    """.DS_Store, ._* and anything under __MACOSX/ or __pycache__/, in any part of the path."""
    in_service_dir = any(part in SERVICE_DIRS for part in parts)
    apple_double = any(part.startswith(SERVICE_PREFIX) for part in parts)
    service = in_service_dir or apple_double or parts[-1] in SERVICE_FILES
    return service


def is_symlink(info):
    mode = info.external_attr >> 16
    link = stat.S_ISLNK(mode)
    return link


def name_bytes(part):
    encoded = part.encode("utf-8", "surrogatepass")
    size = len(encoded)
    return size


def has_long_name(parts):
    sizes = [name_bytes(part) for part in parts]
    too_long = max(sizes) > MAX_NAME_BYTES
    return too_long


def find_wrapper(entries):
    """The one top-level directory that wraps skill/ and runs/, None without one.

    A zip of a folder adds such a directory. It is not a wrapper when it is skill/ or runs/ itself,
    when a file lies next to it, or when it holds neither skill/ nor runs/.
    """
    tops = {parts[0] for parts, _ in entries}
    root_files = [parts for parts, is_dir in entries if len(parts) == 1 and not is_dir]
    if len(tops) != 1 or root_files:
        return None
    top = min(tops)
    if top in (SKILL_DIR, RUNS_DIR):
        return None
    holds_layout = any(len(parts) >= 2 and parts[1] in (SKILL_DIR, RUNS_DIR) for parts, _ in entries)
    wrapper = top if holds_layout else None
    return wrapper


def extension_allowed(parts):
    """Allowed extensions everywhere; inside skill/ also files without one; anything in skill/<name>/assets/."""
    if len(parts) >= 3 and parts[0] == SKILL_DIR and parts[2] == ASSETS_DIR:
        return True
    _, extension = os.path.splitext(parts[-1])
    lowered = extension.lower()
    if lowered in ALLOWED_EXTENSIONS:
        return True
    allowed = parts[0] == SKILL_DIR and not extension
    return allowed


def file_dir_clashes(paths):
    """Paths that are a file and, in another member, a directory: they cannot both be unpacked."""
    path_set = set(paths)
    clashes = set()
    for path in paths:
        parts = path.split("/")
        part_count = len(parts)
        for depth in range(1, part_count):
            prefix = "/".join(parts[:depth])
            if prefix in path_set:
                clashes.add(prefix)
    ordered = sorted(clashes)
    return ordered


def check_members(infos, report):
    """The checks of the central directory in the order of the Grader. Returns {path: ZipInfo} of the files.

    Unsafe paths first, over every member, service files included; then service files are skipped;
    then too long names, symbolic links, the wrapper directory, and the files as they will lie after unpacking.
    """
    unsafe = []
    for info in infos:
        problem = path_problem(info.filename)
        if problem:
            unsafe.append(f"{info.filename} ({problem})")
    if unsafe:
        shown_unsafe = listed(unsafe)
        report.errors.append(f"unsafe paths: {shown_unsafe}")
        return {}
    kept, service = [], []
    for info in infos:
        parts = split_parts(info.filename)
        if not is_service(parts):
            kept.append((info, parts))
        elif not info.is_dir():
            service.append(info.filename)
    if service:
        shown_service = listed(service)
        report.warnings.append(f"service files, the Grader skips them, but do not include them: {shown_service}")
    long_names = [info.filename for info, parts in kept if has_long_name(parts)]
    if long_names:
        shown_long = listed(long_names)
        report.errors.append(f"file or directory names longer than {MAX_NAME_BYTES} bytes: {shown_long}")
        return {}
    symlinks = [info.filename for info, _ in kept if is_symlink(info)]
    if symlinks:
        shown_symlinks = listed(symlinks)
        report.errors.append(f"symbolic links are not accepted: {shown_symlinks}")
        return {}
    entries = [(parts, info.is_dir()) for info, parts in kept]
    wrapper = find_wrapper(entries)
    if wrapper:
        report.lines.append(f"wrapper: {wrapper}/ is taken off, as the Grader does")
    files = []
    for info, parts in kept:
        if info.is_dir():
            continue
        relative = parts[1:] if wrapper else parts
        if relative:
            files.append((relative, info))
    paths = ["/".join(relative) for relative, _ in files]
    counts = collections.Counter(paths)
    repeated = sorted(path for path, count in counts.items() if count > 1)
    clashes = file_dir_clashes(paths)
    deep = [path for path, (relative, _) in zip(paths, files) if len(relative) > MAX_DEPTH]
    big = [path for path, (_, info) in zip(paths, files) if info.file_size > MAX_FILE_BYTES]
    wrong = [path for path, (relative, _) in zip(paths, files) if not extension_allowed(relative)]
    sizes = [info.file_size for _, info in files]
    unpacked_bytes = sum(sizes)
    file_count = len(files)
    if file_count > MAX_FILES:
        report.errors.append(f"{file_count} files, at most {MAX_FILES}")
    if deep:
        shown_deep = listed(deep)
        report.errors.append(f"paths deeper than {MAX_DEPTH} levels: {shown_deep}")
    if repeated:
        shown_repeated = listed(repeated)
        report.errors.append(f"repeated file names: {shown_repeated}")
    if clashes:
        shown_clashes = listed(clashes)
        report.errors.append(f"a file and a directory share a name: {shown_clashes}")
    if big:
        shown_big = listed(big)
        report.errors.append(f"files over {MAX_FILE_BYTES} bytes: {shown_big}")
    if unpacked_bytes > MAX_UNPACKED_BYTES:
        report.errors.append(f"the unpacked archive is {unpacked_bytes} bytes, at most {MAX_UNPACKED_BYTES}")
    if wrong:
        allowed = " ".join(ALLOWED_EXTENSIONS)
        shown_wrong = listed(wrong)
        report.errors.append(f"only {allowed} files are allowed (inside {SKILL_DIR}/ also files without an "
                             f"extension, anything in {SKILL_DIR}/{SKILL_NAME}/{ASSETS_DIR}/), found: {shown_wrong}")
    unpacked_kb = unpacked_bytes / 1024
    report.lines.append(f"files: {file_count} without service files, unpacked {unpacked_kb:.1f} KB")
    members = {path: info for path, (_, info) in zip(paths, files)}
    return members


def text_stream(archive, info):
    """A member as text read the way the Grader reads a file: UTF-8, lines end only at \\n, \\r\\n or \\r."""
    content = archive.read(info)
    raw = io.BytesIO(content)
    stream = io.TextIOWrapper(raw, encoding="utf-8")
    return stream


# ---------------------------------------------------------------- the skill

def frontmatter_scalar(value):
    """A one-line YAML scalar: quotes removed, a trailing " # comment" dropped."""
    quoted = QUOTED_SCALAR_RE.match(value)
    if quoted:
        inner = quoted.group(2)
        return inner
    plain = re.sub(r"\s+#.*$", "", value)
    stripped = plain.strip()
    return stripped


def frontmatter_block(text):
    """(the frontmatter, None) or (None, the problem), split as skills-ref and the Grader split SKILL.md.

    The text starts with ---, and the next --- closes the frontmatter wherever it stands, even inside a value.
    A byte order mark is not stripped: skills-ref and the Grader refuse such a file.
    """
    if text.startswith(BYTE_ORDER_MARK):
        return None, f"{SKILL_MD} starts with a byte order mark (BOM) before ---: save it as UTF-8 without BOM"
    if not text.startswith(FRONTMATTER_DELIMITER):
        return None, f"{SKILL_MD} must start with the YAML frontmatter (---)"
    parts = text.split(FRONTMATTER_DELIMITER, 2)
    part_count = len(parts)
    if part_count < 3:
        return None, f"the frontmatter of {SKILL_MD} is not closed with ---"
    block = parts[1]
    return block, None


def frontmatter_size_problem(block):
    """Why the Grader refuses the frontmatter before parsing it, None when it is small enough."""
    lines = block.splitlines()
    line_count = len(lines)
    encoded = block.encode("utf-8")
    size = len(encoded)
    if line_count <= MAX_FRONTMATTER_LINES and size <= MAX_FRONTMATTER_BYTES:
        return None
    problem = (f"the frontmatter of {SKILL_MD} is too long: {line_count} lines, {size} bytes; at most "
               f"{MAX_FRONTMATTER_LINES} lines and {MAX_FRONTMATTER_BYTES} bytes")
    return problem


def frontmatter_name(block):
    """The value of name in its usual forms (plain with a comment, quoted, folded over lines); None without one.

    Only the exact name of the assignment is compared with it: skills-ref checks the YAML itself.
    """
    name_parts = None
    for line in block.splitlines():
        if name_parts is not None:
            if not line.startswith((" ", "\t")):
                break
            stripped_line = line.strip()
            name_parts.append(stripped_line)
            continue
        match = NAME_FIELD_RE.match(line)
        if match:
            first = match.group(1)
            name_parts = [first.strip()]
    if name_parts is None:
        return None
    kept = [part for part in name_parts if part]
    joined = " ".join(kept)
    name = frontmatter_scalar(joined)
    return name


def missing_refs(text, skill_prefix, paths):
    """scripts/, references/ and assets/ paths named in SKILL.md that the archive does not have."""
    found = SKILL_FILE_REF_RE.findall(text)
    # a path at the end of a sentence carries its full stop
    refs = {ref.rstrip(".") for ref in found}
    ordered = sorted(refs)
    missing = []
    for ref in ordered:
        normalized = posixpath.normpath(ref)
        target = f"{skill_prefix}/{normalized}"
        as_dir = target + "/"
        exists = target in paths or any(path.startswith(as_dir) for path in paths)
        if normalized.startswith("..") or not exists:
            missing.append(ref)
    return missing


def skills_ref_validate(binary, content, dir_name):
    """skills-ref validate on a copy of SKILL.md alone: (exit code, stderr). Raises OSError, TimeoutExpired.

    The copy keeps the directory name of the archive: skills-ref compares it with the name field.
    PYTHONUTF8 makes it read SKILL.md as UTF-8 on Windows too, not in the code page of the system.
    """
    with tempfile.TemporaryDirectory(prefix="hw02_skill_") as temp_dir:
        skill_dir = os.path.join(temp_dir, dir_name)
        os.mkdir(skill_dir)
        md_fpath = os.path.join(skill_dir, SKILL_MD)
        with open(md_fpath, "wb") as fout:
            fout.write(content)
        command = [binary, "validate", skill_dir]
        env = dict(os.environ, PYTHONUTF8="1")
        completed = subprocess.run(command, capture_output=True, timeout=SKILLS_REF_TIMEOUT_SECONDS, env=env,
                                   check=False)
    stderr = completed.stderr.decode("utf-8", errors="replace")
    return completed.returncode, stderr


def check_standard(content, dir_name, where, report):
    """The standard check by skills-ref: its errors zero the parts; a missing or broken skills-ref only warns."""
    binary = shutil.which(SKILLS_REF)
    if binary is None:
        report.warnings.append(f"skills-ref is not installed, the standard check of {SKILL_MD} was skipped "
                               f"(the Grader does it); install it from git, not from PyPI: {SKILLS_REF_INSTALL}")
        report.skipped_standard = "skills-ref is not installed"
        return
    try:
        code, stderr = skills_ref_validate(binary, content, dir_name)
    except (OSError, subprocess.TimeoutExpired) as error:
        code, stderr = None, error.__class__.__name__
    if code == 0:
        report.lines.append(f"{where}: skills-ref validate: valid")
        return
    if code == 1 and SKILLS_REF_FAILED in stderr:
        lines = stderr.splitlines()
        prefix_length = len(SKILLS_REF_ERROR_PREFIX)
        errors = [line[prefix_length:] for line in lines if line.startswith(SKILLS_REF_ERROR_PREFIX)]
        shown = errors or [SKILLS_REF_FAILED]
        for error in shown:
            report.zeroed.append(f"{where}: skills-ref: {error}")
        return
    stripped = stderr.strip()
    stderr_lines = stripped.splitlines()
    last_line = stderr_lines[-1] if stderr_lines else f"exit code {code}"
    # the parser of skills-ref recurses on deep nesting: the fault of SKILL.md, not of the installation
    if last_line.startswith(SKILLS_REF_RECURSION):
        report.zeroed.append(f"{where}: the frontmatter of {SKILL_MD} is not valid YAML: {SKILLS_REF_RECURSION}, "
                             f"the nesting is too deep")
        return
    report.warnings.append(f"skills-ref did not run ({last_line}), the standard check of {SKILL_MD} was skipped "
                           f"(the Grader does it); reinstall it: {SKILLS_REF_INSTALL}")
    report.skipped_standard = "skills-ref did not run"


def check_skill(archive, members, report):
    """skill/hr-assistant/: the requirements of the assignment and skills-ref; any problem zeroes the parts.

    UTF-8 and the size of the frontmatter are checked first, as the Grader checks them before parsing.
    A SKILL.md over 500 lines and a missing referenced file are only warnings.
    """
    skill_parts = [path.split("/") for path in members if path.startswith(SKILL_DIR + "/")]
    if not skill_parts:
        report.zeroed.append(f"{SKILL_DIR}/{SKILL_NAME}/ is missing")
        return
    dirs = sorted({parts[1] for parts in skill_parts if len(parts) >= 3})
    dir_count = len(dirs)
    if dir_count != 1:
        report.zeroed.append(f"{SKILL_DIR}/ must hold exactly one skill directory, {SKILL_NAME}/; found {dir_count}")
        return
    dir_name = dirs[0]
    where = f"{SKILL_DIR}/{dir_name}"
    skill_files = sorted(path for path in members if path.startswith(where + "/"))
    scripts = [path for path in skill_files if path.startswith(f"{where}/{SCRIPTS_DIR}/")]
    references = [path for path in skill_files if path.startswith(f"{where}/references/")]
    file_count, script_count, reference_count = len(skill_files), len(scripts), len(references)
    report.lines.append(f"{where}/: {file_count} files, {script_count} in scripts/, {reference_count} in references/")
    for path in skill_files:
        size = members[path].file_size
        report.lines.append(f"  {path} ({size} bytes)")
    if not scripts:
        report.zeroed.append(f"{where}: no {SCRIPTS_DIR}/, the final skill runs a script")
    md_path = f"{where}/{SKILL_MD}"
    if md_path not in members:
        lowered_md = md_path.lower()
        other_case = [path for path in skill_files if path.lower() == lowered_md]
        no_md = f"{where}: no {SKILL_MD}"
        if other_case:
            found_name = posixpath.basename(other_case[0])
            no_md += f", found {found_name}"
        report.zeroed.append(no_md)
        return
    content = archive.read(members[md_path])
    raw = io.BytesIO(content)
    # the way the Grader reads it: UTF-8, lines end at \n, \r\n or \r, a byte order mark is kept
    stream = io.TextIOWrapper(raw, encoding="utf-8")
    try:
        text = stream.read()
    except UnicodeDecodeError:
        report.zeroed.append(f"{where}: {SKILL_MD} is not UTF-8 text")
        return
    lines = text.splitlines()
    line_count = len(lines)
    if line_count > MAX_SKILL_MD_LINES:
        report.warnings.append(f"{where}: {SKILL_MD} has {line_count} lines, the standard recommends at most "
                               f"{MAX_SKILL_MD_LINES}")
    path_set = set(members)
    missing = missing_refs(text, where, path_set)
    for ref in missing:
        report.warnings.append(f"{where}: {SKILL_MD} refers to a missing file {ref}")
    block, problem = frontmatter_block(text)
    if block is not None:
        problem = frontmatter_size_problem(block)
    if problem:
        report.zeroed.append(f"{where}: {problem}")
        return
    name = frontmatter_name(block)
    # exactly: "hr-assistant " or full-width letters pass skills-ref, not the assignment
    if dir_name != SKILL_NAME or name != SKILL_NAME:
        shown_name = name or ""
        report.zeroed.append(f'{where}: the skill must be {SKILL_DIR}/{SKILL_NAME}/ with "name: {SKILL_NAME}", '
                             f'found the name "{shown_name}"')
    check_standard(content, dir_name, where, report)


def note_hn_ids(archive, members, report):
    """Numbers that look like HN post ids in the skill: the Grader notes them for a manual review."""
    ordered = sorted(members)
    for path in ordered:
        parts = path.split("/")
        if parts[0] != SKILL_DIR or (len(parts) >= 3 and parts[2] == ASSETS_DIR):
            continue
        content = archive.read(members[path])
        text = content.decode("utf-8", errors="replace")
        found = HN_ID_RE.findall(text)
        if found:
            count = len(found)
            report.notes.append(f"{path}: {count} numbers that look like HN post ids; "
                                f"the answer must come from the data, not from the skill")


# ---------------------------------------------------------------- the runs

def is_clean(value):
    """A value without a lone surrogate, which a JSON escape such as \\ud800 gives: the Grader refuses it."""
    try:
        dumped = json.dumps(value, ensure_ascii=False)
        dumped.encode("utf-8")
    except (ValueError, RecursionError, UnicodeEncodeError):
        return False
    return True


def read_events(stream):
    """The events of a stream-json log; a broken line raises ValueError with its number."""
    events = []
    for number, line in enumerate(stream, start=1):
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


def init_model(archive, members, run_path):
    """The model of the init event, read only up to it; None when there is no readable init."""
    info = members.get(f"{run_path}/{LOG_FILENAME}")
    if info is None:
        return None
    stream = text_stream(archive, info)
    try:
        for line in stream:
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                return None
            if event.get("type") == "system" and event.get("subtype") == "init":
                model = event.get("model")
                model_id = model if isinstance(model, str) else ""
                found = model_id if is_clean(model_id) else None
                return found
    except (ValueError, RecursionError, MemoryError):
        return None
    return None


def extract_answer(result_text):
    """The last ```json block of the final answer, else the whole answer if it is one JSON object."""
    blocks = JSON_BLOCK_RE.findall(result_text)
    candidate = blocks[-1] if blocks else result_text
    try:
        answer = json.loads(candidate)
    except (ValueError, RecursionError, MemoryError):
        answer = None
    if answer is not None and not is_clean(answer):
        answer = None
    return answer


def number_or_none(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def tool_inputs(events):
    """(Skill names, Bash commands) the agent passed, in log order."""
    skill_calls, commands = [], []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            raw_input = block.get("input")
            tool_input = raw_input if isinstance(raw_input, dict) else {}
            if block.get("name") == "Skill":
                skill = tool_input.get("skill")
                skill_name = str(skill)
                skill_calls.append(skill_name)
            elif block.get("name") == "Bash":
                command = tool_input.get("command", "")
                command_text = str(command)
                commands.append(command_text)
    return skill_calls, commands


def wall_seconds(archive, members, run_path):
    """The run time from wall_seconds.txt of run.sh, for a log without one; None without it."""
    info = members.get(f"{run_path}/{WALL_SECONDS_FILENAME}")
    if info is None:
        return None
    content = archive.read(info)
    head = content[:MAX_WALL_SECONDS_BYTES]
    try:
        text = head.decode("utf-8")
        stripped = text.strip()
        seconds = float(stripped)
    except (UnicodeDecodeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def parse_run(archive, members, run_path):
    """The flags of a log as the Grader reads them; raises ValueError, UnicodeDecodeError on a broken log."""
    info = members.get(f"{run_path}/{LOG_FILENAME}")
    if info is None:
        raise ValueError(f"no {LOG_FILENAME}")
    stream = text_stream(archive, info)
    events = read_events(stream)
    inits = [event for event in events if event.get("type") == "system" and event.get("subtype") == "init"]
    results = [event for event in events if event.get("type") == "result"]
    if not inits:
        raise ValueError("no init event")
    if not results:
        raise ValueError("no result event")
    init, result = inits[0], results[-1]
    sessions = set()
    for event in events:
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            sessions.add(session_id)
    raw_skills = init.get("skills")
    skills = raw_skills if isinstance(raw_skills, list) else []
    skill_calls, commands = tool_inputs(events)
    raw_text = result.get("result")
    result_text = raw_text if isinstance(raw_text, str) else ""
    answer = extract_answer(result_text)
    model = init.get("model")
    model_id = model if isinstance(model, str) else ""
    raw_duration = result.get("duration_ms")
    duration_ms = number_or_none(raw_duration)
    if duration_ms and duration_ms > 0:
        seconds = duration_ms / 1000
    else:
        seconds = wall_seconds(archive, members, run_path)
    script_runs = [command for command in commands if SCRIPT_MARKER in command]
    session_count = len(sessions)
    only_session = min(sessions) if session_count == 1 else None
    subtype = result.get("subtype")
    shown_subtype = str(subtype)
    raw_cost = result.get("total_cost_usd")
    cost = number_or_none(raw_cost)
    skill_names = [str(skill) for skill in skills]
    parsed = {
        "model": model_id,
        "subtype": shown_subtype,
        "sessions": session_count,
        "session": only_session,
        "skill_available": SKILL_NAME in skill_names,
        "skill_called": SKILL_NAME in skill_calls,
        "script_run": len(script_runs) > 0,
        "answer": answer,
        "seconds": seconds,
        "cost": cost,
    }
    return parsed


def read_run(archive, members, name):
    """A run as a record: parse_run that never raises, a broken log becomes a record with its reason."""
    record = {"name": name, "readable": False, "error": None, "model": None, "subtype": None, "session": None,
              "sessions": 0, "skill_available": False, "skill_called": False, "script_run": False,
              "answer": None, "seconds": None, "cost": None}
    run_path = f"{RUNS_DIR}/{name}"
    try:
        parsed = parse_run(archive, members, run_path)
    except UnicodeDecodeError:
        record["error"] = "not UTF-8 text"
        return record
    except ValueError as error:
        record["error"] = str(error)
        return record
    except (RecursionError, MemoryError) as error:
        record["error"] = error.__class__.__name__
        return record
    record.update(parsed, readable=True)
    return record


def list_runs(members, report):
    """[(stage, model label, number, name)] of runs/, sorted as the Grader sorts them; the rest is noted."""
    entries = {}
    for path in members:
        parts = path.split("/")
        if parts[0] != RUNS_DIR or len(parts) < 2:
            continue
        is_dir = len(parts) >= 3
        entries[parts[1]] = entries.get(parts[1], False) or is_dir
    found, drafts = [], []
    ordered = sorted(entries)
    for name in ordered:
        if name.startswith(DRAFT_PREFIX):
            drafts.append(name)
            continue
        if not entries[name]:
            report.warnings.append(f"{RUNS_DIR}/{name}: not a run directory, the Grader ignores it")
            continue
        match = RUN_DIR_RE.match(name)
        if not match:
            report.warnings.append(f"{RUNS_DIR}/{name}: the name does not follow <stage>_<model>_r<k>, "
                                   f"the Grader ignores it")
            continue
        if match["stage"] not in KNOWN_STAGES:
            stages = ", ".join(KNOWN_STAGES)
            report.warnings.append(f"{RUNS_DIR}/{name}: unknown stage {match['stage']}, expected one of "
                                   f"{stages}; the Grader ignores it")
            continue
        number = int(match["number"])
        found.append((match["stage"], match["model"], number, name))
    if drafts:
        draft_count = len(drafts)
        report.warnings.append(f"{RUNS_DIR}/{DRAFT_PREFIX}*: {draft_count} step 2 runs, not graded, "
                               f"do not include them")
    found.sort()
    return found


def select_step1(entries, step3_model):
    """(names to read, names left unread) of step 1; entries are (name, model of the init event or None).

    Unreadable runs are always read and stay outside the limits. Without a single model of step 3
    the two limits make one budget for all the models.
    """
    unreadable = [name for name, model in entries if model is None]
    base = [name for name, model in entries if model is not None and step3_model and model == step3_model]
    others = [name for name, model in entries if model is not None and not (step3_model and model == step3_model)]
    other_limit = MAX_OTHER_STEP1_RUNS if step3_model else MAX_BASE_STEP1_RUNS + MAX_OTHER_STEP1_RUNS
    chosen = set(unreadable + base[:MAX_BASE_STEP1_RUNS] + others[:other_limit])
    to_read = [name for name, _ in entries if name in chosen]
    unread = [name for name, _ in entries if name not in chosen]
    return to_read, unread


def session_copies(runs):
    """{run name: names of the other runs with its session_id}; every claude -p run has its own session."""
    by_session = collections.defaultdict(list)
    for run in runs:
        if run["readable"] and run["session"]:
            by_session[run["session"]].append(run["name"])
    copies = {}
    for names in by_session.values():
        if len(names) < 2:
            continue
        for name in names:
            others = [other for other in names if other != name]
            copies[name] = sorted(others)
    return copies


def common_reasons(run):
    """Why a run is not valid at all: an unreadable log, no success, a log glued from several sessions."""
    if not run["readable"]:
        return [f"{LOG_FILENAME} is unreadable ({run['error']})"]
    reasons = []
    if run["subtype"] != "success":
        reasons.append(f"the run did not finish with success (subtype {run['subtype']})")
    if run["sessions"] != 1:
        reasons.append(f"{run['sessions']} session ids in one log, expected 1")
    return reasons


def step1_reasons(run, earlier_copies):
    """Why a run of step 1 is not valid: the rules of the process part."""
    reasons = common_reasons(run)
    if not run["readable"]:
        return reasons
    if not run["model"]:
        reasons.append("no model id in the log")
    if run["skill_available"]:
        reasons.append(f"the skill {SKILL_NAME} is available in a run without a skill")
    if earlier_copies:
        shown = ", ".join(earlier_copies)
        reasons.append(f"the same log as {shown}")
    return reasons


def step3_reasons(run, copies):
    """Why a run of step 3 zeroes Skills, Remote and the speedup."""
    reasons = common_reasons(run)
    if not run["readable"]:
        return reasons
    if not run["model"]:
        reasons.append("no model id in the log")
    if not run["skill_called"]:
        reasons.append(f'no call of Skill "{SKILL_NAME}"')
    if not run["script_run"]:
        reasons.append(f"no command with {SCRIPT_MARKER} was run")
    if copies:
        shown = ", ".join(copies)
        reasons.append(f"the same log is used in several runs ({shown})")
    return reasons


def choose_base_model(step1, valid_names, step3_model, step1_models):
    """The model of step 3 whenever step 1 has a run of it; else the one with the most valid runs.

    A tie goes to the model of step 3, then to the first id; None without valid runs.
    """
    if step3_model and step3_model in step1_models:
        return step3_model
    valid_models = [run["model"] for name, run in step1.items() if name in valid_names]
    counts = collections.Counter(valid_models)
    if not counts:
        return None
    count_values = counts.values()
    best = max(count_values)
    leaders = sorted(model for model, count in counts.items() if count == best)
    base_model = step3_model if step3_model in leaders else leaders[0]
    return base_model


def describe_run(name, run):
    if not run["readable"]:
        return f"  {name:28} unreadable: {run['error']}"
    seconds = f"{run['seconds']:.1f} s" if run["seconds"] is not None else "no time"
    cost = f"${run['cost']:.4f}" if run["cost"] is not None else "no cost"
    session = (run["session"] or "?")[:8]
    model = run["model"] or "?"
    line = f"  {name:28} {model:24} {run['subtype']:10} {seconds:>9} {cost:>9}  session {session}"
    return line


def read_runs(archive, members, names):
    """{name: record} of the runs, in the given order."""
    runs = {}
    for name in names:
        record = read_run(archive, members, name)
        runs[name] = record
    return runs


def check_step3(step3, step1, copies, report):
    """Three runs of one model, each with the skill called and a script run, none a copy of another log."""
    step3_count = len(step3)
    if step3_count != RUNS_PER_STAGE:
        report.zeroed.append(f"{step3_count} runs {STAGE_SKILL}_* instead of {RUNS_PER_STAGE}")
    for name, run in step3.items():
        # a copy among the runs of step 3 or of step 1 zeroes the step; a copy in calib is only noted
        run_copies = copies.get(name, [])
        scored_copies = [other for other in run_copies if other in step3 or other in step1]
        reasons = step3_reasons(run, scored_copies)
        for reason in reasons:
            report.zeroed.append(f"{name}: {reason}")
        if run["readable"] and not isinstance(run["answer"], dict):
            report.warnings.append(f"{name}: no JSON object in the final answer: Q of this run is 0 "
                                   f"(check_answer.py shows the contract)")
    models = sorted({run["model"] for run in step3.values() if run["readable"] and run["model"]})
    if len(models) > 1:
        shown_models = ", ".join(models)
        report.zeroed.append(f"step 3 runs use different models: {shown_models}")
    step3_model = models[0] if len(models) == 1 else None
    return step3_model


def check_step1(step1, copies, step3_model, step1_models, report):
    """The valid runs of step 1 and the base model; returns the names of the valid runs."""
    valid_names = set()
    for name, run in step1.items():
        run_copies = copies.get(name, [])
        earlier = [other for other in run_copies if other in step1 and other < name]
        reasons = step1_reasons(run, earlier)
        if reasons:
            joined = "; ".join(reasons)
            report.warnings.append(f"{name}: not a valid step 1 run: {joined}")
            continue
        valid_names.add(name)
    base_model = choose_base_model(step1, valid_names, step3_model, step1_models)
    base_valid = [name for name in valid_names if step1[name]["model"] == base_model]
    valid_count = len(base_valid)
    if step3_model and step3_model not in step1_models and valid_count:
        report.zeroed.append(f"step 1 has no run of the step 3 model {step3_model}, only of {base_model}: "
                             f"steps 1 and 3 run on one base model")
    process = min(valid_count, RUNS_PER_STAGE)
    shown_base = base_model or "none"
    report.lines.append(f"base model: {shown_base}; the process part counts {process}/{RUNS_PER_STAGE} "
                        f"valid runs of step 1")
    if valid_count < RUNS_PER_STAGE:
        report.warnings.append(f"{valid_count} valid runs {STAGE_NO_SKILL}_* of the base model, "
                               f"{RUNS_PER_STAGE} needed: the process part and the speedup suffer")
    return valid_names


def check_speedup(step1, valid_names, step3, step3_model, report):
    """The speedup base: valid runs of step 1 on the model of step 3 with a JSON object and a time."""
    if not step3_model:
        return
    base_seconds = []
    for name in sorted(valid_names):
        run = step1[name]
        if run["model"] == step3_model and isinstance(run["answer"], dict) and run["seconds"]:
            base_seconds.append(run["seconds"])
    step3_seconds = [run["seconds"] for run in step3.values() if run["readable"] and run["seconds"]]
    base_count = len(base_seconds)
    if base_count < RUNS_PER_STAGE:
        report.warnings.append(f"{base_count} valid runs of step 1 on {step3_model} with a JSON answer and a time, "
                               f"{RUNS_PER_STAGE} needed as the speedup base: the speedup part gets 0")
        return
    if not step3_seconds:
        report.warnings.append("no run time of step 3: the speedup part gets 0")
        return
    base_median = statistics.median(base_seconds)
    step3_median = statistics.median(step3_seconds)
    speedup = base_median / step3_median
    report.lines.append(f"speedup by median time: {speedup:.1f} ({base_median:.0f} s / {step3_median:.0f} s); "
                        f"the Grader also needs the mean Q of step 3 >= 0.6")
    if speedup < SPEEDUP_MIN:
        report.warnings.append(f"the speedup is {speedup:.2f}, at least {SPEEDUP_MIN:g} needed: "
                               f"the speedup part gets 0")


def check_calibration(calibration, copies, report):
    """Calibration runs never enter the score: their problems are notes."""
    for name, run in calibration.items():
        reasons = common_reasons(run)
        if run["readable"] and not run["skill_called"]:
            reasons.append(f'no call of Skill "{SKILL_NAME}"')
        if copies.get(name):
            shown = ", ".join(copies[name])
            reasons.append(f"the same log as {shown}")
        for reason in reasons:
            report.notes.append(f"{name}: {reason}")


def check_runs(archive, members, report):
    """Step 3 first: its model is the base model, step 1 is read and judged against it."""
    found = list_runs(members, report)
    names = {stage: [] for stage in KNOWN_STAGES}
    for stage, _, _, name in found:
        names[stage].append(name)
    step3 = read_runs(archive, members, names[STAGE_SKILL])
    calibration = read_runs(archive, members, names[STAGE_CALIBRATION])
    step3_models = sorted({run["model"] for run in step3.values() if run["readable"] and run["model"]})
    step3_model = step3_models[0] if len(step3_models) == 1 else None
    entries = []
    for name in names[STAGE_NO_SKILL]:
        model = init_model(archive, members, f"{RUNS_DIR}/{name}")
        entries.append((name, model))
    to_read, unread = select_step1(entries, step3_model)
    step1_models = {model for _, model in entries if model}
    step1 = read_runs(archive, members, to_read)
    all_runs = []
    for stage_runs in (step1, step3, calibration):
        all_runs += stage_runs.values()
    copies = session_copies(all_runs)
    check_step3(step3, step1, copies, report)
    valid_names = check_step1(step1, copies, step3_model, step1_models, report)
    if unread:
        unread_count = len(unread)
        shown_unread = listed(unread)
        report.warnings.append(f"{unread_count} runs {STAGE_NO_SKILL}_* over the limit of {MAX_BASE_STEP1_RUNS} runs "
                               f"of the base model and {MAX_OTHER_STEP1_RUNS} of the other models are not read by "
                               f"the Grader: {shown_unread}")
    check_speedup(step1, valid_names, step3, step3_model, report)
    check_calibration(calibration, copies, report)
    report.lines.append("runs:")
    for stage_runs in (step1, step3, calibration):
        for name, run in stage_runs.items():
            line = describe_run(name, run)
            report.lines.append(line)
    for name in unread:
        report.lines.append(f"  {name:28} not read: over the limit of step 1")


def check_archive(zip_fpath):
    report = Report()
    size = os.path.getsize(zip_fpath)
    size_kb = size / 1024
    report.lines.append(f"archive: {zip_fpath}, {size_kb:.1f} KB")
    if size > MAX_ARCHIVE_BYTES:
        report.errors.append(f"the archive is {size} bytes, at most {MAX_ARCHIVE_BYTES} (3 MB)")
        return report
    if not zipfile.is_zipfile(zip_fpath):
        report.errors.append("not a zip archive")
        return report
    try:
        with zipfile.ZipFile(zip_fpath) as archive:
            infos = archive.infolist()
            members = check_members(infos, report)
            if report.errors:
                return report
            has_runs = any(path.startswith(RUNS_DIR + "/") for path in members)
            if not has_runs:
                report.errors.append(f"no {RUNS_DIR}/ directory: the whole assignment gets 0")
            top_names = {path.split("/")[0] for path in members}
            extra_tops = top_names - {SKILL_DIR, RUNS_DIR}
            ordered_tops = sorted(extra_tops)
            for top in ordered_tops:
                report.warnings.append(f"{top}: not part of the layout ({SKILL_DIR}/ and {RUNS_DIR}/), "
                                       f"the Grader ignores it")
            check_skill(archive, members, report)
            note_hn_ids(archive, members, report)
            check_runs(archive, members, report)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, zlib.error, EOFError,
            OSError) as error:
        report.errors.append(f"the archive cannot be read ({error.__class__.__name__})")
    return report


def print_report(report):
    for line in report.lines:
        print(line)
    sections = (
        ("errors: the Grader rejects the whole archive, the score is 0", report.errors),
        ("zeroed: the archive is accepted, Skills, Remote and the speedup get 0", report.zeroed),
        ("warnings", report.warnings),
        ("notes", report.notes),
    )
    for title, problems in sections:
        if not problems:
            continue
        print(f"{title}:")
        for problem in problems:
            print(f"  - {problem}")
    if report.errors:
        print("result: REJECTED")
        return 1
    if report.zeroed:
        print("result: ZEROED")
        return 1
    if report.skipped_standard:
        print(f"result: OK, the standard check was skipped: {report.skipped_standard}")
        return 0
    warning_count = len(report.warnings)
    print(f"result: OK, {warning_count} warnings")
    return 0


# ---------------------------------------------------------------- build

def skill_files(skill_root):
    """(path, name in the archive) of the files of skill/hr-assistant/, service files left out."""
    pairs, links = [], []
    walk = os.walk(skill_root)
    for dirpath, dirnames, filenames in walk:
        kept_dirs = [name for name in dirnames if name not in SERVICE_DIRS]
        dirnames[:] = sorted(kept_dirs)
        for dirname in dirnames:
            dir_fpath = os.path.join(dirpath, dirname)
            if os.path.islink(dir_fpath):
                links.append(dir_fpath)
        ordered_files = sorted(filenames)
        for filename in ordered_files:
            if filename in SERVICE_FILES or filename.startswith(SERVICE_PREFIX):
                continue
            fpath = os.path.join(dirpath, filename)
            if os.path.islink(fpath):
                links.append(fpath)
                continue
            relative = os.path.relpath(fpath, skill_root)
            posix_relative = relative.replace(os.sep, "/")
            pairs.append((fpath, f"{SKILL_DIR}/{SKILL_NAME}/{posix_relative}"))
    return pairs, links


def run_files(runs_dir):
    """(path, name in the archive) of the three files of every run to submit; skipped runs are printed."""
    pairs = []
    entries = os.listdir(runs_dir)
    ordered = sorted(entries)
    for entry in ordered:
        run_fpath = os.path.join(runs_dir, entry)
        if not os.path.isdir(run_fpath):
            continue
        if entry.startswith(DRAFT_PREFIX):
            print(f"skipped {entry}: step 2 runs are not part of the submission")
            continue
        match = RUN_DIR_RE.match(entry)
        if not match or match["stage"] not in KNOWN_STAGES:
            print(f"skipped {entry}: not a run <stage>_<model>_r<k> of noskill, skill or calib")
            continue
        log_fpath = os.path.join(run_fpath, LOG_FILENAME)
        if not os.path.isfile(log_fpath):
            print(f"skipped {entry}: no {LOG_FILENAME}")
            continue
        for filename in RUN_FILES:
            fpath = os.path.join(run_fpath, filename)
            if os.path.isfile(fpath):
                pairs.append((fpath, f"{RUNS_DIR}/{entry}/{filename}"))
    return pairs


def build(surname, name, root, runs_dir):
    for value in (surname, name):
        if not NAME_RE.match(value):
            sys.exit(f"error: {value!r}: Surname and Name are letters, as in HW02_Dral_Alexey.zip")
    skill_root = os.path.join(root, SKILL_DIR, SKILL_NAME)
    if not os.path.isdir(skill_root):
        sys.exit(f"error: no {SKILL_DIR}/{SKILL_NAME}/ in {root}")
    if not os.path.isdir(runs_dir):
        sys.exit(f"error: no runs directory {runs_dir}")
    skill_pairs, links = skill_files(skill_root)
    if links:
        shown_links = listed(links)
        sys.exit(f"error: symbolic links are not accepted, replace them with the files: {shown_links}")
    run_pairs = run_files(runs_dir)
    all_pairs = skill_pairs + run_pairs
    zip_fpath = os.path.join(root, f"HW02_{surname}_{name}.zip")
    partial_fpath = zip_fpath + ".partial"
    with zipfile.ZipFile(partial_fpath, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for fpath, arcname in all_pairs:
            archive.write(fpath, arcname)
    replaced = os.path.exists(zip_fpath)
    os.replace(partial_fpath, zip_fpath)
    action = "replaced" if replaced else "built"
    file_count = len(all_pairs)
    print(f"{action} {zip_fpath}: {file_count} files")
    return zip_fpath


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build HW02_<Surname>_<Name>.zip from skill/hr-assistant/ and the runs noskill_*, skill_*, "
                    "calib_* (log.jsonl, stderr.txt, wall_seconds.txt only), then check it; or only check a "
                    "ready archive with --check. The check repeats the archive and layout checks of the Grader.",
        epilog="Exit code 0: the archive is accepted with all parts possible; 1: rejected or zeroed.")
    parser.add_argument("surname", nargs="?", help="your surname, as in HW02_Dral_Alexey.zip")
    parser.add_argument("name", nargs="?", help="your name")
    parser.add_argument("--check", metavar="ZIP", help="only check this archive")
    parser.add_argument("--root", default=".", help="the solution folder with skill/ (default: .)")
    parser.add_argument("--runs-dir", help="the runs to pack (default: <root>/runs)")
    args = parser.parse_args()
    if not args.check and not (args.surname and args.name):
        parser.error("give <Surname> <Name> to build, or --check <zip>")
    return args


def main():
    args = parse_args()
    if args.check:
        if not os.path.isfile(args.check):
            sys.exit(f"error: no file {args.check}")
        zip_fpath = args.check
    else:
        default_runs = os.path.join(args.root, RUNS_DIR)
        runs_dir = args.runs_dir or default_runs
        zip_fpath = build(args.surname, args.name, args.root, runs_dir)
    report = check_archive(zip_fpath)
    code = print_report(report)
    return code


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
