#!/usr/bin/env python3
"""
Definition of Done: UserPromptSubmit hook (v2, 2026-09-18).

Takes a fingerprint of the project's git state when the user prompt arrives.
dod-check.py (Stop hook) compares it again at the end of the turn: a different
fingerprint = a mutation, whatever the editing path (Edit/Write, Bash
sed/heredoc, MCP tools, subagents).

Fingerprint = sha256 of:
  git status --porcelain=v1 --untracked-files=all -z
  + git diff HEAD --no-color            (repository without commits: git diff)
  + for each untracked file: path \\0 size \\0 mtime_ns
Outside a git repository: null fingerprint, dod-check falls back on the transcript scan.

Storage: ~/.claude/.tmp/dod/<session_id>.json; files older than 7 days are purged.
Never blocks, never injects context: always prints {} and exits 0.
git subprocesses are capped at 4 s; any exception = silent no-op.

To unplug: remove both hook entries (UserPromptSubmit and Stop)
from ~/.claude/settings.json, then delete dod-snapshot.py and dod-check.py.

Manual test:
  echo '{"session_id":"t","cwd":"/path/to/repo"}' | ./dod-snapshot.py ; cat ~/.claude/.tmp/dod/t.json
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

GIT_TIMEOUT_S = 4.0
SNAPSHOT_DIR = os.path.join(os.path.expanduser("~"), ".claude", ".tmp", "dod")
PURGE_AGE_S = 7 * 86400


def safe_session_id(value):
    """Safe file name derived from the session_id (None if unusable)."""
    if not isinstance(value, str) or not value.strip():
        return None
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())[:128]


def snapshot_path(session_id):
    return os.path.join(SNAPSHOT_DIR, session_id + ".json")


def _git(cwd, args):
    """stdout (bytes) of a git command; None on failure, timeout or missing git."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def compute_fingerprint(cwd):
    """(toplevel, sha256 hex); (None, None) if cwd is not inside a git repository."""
    if not cwd or not os.path.isdir(cwd):
        return None, None
    top = _git(cwd, ["rev-parse", "--show-toplevel"])
    if not top:
        return None, None
    toplevel = top.decode("utf-8", "surrogateescape").strip()
    if not toplevel:
        return None, None
    status = _git(toplevel, ["status", "--porcelain=v1", "--untracked-files=all", "-z"])
    if status is None:
        return toplevel, None
    diff = _git(toplevel, ["diff", "HEAD", "--no-color"])
    if diff is None:  # repository without commits: HEAD does not exist yet
        diff = _git(toplevel, ["diff", "--no-color"]) or b""
    digest = hashlib.sha256()
    digest.update(status)
    digest.update(b"\0--diff--\0")
    digest.update(diff)
    digest.update(b"\0--untracked--\0")
    for entry in status.split(b"\0"):
        if not entry.startswith(b"?? "):
            continue
        rel = entry[3:]
        path = os.path.join(toplevel, rel.decode("utf-8", "surrogateescape"))
        try:
            st = os.stat(path)
            meta = "%d\0%d" % (st.st_size, st.st_mtime_ns)
        except OSError:
            meta = "missing"
        digest.update(rel + b"\0" + meta.encode("utf-8") + b"\0")
    return toplevel, digest.hexdigest()


def write_snapshot(session_id, cwd, toplevel, fingerprint):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    payload = {
        "session_id": session_id,
        "cwd": cwd,
        "toplevel": toplevel,
        "fingerprint": fingerprint,
        "ts": time.time(),
    }
    tmp = snapshot_path(session_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, snapshot_path(session_id))


def purge_old_snapshots(now=None):
    now = time.time() if now is None else now
    try:
        entries = os.scandir(SNAPSHOT_DIR)
    except OSError:
        return
    with entries:
        for entry in entries:
            try:
                if entry.is_file() and now - entry.stat().st_mtime > PURGE_AGE_S:
                    os.unlink(entry.path)
            except OSError:
                continue


def main():
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, OSError):
        data = {}
    try:
        session_id = safe_session_id(data.get("session_id"))
        cwd = data.get("cwd") or os.getcwd()
        if session_id:
            toplevel, fingerprint = compute_fingerprint(cwd)
            write_snapshot(session_id, cwd, toplevel, fingerprint)
        purge_old_snapshots()
    except Exception:  # never get in the way of the prompt
        pass
    sys.stdout.write("{}\n")
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    main()
