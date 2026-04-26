from pathlib import Path

from ai_issue_worker import cli
from ai_issue_worker.config import load_config
from ai_issue_worker.models import CommandResult, Issue


class FakeGH:
    def __init__(self, repo: str):
        self.repo = repo

    def list_issues(self, ready_label: str):
        return [Issue(1, "Ready", "", [ready_label], "open", updated_at="2026-01-01T00:00:00Z")]

    def blocked_by(self, number: int):
        return []


def test_cli_init_smoke(tmp_path: Path):
    path = tmp_path / "config.yaml"
    assert cli.main(["init", "--path", str(path), "--no-create-labels"]) == 0
    assert path.exists()


def test_cli_init_infers_repo_and_branch_and_creates_labels(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    captured = {}

    def fake_run_cmd(args):
        if args == ["git", "remote", "get-url", "origin"]:
            return CommandResult("git remote get-url origin", 0, "git@github.com:acme/widget.git\n", "", 0.0)
        if args == ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]:
            return CommandResult("git symbolic-ref --short refs/remotes/origin/HEAD", 0, "origin/trunk\n", "", 0.0)
        raise AssertionError(f"unexpected command: {args}")

    class FakeInitGH:
        def __init__(self, repo: str):
            captured["repo"] = repo

        def ensure_labels(self, labels):
            captured["labels"] = labels

    monkeypatch.setattr(cli, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(cli, "GHClient", FakeInitGH)

    assert cli.main(["init", "--path", str(path)]) == 0

    config = load_config(path)
    assert config.repo == "acme/widget"
    assert config.base_branch == "trunk"
    assert captured["repo"] == "acme/widget"
    assert set(captured["labels"]) >= {"ai-ready", "ai-working", "ai-failed", "ai-pr-opened", "blocked", "needs-human"}


def test_repo_from_remote_url_supports_github_and_enterprise_remotes():
    assert cli._repo_from_remote_url("https://github.com/owner/repo.git") == "owner/repo"
    assert cli._repo_from_remote_url("git@github.com:owner/repo.git") == "owner/repo"
    assert cli._repo_from_remote_url("ssh://git@github.example.com/owner/repo.git") == "github.example.com/owner/repo"


def test_cli_list_smoke_with_fake_gh(tmp_path: Path, monkeypatch, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    monkeypatch.setattr(cli, "GHClient", FakeGH)
    assert cli.main(["list", "--config", str(path)]) == 0
    assert "#1" in capsys.readouterr().out


def test_cli_list_defaults_to_dotfile_config(tmp_path: Path, monkeypatch, capsys):
    path = tmp_path / ".ai-issue-worker.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "GHClient", FakeGH)
    assert cli.main(["list"]) == 0
    assert "#1" in capsys.readouterr().out


def test_run_once_passes_model_and_reasoning_overrides(tmp_path: Path, monkeypatch):
    path = tmp_path / ".ai-issue-worker.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    def fake_run_once(config_path, repo_root=None, overrides=None):
        captured["config_path"] = config_path
        captured["repo_root"] = repo_root
        captured["model"] = overrides.model
        captured["reasoning"] = overrides.reasoning
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "run_once", fake_run_once)
    assert cli.main(["run-once", "--model", "gpt-5.4", "--reasoning", "xhigh"]) == 0
    assert captured["config_path"] == Path(".ai-issue-worker.yaml")
    assert captured["model"] == "gpt-5.4"
    assert captured["reasoning"] == "xhigh"


def test_cli_create_issue_uses_ready_label_and_generated_body(tmp_path: Path, monkeypatch, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeCreateGH:
        def __init__(self, repo: str):
            self.repo = repo

        def create_issue(self, title: str, body_file: Path, labels: list[str]):
            captured["repo"] = self.repo
            captured["title"] = title
            captured["body"] = body_file.read_text(encoding="utf-8")
            captured["labels"] = labels
            return "https://github.com/owner/repo/issues/123"

    monkeypatch.setattr(
        cli,
        "_generate_issue_draft",
        lambda config, repo_root, description, title_hint, draft_dir: (
            "Generated parser failure title",
            "## Summary\n\nGenerated body.\n",
        ),
    )
    monkeypatch.setattr(cli, "GHClient", FakeCreateGH)
    result = cli.main(["create", "--config", str(path), "--no-edit", "Parser fails on empty input"])

    assert result == 0
    assert captured["repo"] == "owner/repo"
    assert captured["title"] == "Generated parser failure title"
    assert captured["labels"] == ["ai-ready"]
    assert captured["body"] == "## Summary\n\nGenerated body.\n"
    assert "created issue: https://github.com/owner/repo/issues/123" in capsys.readouterr().out


def test_cli_create_issue_derives_title_and_runs_editor(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeCreateGH:
        def __init__(self, repo: str):
            self.repo = repo

        def create_issue(self, title: str, body_file: Path, labels: list[str]):
            captured["title"] = title
            captured["body"] = body_file.read_text(encoding="utf-8")
            captured["labels"] = labels
            return "https://github.com/owner/repo/issues/124"

    monkeypatch.setattr(
        cli,
        "_generate_issue_draft",
        lambda config, repo_root, description, title_hint, draft_dir: (
            "Initial generated title",
            "## Summary\n\nInitial generated body.\n",
        ),
    )

    def fake_editor(body_file: Path, editor: str | None = None):
        body_file.write_text(
            "Title: Edited title\n\n## Summary\n\nEdited in editor.\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(cli, "GHClient", FakeCreateGH)
    monkeypatch.setattr(cli, "_run_editor", fake_editor)

    result = cli.main(["create", "--config", str(path), "Fix parser crash", "when input is empty"])

    assert result == 0
    assert captured["title"] == "Edited title"
    assert captured["labels"] == ["ai-ready"]
    assert captured["body"] == "## Summary\n\nEdited in editor.\n"


def test_parse_issue_draft_file_requires_title_line():
    try:
        cli._parse_issue_draft_file("## Summary\n\nBody")
    except cli.ConfigError as exc:
        assert "Title:" in str(exc)
    else:
        raise AssertionError("expected ConfigError")
