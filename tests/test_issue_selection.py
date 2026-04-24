from ai_issue_worker.config import IssueSelectionConfig
from ai_issue_worker.issue_selection import candidate_issues
from ai_issue_worker.models import Issue


def test_issue_selection_filters_excluded_labels():
    config = IssueSelectionConfig()
    issues = [
        Issue(1, "ready", "", ["ai-ready"], "open", updated_at="2026-01-01T00:00:00Z"),
        Issue(2, "working", "", ["ai-ready", "ai-working"], "open", updated_at="2026-01-02T00:00:00Z"),
        Issue(3, "blocked", "", ["ai-ready", "blocked"], "open", updated_at="2026-01-03T00:00:00Z"),
    ]
    assert [issue.number for issue in candidate_issues(issues, config)] == [1]

