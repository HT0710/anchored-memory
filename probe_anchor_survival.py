#!/usr/bin/env python3
"""
Probe: do code anchors survive long enough for anchor-derived staleness to work?

The idea under test: attach each memory to a code location,
then let `git` decide when the memory is suspect -- the anchored symbol was
rewritten after the memory was formed, so the memory is questionable.

The idea inverts if anchors die faster than memories stay useful. The most
valuable memory is often "we tried X here and it was a disaster", and the code
being gone is *why* it matters. This script measures the survival half of that.

    Stage A  git-only, no transcripts needed. Runs on any repo today.
             How long does a touched file/symbol survive before rewrite/deletion?
    Stage B  transcript-driven. Needs sessions recorded against this repo.
             Which anchors did real sessions touch, and did those survive?

Output is one JSON blob plus a readable summary. No writes outside --out.

    ./probe_anchor_survival.py --repo . --out probe-results.json
    ./probe_anchor_survival.py --repo . --transcripts ~/.claude/projects/<dir>

ponytail: file-granular by default; --symbols adds a crude regex symbol pass for
Python only. Use a language-aware parser before treating Stage B symbol
results as reliable.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------


def git(repo: Path, *args: str, check: bool = True) -> str:
    """Run a git command in repo, return stdout. Empty string on tolerated failure."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if check:
            raise RuntimeError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return ""
    return proc.stdout


def assert_git_repo(repo: Path) -> None:
    """Accept a normal checkout, a worktree, or a bare clone (bare is fine --
    this probe only reads history, never the working tree)."""
    if (repo / ".git").exists():
        return
    if git(repo, "rev-parse", "--is-inside-work-tree", check=False).strip() == "true":
        return
    if git(repo, "rev-parse", "--is-bare-repository", check=False).strip() == "true":
        return
    sys.exit(f"error: {repo} is not a git repository")


def parse_git_date(s: str) -> datetime:
    """Parse git ISO-8601 (`%aI`) into an aware datetime."""
    return datetime.fromisoformat(s.strip())


# --------------------------------------------------------------------------
# Stage A -- file-level anchor survival
# --------------------------------------------------------------------------


def baseline_sizes(repo: Path, since: str | None, paths: set[str]) -> dict[str, int]:
    """
    Line count of each path as it stood at the start of the window.

    Returns {} when there is no --since (every file's history is fully visible,
    so the creating commit's own additions are the correct denominator).
    Files that did not exist yet are absent from the result.
    """
    if not since or not paths:
        return {}
    base = git(repo, "rev-list", "-1", f"--before={since}", "HEAD", check=False).strip()
    if not base:
        return {}

    listing = git(repo, "ls-tree", "-r", "--name-only", base, check=False).splitlines()
    present = paths & set(listing)
    if not present:
        return {}

    sizes: dict[str, int] = {}
    # one git call per batch, not per file
    batch: list[str] = []

    def drain(batch: list[str]) -> None:
        if not batch:
            return
        proc = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            input="".join(f"{base}:{p}\n" for p in batch),
            capture_output=True,
            text=True,
            errors="replace",
        )
        # output is: "<sha> blob <size>\n<contents>\n" per request, in order
        out = proc.stdout
        pos = 0
        for path in batch:
            nl = out.find("\n", pos)
            if nl == -1:
                return
            header = out[pos:nl]
            parts = header.split()
            if len(parts) != 3 or parts[1] != "blob":
                pos = nl + 1
                continue
            n = int(parts[2])
            body = out[nl + 1 : nl + 1 + n]
            sizes[path] = body.count("\n") + (1 if body and not body.endswith("\n") else 0)
            pos = nl + 1 + n + 1
    for path in sorted(present):
        batch.append(path)
        if len(batch) >= 400:
            drain(batch); batch = []
    drain(batch)
    return sizes


