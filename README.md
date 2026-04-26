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

`init` writes `.ai-issue-worker.yaml`, inferring the GitHub repo from
`origin` and the base branch from `origin/HEAD` or the current branch when
possible:

```yaml
repo: owner/repo
base_branch: main
```

It also creates or updates the GitHub labels used by the automation, including
`ai-ready`, `ai-working`, `ai-failed`, and `ai-pr-opened`, when `gh` is
authenticated for the inferred repo. Use `--repo`, `--base-branch`, or
`--no-create-labels` to override those defaults. It appends the local artifact
directories `.ai-logs`, `.ai-runs`, `.ai-runtime`, and `.ai-worktrees` to
`.gitignore`.

List candidate issues:

```bash
ai-issue list
```

By default, candidates exclude issues that have open native GitHub issue
dependencies in their `blocked by` relationship, in addition to excluding
configured blocked labels such as `blocked` and `needs-human`.

To let the worker continue through a dependency chain, enable stacked PRs. In
this mode, an issue with exactly one open blocker can be selected after that
blocker has an ai-issue PR open; the downstream worktree is based on the
blocker's branch and its PR targets that branch:

```yaml
issue_selection:
  allow_stacked_prs: true
  max_stack_depth: 3
```

To use label-only selection, disable the dependency check:

```yaml
issue_selection:
  respect_issue_dependencies: false
```

Create a new AI-ready issue from rough local notes. The command sends your notes
through the configured Codex agent to draft a formal title and Markdown body,
opens that draft in your editor, then creates the GitHub issue with the
configured ready label:

```bash
ai-issue create --title "Fix parser crash" "Parser crashes when input is empty."
```

It uses the same `agent.command`, `agent.model`, and `agent.reasoning` settings
as the worker. Use `--description-file path/to/issue.txt` for longer notes, or
`--no-edit` for non-interactive scripts.

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

review:
  enabled: true
  command: codex exec --sandbox read-only
  max_iterations: 3
  fix_priorities: [P0, P1]
```

When review is enabled, the worker runs a separate Codex code-review session after
the initial implementation and verifier pass. The review command defaults to a
read-only Codex sandbox. If that review reports configured blocking priorities,
the worker runs a separate Codex fix session, verifies again, and repeats until
the review is clean or `review.max_iterations` fix passes have been used.

Each issue run directory also contains `artifacts.log`, a timestamped manifest of
generated run artifacts such as prompts, Codex logs, verifier logs, review files,
job records, PR bodies, and latest-file updates. After each Codex session, the
manifest records token usage when the configured Codex command exposes it in
stdout/stderr, plus a cumulative total across Codex logs in that issue directory.

Start a simple background loop:

```bash
ai-issue start
ai-issue status
ai-issue logs
ai-issue stop
```

## Safety

V1 is not sandboxed. Run it only on trusted repositories and keep draft PR review enabled. The worker does not auto-merge.

GitHub issue comments, issue bodies, and PR bodies are scrubbed before upload to
mask local user-home paths such as `/Users/name/...`.
