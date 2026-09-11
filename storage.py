"""Repository-local anchored-memory storage routing."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - documented POSIX-only locking
    fcntl = None


def repo_root(start: Path | None = None) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(start or Path.cwd()), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 and out.stdout.strip() else None


def path(repo: Path, name: str) -> Path:
    override = os.environ.get(f"ANCHORED_MEMORY_{'CLAIMS' if name == 'claims.json' else 'LOG'}")
    return Path(override).expanduser() if override else repo / ".anchored-memory" / name


def legacy_home_requires_migration(name: str) -> bool:
    override = os.environ.get(f"ANCHORED_MEMORY_{'CLAIMS' if name == 'claims.json' else 'LOG'}")
    return bool(os.environ.get("ANCHORED_MEMORY_HOME")) and not bool(override)


@contextmanager
def local_store_lock(repo: Path):
    """Serialize default-store migration, mutations, and recording in one repo."""
    if fcntl is None:
        raise OSError("POSIX fcntl locking required")
    git = repo / ".git"
    if git.is_file():
        git = Path(git.read_text().strip().partition(":")[2].strip())
        if not git.is_absolute():
            git = (repo / git).resolve()
    lock_path = git / "anchored-memory.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def require_claim_store(repo: Path) -> None:
    if legacy_home_requires_migration("claims.json"):
        sys.exit("error: ANCHORED_MEMORY_HOME is retired; run migrate-legacy or set ANCHORED_MEMORY_CLAIMS")
