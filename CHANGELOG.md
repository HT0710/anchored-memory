# Changelog

Notable changes to Anchored Memory. Versions follow [Semantic Versioning](https://semver.org/); before 1.0, any release may change the claim store or the hook setup.

## [0.1.0] - 2026-09-15

First tagged release. **Experimental:** general usefulness and token savings remain unproven; see Limits in the README.

### Added
- Claim store: `add`, `list`, `check`, `supersede`, `revoke`. Claims anchor to a file or a Python symbol.
- Git-based staleness check. `failure` claims stay unflagged when their code changes or is deleted.
- Hooks: SessionStart capture policy, PreToolUse recall on edit tools, PostToolUse edit recorder.
- `add` accepts `--anchor` more than once, writing one claim per file with the same text.
- The recorder also logs edits made through shell commands, found by comparing the worktree with a snapshot after each command.

### Fixed
- `./f.py` anchors were stored as typed and never recalled.
- Definitions inside `if`, `try`, `with` or `for` blocks were invisible to symbol extraction.
- One tracked file that is not UTF-8 crashed `check` and emptied recall.
- A last line without a trailing newline was not counted.
- Rename hints named the enclosing class instead of the renamed method.
- A claim dated before the repository's first commit reported "path absent"; it now says the claim predates the history and how to re-date it.

### Removed
- `migrate-legacy` and the retired `ANCHORED_MEMORY_HOME` variable, which is now ignored.

### Upgrading from an untagged checkout
- Add `Bash` to the recorder matcher: `Edit|Write|MultiEdit|NotebookEdit|Bash`.
- Remove any `ANCHORED_MEMORY_HOME` setting; it no longer does anything.
