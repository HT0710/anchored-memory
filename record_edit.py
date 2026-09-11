#!/usr/bin/env python3
"""PostToolUse recorder. Stores only in-repository edit metadata; stays silent."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from storage import legacy_home_requires_migration, local_store_lock, path as storage_path, repo_root

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MAX_BYTES = 64 * 1024 * 1024


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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("tool_name") not in EDIT_TOOLS: return 0
        cwd = payload.get("cwd", os.getcwd())
        inp = payload.get("tool_input")
        if not isinstance(cwd, str) or not isinstance(inp, dict): return 0
        root = repo_root(Path(cwd))
        if root is None or legacy_home_requires_migration("edits.jsonl"): return 0
        paths = relative_paths(root, cwd, inp)
        if not paths: return 0
        response = payload.get("tool_response")
        failed = bool(response.get("error")) or response.get("success") is False if isinstance(response, dict) else isinstance(response, str) and response.lstrip().lower().startswith("error")
        rows = [{"ts": datetime.now(timezone.utc).isoformat(), "session": payload.get("session_id"), "tool": payload["tool_name"], "path": p, "in_repo": True, "repo": root.name, "head": head_sha(root), "failed": failed} for p in paths]
        log = storage_path(root, "edits.jsonl")
        if os.environ.get("ANCHORED_MEMORY_LOG"):
            log.parent.mkdir(parents=True, exist_ok=True)
            rotate(log)
            with log.open("a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            with local_store_lock(root):
                log.parent.mkdir(parents=True, exist_ok=True)
                rotate(log)
                with log.open("a", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return 0

if __name__ == "__main__": sys.exit(main())
