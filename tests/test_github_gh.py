import json

from ai_issue_worker.github_gh import GHClient


class Result:
    exit_code = 0
    stderr = ""
    command = "gh api"

    def __init__(self, stdout: str):
        self.stdout = stdout


def test_blocked_by_uses_gh_api_and_flattens_paginated_results(monkeypatch):
    captured = {}
    pages = [
        [{"number": 2, "title": "Open blocker", "body": "", "labels": [], "state": "open", "updated_at": "2026-01-01"}],
        [{"number": 3, "title": "Closed blocker", "body": "", "labels": [], "state": "closed"}],
    ]

    def fake_run_cmd(args):
        captured["args"] = args
        return Result(json.dumps(pages))

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    blockers = GHClient("owner/repo").blocked_by(1)

    assert captured["args"] == [
        "gh",
        "api",
        "repos/owner/repo/issues/1/dependencies/blocked_by",
        "--paginate",
        "--slurp",
    ]
    assert [issue.number for issue in blockers] == [2, 3]


def test_blocked_by_passes_hostname_for_hosted_repo(monkeypatch):
    captured = {}

    def fake_run_cmd(args):
        captured["args"] = args
        return Result("[]")

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    GHClient("github.example.com/owner/repo").blocked_by(1)

    assert captured["args"][:5] == [
        "gh",
        "api",
        "--hostname",
        "github.example.com",
        "repos/owner/repo/issues/1/dependencies/blocked_by",
    ]
