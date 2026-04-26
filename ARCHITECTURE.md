# Architecture

## System Overview

`local-ai-issue-worker` is a single-process Python CLI that automates a conservative issue-to-draft-PR workflow around three external tools:

- `gh` for GitHub issue, label, comment, and PR operations.
- `git` plus `git worktree` for isolated branches.
- a Codex CLI command for implementation, review, and issue drafting.

The codebase is structured as a thin set of adapters around one orchestrator. There is no service layer, database, or background queue beyond local files.

## End-to-End Flow

`ai-issue run-once` calls `runner.run_once()`, which performs these steps:

1. Load and validate config.
2. Resolve worker paths relative to the repo root.
3. Acquire `.ai-runtime/worker.lock` to prevent concurrent runs.
4. Check external dependencies with `gh --version`, `git --version`, the configured agent command, optional review command, GitHub auth, and git-base cleanliness.
5. List `ai-ready` issues and filter them through label and dependency rules.
6. Re-fetch the selected issue for fresh title/body content.
7. Create a worktree and unique branch.
8. Run the implementation Codex session from a generated prompt.
9. Run verifier commands and optional repair attempts.
10. Run a read-only review Codex session; if configured blocking priorities are found, run a fix session and verify again.
11. Inspect the final diff against diff-policy limits.
12. Commit, push, create a draft PR, label the issue, comment success, and optionally remove the worktree.

`ai-issue start` runs the same `run_once()` loop inside `daemon.daemon_loop()`. The daemon itself is intentionally simple: PID file, status file, and a sleep loop.

## Main Control Surfaces

### `cli.py`

- Parses subcommands.
- Handles config bootstrapping in `init`.
- Exposes manual/operator commands such as `inspect`, `retry`, `resume`, and `clean`.
- Starts or stops the daemon.
- Delegates all issue execution to `runner.run_once()`.

### `runner.py`

This is the repo's center of gravity.

- Defines worker exit codes.
- Applies model/reasoning overrides.
- Computes workable issue plans, including stacked PR base-branch selection.
- Builds prompts and invokes Codex sessions.
- Runs verifier and review/fix loops.
- Applies diff policy.
- Writes job records and artifact logs.
- Finalizes GitHub labels/comments and git cleanup.
- Supports explicit continuation of an existing ai-issue PR by reusing the recorded branch/worktree and updating the existing PR instead of opening a new one.
- Supports queued continuation work through the `ai-resume` label so the daemon and `run-once` path can process PR revisions alongside new issues.

When making behavioral changes, start here and verify the corresponding tests in `tests/test_runner_review.py`.

## Supporting Modules

### Config and Data

- `config.py`: dataclass config schema, defaults, YAML loading, and validation.
- `models.py`: plain data containers for issues, job records, verifier output, and diff summaries.

### Tool Adapters

- `github_gh.py`: wraps `gh` invocations and normalizes issue data.
- `worktree.py`: wraps git checks, worktree operations, commit, push, and branch naming.
- `codex_backend.py`: converts configured Codex command strings into runnable argv and captures logs.
- `verifier.py`: runs configured verification commands and formats summaries.
- `shell.py`: shared subprocess wrapper with timeout and `FileNotFoundError` handling.

### Policy/Support

- `issue_selection.py`: label-based selection ordering and exclusions.
- `diff_policy.py`: file-count, diff-size, path, lockfile, and diff-check enforcement.
- `prompt.py`: implementation, repair, review, review-fix, and issue-draft prompt builders.
- `privacy.py`: scrubs local home-directory paths before text leaves the machine.
- `jobs.py`: timestamped artifacts, `latest.*` copies, job record persistence, and token-usage summaries.
- `locking.py`: non-blocking file lock used by the worker loop.
- `daemon.py`: background loop and status-file updates.
- `token_usage.py`: best-effort extraction and accumulation of token counts from Codex logs.

## State and Artifacts

The filesystem is the operational state store:

- `.ai-worktrees/issue-<n>/`: checked-out worktree for a run.
- `.ai-runs/issue-<n>/`: run history and latest artifacts.
- `.ai-logs/worker.log`: daemon stdout/stderr.
- `.ai-runtime/worker.lock`: non-blocking lock file.
- `.ai-runtime/worker.pid`: daemon PID.
- `.ai-runtime/worker.status.json`: daemon status snapshot.

Per-issue run directories contain both timestamped files and latest aliases. Common artifacts:

- `run-<stamp>.json` and `latest.json`
- `prompt-<stamp>.md` and `prompt.md`
- `codex-<stamp>.log` and `codex.log`
- `verify-<stamp>.log` and `verify.log`
- `review-<stamp>.md` and `review.md`
- `pr-body-<stamp>.md` and `pr_body.md`
- `artifacts.log`

The run directory is important because stacked PR selection reads the latest job record for blocker issues.

## Review and Repair Semantics

The worker has two distinct Codex roles:

- Implementation sessions are allowed to edit files.
- Review sessions must be read-only and are validated by comparing `_diff_snapshot()` before and after the review run.

If the review output reports configured blocking priorities, the worker runs a separate fix session, then re-runs verification and review. The loop stops when:

- review is clean,
- verification fails,
- a Codex session fails, or
- `review.max_iterations` is exhausted.

## Safety Boundaries

- No auto-merge.
- No automatic commit/push from inner prompts.
- Diff policy rejects oversized or risky changes after the agent/review loops finish.
- GitHub outbound text is sanitized.
- Base checkout cleanliness is enforced before worktree creation unless explicitly relaxed.

## Extension Points

The current design is intentionally adapter-friendly:

- Agent backend: `agent.py` protocol plus `codex_backend.py`.
- Verification strategy: `verify.commands` and `verifier.py`.
- Selection rules: `issue_selection.py` plus dependency logic in `runner.py`.
- PR formatting: `pr.py` and config templates.
- Runtime docs surfaced to agents: `prompt.repository_instructions()`.

If you add a new subsystem, keep it narrow and preserve the current pattern: small adapters, explicit artifacts, and orchestration concentrated in `runner.py`.
