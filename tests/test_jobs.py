from pathlib import Path

from ai_issue_worker.jobs import load_job_record, write_job_record
from ai_issue_worker.models import JobRecord


def test_job_record_written_and_loaded(tmp_path: Path):
    record = JobRecord(
        issue_number=123,
        issue_title="Title",
        branch_name="ai/issue-123-title",
        worktree_path="/tmp/worktree",
        status="selected",
        started_at="2026-04-23T00:00:00Z",
        finished_at=None,
        pr_url=None,
        error_summary=None,
        changed_files=[],
        verifier_passed=None,
    )
    path = write_job_record(tmp_path, record, timestamp="20260423-000000")
    loaded = load_job_record(path)
    latest = load_job_record(tmp_path / "latest.json")
    assert loaded == record
    assert latest == record

