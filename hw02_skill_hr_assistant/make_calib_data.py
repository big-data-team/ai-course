#!/usr/bin/env python3
"""Copy the dataset cut at 2024-12 for the calibration run (3.2.8 of the assignment).

The copy holds the hn_whoishiring_YYYY-MM.json files up to the last month, inclusive: on it "now" is
2024-12 and the window is 2024-01..2024-12. Files are cloned where the file system allows it
(APFS on macOS, btrfs or XFS on Linux), so the copy takes no extra disk space; elsewhere they are copied.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

DATA_FILE_RE = re.compile(r"^hn_whoishiring_(?P<month>\d{4}-\d{2})\.json$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CALIBRATION_LAST_MONTH = "2024-12"


def files_up_to(dump_dir, last_month):
    """The data files of dump_dir with a month not after last_month, by month."""
    names = os.listdir(dump_dir)
    chosen = []
    for name in sorted(names):
        matched = DATA_FILE_RE.match(name)
        if matched and matched["month"] <= last_month:
            fpath = os.path.join(dump_dir, name)
            chosen.append(fpath)
    return chosen


def clone_command(sources, out_dir):
    """cp that clones files, or None where no such cp is known."""
    if sys.platform == "darwin":
        command = ["cp", "-c"] + sources + [out_dir]
        return command
    if sys.platform.startswith("linux"):
        command = ["cp", "--reflink=auto"] + sources + [out_dir]
        return command
    return None


def copy_files(sources, out_dir):
    """Clone in one cp call; on any failure copy file by file. Returns how the files got there."""
    command = clone_command(sources, out_dir)
    if command is not None:
        completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if completed.returncode == 0:
            return "cloned"
    for source in sources:
        name = os.path.basename(source)
        target = os.path.join(out_dir, name)
        shutil.copyfile(source, target)
    return "copied"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy the hn_whoishiring_YYYY-MM.json files up to 2024-12 inclusive into a new directory "
                    "for the calibration run: ./run.sh calib <model> 1 skill/hr-assistant <out_dir>.")
    parser.add_argument("dump_dir", help="the unzipped course dataset, for example data/")
    parser.add_argument("out_dir", help="a new directory for the copy, for example data_2024/; must not exist")
    parser.add_argument("--last-month", default=CALIBRATION_LAST_MONTH,
                        help=f"the last month to keep, YYYY-MM (default {CALIBRATION_LAST_MONTH})")
    args = parser.parse_args()
    return args


def main():
    args = parse_args()
    if not MONTH_RE.match(args.last_month):
        sys.exit(f"error: --last-month must be YYYY-MM, got {args.last_month}")
    if not os.path.isdir(args.dump_dir):
        sys.exit(f"error: no directory {args.dump_dir}")
    if os.path.exists(args.out_dir):
        sys.exit(f"error: {args.out_dir} already exists: give a new directory")
    sources = files_up_to(args.dump_dir, args.last_month)
    if not sources:
        sys.exit(f"error: no hn_whoishiring_YYYY-MM.json up to {args.last_month} in {args.dump_dir}")
    os.makedirs(args.out_dir)
    how = copy_files(sources, args.out_dir)
    first = os.path.basename(sources[0])
    last = os.path.basename(sources[-1])
    file_count = len(sources)
    print(f"{how} {file_count} files into {args.out_dir}: {first} .. {last}")
    if args.last_month == CALIBRATION_LAST_MONTH and not last.endswith(f"{CALIBRATION_LAST_MONTH}.json"):
        print(f"warning: the data have no {CALIBRATION_LAST_MONTH}: the calibration window is "
              f"2024-01..{CALIBRATION_LAST_MONTH}", file=sys.stderr)


if __name__ == "__main__":
    main()
