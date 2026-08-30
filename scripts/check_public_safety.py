#!/usr/bin/env python3
"""Fail on private runtime paths, likely credentials, or machine home paths.

The checker prints only a category and tracked path. It never prints matched
content. Use --history before publication to scan every reachable Git blob.
"""

import argparse
import re
import subprocess
import sys
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Set, Tuple


MAX_TRACKED_BLOB_BYTES = 5 * 1024 * 1024

CONTENT_PATTERNS = (
    ("private-key-material", re.compile(b"-----BEGIN " + b"(?:RSA |EC |OPENSSH )?" + b"PRIVATE KEY-----")),
    ("openai-token", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws-access-key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("unix-home-path", re.compile(rb"(?:/Users|/home)/[A-Za-z0-9._-]+/")),
    ("windows-home-path", re.compile(rb"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\")),
)


def git_bytes(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def unsafe_path_reason(path_value: str) -> str:
    path = PurePosixPath(path_value)
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "environment-file"
    if name in {"auth.json", "credentials.json", "state.sqlite"}:
        return "private-runtime-file"
    if name.endswith((".pem", ".p12", ".pfx", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".log")):
        return "private-runtime-file"
    if ".scheduler" in parts:
        return "private-runtime-directory"
    return ""


def content_findings(data: bytes) -> Iterable[str]:
    if len(data) > MAX_TRACKED_BLOB_BYTES:
        yield "large-unreviewed-blob"
        return
    if b"\0" in data:
        return
    for category, pattern in CONTENT_PATTERNS:
        if pattern.search(data):
            yield category


def current_findings() -> Tuple[Set[Tuple[str, str]], int]:
    findings: Set[Tuple[str, str]] = set()
    raw_paths = git_bytes("ls-files", "-z").split(b"\0")
    paths = [value.decode("utf-8", "surrogateescape") for value in raw_paths if value]
    for path in paths:
        reason = unsafe_path_reason(path)
        if reason:
            findings.add((reason, path))
        data = git_bytes("show", ":%s" % path)
        for category in content_findings(data):
            findings.add((category, path))
    return findings, len(paths)


def history_findings() -> Tuple[Set[Tuple[str, str]], int]:
    findings: Set[Tuple[str, str]] = set()
    commits = [value for value in git_bytes("rev-list", "--all").decode("ascii").splitlines() if value]
    scanned_blobs: Dict[str, Set[str]] = {}
    for commit in commits:
        entries = git_bytes("ls-tree", "-r", "-z", commit).split(b"\0")
        for entry in entries:
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            _mode, object_type, object_id = metadata.decode("ascii").split(" ")
            if object_type != "blob":
                continue
            path = raw_path.decode("utf-8", "surrogateescape")
            reason = unsafe_path_reason(path)
            if reason:
                findings.add(("history-%s" % reason, path))
            if object_id not in scanned_blobs:
                data = git_bytes("cat-file", "-p", object_id)
                scanned_blobs[object_id] = set(content_findings(data))
            for category in scanned_blobs[object_id]:
                findings.add(("history-%s" % category, path))
    return findings, len(commits)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true", help="scan every reachable Git commit")
    args = parser.parse_args()

    findings, tracked_count = current_findings()
    commit_count = 0
    if args.history:
        historical, commit_count = history_findings()
        findings.update(historical)

    if findings:
        for category, path in sorted(findings):
            print("%s: %s" % (category, path), file=sys.stderr)
        return 1

    suffix = ", %d commits" % commit_count if args.history else ""
    print("Public-safety check passed (%d tracked files%s)" % (tracked_count, suffix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
