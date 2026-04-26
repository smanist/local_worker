# Operations

## Core Commands

Bootstrap config and labels:

```bash
ai-issue init
```

Inspect candidate issues:

```bash
ai-issue list
```

Run one local execution cycle:

```bash
ai-issue run-once
```

Create a new `ai-ready` issue from rough notes:

```bash
ai-issue create --title "Fix parser crash" "Parser crashes when input is empty."
```

Start or stop the background loop:

```bash
ai-issue start
ai-issue status
ai-issue logs
ai-issue stop
```

Inspect local worker state:

```bash
ai-issue inspect
```

Retry a failed issue:

```bash
ai-issue retry <issue-number>
```

Clean old run directories and worktrees:

```bash
ai-issue clean --older-than 7d
```

## Config Fields That Matter Most

The highest-leverage config sections in `.ai-issue-worker.yaml` are:

- `issue_selection`: labels, dependency behavior, stacked PR support, and ordering.
- `agent`: Codex command, model, reasoning, timeout, and repair attempts.
- `review`: whether review is enabled, the read-only review command, and blocking priorities.
- `verify`: commands run inside the worktree after implementation or review fixes.
- `diff_policy`: file-count, diff-size, lockfile, and rejected-path guardrails.
- `git`: branch prefix, cleanup behavior, dirty-base tolerance, and commit-message template.
- `pr`: draft mode and PR title/body templates.

## Where To Look During Debugging

For a specific issue run:

1. Open `.ai-runs/issue-<n>/latest.json` for overall status.
2. Read `.ai-runs/issue-<n>/artifacts.log` for the artifact timeline.
3. Read `.ai-runs/issue-<n>/prompt.md` to see the latest prompt the worker sent.
4. Read `.ai-runs/issue-<n>/codex.log`, `verify.log`, `review.md`, and `pr_body.md` depending on the failure stage.

For daemon state:

1. Read `.ai-runtime/worker.status.json`.
2. Read `.ai-logs/worker.log`.
3. Check `.ai-runtime/worker.lock` and `.ai-runtime/worker.pid`.

## Common Failure Classes

- `EXIT_CONFIG`: invalid or missing config.
- `EXIT_DEPENDENCY`: `gh`, `git`, or Codex command is unavailable.
- `EXIT_GH`: GitHub auth or API failure.
- `EXIT_GIT`: dirty base checkout, fetch/worktree failure, commit/push failure.
- `EXIT_AGENT`: implementation, repair, or review Codex session failed.
- `EXIT_VERIFY`: verifier failed, diff policy rejected changes, or no useful diff was produced.
- `EXIT_PR`: draft PR creation failed.
- `EXIT_LOCK`: another worker instance already holds the runtime lock.

## Behavioral Notes

- Review is a second-pass gate, not a formatter. It should report findings without editing files.
- Stacked PRs are only considered when dependency checking is enabled, there is exactly one open blocker, and that blocker already has a recorded `pr_opened` job.
- `clean --delete-local-branches` deletes local branches with `git branch -D`; use it deliberately.
- `keep_worktree_on_failure` and `keep_worktree_on_success` change how much state remains available for inspection after runs.

## Test Workflow

Fast validation:

```bash
pytest
```

Full verifier-style validation when local tools are installed:

```bash
ruff check .
ruff format --check .
pyright
pytest
```
