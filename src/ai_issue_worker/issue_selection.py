from __future__ import annotations

from .config import IssueSelectionConfig
from .models import Issue


def excluded_labels(config: IssueSelectionConfig) -> set[str]:
    return {
        config.working_label,
        config.failed_label,
        config.pr_opened_label,
        *config.blocked_labels,
    }


def candidate_issues(issues: list[Issue], config: IssueSelectionConfig) -> list[Issue]:
    excluded = excluded_labels(config)
    candidates = [
        issue
        for issue in issues
        if config.ready_label in issue.labels and not (set(issue.labels) & excluded) and issue.state.lower() == "open"
    ]
    if config.selection_order == "oldest_updated":
        candidates.sort(key=lambda issue: issue.updated_at or "")
    elif config.selection_order == "newest_updated":
        candidates.sort(key=lambda issue: issue.updated_at or "", reverse=True)
    else:
        candidates.sort(key=lambda issue: issue.number)
    return candidates


def select_one_issue(issues: list[Issue], config: IssueSelectionConfig) -> Issue | None:
    candidates = candidate_issues(issues, config)
    return candidates[0] if candidates else None

