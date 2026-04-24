# Local AI Issue Worker

`local-ai-issue-worker` is a local CLI that processes GitHub issues labeled for AI work. It uses `gh` for GitHub operations, `git worktree` for isolated changes, a configurable Codex CLI backend for edits, local verifier commands, and draft pull requests for human review.

## Install

```bash
python -m pip install -e .
```

For tests:

```bash
python -m pip install -e '.[test]'
pytest
```

## Quick Start

```bash
ai-issue init
```

Edit `.ai-issue-worker.yaml`, especially:

```yaml
repo: owner/repo
base_branch: main
```

List candidate issues:

```bash
ai-issue list
```

Run one local cycle:

```bash
ai-issue run-once
```

Pick a Codex model and reasoning effort for a single run:

```bash
ai-issue run-once --model gpt-5.4 --reasoning high
```

For persistent defaults, set these in `.ai-issue-worker.yaml`:

```yaml
agent:
  command: codex exec --full-auto
  model: gpt-5.4
  reasoning: high
```

Start a simple background loop:

```bash
ai-issue start
ai-issue status
ai-issue logs
ai-issue stop
```

## Safety

V1 is not sandboxed. Run it only on trusted repositories and keep draft PR review enabled. The worker does not auto-merge.
