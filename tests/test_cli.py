from pathlib import Path

from ai_issue_worker import cli
from ai_issue_worker.models import Issue


class FakeGH:
    def __init__(self, repo: str):
        self.repo = repo

    def list_issues(self, ready_label: str):
        return [Issue(1, "Ready", "", [ready_label], "open", updated_at="2026-01-01T00:00:00Z")]


def test_cli_init_smoke(tmp_path: Path):
    path = tmp_path / "config.yaml"
    assert cli.main(["init", "--path", str(path)]) == 0
    assert path.exists()


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

    monkeypatch.setattr(cli, "GHClient", FakeCreateGH)
    result = cli.main(["create", "--config", str(path), "--title", "Broken parser", "--no-edit", "Parser fails on empty input"])

    assert result == 0
    assert captured["repo"] == "owner/repo"
    assert captured["title"] == "Broken parser"
    assert captured["labels"] == ["ai-ready"]
    assert "## Summary" in captured["body"]
    assert "Parser fails on empty input" in captured["body"]
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

    def fake_editor(body_file: Path, editor: str | None = None):
        existing = body_file.read_text(encoding="utf-8")
        body_file.write_text(f"{existing}\nEdited in editor.\n", encoding="utf-8")

    monkeypatch.setattr(cli, "GHClient", FakeCreateGH)
    monkeypatch.setattr(cli, "_run_editor", fake_editor)

    result = cli.main(["create", "--config", str(path), "Fix parser crash", "when input is empty"])

    assert result == 0
    assert captured["title"] == "Fix parser crash when input is empty"
    assert captured["labels"] == ["ai-ready"]
    assert "Edited in editor." in captured["body"]
