from pathlib import Path

from ai_issue_worker.config import config_from_dict
from ai_issue_worker.models import Issue
from ai_issue_worker.prompt import build_prompt


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

