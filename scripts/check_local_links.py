#!/usr/bin/env python3
"""Check relative links in tracked Markdown files."""

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "data:")


def tracked_markdown_files() -> list:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [Path(value.decode("utf-8")) for value in result.stdout.split(b"\0") if value]


def link_target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        value = value[1 : value.index(">")]
    elif " " in value:
        value = value.split(" ", 1)[0]
    return unquote(value.split("#", 1)[0].split("?", 1)[0])


def main() -> int:
    root = Path.cwd().resolve()
    failures = []
    checked = 0
    for markdown_path in tracked_markdown_files():
        text = markdown_path.read_text(encoding="utf-8")
        for match in LINK_PATTERN.finditer(text):
            raw = match.group(1).strip()
            if not raw or raw.startswith("#") or raw.lower().startswith(EXTERNAL_PREFIXES):
                continue
            target = link_target(raw)
            if not target:
                continue
            resolved = (markdown_path.parent / target).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                failures.append("%s: link leaves the repository: %s" % (markdown_path, target))
                continue
            checked += 1
            if not resolved.exists():
                failures.append("%s: missing local target: %s" % (markdown_path, target))

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1

    print("Local-link check passed (%d links)" % checked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
