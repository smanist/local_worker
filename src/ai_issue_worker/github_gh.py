from __future__ import annotations

import json
from pathlib import Path

from .models import Issue
from .shell import run_cmd


class GHError(RuntimeError):
    pass


class GHClient:
    def __init__(self, repo: str):
        self.repo = repo

    def _api_repo_args(self) -> tuple[list[str], str]:
        parts = self.repo.split("/")
        if len(parts) == 2:
            owner, repo = parts
            return [], f"repos/{owner}/{repo}"
        if len(parts) == 3:
            host, owner, repo = parts
            return ["--hostname", host], f"repos/{owner}/{repo}"
        raise GHError("repo must be in owner/repo or host/owner/repo form")

    def _run(self, args: list[str]):
        result = run_cmd(args)
        if result.exit_code != 0:
            raise GHError(result.stderr.strip() or result.stdout.strip() or f"gh command failed: {result.command}")
        return result

    def validate(self) -> None:
        self._run(["gh", "auth", "status"])
        self._run(["gh", "repo", "view", self.repo])

    def list_issues(self, ready_label: str) -> list[Issue]:
        result = self._run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--label",
                ready_label,
                "--json",
                "number,title,body,labels,state,url,updatedAt",
            ]
        )
        return [Issue.from_gh(item) for item in json.loads(result.stdout or "[]")]

    def view_issue(self, number: int) -> Issue:
        result = self._run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "number,title,body,labels,state,url,updatedAt",
            ]
        )
        return Issue.from_gh(json.loads(result.stdout))

    def blocked_by(self, number: int) -> list[Issue]:
        hostname_args, repo_path = self._api_repo_args()
        result = self._run(
            [
                "gh",
                "api",
                *hostname_args,
                f"{repo_path}/issues/{number}/dependencies/blocked_by",
                "--paginate",
                "--slurp",
            ]
        )
        data = json.loads(result.stdout or "[]")
        if data and all(isinstance(page, list) for page in data):
            items = [item for page in data for item in page]
        else:
            items = data
        return [Issue.from_gh(item) for item in items]

    def add_label(self, number: int, label: str) -> None:
        self._run(["gh", "issue", "edit", str(number), "--repo", self.repo, "--add-label", label])

    def remove_label(self, number: int, label: str) -> None:
        self._run(["gh", "issue", "edit", str(number), "--repo", self.repo, "--remove-label", label])

    def comment(self, number: int, body_file: Path) -> None:
        self._run(["gh", "issue", "comment", str(number), "--repo", self.repo, "--body-file", str(body_file)])

    def create_issue(self, title: str, body_file: Path, labels: list[str] | None = None) -> str:
        args = [
            "gh",
            "issue",
            "create",
            "--repo",
            self.repo,
            "--title",
            title,
            "--body-file",
            str(body_file),
        ]
        for label in labels or []:
            args.extend(["--label", label])
        result = self._run(args)
        return result.stdout.strip().splitlines()[-1]

    def create_pr(
        self,
        base: str,
        head: str,
        title: str,
        body_file: Path,
        draft: bool = True,
    ) -> str:
        args = [
            "gh",
            "pr",
            "create",
            "--repo",
            self.repo,
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body-file",
            str(body_file),
        ]
        if draft:
            args.append("--draft")
        result = self._run(args)
        return result.stdout.strip().splitlines()[-1]
