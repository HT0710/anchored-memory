#!/usr/bin/env python3
"""
Claim store for the anchored-memory PoC. One JSON file, no database.

A claim is a thing worth remembering, attached to a place in the code:

    {"id": "c3", "kind": "decision", "text": "...",
     "anchor": "src/auth/provider.py::validate",
     "valid_from": "2026-04-02", "state": "active"}

Every claim MUST have an anchor. That is the whole premise -- an unanchored claim
cannot be staleness-checked, so the store refuses it.

    ./claims.py add --kind decision --anchor path/to/f.py \
        --text "chose JWT over sessions; sessions broke behind the proxy"
    ./claims.py list [--kind failure] [--include-inactive]
    ./claims.py check                 # staleness for every active claim
    ./claims.py supersede c3 --by c9
    ./claims.py revoke c3 --note "never actually true"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from storage import local_store_lock, path as storage_path, repo_root as find_repo_root

try:
    import fcntl
except ImportError:  # pragma: no cover - documented POSIX-only mutation lock
    fcntl = None

KINDS = ("decision", "failure", "convention")
STATES = ("active", "superseded", "revoked")


def store_path(repo: Path) -> Path:
    return storage_path(repo, "claims.json")


def repo_root(start: Path | None = None) -> Path:
    root = find_repo_root(start)
    if root is None:
        sys.exit("error: not inside a git repository")
    return root


def valid_claim(claim: object) -> bool:
    if not isinstance(claim, dict):
        return False
    required = ("id", "kind", "text", "anchor", "valid_from", "state")
    if not all(isinstance(claim.get(k), str) and claim[k] for k in required):
        return False
    if claim["kind"] not in KINDS or claim["state"] not in STATES:
        return False
    try:
        datetime.fromisoformat(claim["valid_from"])
    except ValueError:
        return False
    path, _ = split_anchor(claim["anchor"])
    return not Path(path).is_absolute() and ".." not in Path(path).parts


def load(repo: Path) -> list[dict]:
    p = store_path(repo)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"error: cannot read valid JSON from {p} ({e}); fix or move it aside")
    claims = data.get("claims") if isinstance(data, dict) else data
    if not isinstance(claims, list) or not all(valid_claim(c) for c in claims):
        sys.exit(f"error: {p} must contain valid claim objects; refusing to overwrite it")
    return claims


@contextmanager
def mutation_lock(repo: Path):
    """POSIX advisory lock for each full claim-store mutation."""
    if fcntl is None:
        sys.exit("error: claim mutations require POSIX fcntl locking")
    p = store_path(repo)
    if os.environ.get("ANCHORED_MEMORY_CLAIMS"):
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.with_suffix(p.suffix + ".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    else:
        with local_store_lock(repo):
            yield


def save(repo: Path, claims: list[dict]) -> Path:
    p = store_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    # write-then-rename so an interrupted write cannot truncate the store
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"claims": claims}, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(p)
    return p


def next_id(claims: list[dict]) -> str:
    n = 0
    for c in claims:
        cid = c.get("id", "")
        if cid.startswith("c") and cid[1:].isdigit():
            n = max(n, int(cid[1:]))
    return f"c{n + 1}"


def split_anchor(anchor: str) -> tuple[str, str | None]:
    """`path::symbol` -> (path, symbol). Bare path -> (path, None)."""
    if "::" in anchor:
        path, _, sym = anchor.partition("::")
        return path, sym or None
    return anchor, None


def normalize_anchor(anchor: str) -> str:
    """`./f.py` and `f.py` name one place; recall matches the canonical form only."""
    path, sym = split_anchor(anchor)
    if not path:
        return anchor
    path = os.path.normpath(path)
    return f"{path}::{sym}" if sym else path


def validate_anchor(repo: Path, anchor: str) -> tuple[bool, str]:
    """
    Does the anchor point at something git knows about right now?

    A missing path is allowed -- a claim about deleted code is often the most
    valuable kind -- but the user is told, because a typo looks identical to a
    deletion and only they can tell the difference.
    """
    path, _sym = split_anchor(anchor)
    if not path or Path(path).is_absolute() or ".." in Path(path).parts:
        return False, "anchor must be a safe repository-relative path"
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", path],
        capture_output=True, text=True,
    ).returncode == 0
    if tracked:
        return True, "tracked at HEAD"
    ever = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%H", "--", path],
        capture_output=True, text=True,
    ).stdout.strip()
    if ever:
        return False, "not at HEAD, but exists in history (deleted?)"
    return False, "git has never seen this path -- typo?"


def cmd_add(args, repo: Path) -> int:
    text = args.text.strip()
    if not text:
        sys.exit("error: --text must not be blank")
    # one fact can concern several files; every anchor must pass before any is written
    anchors = list(dict.fromkeys(normalize_anchor(a) for a in args.anchor))
    for anchor in anchors:
        ok, why = validate_anchor(repo, anchor)
        if not ok and "safe repository-relative" in why:
            sys.exit(f"error: {why}")
        if not ok and not args.force:
            print(f"anchor: {anchor}\n  {why}")
            if "never seen" in why:
                return sys.exit("refusing to add; pass --force if the path is right")
            print("  (adding anyway -- a claim about deleted code is still a claim)")

    vf = args.valid_from or date.today().isoformat()
    try:
        datetime.fromisoformat(vf)
    except ValueError:
        sys.exit(f"error: --valid-from {vf!r} is not an ISO date")

    with mutation_lock(repo):
        claims = load(repo)
        normalized = " ".join(text.split())
        added = []
        for anchor in anchors:
            duplicate = next((
                e for e in claims
                if e.get("state") == "active" and e.get("kind") == args.kind
                and e.get("anchor") == anchor
                and " ".join(e.get("text", "").split()) == normalized
            ), None)
            if duplicate is not None:
                print(f"duplicate {duplicate.get('id')} ({args.kind}) -> {anchor}")
                continue
            claim = {
                "id": next_id(claims),
                "kind": args.kind,
                "text": text,
                "anchor": anchor,
                "valid_from": vf,
                "state": "active",
                "source": args.source,
                "added_at": datetime.now(timezone.utc).isoformat(),
            }
            if args.note:
                claim["note"] = args.note
            claims.append(claim)
            added.append(claim)
        if not added:
            return 0
        p = save(repo, claims)
    for claim in added:
        print(f"added {claim['id']} ({claim['kind']}) -> {claim['anchor']}")
    print(f"  {p}")
    return 0


def cmd_list(args, repo: Path) -> int:
    claims = load(repo)
    if not claims:
        print("no claims yet. add one with:  ./claims.py add --help")
        return 0
    for c in claims:
        if c["state"] != "active" and not args.include_inactive:
            continue
        if args.kind and c["kind"] != args.kind:
            continue
        mark = "" if c["state"] == "active" else f" [{c['state']}]"
        print(f"{c['id']:>4} {c['kind']:<11} {c['valid_from']}  {c['anchor']}{mark}")
        print(f"      {c['text']}")
    return 0


def cmd_supersede(args, repo: Path) -> int:
    with mutation_lock(repo):
        claims = load(repo)
        ids = {c.get("id") for c in claims}
        if args.id not in ids:
            sys.exit(f"error: no claim {args.id}")
        if args.by and args.by not in ids:
            sys.exit(f"error: no claim {args.by} to supersede it with")
        for c in claims:
            if c.get("id") == args.id:
                c["state"] = "superseded"
                c["superseded_by"] = args.by
                c["valid_to"] = date.today().isoformat()
        save(repo, claims)
    print(f"{args.id} superseded" + (f" by {args.by}" if args.by else ""))
    return 0


def cmd_revoke(args, repo: Path) -> int:
    with mutation_lock(repo):
        claims = load(repo)
        if args.id not in {c.get("id") for c in claims}:
            sys.exit(f"error: no claim {args.id}")
        for c in claims:
            if c.get("id") == args.id:
                c["state"] = "revoked"
                c["valid_to"] = date.today().isoformat()
                if args.note:
                    c["revoke_note"] = args.note
        save(repo, claims)
    print(f"{args.id} revoked")
    return 0


def cmd_check(args, repo: Path) -> int:
    """Staleness for every active claim. Delegates to staleness.py."""
    from staleness import resolve  # local import: check is the only user

    claims = [c for c in load(repo) if c["state"] == "active"]
    if not claims:
        print("no active claims")
        return 0

    rows = [(c, resolve(repo, c)) for c in claims]
    width = max(len(c["anchor"]) for c, _ in rows)
    flagged = 0
    for c, r in rows:
        if r.flagged:
            flagged += 1
        tag = {
            "fresh": "fresh",
            "rewritten": "REWRITTEN",
            "gone": "GONE",
            "unassessable": "unassessable",
        }[r.status]
        # only say "exempt" when kind is genuinely why it went unflagged --
        # `unassessable` is a missing baseline, not an exemption
        exempt = ""
        if not r.flagged and r.status in ("gone", "rewritten"):
            exempt = "  (kind exempt)"
        print(f"{c['id']:>4} {c['anchor']:<{width}}  {tag:<10}{exempt}")
        if r.detail:
            print(f"      {r.detail}")
    print()
    print(f"{flagged}/{len(rows)} active claims would be flagged stale")
    print("VERDICT NEEDED: of those flagged, how many were still useful?")
    print("  Mostly useful => the trigger is wrong. That is the kill condition.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="record a claim")
    a.add_argument("--kind", required=True, choices=KINDS)
    a.add_argument("--anchor", required=True, action="append",
                   help="path or path::symbol; repeat to attach one fact to several files")
    a.add_argument("--text", required=True)
    a.add_argument("--valid-from", default=None, help="ISO date (default: today)")
    a.add_argument("--source", default="hand", help="hand | mined | agent")
    a.add_argument("--note", default=None)
    a.add_argument("--force", action="store_true", help="add despite an unknown path")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list", help="show claims")
    l.add_argument("--kind", choices=KINDS)
    l.add_argument("--include-inactive", action="store_true")
    l.set_defaults(fn=cmd_list)

    c = sub.add_parser("check", help="staleness for all active claims")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("supersede", help="a later claim replaced this one")
    s.add_argument("id")
    s.add_argument("--by", default=None)
    s.set_defaults(fn=cmd_supersede)

    r = sub.add_parser("revoke", help="this was never true, or its anchor is gone")
    r.add_argument("id")
    r.add_argument("--note", default=None)
    r.set_defaults(fn=cmd_revoke)

    args = ap.parse_args()
    repo = repo_root()
    return args.fn(args, repo)


if __name__ == "__main__":
    sys.exit(main())
