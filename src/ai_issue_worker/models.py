from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]
    state: str
    url: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_gh(cls, data: dict[str, Any]) -> "Issue":
        labels = data.get("labels") or []
        names = [item["name"] if isinstance(item, dict) else str(item) for item in labels]
        return cls(
            number=int(data["number"]),
            title=data.get("title") or "",
            body=data.get("body") or "",
            labels=names,
            state=data.get("state") or "",
            url=data.get("url"),
            updated_at=data.get("updatedAt") or data.get("updated_at"),
        )


@dataclass
class JobRecord:
    issue_number: int
    issue_title: str
    branch_name: str
    worktree_path: str
    status: str
    started_at: str
    finished_at: str | None = None
    pr_url: str | None = None
    error_summary: str | None = None
    changed_files: list[str] = field(default_factory=list)
    verifier_passed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRecord":
        return cls(**data)


@dataclass
class AgentResult:
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float


@dataclass
class VerifyResult:
    passed: bool
    commands: list[CommandResult]


@dataclass
class DiffSummary:
    changed_files: list[str]
    diff_stat: str
    diff_line_count: int
    rejected: bool
    rejection_reason: str | None

