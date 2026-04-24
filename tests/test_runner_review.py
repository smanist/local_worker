import subprocess
from pathlib import Path

from ai_issue_worker.config import config_from_dict
from ai_issue_worker.models import AgentResult, CommandResult, DiffSummary, Issue, VerifyResult
from ai_issue_worker.runner import _diff_snapshot, _run_agent_and_verify, blocking_review_priorities, select_workable_issue


def _issue() -> Issue:
    return Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")


def _diff() -> DiffSummary:
    return DiffSummary(["src/app.py"], " src/app.py | 2 +-", 2, False, None)


class FakeDependencyGH:
    def __init__(self, blockers_by_issue: dict[int, list[Issue]]):
        self.blockers_by_issue = blockers_by_issue
        self.checked: list[int] = []

    def blocked_by(self, number: int) -> list[Issue]:
        self.checked.append(number)
        return self.blockers_by_issue.get(number, [])


def test_blocking_review_priorities_prefers_structured_line():
    review = "BLOCKING_PRIORITIES: P1\n\n[P1] Bad edge case\n[P2] Cleanup"

    assert blocking_review_priorities(review, ["P0", "P1"]) == ["P1"]


def test_blocking_review_priorities_fallback_ignores_plain_no_p0_p1_text():
    review = "No P0/P1 findings found.\n\n[P2] Optional cleanup"

    assert blocking_review_priorities(review, ["P0", "P1"]) == []


def test_select_workable_issue_skips_candidates_with_open_blockers():
    config = config_from_dict({"repo": "owner/repo"}).issue_selection
    issues = [
        Issue(1, "Blocked", "", ["ai-ready"], "open", updated_at="2026-01-01T00:00:00Z"),
        Issue(2, "Ready", "", ["ai-ready"], "open", updated_at="2026-01-02T00:00:00Z"),
    ]
    gh = FakeDependencyGH({1: [Issue(10, "Blocker", "", [], "open")]})

    issue = select_workable_issue(gh, issues, config)

    assert issue and issue.number == 2
    assert gh.checked == [1, 2]


def test_select_workable_issue_allows_candidates_with_closed_blockers():
    config = config_from_dict({"repo": "owner/repo"}).issue_selection
    issues = [Issue(1, "Unblocked", "", ["ai-ready"], "open", updated_at="2026-01-01T00:00:00Z")]
    gh = FakeDependencyGH({1: [Issue(10, "Done blocker", "", [], "closed")]})

    issue = select_workable_issue(gh, issues, config)

    assert issue and issue.number == 1


def test_select_workable_issue_can_ignore_dependency_checks():
    config = config_from_dict(
        {"repo": "owner/repo", "issue_selection": {"respect_issue_dependencies": False}}
    ).issue_selection
    issues = [Issue(1, "Blocked but selected", "", ["ai-ready"], "open", updated_at="2026-01-01T00:00:00Z")]
    gh = FakeDependencyGH({1: [Issue(10, "Blocker", "", [], "open")]})

    issue = select_workable_issue(gh, issues, config)

    assert issue and issue.number == 1
    assert gh.checked == []


def test_run_agent_review_fix_loop_until_clean(monkeypatch, tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo", "verify": {"commands": ["pytest"]}})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls: list[str] = []
    commands: list[str | None] = []
    outputs = iter(
        [
            "implementation complete",
            "BLOCKING_PRIORITIES: P1\n\n[P1] Broken edge case",
            "fixed blocking review findings",
            "BLOCKING_PRIORITIES: NONE\n\nNo findings.",
        ]
    )

    def fake_codex(config, worktree_path, prompt_path, log_path, command=None):
        calls.append(prompt_path.read_text(encoding="utf-8").splitlines()[0])
        commands.append(command)
        log_path.write_text("log", encoding="utf-8")
        return AgentResult(True, 0, next(outputs), "", 0.1, False)

    verify_count = 0

    def fake_verifier(config, worktree_path, log_path):
        nonlocal verify_count
        verify_count += 1
        log_path.write_text("PASS pytest", encoding="utf-8")
        return VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 0.1)])

    monkeypatch.setattr("ai_issue_worker.runner._run_codex_session", fake_codex)
    monkeypatch.setattr("ai_issue_worker.runner._diff_snapshot", lambda worktree_path: "diff")
    monkeypatch.setattr("ai_issue_worker.runner.run_verifier", fake_verifier)
    monkeypatch.setattr("ai_issue_worker.runner.inspect_diff", lambda worktree_path, config: _diff())

    ok, verify, diff, error, failure_kind = _run_agent_and_verify(
        config,
        _issue(),
        tmp_path,
        tmp_path,
        run_dir,
        "20260424",
    )

    assert ok is True
    assert verify and verify.passed is True
    assert diff.changed_files == ["src/app.py"]
    assert error == ""
    assert failure_kind == ""
    assert calls == ["# Task", "# Code review task", "# Review fix task", "# Code review task"]
    assert commands == [None, config.review.command, None, config.review.command]
    assert verify_count == 2


