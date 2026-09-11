#!/usr/bin/env python3
"""
Symbol-level anchor resolution, stdlib only.

File-level checks can flag a function's claim when unrelated code changes.
Python's stdlib `ast` locates symbols directly in the source being compared,
without an external index or build step.

    ./symbols.py list path/to/file.py
    ./symbols.py span path/to/file.py::ClassName.method --rev HEAD
    ./symbols.py compare path/to/file.py::fn --base <sha>

ponytail: Python only; other languages fall back to file-level staleness.
Add a language parser when symbol-level support is needed.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# A symbol is substantially replaced when this share of its original lines are
# no longer present. Same threshold as file-level staleness: deletion is the
# discriminator, not total churn.
SYMBOL_DELETE_RATIO = 0.35
SYMBOL_MIN_DELETED = 5   # lower than the file threshold; symbols are smaller


@dataclass
class Span:
    name: str
    kind: str      # function | asyncfunction | class | method
    start: int     # 1-indexed, inclusive
    end: int       # 1-indexed, inclusive
    lines: list[str]


def git(repo: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return p.returncode, p.stdout


def file_at(repo: Path, rev: str, path: str) -> str | None:
    code, out = git(repo, "show", f"{rev}:{path}")
    return out if code == 0 else None


def _walk(node: ast.AST, prefix: str, src_lines: list[str], out: dict[str, Span]) -> None:
    """
    Collect qualified names. A method inside a class is `Class.method`, so an
    anchor can name it unambiguously when two classes share a method name.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qual = f"{prefix}.{child.name}" if prefix else child.name
            if isinstance(child, ast.ClassDef):
                kind = "class"
            elif prefix:
                kind = "method"
            elif isinstance(child, ast.AsyncFunctionDef):
                kind = "asyncfunction"
            else:
                kind = "function"

            start = child.lineno
            # decorators belong to the symbol: changing one changes its meaning
            if child.decorator_list:
                start = min(start, min(d.lineno for d in child.decorator_list))
            end = getattr(child, "end_lineno", None) or start

            out[qual] = Span(
                name=qual,
                kind=kind,
                start=start,
                end=end,
                lines=src_lines[start - 1 : end],
            )
            # recurse so nested classes and methods are addressable too
            _walk(child, qual, src_lines, out)


