from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from .models import JobRecord


def utc_timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def issue_run_dir(run_root: Path, issue_number: int) -> Path:
    return run_root / f"issue-{issue_number}"


def write_text_artifact(path: Path, latest_path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    shutil.copyfile(path, latest_path)


def write_job_record(run_dir: Path, record: JobRecord, timestamp: str | None = None) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or utc_timestamp()
    path = run_dir / f"run-{stamp}.json"
    path.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    shutil.copyfile(path, run_dir / "latest.json")
    return path


def load_job_record(path: Path) -> JobRecord:
    return JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def recent_jobs(run_root: Path) -> list[JobRecord]:
    records: list[JobRecord] = []
    if not run_root.exists():
        return records
    for latest in sorted(run_root.glob("issue-*/latest.json")):
        try:
            records.append(load_job_record(latest))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    records.sort(key=lambda record: record.started_at, reverse=True)
    return records

