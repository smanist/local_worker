from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, REASONING_EFFORTS, ConfigError, load_config, write_default_config
from .codex_backend import CodexBackend
from .daemon import pid_alive, read_json, write_status
from .github_gh import GHClient, GHError
from .jobs import issue_run_dir, recent_jobs
from .locking import lock_status
from .prompt import build_issue_draft_prompt
from .runner import RunOverrides, configured_paths, run_once, workable_issues
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


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""


def _derive_issue_title(description: str, title: str | None) -> str:
    if title:
        return title.strip()
    first = _first_nonempty_line(description)
    if not first:
        return ""
    first = first.removeprefix("- ").removeprefix("* ").strip()
    return first[:120].rstrip(" .")


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _parse_issue_draft_json(text: str) -> tuple[str, str]:
    stripped = _strip_code_fence(text)
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        title = data.get("title")
        body = data.get("body")
        if isinstance(title, str) and isinstance(body, str):
            return title.strip(), body.strip()
    raise RuntimeError("agent did not return valid issue draft JSON")


def _render_issue_draft(title: str, body: str) -> str:
    body = body.strip()
    return f"""Title: {title.strip()}

{body}
"""


def _parse_issue_draft_file(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    title = ""
    body_start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        if stripped.lower().startswith("title:"):
            title = stripped.split(":", 1)[1].strip()
            body_start = index + 1
            break
        raise ConfigError("draft must start with a `Title:` line")
    body = "\n".join(lines[body_start:]).strip()
    if not title:
        raise ConfigError("issue title is empty after editing; aborting")
    if not body:
        raise ConfigError("issue body is empty after editing; aborting")
    return title, body


def _read_description(args) -> str:
    if args.description_file:
        if args.description_file == "-":
            return sys.stdin.read()
        return Path(args.description_file).read_text(encoding="utf-8")
    if args.description:
        return " ".join(args.description)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _editor_command(editor: str | None = None) -> list[str]:
    for command in [editor, os.environ.get("VISUAL"), os.environ.get("EDITOR")]:
        if command:
            return shlex.split(command)
    for name in ["nano", "vi"]:
        path = shutil.which(name)
        if path:
            return [path]
    raise RuntimeError("no editor found; set EDITOR or pass --no-edit")


def _run_editor(path: Path, editor: str | None = None) -> None:
    result = subprocess.run([*_editor_command(editor), str(path)], check=False)
    if result.returncode != 0:
        raise RuntimeError(f"editor exited with status {result.returncode}")


def _generate_issue_draft(config, repo_root: Path, description: str, title_hint: str, draft_dir: Path) -> tuple[str, str]:
    prompt_path = draft_dir / "issue-draft.prompt.md"
    log_path = draft_dir / "issue-draft.log"
    prompt_path.write_text(build_issue_draft_prompt(description, repo_root, config, title_hint=title_hint), encoding="utf-8")
    backend = CodexBackend(
        config.agent.command,
        log_path=log_path,
        model=config.agent.model,
        reasoning=config.agent.reasoning,
    )
    result = backend.run(repo_root, prompt_path, timeout_sec=config.agent.timeout_minutes * 60)
    if not result.success:
        detail = result.stderr.strip() or result.stdout.strip() or "agent failed without output"
        raise RuntimeError(f"issue draft generation failed: {detail}")
    title, body = _parse_issue_draft_json(result.stdout)
    if not title:
        title = title_hint.strip()
    if not title:
        raise RuntimeError("agent returned an empty issue title")
    if not body:
        raise RuntimeError("agent returned an empty issue body")
    return title, body


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
        gh = GHClient(config.repo)
        issues = gh.list_issues(config.issue_selection.ready_label)
        candidates = workable_issues(gh, issues, config.issue_selection)
    except (ConfigError, GHError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([issue.__dict__ for issue in candidates], indent=2, sort_keys=True))
        return 0
    for issue in candidates:
        print(f"#{issue.number}\t{issue.state}\t{issue.updated_at or ''}\t{','.join(issue.labels)}\t{issue.title}")
    return 0


def cmd_create(args) -> int:
    try:
        config = _load(args.config)
        description = _read_description(args).strip()
        if not description:
            raise ConfigError("issue description is required; pass text, --description-file, or pipe stdin")
        title_hint = _derive_issue_title(description, args.title)
        draft_dir = Path(tempfile.mkdtemp(prefix="ai-issue-create-"))
        draft_file = draft_dir / "issue.md"
        try:
            title, body = _generate_issue_draft(config, Path.cwd(), description, title_hint, draft_dir)
            draft_file.write_text(_render_issue_draft(title, body), encoding="utf-8")
            if not args.no_edit:
                _run_editor(draft_file, args.editor)
            edited_title, edited_body = _parse_issue_draft_file(draft_file.read_text(encoding="utf-8"))
            draft_file.write_text(f"{edited_body}\n", encoding="utf-8")
            url = GHClient(config.repo).create_issue(
                edited_title,
                draft_file,
                labels=[config.issue_selection.ready_label],
            )
        finally:
            shutil.rmtree(draft_dir, ignore_errors=True)
    except (ConfigError, GHError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"created issue: {url}")
    return 0


def cmd_run_once(args) -> int:
    return run_once(Path(args.config), overrides=RunOverrides(model=args.model, reasoning=args.reasoning))


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

        return daemon_loop(Path(args.config), interval, model=args.model, reasoning=args.reasoning)
    log_handle = log_file.open("a", encoding="utf-8")
    command = [sys.executable, "-m", "ai_issue_worker.daemon", "--config", str(Path(args.config).resolve()), "--interval", str(interval)]
    if args.model:
        command.extend(["--model", args.model])
    if args.reasoning:
        command.extend(["--reasoning", args.reasoning])
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
        return run_once(Path(args.config), overrides=RunOverrides(model=args.model, reasoning=args.reasoning))
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
    run.add_argument("--model")
    run.add_argument("--reasoning", choices=REASONING_EFFORTS)
    run.set_defaults(func=cmd_run_once)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=cmd_list)

    create = sub.add_parser("create")
    create.add_argument("description", nargs="*")
    create.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    create.add_argument("--title")
    create.add_argument("--description-file")
    create.add_argument("--editor")
    create.add_argument("--no-edit", action="store_true")
    create.set_defaults(func=cmd_create)

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    inspect.add_argument("--issue", type=int)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    start = sub.add_parser("start")
    start.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    start.add_argument("--interval", dest="interval_minutes", type=parse_interval_minutes)
    start.add_argument("--model")
    start.add_argument("--reasoning", choices=REASONING_EFFORTS)
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
    retry.add_argument("--model")
    retry.add_argument("--reasoning", choices=REASONING_EFFORTS)
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
