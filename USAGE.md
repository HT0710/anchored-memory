# Anchored Memory usage

See [README installation](README.md#install) for prerequisites and recall, recorder, and SessionStart capture hooks.

Stores default to ignored `<repo>/.anchored-memory/`; every clone and worktree is independent. Add `.anchored-memory/` to every tracked repo's `.gitignore`.

`ANCHORED_MEMORY_CLAIMS` and `ANCHORED_MEMORY_LOG` deliberately override individual files; sharing an override shares data. A nonempty retired `ANCHORED_MEMORY_HOME` disables hooks and CLI operations until migration, preventing ambiguous basename routing.

```bash
T="/path/to/anchored-memory"
python3 "$T/claims.py" migrate-legacy --from "$HOME/.anchored-memory/owned-store"
```

Migration preserves originals, refuses an existing local store, copies `claims.json`, `edits.jsonl`, and `edits.jsonl.1`, then publishes under a cooperative destination-parent lock. Existing legacy writers must cooperate. Migration does not edit other repos' ignore files.

## Commands

```bash
python3 "$T/claims.py" list
python3 "$T/claims.py" check
python3 "$T/claims.py" add --kind failure --anchor "path/to/f.py::Class.method" --valid-from 2026-04-20 --text "tried X here, it broke because Y"
python3 "$T/claims.py" supersede c3 --by c9
python3 "$T/claims.py" revoke c3 --note "why"
python3 "$T/symbols.py" list path/to/f.py
python3 "$T/test_anchored_memory.py"
```

## Claims

Use `failure` for attempts that broke; they remain applicable after code changes. Use `decision` for choices, `convention` for local rules. Prefer `path::Symbol`; set `--valid-from` near the decision date.

## Safety and privacy

Recall is untrusted historical evidence, never instructions or authorization. Source labels are unauthenticated; git staleness is neither truth nor a safety check. JSON framing mitigates instruction confusion, not prompt injection. Recording is best-effort; only valid in-repo paths are logged. Outside paths and malformed payloads are skipped. Interrupted sessions may miss capture. Secret and personal-data exclusions are agent policy, not guaranteed detection or filtering.