@dataclass
class FileLife:
    path: str
    first_seen: str
    last_touched: str
    n_commits: int
    alive: bool
    lifespan_days: float
    # days from first observation to the next substantive rewrite (deletion-based)
    days_to_rewrite: float | None = None
    # same, under the old churn rule -- for measuring the correction only
    days_to_rewrite_legacy: float | None = None
    # line count at first observation; 0 means the file was born in the window
    size_at_start: int = 0


# What counts as a rewrite.
#
# The first version of this probe used (added + deleted) / size, which conflates
# growth with replacement: appending 400 lines to a 200-line file scored as a
# "rewrite" of content that was never touched. For staleness that is wrong --
# a memory about existing code is not invalidated by new code appearing next to
# it. DELETION is the discriminator, so the ratio is deleted / size_before.
REWRITE_DELETE_RATIO = 0.35
REWRITE_MIN_DELETED = 15

# Kept only so a run can report how much the old rule over-counted.
LEGACY_CHURN_RATIO = 0.5
LEGACY_MIN_LINES = 20


def collect_file_lives(
    repo: Path,
    since: str | None,
    until: str | None,
    path_filter: re.Pattern | None,
    exclude: re.Pattern | None,
    all_refs: bool,
    bulk_threshold: int,
) -> tuple[dict[str, FileLife], dict[str, list[tuple[datetime, int, int]]], dict]:
    """
    Walk the whole history once with --numstat and build per-path timelines.

    Returns (lives, churn) where churn maps path -> [(when, added, deleted), ...]
    """
    args = [
        "log",
        *(["--all"] if all_refs else []),
        "--no-merges",
        "--numstat",
        "--format=__C__%H\t%aI",
        "--diff-filter=AMD",
    ]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")

    raw = git(repo, *args)

    # Group into commits first so a bulk commit can be dropped whole. A commit
    # touching thousands of files is a vendored tree, a squash, or a bulk move --
    # it says nothing about whether one anchored memory went stale, and it
    # dominates every percentile if left in.
    commits: list[tuple[datetime, list[tuple[str, int, int]]]] = []
    when: datetime | None = None
    cur: list[tuple[str, int, int]] = []

    def flush() -> None:
        if when is not None and cur:
            commits.append((when, list(cur)))

    for line in raw.splitlines():
        if line.startswith("__C__"):
            flush()
            cur = []
            _, _, rest = line.partition("__C__")
            _sha, _, iso = rest.partition("\t")
            try:
                when = parse_git_date(iso)
            except ValueError:
                when = None
            continue
        if not line.strip() or when is None:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, deleted_s, path = parts
        if added_s == "-" or deleted_s == "-":
            continue  # binary
        # renames arrive as "old => new" or "dir/{a => b}/f"; skipped, see NOTE in output
        if "=>" in path:
            continue
        cur.append((path, int(added_s), int(deleted_s)))
    flush()

    churn: dict[str, list[tuple[datetime, int, int]]] = defaultdict(list)
    dropped_commits = 0
    dropped_rows = 0

    for when_i, rows in commits:
        if len(rows) >= bulk_threshold:
            dropped_commits += 1
            dropped_rows += len(rows)
            continue
        for path, added, deleted in rows:
            if exclude and exclude.search(path):
                continue
            if path_filter and not path_filter.search(path):
                continue
            churn[path].append((when_i, added, deleted))

    filtering = {
        "commits_seen": len(commits),
        "bulk_commits_dropped": dropped_commits,
        "bulk_rows_dropped": dropped_rows,
        "bulk_threshold": bulk_threshold,
        "all_refs": all_refs,
    }

    # which paths still exist at HEAD. ls-tree, not ls-files: the index is empty
    # in a --no-checkout clone, which would report every file as deleted.
    alive_now = set(git(repo, "ls-tree", "-r", "HEAD", "--name-only").splitlines())

    # True line counts as of the window's start. Without this, a file that
    # already existed before --since gets its size seeded from a *modify*
    # hunk, which is far too small, and then every later change trips the
    # ratio. This was the main reason the windowed medians collapsed.
    baseline = baseline_sizes(repo, since, set(churn.keys()))

    lives: dict[str, FileLife] = {}
    for path, events in churn.items():
        events.sort(key=lambda e: e[0])
        first, last = events[0][0], events[-1][0]

        # Denominator: real size at window start if the file predates the window,
        # else the lines its creating commit added.
        start_size = baseline.get(path, 0)
        born_in_window = start_size == 0
        size = max(start_size or events[0][1], 1)

        days_to_rewrite = None
        days_to_rewrite_legacy = None
        legacy_size = size
        # skip the creating commit only when it really is the creation
        tail = events[1:] if born_in_window else events

        for when_i, added, deleted in tail:
            if days_to_rewrite is None:
                if deleted >= REWRITE_MIN_DELETED and deleted >= REWRITE_DELETE_RATIO * size:
                    days_to_rewrite = (when_i - first).total_seconds() / 86400
            if days_to_rewrite_legacy is None:
                touched = added + deleted
                if touched >= LEGACY_MIN_LINES and touched >= LEGACY_CHURN_RATIO * legacy_size:
                    days_to_rewrite_legacy = (when_i - first).total_seconds() / 86400
            size = max(size + added - deleted, 1)
            legacy_size = max(legacy_size + added - deleted, 1)
            if days_to_rewrite is not None and days_to_rewrite_legacy is not None:
                break

        lives[path] = FileLife(
            path=path,
            first_seen=first.isoformat(),
            last_touched=last.isoformat(),
            n_commits=len(events),
            alive=path in alive_now,
            lifespan_days=(last - first).total_seconds() / 86400,
            days_to_rewrite=days_to_rewrite,
            days_to_rewrite_legacy=days_to_rewrite_legacy,
            size_at_start=start_size,
        )

    return lives, churn, filtering


