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
