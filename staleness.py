#!/usr/bin/env python3
"""
Resolve a claim's anchor against git and say whether the claim is suspect.

The one mechanism this whole idea rests on: no model, no judgment, just history.
A claim is suspect when the code it was about has been substantively replaced
since the claim was made.

    fresh         anchor there, not substantively rewritten since valid_from
    rewritten     >= REWRITE_DELETE_RATIO of the lines present at valid_from are gone
    gone          the anchored path no longer exists at HEAD
    unassessable  the path did not exist at valid_from, so there is no baseline

Deletion is the discriminator, not total churn: appending code next to a
function does not invalidate what was decided about that function.
Counting added lines as replacements can over-flag unchanged code.

Anchors may be `path` or `path::Symbol`. A symbol anchor is resolved per-symbol
via symbols.py when the file is Python, and falls back to file-level otherwise --
a coarse verdict beats a wrong one.

`failure` claims are never flagged. "We tried X here and it was a disaster" does
not stop being true when X is deleted -- the deletion is usually the proof. Only
`decision` and `convention` claims stale.

    ./staleness.py path/to/file.py --since 2026-04-01
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REWRITE_DELETE_RATIO = 0.35
REWRITE_MIN_DELETED = 15

# Kinds whose truth does not depend on the code still being there.
STALE_EXEMPT_KINDS = {"failure"}


@dataclass
class Verdict:
    status: str          # fresh | rewritten | gone | unassessable
    flagged: bool        # would this be surfaced as stale, after kind exemption
    detail: str = ""


def git(repo: Path, *args: str) -> tuple[int, str]:
    # `git show rev:path` streams a blob, and a tracked blob is bytes, not text
    p = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace"
    )
    return p.returncode, p.stdout


def _split(anchor: str) -> tuple[str, str | None]:
    if "::" in anchor:
        path, _, sym = anchor.partition("::")
        return path, sym or None
    return anchor, None


def _lines_at(repo: Path, rev: str, path: str) -> int | None:
    code, out = git(repo, "show", f"{rev}:{path}")
    # splitlines, not count("\n"): a last line without a newline is still a line
    return len(out.splitlines()) if code == 0 else None


def _base_commit(repo: Path, since: str) -> str | None:
    """
    The commit that represents "the code as the claim describes it".

    Must be the last commit ON the claim's date, not the last one strictly
    before it: a claim mined from commit X describes the state X produced, and
    `--before=<date>` lands one commit too early -- reporting a file that X
    itself created as "absent at baseline". Anchor `--until` to the end of the
    day and fall back to the strict-before commit for claims whose date has no
    commits at all.
    """
    code, out = git(repo, "rev-list", "-1", f"--until={since} 23:59:59", "HEAD")
    if code == 0 and out.strip():
        return out.strip()
    code, out = git(repo, "rev-list", "-1", f"--before={since}", "HEAD")
    return out.strip() or None if code == 0 else None


def _deleted_since(repo: Path, base: str, path: str) -> int:
    """
    Lines of `path` present at `base` that are gone at HEAD.

    One diff against the baseline, NOT a sum of per-commit numstats. Summing
    double-counts: a file rewritten three times reports 3x its own length in
    deletions. The net
    diff answers the question actually being asked -- how much of what the
    claim was written about is still there.
    """
    code, out = git(repo, "diff", "--numstat", f"{base}..HEAD", "--", path)
    if code != 0:
        return 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].isdigit():
            return int(parts[1])
    return 0


def resolve(repo: Path, claim: dict) -> Verdict:
    """Verdict for one claim dict (needs `anchor`, `valid_from`, `kind`)."""
    anchor = claim["anchor"]
    since = claim["valid_from"]
    kind = claim.get("kind", "decision")
    path, symbol = _split(anchor)
    exempt = kind in STALE_EXEMPT_KINDS

    # gone?
    tracked = git(repo, "ls-files", "--error-unmatch", path)[0] == 0
    if not tracked:
        ever = git(repo, "log", "-1", "--format=%H", "--", path)[1].strip()
        if not ever:
            return Verdict("gone", False, "path unknown to git -- likely a typo, not staleness")
        return Verdict(
            "gone", not exempt,
            "deleted from HEAD" + ("  (failure claims exempt)" if exempt else ""),
        )

    base = _base_commit(repo, since)

    # Symbol-level checks can preserve a claim when unrelated code in its
    # file changes. Use symbol granularity when the language is supported.
    if symbol:
        # Resolve `symbols` from THIS file's directory rather than trusting the
        # caller to have put it on sys.path. It previously inherited a path from
        # whichever module imported it, and the ImportError below falls back to
        # file-level granularity silently -- so an unusual entry point lost
        # symbol precision without any error.
        _here = str(Path(__file__).resolve().parent)
        if _here not in sys.path:
            sys.path.insert(0, _here)
        try:
            from symbols import compare as compare_symbol
        except ImportError:
            compare_symbol = None
        if compare_symbol is not None and base:
            sv = compare_symbol(repo, path, symbol, base)
            if sv.status != "unassessable":
                suspect = sv.status in ("gone", "rewritten")
                return Verdict(
                    sv.status,
                    suspect and not exempt,
                    sv.detail + ("  (failure claims exempt)" if exempt and suspect else ""),
                )
            # symbol unresolvable (renamed, or not Python): fall through to the
            # file-level rule rather than reporting nothing at all

    if base is None:
        # No commit on or before valid_from: the claim is older than the
        # history git holds (a young repo, or history rewritten or republished
        # since). The file may well have existed -- git cannot see that far.
        roots = git(repo, "log", "--max-parents=0", "--format=%as", "HEAD")[1].split()
        earliest = f" (earliest commit {min(roots)})" if roots else ""
        return Verdict(
            "unassessable", False,
            f"claim predates repository history{earliest}; re-add with a later "
            "--valid-from and supersede this claim",
        )

    size_then = _lines_at(repo, base, path)
    if not size_then:
        # The file did not exist at valid_from: the claim predates it.
        # Reported as its own status: calling this "fresh" would let an
        # unassessable anchor read as a verified one.
        return Verdict(
            "unassessable", False,
            f"path absent at {since}; nothing to compare against",
        )

    deleted = _deleted_since(repo, base, path)
    ratio = deleted / size_then
    if deleted >= REWRITE_MIN_DELETED and ratio >= REWRITE_DELETE_RATIO:
        return Verdict(
            "rewritten", not exempt,
            f"{deleted} of {size_then} lines removed since {since} "
            f"({ratio:.0%})" + ("  (failure claims exempt)" if exempt else ""),
        )

    return Verdict(
        "fresh", False,
        f"{deleted} of {size_then} lines removed since {since} ({ratio:.0%})",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Check one anchor's staleness.")
    ap.add_argument("anchor", help="path or path::symbol")
    ap.add_argument("--since", required=True, help="the claim's valid_from (ISO date)")
    ap.add_argument("--kind", default="decision", help="decision | failure | convention")
    ap.add_argument("--repo", default=".")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if git(repo, "rev-parse", "--show-toplevel")[0] != 0:
        sys.exit(f"error: {repo} is not a git repository")

    v = resolve(repo, {"anchor": args.anchor, "valid_from": args.since, "kind": args.kind})
    print(f"{args.anchor}  ->  {v.status}{'  [FLAGGED]' if v.flagged else ''}")
    if v.detail:
        print(f"  {v.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
