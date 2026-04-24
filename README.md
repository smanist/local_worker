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
ai-issue list --config .ai-issue-worker.yaml
```

Run one local cycle:

```bash
ai-issue run-once --config .ai-issue-worker.yaml
```

Start a simple background loop:

```bash
ai-issue start --config .ai-issue-worker.yaml
ai-issue status --config .ai-issue-worker.yaml
ai-issue logs --config .ai-issue-worker.yaml
ai-issue stop --config .ai-issue-worker.yaml
```

## Safety

V1 is not sandboxed. Run it only on trusted repositories and keep draft PR review enabled. The worker does not auto-merge.