# --------------------------------------------------------------------------
# Stage A' -- crude Python symbol survival
# --------------------------------------------------------------------------

DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", re.M)


def symbol_survival(
    repo: Path, lives: dict[str, FileLife], limit: int, all_refs: bool
) -> dict:
    """
    For the busiest Python files, compare symbols defined at first appearance
    against symbols present at HEAD. Approximate by design -- see module docstring.
    """
    py = [
        fl
        for fl in lives.values()
        if fl.path.endswith(".py") and fl.alive and fl.n_commits > 1
    ]
    py.sort(key=lambda fl: fl.n_commits, reverse=True)
    py = py[:limit]

    survived = died = 0
    per_file = []

    for fl in py:
        first_sha = git(
            repo,
            "log",
            *(["--all"] if all_refs else []),
            "--format=%H",
            "--reverse",
            "--",
            fl.path,
        ).split()
        if not first_sha:
            continue
        old = git(repo, "show", f"{first_sha[0]}:{fl.path}", check=False)
        new = git(repo, "show", f"HEAD:{fl.path}", check=False)
        if not old or not new:
            continue
        old_syms = set(DEF_RE.findall(old))
        new_syms = set(DEF_RE.findall(new))
        if not old_syms:
            continue
        s = len(old_syms & new_syms)
        d = len(old_syms - new_syms)
        survived += s
        died += d
        per_file.append(
            {
                "path": fl.path,
                "symbols_at_birth": len(old_syms),
                "survived": s,
                "gone": d,
                "commits": fl.n_commits,
            }
        )

    total = survived + died
    return {
        "files_sampled": len(per_file),
        "symbols_at_birth": total,
        "survived": survived,
        "gone": died,
        "survival_rate": round(survived / total, 3) if total else None,
        "per_file": per_file,
    }


# --------------------------------------------------------------------------
# Stage B -- transcript anchors
# --------------------------------------------------------------------------

EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


@dataclass
class TranscriptScan:
    sessions: int = 0
    turns: int = 0
    tool_calls: int = 0
    edit_calls: int = 0
    paths: Counter = field(default_factory=Counter)
    first_ts: str | None = None
    last_ts: str | None = None


