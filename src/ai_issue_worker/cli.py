from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config, write_default_config
from .daemon import pid_alive, read_json, write_status
from .github_gh import GHClient, GHError
from .issue_selection import candidate_issues
from .jobs import issue_run_dir, recent_jobs
from .locking import lock_status
from .runner import configured_paths, run_once
from .shell import run_cmd
from .worktree import GitError, remove_worktree


def parse_interval_minutes(value: str) -> int:
    raw = value.strip().lower()
    if raw.endswith("m"):
        return int(raw[:-1])
    if raw.endswith("h"):
        return int(raw[:-1]) * 60
    return int(raw)


def parse_age(value: str) -> timedelta:
    raw = value.strip().lower()
    if raw.endswith("d"):
        return timedelta(days=int(raw[:-1]))
    if raw.endswith("h"):
        return timedelta(hours=int(raw[:-1]))
    return timedelta(days=int(raw))


def _parse_started_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.removesuffix("Z"))
    except ValueError:
        return None


def _load(path: str):
    return load_config(Path(path))


def _paths(config, root: Path):
    paths = configured_paths(config, root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def cmd_init(args) -> int:
    try:
        write_default_config(Path(args.path), force=args.force)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"created {args.path}")
    return 0


def cmd_list(args) -> int:
    try:
        config = _load(args.config)
        issues = GHClient(config.repo).list_issues(config.issue_selection.ready_label)
        candidates = candidate_issues(issues, config.issue_selection)
    except (ConfigError, GHError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([issue.__dict__ for issue in candidates], indent=2, sort_keys=True))
        return 0
    for issue in candidates:
        print(f"#{issue.number}\t{issue.state}\t{issue.updated_at or ''}\t{','.join(issue.labels)}\t{issue.title}")
    return 0


def cmd_run_once(args) -> int:
    return run_once(Path(args.config))


