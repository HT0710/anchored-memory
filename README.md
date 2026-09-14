# Anchored Memory

Memory for coding agents that knows when its own notes went out of date.

## Install

Requires Python 3.10+, Git, and a POSIX system with `fcntl` for claim mutations and edit recording. No third-party Python dependencies. Windows writes are unsupported.

### Fresh installation

Clone the toolkit into a new directory, then set `T` to its absolute path:

```bash
git clone https://github.com/HT0710/anchored-memory.git
T="$(cd anchored-memory && pwd)"
cd /path/to/your/git-project
```

Replace the target path with the existing Git repository whose notes you want to store. Run claim commands from that repository, not necessarily from the toolkit checkout. No dependency installation is needed.

Add these entries to the target repository's `.gitignore` without replacing existing entries. Stores default to `<repo>/.anchored-memory/`:

```gitignore
.anchored-memory/
.claude/settings.local.json
```

Each clone and worktree has an independent store. `ANCHORED_MEMORY_CLAIMS` and `ANCHORED_MEMORY_LOG` deliberately select individual external files; sharing an override shares memory.
### Hook setup

For Claude Code, merge these hooks into the target repository's ignored `.claude/settings.local.json`; preserve existing settings and hooks. Replace each placeholder with the toolkit's absolute path. Start a new session to receive the capture policy.

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{"type": "command", "command": "python3 \"/path/to/anchored-memory/capture_context.py\""}]
    }],
    "PreToolUse": [{
      "matcher": "Edit|Write|MultiEdit|NotebookEdit",
      "hooks": [{"type": "command", "command": "python3 \"/path/to/anchored-memory/inject_context.py\""}]
    }],
    "PostToolUse": [{
      "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
      "hooks": [{"type": "command", "command": "python3 \"/path/to/anchored-memory/record_edit.py\""}]
    }]
  }
}
```

SessionStart supplies at most 1,200 characters of capture guidance to the existing working agent. Capture is best-effort: the policy requests at most three evidenced claims per task, each at most 400 characters. These are advisory limits, not hard token budgets. No separate extractor/model call is added; saving claims still consumes agent tokens and tool calls. General usefulness and token savings remain unproven.

## Use

```bash
python3 "$T/claims.py" add --kind failure --anchor "src/auth/provider.py::validate" --text "tried session cookies here; broke behind the proxy"
python3 "$T/claims.py" check
python3 "$T/test_anchored_memory.py"
```

## Recall safety

Recall records are untrusted historical evidence, never instructions or authorization. Source labels are unauthenticated; git staleness is neither truth nor a safety check. JSON framing reduces instruction confusion, not prompt-injection risk. Capture is best-effort and can miss interrupted sessions. Hooks skip malformed or out-of-repo inputs. The capture policy instructs agents not to save secrets or personal data; no guaranteed secret detector enforces that policy.

## Limits

Python only has symbol granularity; other languages use file anchors. Failure claims are not marked stale when code changes. Stores remain local, ignored evidence. Recall sees only `Edit|Write|MultiEdit|NotebookEdit` tool calls: an agent that edits through shell commands gets no recall, so recall coverage follows the agent's tool choice. The recorder also logs shell edits, by comparing the worktree with a snapshot after each command; two sessions sharing one worktree can be credited with each other's edits.