def extract(source: str) -> dict[str, Span]:
    """All addressable symbols in a Python source string, by qualified name."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    lines = source.splitlines()
    found: dict[str, Span] = {}
    _walk(tree, "", lines, found)
    return found


def resolve_span(repo: Path, path: str, symbol: str, rev: str) -> Span | None:
    src = file_at(repo, rev, path)
    if src is None:
        return None
    syms = extract(src)
    if symbol in syms:
        return syms[symbol]
    # allow a bare method name when it is unambiguous, so anchors stay writable
    tail = [s for q, s in syms.items() if q.rsplit(".", 1)[-1] == symbol]
    return tail[0] if len(tail) == 1 else None


@dataclass
class SymbolVerdict:
    status: str        # fresh | rewritten | gone | unassessable
    detail: str = ""
    deleted: int = 0
    size_then: int = 0


# A candidate rename must keep at least this much of the original body.
RENAME_SIMILARITY = 0.60

# Only a UNIQUE same-named definition elsewhere is worth reporting. Two
# candidates is a coin flip presented as evidence, which is worse than silence.
MOVE_HINT_MAX_CANDIDATES = 1


def _find_renamed(repo: Path, path: str, then: Span) -> tuple[str, float] | None:
    """
    Best same-file candidate for a symbol that vanished under its own name.

    Compares bodies, not names: if some symbol at HEAD still contains most of
    the original's lines, the code moved rather than died. Returns None when
    nothing is similar enough, so a real deletion still reads as `gone`.
    """
    src = file_at(repo, "HEAD", path)
    if src is None:
        return None
    old = [l.strip() for l in then.lines if l.strip()]
    if len(old) < 3:
        return None  # too small to identify by content

    best: tuple[str, float] | None = None
    for name, sp in extract(src).items():
        new = [l.strip() for l in sp.lines if l.strip()]
        if not new:
            continue
        sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
        kept = sum(b.size for b in sm.get_matching_blocks())
        sim = kept / len(old)
        if sim >= RENAME_SIMILARITY and (best is None or sim > best[1]):
            best = (name, sim)
    return best


def _same_name_elsewhere(repo: Path, path: str, symbol: str) -> str | None:
    """
    Any other tracked file defining `symbol` by that name at HEAD.

    One `git grep` over HEAD, no file reads and no body comparison -- so it is a
    hint about where to look, never evidence that the code is the same code. A
    genuine cross-file move with a preserved body would need a similarity pass
    over every candidate, which is far too slow for the edit-time hook and is
    deliberately not done here.
    """
    leaf = symbol.rsplit(".", 1)[-1]
    code, out = git(
        repo, "grep", "-l", "-E", f"^\\s*(async def|def|class) {leaf}\\b", "HEAD", "--", "*.py"
    )
    if code != 0 or not out.strip():
        return None
    others = []
    for line in out.splitlines():
        # `git grep ... HEAD` prefixes each path with "HEAD:"
        cand = line.split(":", 1)[-1].strip()
        if cand and cand != path:
            others.append(cand)
    # A name found all over the tree is a common name (`main`, `setUp`,
    # `delete_thread`), not a relocation. Suggesting one of 22 candidates is
    # worse than saying nothing -- it reads as evidence and is a coin flip.
    # Only a near-unique match is worth reporting.
    if not others or len(others) > MOVE_HINT_MAX_CANDIDATES:
        return None
    return others[0] if len(others) == 1 else None


def compare(repo: Path, path: str, symbol: str, base: str) -> SymbolVerdict:
    """
    How much of `symbol` as it stood at `base` survives at HEAD.

    difflib over the two line lists, not a git diff: the symbol may have moved
    within the file, and a line-range diff would score the move itself as
    churn. Matching on content ignores position, which is what "is this still
    the same code" actually means.
    """
    then = resolve_span(repo, path, symbol, base)
    if then is None:
        if file_at(repo, base, path) is None:
            return SymbolVerdict("unassessable", f"{path} absent at baseline")
        return SymbolVerdict("unassessable", f"{symbol} not found at baseline")

    now = resolve_span(repo, path, symbol, "HEAD")
    if now is None:
        if file_at(repo, "HEAD", path) is None:
            return SymbolVerdict("gone", f"{path} deleted from HEAD", len(then.lines), len(then.lines))

        # The symbol is not there under that name -- but it may have been
        # renamed, which is not staleness: the code and the decision behind it
        # both survived. Look for a symbol at HEAD whose body still matches, so
        # a rename does not read as a deletion.
        moved = _find_renamed(repo, path, then)
        if moved is not None:
            new_name, sim = moved
            return SymbolVerdict(
                "fresh",
                f"{symbol} appears renamed to {new_name} ({sim:.0%} of its body intact); "
                "treating as surviving, not deleted",
                0, len(then.lines),
            )
        # A same-named symbol elsewhere in the tree is a hint that the code
        # moved rather than died. Reported as a hint, not a verdict: this is a
        # name grep, not a body comparison, so it cannot tell a relocation from
        # an unrelated function that happens to share a name. Cheap enough for
        # the offline `check` path; the push hook shows the same text but only
        # ever reaches here for anchors it already matched.
        elsewhere = _same_name_elsewhere(repo, path, symbol)
        where = f"; possibly moved to {elsewhere} (same name, body not compared)" if elsewhere else ""
        return SymbolVerdict(
            "gone",
            f"{symbol} no longer defined in {path}{where}",
            len(then.lines),
            len(then.lines),
        )

    old = [l.strip() for l in then.lines]
    new = [l.strip() for l in now.lines]
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    kept = sum(block.size for block in sm.get_matching_blocks())
    deleted = max(len(old) - kept, 0)
    size_then = max(len(old), 1)
    ratio = deleted / size_then

    if deleted >= SYMBOL_MIN_DELETED and ratio >= SYMBOL_DELETE_RATIO:
        return SymbolVerdict(
            "rewritten",
            f"{deleted} of {size_then} lines of {symbol} replaced since baseline ({ratio:.0%})",
            deleted, size_then,
        )
    return SymbolVerdict(
        "fresh",
        f"{deleted} of {size_then} lines of {symbol} replaced since baseline ({ratio:.0%})",
        deleted, size_then,
    )


def repo_root(start: Path | None = None) -> Path:
    code, out = git(start or Path.cwd(), "rev-parse", "--show-toplevel")
    if code != 0:
        sys.exit("error: not inside a git repository")
    return Path(out.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description="Python symbol spans and survival.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("list", help="symbols in a file at a revision")
    l.add_argument("path")
    l.add_argument("--rev", default="HEAD")

    s = sub.add_parser("span", help="line span of one symbol")
    s.add_argument("anchor", help="path::Qualified.Name")
    s.add_argument("--rev", default="HEAD")

    c = sub.add_parser("compare", help="symbol survival between a base and HEAD")
    c.add_argument("anchor", help="path::Qualified.Name")
    c.add_argument("--base", required=True, help="baseline sha or date-resolvable rev")

    args = ap.parse_args()
    repo = repo_root()

    if args.cmd == "list":
        src = file_at(repo, args.rev, args.path)
        if src is None:
            sys.exit(f"error: {args.path} not present at {args.rev}")
        syms = extract(src)
        if not syms:
            print("(no symbols -- not Python, or unparseable at this revision)")
            return 0
        for q, sp in sorted(syms.items(), key=lambda kv: kv[1].start):
            print(f"{sp.start:>5}-{sp.end:<5} {sp.kind:<14} {q}")
        return 0

    path, _, symbol = args.anchor.partition("::")
    if not symbol:
        sys.exit("error: anchor must be path::Symbol")

    if args.cmd == "span":
        sp = resolve_span(repo, path, symbol, args.rev)
        if sp is None:
            sys.exit(f"error: {symbol} not found in {path} at {args.rev}")
        print(f"{path}::{sp.name}  {sp.kind}  lines {sp.start}-{sp.end}  ({len(sp.lines)} lines)")
        return 0

    v = compare(repo, path, symbol, args.base)
    print(f"{args.anchor}  ->  {v.status}")
    if v.detail:
        print(f"  {v.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