def scan_transcripts(dirs: list[Path], repo: Path) -> TranscriptScan:
    """
    Pull edited file paths out of Claude Code session transcripts.

    This is the deterministic layer from the brief -- tool calls and their args,
    no model in the extraction path. Only paths resolving inside `repo` are kept.
    """
    scan = TranscriptScan()
    repo = repo.resolve()
    timestamps: list[str] = []

    files = [f for d in dirs for f in sorted(d.glob("*.jsonl"))]
    for jf in files:
        scan.sessions += 1
        for line in jf.open(errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("timestamp"):
                timestamps.append(rec["timestamp"])
            scan.turns += 1

            msg = rec.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                scan.tool_calls += 1
                name = block.get("name", "")
                if name not in EDIT_TOOLS:
                    continue
                scan.edit_calls += 1
                fp = (block.get("input") or {}).get("file_path")
                if not fp:
                    continue
                try:
                    rel = Path(fp).resolve().relative_to(repo)
                except (ValueError, OSError):
                    continue  # outside this repo
                scan.paths[str(rel)] += 1

    if timestamps:
        timestamps.sort()
        scan.first_ts, scan.last_ts = timestamps[0], timestamps[-1]
    return scan


def join_anchors(scan: TranscriptScan, lives: dict[str, FileLife]) -> dict:
    """The actual question: of the anchors real sessions produced, how many held?"""
    hits, misses = [], []
    for path, n in scan.paths.most_common():
        fl = lives.get(path)
        if fl is None:
            misses.append(path)
            continue
        hits.append(
            {
                "path": path,
                "edits_in_sessions": n,
                "alive": fl.alive,
                "commits_since_birth": fl.n_commits,
                "days_to_rewrite": fl.days_to_rewrite,
            }
        )
    alive = sum(1 for h in hits if h["alive"])
    rewritten = sum(1 for h in hits if h["days_to_rewrite"] is not None)
    return {
        "anchors_resolved": len(hits),
        "anchors_unresolved": len(misses),
        "unresolved_sample": misses[:10],
        "still_alive": alive,
        "deleted": len(hits) - alive,
        "rewritten_since": rewritten,
        "would_be_flagged_stale": rewritten + (len(hits) - alive),
        "anchors": hits[:50],
    }


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def quantiles(xs: list[float]) -> dict:
    if not xs:
        return {}
    xs = sorted(xs)
    return {
        "n": len(xs),
        "p10": round(xs[int(0.10 * (len(xs) - 1))], 1),
        "median": round(statistics.median(xs), 1),
        "p90": round(xs[int(0.90 * (len(xs) - 1))], 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure how long code anchors survive, to test anchor-derived staleness."
    )
    ap.add_argument("--repo", default=".", help="repository to analyze (default: cwd)")
    ap.add_argument(
        "--transcripts",
        nargs="*",
        default=[],
        help="Claude Code project dirs holding *.jsonl sessions (Stage B)",
    )
    ap.add_argument("--since", default=None, help="limit history, e.g. '12 months ago'")
    ap.add_argument(
        "--until",
        default=None,
        help="upper bound on history; with --since this slices one project phase",
    )
    ap.add_argument(
        "--path-filter", default=None, help="regex; only paths matching are counted"
    )
    ap.add_argument(
        "--exclude",
        default=r"(^|/)(node_modules|\.venv|venv|dist|build|vendor|third_party|"
        r"site-packages|__pycache__|\.next|target)/|\.(lock|min\.js|map)$",
        help="regex of paths to ignore; default drops vendored and generated trees",
    )
    ap.add_argument(
        "--all-refs",
        action="store_true",
        help="include every branch (default: HEAD only -- worktree branches skew results)",
    )
    ap.add_argument(
        "--bulk-threshold",
        type=int,
        default=500,
        help="drop commits touching >= this many files (vendored trees, squashes)",
    )
    ap.add_argument(
        "--symbols",
        action="store_true",
        help="also run the crude Python symbol-survival pass",
    )
    ap.add_argument("--symbol-limit", type=int, default=40)
    ap.add_argument("--out", default=None, help="write full JSON results here")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    assert_git_repo(repo)
    pf = re.compile(args.path_filter) if args.path_filter else None
    ex = re.compile(args.exclude) if args.exclude else None

    print(f"repo: {repo}")
    print(f"scope: {'all refs' if args.all_refs else 'HEAD only'}")
    print("stage A: walking history ...", flush=True)
    lives, _churn, filtering = collect_file_lives(
        repo, args.since, args.until, pf, ex, args.all_refs, args.bulk_threshold
    )
    if not lives:
        sys.exit("error: no file history found (check --since / --path-filter)")

    alive = [fl for fl in lives.values() if fl.alive]
    dead = [fl for fl in lives.values() if not fl.alive]
    rewritten = [fl for fl in lives.values() if fl.days_to_rewrite is not None]
    rewritten_legacy = [
        fl for fl in lives.values() if fl.days_to_rewrite_legacy is not None
    ]

    results: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo),
        "params": {
            "since": args.since,
            "until": args.until,
            "path_filter": args.path_filter,
            "rewrite_delete_ratio": REWRITE_DELETE_RATIO,
            "rewrite_min_deleted": REWRITE_MIN_DELETED,
            "exclude": args.exclude,
            **filtering,
        },
        "stage_a": {
            "files_seen": len(lives),
            "alive_at_head": len(alive),
            "deleted": len(dead),
            "ever_rewritten": len(rewritten),
            "ever_rewritten_legacy": len(rewritten_legacy),
            "days_to_first_rewrite": quantiles(
                [fl.days_to_rewrite for fl in rewritten]  # type: ignore[misc]
            ),
            "days_to_first_rewrite_legacy": quantiles(
                [fl.days_to_rewrite_legacy for fl in rewritten_legacy]  # type: ignore[misc]
            ),
            "files_predating_window": sum(
                1 for fl in lives.values() if fl.size_at_start > 0
            ),
            "lifespan_days_deleted": quantiles([fl.lifespan_days for fl in dead]),
        },
    }

    print()
    print("=" * 62)
    print("STAGE A  file-level anchor survival")
    print("=" * 62)
    a = results["stage_a"]
    if filtering["bulk_commits_dropped"]:
        print(
            f"  dropped {filtering['bulk_commits_dropped']} bulk commit(s) "
            f"(>= {filtering['bulk_threshold']} files, "
            f"{filtering['bulk_rows_dropped']} rows) as vendored/squashed"
        )
    print(f"  commits analyzed         : {filtering['commits_seen']}")
    print(f"  files touched in history : {a['files_seen']}")
    print(
        f"  still at HEAD            : {a['alive_at_head']}  "
        f"({pct(a['alive_at_head'], a['files_seen'])})"
    )
    print(
        f"  deleted                  : {a['deleted']}  "
        f"({pct(a['deleted'], a['files_seen'])})"
    )
    print(
        f"  substantively rewritten  : {a['ever_rewritten']}  "
        f"({pct(a['ever_rewritten'], a['files_seen'])})"
    )
    if a["days_to_first_rewrite"]:
        q = a["days_to_first_rewrite"]
        print(
            f"  days to first rewrite    : p10 {q['p10']}  median {q['median']}  p90 {q['p90']}"
        )
        if q["p10"] == q["p90"]:
            print(
                "  !! p10 == p90 -- the distribution collapsed. Almost certainly one\n"
                "     bulk commit still dominating; lower --bulk-threshold or widen\n"
                "     --exclude before reading anything into these numbers."
            )
    if a["days_to_first_rewrite_legacy"]:
        ql = a["days_to_first_rewrite_legacy"]
        print(
            f"  [old churn rule]         : {a['ever_rewritten_legacy']} files, "
            f"median {ql['median']}  p90 {ql['p90']}"
        )
        print("  (old rule counted growth as rewrite; shown to size the correction)")
    if a.get("files_predating_window"):
        print(
            f"  files predating window   : {a['files_predating_window']} "
            "(sized from the window's base commit, not from a hunk)"
        )
    if not args.since:
        print()
        print("  !! FULL-HISTORY RUN -- days-to-rewrite below is NOT usable.")
        print("     A file created years ago and gutted later contributes a")
        print("     multi-thousand-day 'time to rewrite' that says nothing about")
        print("     how long a memory stays valid; survivorship inflates every")
        print("     percentile. Re-run with --since (e.g. --since '24 months ago')")
        print("     for a figure you can act on. The alive/deleted counts above")
        print("     are still meaningful; the day percentiles are not.")
    print()
    print("  READ AS: median days-to-rewrite describes observed code rewrites only.")
    print("  It does not measure memory usefulness or a validated memory half-life.")
    print("  These exploratory results do not establish staleness accuracy.")

    if args.symbols:
        print()
        print("stage A': python symbol survival ...", flush=True)
        results["stage_a_symbols"] = symbol_survival(
            repo, lives, args.symbol_limit, args.all_refs
        )
        s = results["stage_a_symbols"]
        print(f"  files sampled   : {s['files_sampled']}")
        print(f"  symbols at birth: {s['symbols_at_birth']}")
        print(f"  survival rate   : {s['survival_rate']}")
        print("  (regex-based, Python only -- replace with the CSG index before trusting)")

    if args.transcripts:
        dirs = [Path(d).expanduser() for d in args.transcripts]
        dirs = [d for d in dirs if d.is_dir()]
        print()
        print("stage B: scanning transcripts ...", flush=True)
        if not dirs:
            print("  no readable transcript dirs given -- skipped")
        else:
            scan = scan_transcripts(dirs, repo)
            joined = join_anchors(scan, lives)
            results["stage_b"] = {
                "sessions": scan.sessions,
                "turns": scan.turns,
                "tool_calls": scan.tool_calls,
                "edit_calls": scan.edit_calls,
                "distinct_paths": len(scan.paths),
                "first_ts": scan.first_ts,
                "last_ts": scan.last_ts,
                **joined,
            }
            b = results["stage_b"]
            print("=" * 62)
            print("STAGE B  anchors real sessions actually produced")
            print("=" * 62)
            print(f"  sessions / edit calls : {b['sessions']} / {b['edit_calls']}")
            print(f"  distinct paths edited : {b['distinct_paths']}")
            print(
                f"  resolved in history   : {b['anchors_resolved']}  "
                f"(unresolved {b['anchors_unresolved']})"
            )
            if b["anchors_resolved"]:
                print(
                    f"  would be flagged stale: {b['would_be_flagged_stale']} / "
                    f"{b['anchors_resolved']}  "
                    f"({pct(b['would_be_flagged_stale'], b['anchors_resolved'])})"
                )
                print()
                print("  READ AS: this is the false-positive ceiling for the push channel.")
                print("  Now read the sessions behind a sample of the flagged anchors and")
                print("  ask: was that memory still useful? Every 'yes' is a wrong flag.")
            if b["anchors_unresolved"]:
                print()
                print("  NOTE: unresolved anchors are paths sessions edited that git never")
                print("  saw -- untracked, gitignored, or since renamed. Renames are skipped")
                print("  by this probe; if this count is high, add --follow handling.")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, default=str))
        print()
        print(f"full results -> {args.out}")

    print()
    print("VERDICT NEEDED (human, not this script):")
    print("  Do memories about rewritten or deleted code stay useful?")
    print("  If yes, anchor-derived staleness inverts and needs a different rule.")
    print("  That is the kill-switch at step 3 of the brief.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
