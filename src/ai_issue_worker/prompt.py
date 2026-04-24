from __future__ import annotations

from pathlib import Path

from .config import WorkerConfig
from .models import DiffSummary, Issue


MAX_INSTRUCTION_CHARS = 12_000


def repository_instructions(repo_root: Path) -> str:
    parts: list[str] = []
    for name in ["AGENTS.md", "CONTRIBUTING.md", "README.md", "pyproject.toml"]:
        path = repo_root / name
        if path.exists() and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if name == "AGENTS.md":
                snippet = text
            else:
                snippet = text[:3000]
            parts.append(f"## {name}\n\n{snippet.strip()}")
    joined = "\n\n".join(parts).strip()
    return joined[:MAX_INSTRUCTION_CHARS] if joined else "No repository instruction files were found."


def build_prompt(issue: Issue, config: WorkerConfig, repo_root: Path) -> str:
    verify_commands = "\n".join(f"- `{command}`" for command in config.verify.commands)
    return f"""# Task

You are working in a local git worktree for repository `{config.repo}`.

Fix GitHub issue #{issue.number}.

## Issue title

{issue.title}

## Issue body

{issue.body}

## Constraints

- Make the minimal code change needed to resolve the issue.
- Do not modify unrelated files.
- Do not change public APIs unless necessary.
- Add or update tests when appropriate.
- Prefer simple, maintainable changes.
- Do not commit changes. The outer worker will commit.
- Do not create branches or pull requests. The outer worker will do that.
- Stop after editing files. The outer worker will run verification.

## Repository verification commands

The outer worker will run:

{verify_commands}

## Repository instructions

{repository_instructions(repo_root)}

## Expected deliverable

A modified working tree that addresses the issue.
"""


def build_repair_prompt(issue: Issue, verify_logs: str, diff_summary: DiffSummary) -> str:
    return f"""# Repair task

Your previous attempt to fix GitHub issue #{issue.number} did not pass verification.

## Failed verification logs

{verify_logs[-12000:]}

## Current git diff summary

Changed files:
{chr(10).join(diff_summary.changed_files)}

Diff stat:
{diff_summary.diff_stat}

## Instructions

Fix the verification failures with minimal changes.
Do not revert unrelated useful changes unless needed.
Do not commit changes.
"""

