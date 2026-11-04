"""Self-check: run your parse_datetime on the open cases and print what fails.

Usage: python check_open_cases.py task_<Surname>_<Name>_tool_datetime_parser.py
A simplified check: the grader also validates the response against outputSchema.
"""
import importlib.util
import json
import sys
from collections import defaultdict
from datetime import datetime


def normalize(value):
    """Datetimes compare as instant + offset, dates as strings."""
    if isinstance(value, str) and "T" in value:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (moment.timestamp(), moment.utcoffset())
    return value


def same(expected, actual):
    if not isinstance(actual, dict) or actual.get("status") != expected["status"]:
        return False
    if actual.get("timezone") != expected["timezone"]:
        return False
    if expected["status"] == "ok":
        return actual.get("kind") == expected["kind"] and normalize(actual.get("value")) == normalize(expected["value"])
    if expected["status"] == "ambiguous":
        got = sorted(map(str, map(normalize, actual.get("alternatives") or [])))
        return got == sorted(map(str, map(normalize, expected["alternatives"])))
    return True


def main():
    spec = importlib.util.spec_from_file_location("solution", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    # registered as a regular import would be: dataclasses and typing look the module up by name
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cases = json.load(open("open_cases.json", encoding="utf-8"))
    per_category = defaultdict(list)
    for case in cases:
        try:
            actual = module.parse_datetime(case["text"], reference_time=case["reference_time"], timezone=case.get("timezone"))
            passed = same(case["expected"], actual)
        except Exception as error:  # pylint: disable=broad-except
            actual, passed = f"{type(error).__name__}: {error}", False
        per_category[case["category"]].append(passed)
        if not passed:
            print(f"FAIL {case['id']}: {case['text']!r}\n     expected {case['expected']}\n     got      {actual}")
    shares = {category: sum(values) / len(values) for category, values in per_category.items()}
    for category, share in sorted(shares.items()):
        print(f"{category:16} {share:.2f}")
    print(f"open cases score (categories weigh the same): {sum(shares.values()) / len(shares):.2f}")


if __name__ == "__main__":
    main()
