from __future__ import annotations

import shlex
import shutil
from pathlib import Path

from .codex_backend import CodexBackend
from .config import ConfigError, WorkerConfig, load_config
from .diff_policy import inspect_diff
from .github_gh import GHClient, GHError
from .issue_selection import candidate_issues, select_one_issue
from .jobs import issue_run_dir, utc_iso, utc_timestamp, write_job_record, write_text_artifact
from .locking import FileLock, LockHeld
from .models import DiffSummary, Issue, JobRecord, VerifyResult
from .pr import build_pr_body, render_template
from .prompt import build_prompt, build_repair_prompt
from .shell import run_cmd
from .verifier import format_verification_summary, run_verifier
from .worktree import GitError, add_worktree, commit_all, push_branch, remove_worktree, unique_branch_name
from .worktree import ensure_git_ok


EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_DEPENDENCY = 2
EXIT_GH = 3
EXIT_GIT = 4
EXIT_AGENT = 5
EXIT_VERIFY = 6
EXIT_PR = 7
EXIT_LOCK = 8


class DependencyError(RuntimeError):
    pass


def _path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def configured_paths(config: WorkerConfig, root: Path) -> dict[str, Path]:
    return {
        "worktree_root": _path(root, config.paths.worktree_root),
        "run_root": _path(root, config.paths.run_root),
        "log_root": _path(root, config.paths.log_root),
        "runtime_root": _path(root, config.paths.runtime_root),
    }


def check_dependencies(config: WorkerConfig) -> None:
    checks = [
        ["gh", "--version"],
        ["git", "--version"],
        [*shlex.split(config.agent.command), "--version"],
    ]
    for check in checks:
        result = run_cmd(check)
        if result.exit_code != 0:
            raise DependencyError(result.stderr.strip() or f"dependency check failed: {result.command}")
    GHClient(config.repo).validate()
    ensure_git_ok(config.base_branch, allow_dirty=config.git.allow_dirty_base)


def _comment_failure(gh: GHClient, issue: Issue, run_dir: Path, message: str) -> None:
    comment = run_dir / "failure-comment.md"
    comment.write_text(message, encoding="utf-8")
    try:
        gh.comment(issue.number, comment)
    except GHError:
        pass


def _finalize_issue_failure(
    gh: GHClient,
    config: WorkerConfig,
    issue: Issue,
    run_dir: Path,
    message: str,
    add_failed_label: bool = True,
) -> None:
    if add_failed_label:
        try:
            gh.add_label(issue.number, config.issue_selection.failed_label)
        except GHError:
            pass
    try:
        gh.remove_label(issue.number, config.issue_selection.working_label)
    except GHError:
        pass
    _comment_failure(gh, issue, run_dir, message)


def _write_record(run_dir: Path, record: JobRecord, status: str, error: str | None = None) -> None:
    record.status = status
    record.error_summary = error
    record.finished_at = utc_iso()
    write_job_record(run_dir, record)


def _record_for(issue: Issue, branch: str, worktree_path: Path) -> JobRecord:
    return JobRecord(
        issue_number=issue.number,
        issue_title=issue.title,
        branch_name=branch,
        worktree_path=str(worktree_path),
        status="selected",
        started_at=utc_iso(),
        finished_at=None,
        pr_url=None,
        error_summary=None,
        changed_files=[],
        verifier_passed=None,
    )


def _run_agent_and_verify(
    config: WorkerConfig,
    issue: Issue,
    repo_root: Path,
    worktree_path: Path,
    run_dir: Path,
    stamp: str,
) -> tuple[bool, VerifyResult | None, DiffSummary, str]:
    prompt_text = build_prompt(issue, config, repo_root)
    prompt_path = run_dir / f"prompt-{stamp}.md"
    write_text_artifact(prompt_path, run_dir / "prompt.md", prompt_text)

    codex_log = run_dir / f"codex-{stamp}.log"
    backend = CodexBackend(config.agent.command, codex_log)
    agent = backend.run(worktree_path, prompt_path, timeout_sec=config.agent.timeout_minutes * 60)
    shutil.copyfile(codex_log, run_dir / "codex.log")
    if not agent.success:
        return False, None, inspect_diff(worktree_path, config.diff_policy), (
            f"Agent failed with exit code {agent.exit_code}."
            f"\n\nstdout:\n{agent.stdout[-2000:]}\n\nstderr:\n{agent.stderr[-2000:]}"
        )

    verify_log = run_dir / f"verify-{stamp}.log"
    verify = run_verifier(config.verify, worktree_path, verify_log)
    latest_verify = run_dir / "verify.log"
    latest_verify.write_text(verify_log.read_text(encoding="utf-8"), encoding="utf-8")
    diff = inspect_diff(worktree_path, config.diff_policy)

    repairs = 0
    while not verify.passed and repairs < config.agent.max_repair_attempts:
        repairs += 1
        repair_stamp = f"{stamp}-repair-{repairs}"
        repair_prompt = build_repair_prompt(issue, verify_log.read_text(encoding="utf-8"), diff)
        repair_prompt_path = run_dir / f"prompt-{repair_stamp}.md"
        write_text_artifact(repair_prompt_path, run_dir / "prompt.md", repair_prompt)
        backend = CodexBackend(config.agent.command, run_dir / f"codex-{repair_stamp}.log")
        repair = backend.run(worktree_path, repair_prompt_path, timeout_sec=config.agent.timeout_minutes * 60)
        shutil.copyfile(run_dir / f"codex-{repair_stamp}.log", run_dir / "codex.log")
        if not repair.success:
            return False, verify, diff, f"Repair attempt failed with exit code {repair.exit_code}."
        verify_log = run_dir / f"verify-{repair_stamp}.log"
        verify = run_verifier(config.verify, worktree_path, verify_log)
        latest_verify.write_text(verify_log.read_text(encoding="utf-8"), encoding="utf-8")
        diff = inspect_diff(worktree_path, config.diff_policy)

    return verify.passed, verify, diff, "" if verify.passed else "Verification failed."


