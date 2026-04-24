from pathlib import Path

from ai_issue_worker.config import config_from_dict
from ai_issue_worker.models import CommandResult, DiffSummary, Issue, VerifyResult
from ai_issue_worker.prompt import build_issue_draft_prompt, build_prompt, build_review_fix_prompt, build_review_prompt


def test_prompt_contains_issue_and_constraints(tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo", "verify": {"commands": ["pytest"]}})
    (tmp_path / "AGENTS.md").write_text("Follow local instructions.", encoding="utf-8")
    issue = Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")
    prompt = build_prompt(issue, config, tmp_path)
    assert "Fix GitHub issue #123" in prompt
    assert "Bug title" in prompt
    assert "Bug body" in prompt
    assert "`pytest`" in prompt
    assert "Do not commit changes" in prompt
    assert "Do not create branches or pull requests" in prompt


def test_review_prompt_requests_structured_blocking_priorities(tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo", "review": {"fix_priorities": ["P0", "P1"]}})
    (tmp_path / "AGENTS.md").write_text("Follow local instructions.", encoding="utf-8")
    issue = Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")
    diff = DiffSummary(["src/app.py"], " src/app.py | 2 +-", 2, False, None)
    verify = VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 1.0)])

    prompt = build_review_prompt(issue, config, tmp_path, diff, verify)

    assert "Review the current working tree changes for GitHub issue #123" in prompt
    assert "Do not edit files" in prompt
    assert "BLOCKING_PRIORITIES: NONE" in prompt
    assert "Treat only P0, P1 as blocking" in prompt
    assert "src/app.py" in prompt
    assert "PASS pytest" in prompt
    assert "Follow local instructions." in prompt


def test_review_fix_prompt_targets_only_blocking_findings():
    issue = Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")
    diff = DiffSummary(["src/app.py"], " src/app.py | 2 +-", 2, False, None)

    prompt = build_review_fix_prompt(issue, "[P1] Broken edge case", diff, ["P1"])

    assert "Review fix task" in prompt
    assert "Bug title" in prompt
    assert "[P1] Broken edge case" in prompt
    assert "Fix only review findings with these priorities: P1" in prompt
    assert "Do not address findings with other priorities" in prompt
    assert "Do not commit changes" in prompt


def test_issue_draft_prompt_requires_json_output(tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo"})
    (tmp_path / "README.md").write_text("Parser project.", encoding="utf-8")

    prompt = build_issue_draft_prompt("parser crashes on empty input", tmp_path, config, title_hint="Parser crash on empty input")

    assert "Return only valid JSON" in prompt
    assert '"title"' in prompt
    assert '"body"' in prompt
    assert "parser crashes on empty input" in prompt
    assert "Title hint: Parser crash on empty input" in prompt
    assert "Parser project." in prompt
