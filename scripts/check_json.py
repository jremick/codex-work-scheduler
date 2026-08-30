#!/usr/bin/env python3
"""Validate every tracked JSON file without third-party dependencies."""

import json
import subprocess
import sys
from pathlib import Path


def tracked_json_files() -> list:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.json"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [Path(value.decode("utf-8")) for value in result.stdout.split(b"\0") if value]


def main() -> int:
    failures = []
    files = tracked_json_files()
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as handle:
                json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            failures.append("%s: %s" % (path, exc.__class__.__name__))

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1

    print("JSON check passed (%d tracked files)" % len(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