def process_issue(config: WorkerConfig, issue: Issue, repo_root: Path, paths: dict[str, Path]) -> int:
    gh = GHClient(config.repo)
    stamp = utc_timestamp()
    branch = unique_branch_name(config.git, issue.number, issue.title)
    worktree_path = paths["worktree_root"] / f"issue-{issue.number}"
    run_dir = issue_run_dir(paths["run_root"], issue.number)
    run_dir.mkdir(parents=True, exist_ok=True)
    record = _record_for(issue, branch, worktree_path)
    write_job_record(run_dir, record, stamp)

    gh.add_label(issue.number, config.issue_selection.working_label)
    record.status = "working"
    write_job_record(run_dir, record, stamp)

    try:
        add_worktree(worktree_path, branch, config.base_branch)
    except GitError as exc:
        _finalize_issue_failure(gh, config, issue, run_dir, f"Git worktree setup failed:\n\n{exc}", add_failed_label=False)
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_GIT

    ok, verify, diff, error = _run_agent_and_verify(config, issue, repo_root, worktree_path, run_dir, stamp)
    record.changed_files = diff.changed_files
    record.verifier_passed = verify.passed if verify else None
    if not ok:
        status = "agent_failed" if verify is None else "verify_failed"
        _finalize_issue_failure(gh, config, issue, run_dir, error, add_failed_label=True)
        _write_record(run_dir, record, status, error)
        return EXIT_AGENT if verify is None else EXIT_VERIFY

    if diff.rejected:
        status = "no_changes" if diff.rejection_reason == "no changes" else "diff_rejected"
        message = f"Diff policy rejected the result:\n\n{diff.rejection_reason}"
        _finalize_issue_failure(gh, config, issue, run_dir, message, add_failed_label=True)
        _write_record(run_dir, record, status, diff.rejection_reason)
        return EXIT_VERIFY

    try:
        commit_message = render_template(config.git.commit_message_template, issue)
        commit_all(worktree_path, commit_message)
        record.status = "committed"
        write_job_record(run_dir, record, stamp)
        push_branch(worktree_path, branch)
        record.status = "pushed"
        write_job_record(run_dir, record, stamp)
    except GitError as exc:
        _finalize_issue_failure(gh, config, issue, run_dir, f"Git commit/push failed:\n\n{exc}", add_failed_label=False)
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_GIT

    verification_summary = format_verification_summary(verify)
    pr_body = build_pr_body(config.pr, issue, verification_summary, diff)
    pr_body_path = run_dir / f"pr-body-{stamp}.md"
    write_text_artifact(pr_body_path, run_dir / "pr_body.md", pr_body)
    try:
        pr_title = render_template(config.pr.title_template, issue)
        pr_url = gh.create_pr(config.base_branch, branch, pr_title, pr_body_path, draft=config.pr.draft)
    except GHError as exc:
        _finalize_issue_failure(gh, config, issue, run_dir, f"PR creation failed:\n\n{exc}", add_failed_label=False)
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_PR

    record.pr_url = pr_url
    try:
        gh.add_label(issue.number, config.issue_selection.pr_opened_label)
        gh.remove_label(issue.number, config.issue_selection.working_label)
        if config.git.remove_ready_on_pr:
            gh.remove_label(issue.number, config.issue_selection.ready_label)
        comment = run_dir / "success-comment.md"
        comment.write_text(f"Draft PR opened: {pr_url}\n", encoding="utf-8")
        gh.comment(issue.number, comment)
    except GHError:
        pass

    _write_record(run_dir, record, "pr_opened", None)
    if not config.git.keep_worktree_on_success:
        try:
            remove_worktree(worktree_path)
        except GitError:
            pass
    return EXIT_OK


def run_once(config_path: Path, repo_root: Path | None = None) -> int:
    root = repo_root or Path.cwd()
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG
    paths = configured_paths(config, root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    try:
        with FileLock(paths["runtime_root"] / "worker.lock"):
            try:
                check_dependencies(config)
            except DependencyError as exc:
                print(f"dependency error: {exc}")
                return EXIT_DEPENDENCY
            except GHError as exc:
                print(f"GitHub error: {exc}")
                return EXIT_GH
            except GitError as exc:
                print(f"git error: {exc}")
                return EXIT_GIT

            gh = GHClient(config.repo)
            try:
                issues = gh.list_issues(config.issue_selection.ready_label)
                candidates = candidate_issues(issues, config.issue_selection)
                issue = select_one_issue(candidates, config.issue_selection)
                if issue is None:
                    return EXIT_OK
                issue = gh.view_issue(issue.number)
                return process_issue(config, issue, root, paths)
            except GHError as exc:
                print(f"GitHub error: {exc}")
                return EXIT_GH
    except LockHeld:
        print("lock already held")
        return EXIT_LOCK
