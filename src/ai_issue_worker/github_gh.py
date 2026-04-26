from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import Issue
from .privacy import sanitize_user_paths
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

    @contextmanager
    def _sanitized_body_file(self, body_file: Path):
        body = body_file.read_text(encoding="utf-8", errors="replace")
        sanitized = sanitize_user_paths(body)
        if sanitized == body:
            yield body_file
            return

        temp_path: Path | None = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
                handle.write(sanitized)
                temp_path = Path(handle.name)
            yield temp_path
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)

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

    def ensure_label(self, name: str, color: str, description: str) -> None:
        self._run(
            [
                "gh",
                "label",
                "create",
                name,
                "--repo",
                self.repo,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ]
        )

    def ensure_labels(self, labels: Mapping[str, tuple[str, str]]) -> None:
        for name, (color, description) in labels.items():
            self.ensure_label(name, color, description)

    def comment(self, number: int, body_file: Path) -> None:
        with self._sanitized_body_file(body_file) as sanitized:
            self._run(["gh", "issue", "comment", str(number), "--repo", self.repo, "--body-file", str(sanitized)])

    def create_issue(self, title: str, body_file: Path, labels: list[str] | None = None) -> str:
        args = [
            "gh",
            "issue",
            "create",
            "--repo",
            self.repo,
            "--title",
            sanitize_user_paths(title),
        ]
        body_index = len(args)
        args.extend(["--body-file", ""])
        for label in labels or []:
            args.extend(["--label", label])
        with self._sanitized_body_file(body_file) as sanitized:
            args[body_index + 1] = str(sanitized)
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
            sanitize_user_paths(title),
        ]
        body_index = len(args)
        args.extend(["--body-file", ""])
        if draft:
            args.append("--draft")
        with self._sanitized_body_file(body_file) as sanitized:
            args[body_index + 1] = str(sanitized)
            result = self._run(args)
        return result.stdout.strip().splitlines()[-1]