def cmd_inspect(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    jobs = recent_jobs(paths["run_root"])
    data = {
        "repo": config.repo,
        "base_branch": config.base_branch,
        "labels": config.issue_selection.__dict__,
        "lock_status": lock_status(paths["runtime_root"] / "worker.lock"),
        "worktree_root": str(paths["worktree_root"]),
        "run_root": str(paths["run_root"]),
        "jobs": [job.to_dict() for job in jobs if args.issue is None or job.issue_number == args.issue],
        "open_working_issues": [],
    }
    try:
        working = GHClient(config.repo).list_issues(config.issue_selection.working_label)
        data["open_working_issues"] = [issue.__dict__ for issue in working]
    except GHError:
        pass
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(f"repo: {data['repo']}")
        print(f"base_branch: {data['base_branch']}")
        print(f"lock: {data['lock_status']}")
        print(f"worktree_root: {data['worktree_root']}")
        print(f"run_root: {data['run_root']}")
        print("recent jobs:")
        for job in data["jobs"]:
            print(f"  issue #{job['issue_number']}: {job['status']} {job['branch_name']}")
        if data["open_working_issues"]:
            print("open ai-working issues:")
            for issue in data["open_working_issues"]:
                print(f"  #{issue['number']}: {issue['title']}")
    return 0


def cmd_start(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    pid_file = paths["runtime_root"] / "worker.pid"
    status_file = paths["runtime_root"] / "worker.status.json"
    log_file = paths["log_root"] / "worker.log"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            if pid_alive(pid):
                print(f"worker already running with pid {pid}")
                return 1
        except ValueError:
            pass
    interval = args.interval_minutes if args.interval_minutes is not None else config.scheduler.interval_minutes
    if args.foreground:
        from .daemon import daemon_loop

        return daemon_loop(Path(args.config), interval)
    log_handle = log_file.open("a", encoding="utf-8")
    command = [sys.executable, "-m", "ai_issue_worker.daemon", "--config", str(Path(args.config).resolve()), "--interval", str(interval)]
    proc = subprocess.Popen(command, cwd=Path.cwd(), stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True)
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    write_status(status_file, running=True, pid=proc.pid, started_at=None, last_status="starting", log_file=str(log_file))
    print(f"started worker pid {proc.pid}")
    return 0


def cmd_stop(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    pid_file = paths["runtime_root"] / "worker.pid"
    if not pid_file.exists():
        print("worker is not running")
        return 0
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        pid_file.unlink()
        print("removed invalid pid file")
        return 0
    if not pid_alive(pid):
        pid_file.unlink()
        print("worker is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not pid_alive(pid):
            if pid_file.exists():
                pid_file.unlink()
            print("worker stopped")
            return 0
        time.sleep(0.1)
    print(f"sent SIGTERM to pid {pid}; process is still exiting")
    return 0


def cmd_status(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    pid_file = paths["runtime_root"] / "worker.pid"
    status = read_json(paths["runtime_root"] / "worker.status.json")
    pid = None
    running = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            running = pid_alive(pid)
        except ValueError:
            running = False
    print(f"running: {'yes' if running else 'no'}")
    print(f"pid: {pid or status.get('pid') or ''}")
    print(f"started_at: {status.get('started_at') or ''}")
    print(f"last_run_at: {status.get('last_run_at') or ''}")
    print(f"last_status: {status.get('last_status') or ''}")
    print(f"log_file: {status.get('log_file') or str(paths['log_root'] / 'worker.log')}")
    return 0


def cmd_logs(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    if args.issue:
        log_file = paths["run_root"] / f"issue-{args.issue}" / "verify.log"
    else:
        log_file = paths["log_root"] / "worker.log"
    if not log_file.exists():
        print(f"log file not found: {log_file}")
        return 0
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.tail :]:
        print(line)
    if args.follow:
        with log_file.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            try:
                while True:
                    line = handle.readline()
                    if line:
                        print(line, end="")
                    else:
                        time.sleep(1)
            except KeyboardInterrupt:
                return 0
    return 0


def cmd_retry(args) -> int:
    try:
        config = _load(args.config)
        gh = GHClient(config.repo)
        gh.remove_label(args.issue, config.issue_selection.failed_label)
        gh.add_label(args.issue, config.issue_selection.ready_label)
    except (ConfigError, GHError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.run_now:
        return run_once(Path(args.config))
    print(f"issue #{args.issue} marked ready")
    return 0


def cmd_clean(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    jobs = recent_jobs(paths["run_root"])
    selected = [job for job in jobs if args.issue is None or job.issue_number == args.issue]
    if args.failed:
        selected = [job for job in selected if "failed" in job.status or job.status in {"verify_failed", "agent_failed", "diff_rejected"}]
    if args.older_than:
        cutoff = datetime.utcnow() - parse_age(args.older_than)
        selected = [job for job in selected if (started := _parse_started_at(job.started_at)) is not None and started < cutoff]
    for job in selected:
        worktree = Path(job.worktree_path)
        print(f"{'would remove' if args.dry_run else 'removing'} worktree {worktree}")
        if not args.dry_run and worktree.exists():
            try:
                remove_worktree(worktree)
            except GitError as exc:
                print(f"failed to remove {worktree}: {exc}", file=sys.stderr)
                continue
        run_dir = issue_run_dir(paths["run_root"], job.issue_number)
        print(f"{'would remove' if args.dry_run else 'removing'} run directory {run_dir}")
        if not args.dry_run and run_dir.exists():
            shutil.rmtree(run_dir)
        if args.delete_local_branches and not args.dry_run:
            run_cmd(["git", "branch", "-D", job.branch_name])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-issue")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--path", default=DEFAULT_CONFIG_PATH)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("run-once")
    run.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    run.set_defaults(func=cmd_run_once)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=cmd_list)

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    inspect.add_argument("--issue", type=int)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    start = sub.add_parser("start")
    start.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    start.add_argument("--interval", dest="interval_minutes", type=parse_interval_minutes)
    start.add_argument("--foreground", action="store_true")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop")
    stop.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status")
    status.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    status.set_defaults(func=cmd_status)

    logs = sub.add_parser("logs")
    logs.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    logs.add_argument("--tail", type=int, default=100)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--issue", type=int)
    logs.set_defaults(func=cmd_logs)

    retry = sub.add_parser("retry")
    retry.add_argument("issue", type=int)
    retry.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    retry.add_argument("--run-now", action="store_true")
    retry.set_defaults(func=cmd_retry)

    clean = sub.add_parser("clean")
    clean.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    clean.add_argument("--issue", type=int)
    clean.add_argument("--failed", action="store_true")
    clean.add_argument("--older-than")
    clean.add_argument("--dry-run", action="store_true")
    clean.add_argument("--delete-local-branches", action="store_true")
    clean.set_defaults(func=cmd_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
