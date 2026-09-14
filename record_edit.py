#!/usr/bin/env python3
"""PostToolUse recorder. Stores only in-repository edit metadata; stays silent."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import subprocess
import time

from storage import git_dir, legacy_home_requires_migration, local_store_lock, path as storage_path, repo_root

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MAX_BYTES = 64 * 1024 * 1024
# Lives in the per-worktree git dir: never tracked, never shared by a log override.
SNAPSHOT = "anchored-memory-worktree.json"


def head_sha(root: Path) -> str | None:
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError): return None


def relative_paths(root: Path, cwd: str, inp: dict) -> list[str]:
    value = inp.get("file_path") or inp.get("notebook_path")
    if not isinstance(value, str): return []
    try:
        p = (Path(cwd) / value if not Path(value).is_absolute() else Path(value)).resolve()
        return [str(p.relative_to(root))]
    except (OSError, ValueError): return []


def rotate(log: Path) -> None:
    if log.exists() and log.stat().st_size > MAX_BYTES: log.replace(log.with_suffix(".jsonl.1"))


def append(log: Path, rows: list[dict]) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    rotate(log)
    with log.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def worktree_scan(root: Path) -> dict | None:
    """HEAD plus which uncommitted paths exist. The clock is read before git
    runs, so a file written during the scan still counts as newer next time."""
    ts = time.time_ns()
    try:
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"], capture_output=True, timeout=10)
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError): return None
    if status.returncode: return None
    files: dict[str, bool] = {}
    items = status.stdout.split(b"\0")
    i = 0
    while i < len(items):
        item, i = items[i], i + 1
        if len(item) < 4: continue
        rel = os.fsdecode(item[3:])
        files[rel] = os.path.lexists(root / rel)
        if item[:1] in (b"R", b"C") and i < len(items):  # a rename names its source next
            src, i = os.fsdecode(items[i]), i + 1
            files[src] = os.path.lexists(root / src)
    return {"ts": ts, "head": head.stdout.strip() or None, "files": files}


def shell_changes(root: Path, before: dict, after: dict) -> list[str]:
    """Paths written or deleted since `before` was taken. Judged by mtime, not by
    leaving the dirty set, so committing an older edit is not a new edit."""
    candidates = set(before["files"]) | set(after["files"])
    if before.get("head") and after["head"] and before["head"] != after["head"]:
        # a file written and committed within one command is clean again by now
        out = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "-z", before["head"], after["head"]], capture_output=True, timeout=10)
        if out.returncode == 0:
            candidates |= {os.fsdecode(p) for p in out.stdout.split(b"\0") if p}
    changed = []
    for rel in sorted(candidates):
        try:
            # ponytail: ns mtimes; a coarse-mtime filesystem can miss an edit made in the snapshot's own second
            newer = (root / rel).lstat().st_mtime_ns >= before["ts"]
        except OSError:
            # missing now: an edit only if it existed at the snapshot; paths absent
            # from the snapshot were clean, hence present
            newer = before["files"].get(rel, True)
        if newer: changed.append(rel)
    return changed


def load_snapshot(snap: Path) -> dict | None:
    try:
        data = json.loads(snap.read_text())
    except (OSError, ValueError): return None
    ok = isinstance(data, dict) and isinstance(data.get("ts"), int) and isinstance(data.get("files"), dict)
    return data if ok else None


def save_snapshot(snap: Path, state: dict) -> None:
    tmp = snap.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(snap)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict): return 0
        tool = payload.get("tool_name")
        if tool not in EDIT_TOOLS and tool != "Bash": return 0
        cwd = payload.get("cwd", os.getcwd())
        inp = payload.get("tool_input")
        if not isinstance(cwd, str) or not isinstance(inp, dict): return 0
        root = repo_root(Path(cwd))
        if root is None or legacy_home_requires_migration("edits.jsonl"): return 0
        # shell edits are found by comparing the worktree, never by parsing the command
        paths = [] if tool == "Bash" else relative_paths(root, cwd, inp)
        if tool != "Bash" and not paths: return 0
        response = payload.get("tool_response")
        failed = bool(response.get("error")) or response.get("success") is False if isinstance(response, dict) else isinstance(response, str) and response.lstrip().lower().startswith("error")
        log = storage_path(root, "edits.jsonl")
        snap = git_dir(root) / SNAPSHOT
        # one lock for snapshot and log: flock would deadlock on a nested acquire
        # ponytail: one snapshot per worktree; two sessions in it can be credited with each other's edits
        with local_store_lock(root):
            before = load_snapshot(snap) if tool == "Bash" else None
            after = worktree_scan(root)
            if after is not None:
                if before is not None:
                    paths = shell_changes(root, before, after)
                # edit tools refresh it too, so the next shell command does not re-log their edit
                save_snapshot(snap, after)
            if paths:
                head = head_sha(root)
                append(log, [{"ts": datetime.now(timezone.utc).isoformat(), "session": payload.get("session_id"), "tool": tool, "path": p, "in_repo": True, "repo": root.name, "head": head, "failed": failed} for p in paths])
    except Exception:
        pass
    return 0

if __name__ == "__main__": sys.exit(main())