def test_run_agent_review_loop_stops_after_max_fix_iterations(monkeypatch, tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo", "review": {"max_iterations": 1}})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outputs = iter(
        [
            "implementation complete",
            "BLOCKING_PRIORITIES: P1\n\n[P1] Broken edge case",
            "fixed blocking review findings",
            "BLOCKING_PRIORITIES: P1\n\n[P1] Still broken",
        ]
    )

    def fake_codex(config, worktree_path, prompt_path, log_path, command=None):
        log_path.write_text("log", encoding="utf-8")
        return AgentResult(True, 0, next(outputs), "", 0.1, False)

    def fake_verifier(config, worktree_path, log_path):
        log_path.write_text("PASS pytest", encoding="utf-8")
        return VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 0.1)])

    monkeypatch.setattr("ai_issue_worker.runner._run_codex_session", fake_codex)
    monkeypatch.setattr("ai_issue_worker.runner._diff_snapshot", lambda worktree_path: "diff")
    monkeypatch.setattr("ai_issue_worker.runner.run_verifier", fake_verifier)
    monkeypatch.setattr("ai_issue_worker.runner.inspect_diff", lambda worktree_path, config: _diff())

    ok, verify, diff, error, failure_kind = _run_agent_and_verify(
        config,
        _issue(),
        tmp_path,
        tmp_path,
        run_dir,
        "20260424",
    )

    assert ok is False
    assert verify and verify.passed is True
    assert diff.changed_files == ["src/app.py"]
    assert "1 fix iteration" in error
    assert "[P1] Still broken" in error
    assert failure_kind == "review"


def test_run_agent_fails_when_review_session_modifies_worktree(monkeypatch, tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo"})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outputs = iter(["implementation complete", "BLOCKING_PRIORITIES: NONE\n\nNo findings."])
    snapshots = iter(["before-review", "after-review"])

    def fake_codex(config, worktree_path, prompt_path, log_path, command=None):
        log_path.write_text("log", encoding="utf-8")
        return AgentResult(True, 0, next(outputs), "", 0.1, False)

    def fake_verifier(config, worktree_path, log_path):
        log_path.write_text("PASS pytest", encoding="utf-8")
        return VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 0.1)])

    monkeypatch.setattr("ai_issue_worker.runner._run_codex_session", fake_codex)
    monkeypatch.setattr("ai_issue_worker.runner._diff_snapshot", lambda worktree_path: next(snapshots))
    monkeypatch.setattr("ai_issue_worker.runner.run_verifier", fake_verifier)
    monkeypatch.setattr("ai_issue_worker.runner.inspect_diff", lambda worktree_path, config: _diff())

    ok, verify, diff, error, failure_kind = _run_agent_and_verify(
        config,
        _issue(),
        tmp_path,
        tmp_path,
        run_dir,
        "20260424",
    )

    assert ok is False
    assert verify and verify.passed is True
    assert diff.changed_files == ["src/app.py"]
    assert "modified the worktree" in error
    assert failure_kind == "agent"


def test_diff_snapshot_changes_when_untracked_file_content_changes(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    untracked = tmp_path / "new.txt"
    untracked.write_text("before\n", encoding="utf-8")
    before = _diff_snapshot(tmp_path)
    untracked.write_text("after\n", encoding="utf-8")
    after = _diff_snapshot(tmp_path)

    assert before != after


def test_diff_snapshot_changes_when_ignored_file_content_changes(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".env\nignored/\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    ignored = tmp_path / ".env"
    ignored.write_text("before\n", encoding="utf-8")
    before = _diff_snapshot(tmp_path)
    ignored.write_text("after\n", encoding="utf-8")
    after = _diff_snapshot(tmp_path)

    assert before != after
