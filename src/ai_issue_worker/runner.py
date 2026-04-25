from __future__ import annotations

import hashlib
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from .codex_backend import CodexBackend
from .config import ConfigError, IssueSelectionConfig, WorkerConfig, load_config
from .diff_policy import inspect_diff
from .github_gh import GHClient, GHError
from .issue_selection import candidate_issues
from .jobs import issue_run_dir, load_job_record, utc_iso, utc_timestamp, write_job_record, write_text_artifact
from .locking import FileLock, LockHeld
from .models import DiffSummary, Issue, JobRecord, VerifyResult
from .pr import build_pr_body, render_template
from .prompt import build_prompt, build_repair_prompt, build_review_fix_prompt, build_review_prompt
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

REVIEW_FINDING_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:\[(P[0-3])\]|(P[0-3])\s*[:\-])", re.MULTILINE)


class DependencyError(RuntimeError):
    pass


class RunOverrides:
    def __init__(self, model: str | None = None, reasoning: str | None = None):
        self.model = model
        self.reasoning = reasoning


@dataclass(frozen=True)
class IssueWorkPlan:
    issue: Issue
    base_branch: str
    stack_depth: int = 0
    blocker_issue_numbers: list[int] | None = None


def apply_overrides(config: WorkerConfig, overrides: RunOverrides | None = None) -> WorkerConfig:
    if overrides is None:
        return config
    if overrides.model:
        config.agent.model = overrides.model
    if overrides.reasoning:
        config.agent.reasoning = overrides.reasoning
    return config


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


def _relative_prefix(root: Path, path: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return None


def check_dependencies(config: WorkerConfig, root: Path | None = None, paths: dict[str, Path] | None = None) -> None:
    checks = [
        ["gh", "--version"],
        ["git", "--version"],
        [*shlex.split(config.agent.command), "--version"],
    ]
    if config.review.enabled:
        checks.append([*shlex.split(config.review.command), "--version"])
    for check in checks:
        result = run_cmd(check)
        if result.exit_code != 0:
            raise DependencyError(result.stderr.strip() or f"dependency check failed: {result.command}")
    GHClient(config.repo).validate()
    allowed_prefixes: list[str] = []
    if root and paths:
        for key in ("worktree_root", "run_root", "log_root", "runtime_root"):
            prefix = _relative_prefix(root, paths[key])
            if prefix:
                allowed_prefixes.append(prefix)
    ensure_git_ok(config.base_branch, allow_dirty=config.git.allow_dirty_base, allowed_dirty_prefixes=allowed_prefixes)


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


def _record_for(issue: Issue, branch: str, worktree_path: Path, plan: IssueWorkPlan) -> JobRecord:
    return JobRecord(
        issue_number=issue.number,
        issue_title=issue.title,
        branch_name=branch,
        worktree_path=str(worktree_path),
        status="selected",
        started_at=utc_iso(),
        base_branch=plan.base_branch,
        stack_depth=plan.stack_depth,
        blocker_issue_numbers=list(plan.blocker_issue_numbers or []),
        finished_at=None,
        pr_url=None,
        error_summary=None,
        changed_files=[],
        verifier_passed=None,
    )


def _open_blockers(gh: GHClient, issue: Issue) -> list[Issue]:
    return [blocker for blocker in gh.blocked_by(issue.number) if blocker.state.lower() == "open"]


def _latest_pr_job(paths: dict[str, Path], issue_number: int) -> JobRecord | None:
    latest = issue_run_dir(paths["run_root"], issue_number) / "latest.json"
    if not latest.exists():
        return None
    try:
        record = load_job_record(latest)
    except (OSError, ValueError, TypeError):
        return None
    if record.status != "pr_opened" or not record.pr_url:
        return None
    return record


def _work_plan_for_issue(
    gh: GHClient,
    issue: Issue,
    config: IssueSelectionConfig,
    base_branch: str,
    paths: dict[str, Path],
) -> IssueWorkPlan | None:
    if not config.respect_issue_dependencies:
        return IssueWorkPlan(issue, base_branch)

    blockers = _open_blockers(gh, issue)
    if not blockers:
        return IssueWorkPlan(issue, base_branch)
    if not config.allow_stacked_prs or len(blockers) != 1:
        return None

    blocker = blockers[0]
    blocker_job = _latest_pr_job(paths, blocker.number)
    if blocker_job is None:
        return None
    stack_depth = blocker_job.stack_depth + 1
    if stack_depth > config.max_stack_depth:
        return None
    return IssueWorkPlan(issue, blocker_job.branch_name, stack_depth, [blocker.number])


def workable_issue_plans(
    gh: GHClient,
    issues: list[Issue],
    config: IssueSelectionConfig,
    base_branch: str,
    paths: dict[str, Path],
) -> list[IssueWorkPlan]:
    candidates = candidate_issues(issues, config)
    plans: list[IssueWorkPlan] = []
    for issue in candidates:
        plan = _work_plan_for_issue(gh, issue, config, base_branch, paths)
        if plan is not None:
            plans.append(plan)
    return plans


def workable_issues(
    gh: GHClient,
    issues: list[Issue],
    config: IssueSelectionConfig,
    base_branch: str,
    paths: dict[str, Path],
) -> list[Issue]:
    return [plan.issue for plan in workable_issue_plans(gh, issues, config, base_branch, paths)]


def select_work_plan(
    gh: GHClient,
    issues: list[Issue],
    config: IssueSelectionConfig,
    base_branch: str,
    paths: dict[str, Path],
) -> IssueWorkPlan | None:
    plans = workable_issue_plans(gh, issues, config, base_branch, paths)
    return plans[0] if plans else None


def blocking_review_priorities(review_output: str, blocking_priorities: list[str]) -> list[str]:
    blocking = set(blocking_priorities)
    for line in review_output.splitlines():
        if line.strip().upper().startswith("BLOCKING_PRIORITIES:"):
            value = line.split(":", 1)[1].strip().upper()
            if value in {"", "NONE", "NO", "N/A"}:
                return []
            found = [priority for priority in re.findall(r"P[0-3]", value) if priority in blocking]
            return sorted(set(found), key=found.index)

    matches: list[str] = []
    for match in REVIEW_FINDING_RE.finditer(review_output):
        priority = match.group(1) or match.group(2)
        if priority in blocking and priority not in matches:
            matches.append(priority)
    return matches


def _run_codex_session(
    config: WorkerConfig,
    worktree_path: Path,
    prompt_path: Path,
    log_path: Path,
    command: str | None = None,
):
    backend = CodexBackend(
        command or config.agent.command,
        log_path,
        model=config.agent.model,
        reasoning=config.agent.reasoning,
    )
    result = backend.run(worktree_path, prompt_path, timeout_sec=config.agent.timeout_minutes * 60)
    if log_path.exists():
        shutil.copyfile(log_path, log_path.parent / "codex.log")
    return result


def _diff_snapshot(worktree_path: Path) -> str:
    status = run_cmd(["git", "status", "--porcelain"], cwd=worktree_path)
    result = run_cmd(["git", "diff", "--binary", "HEAD"], cwd=worktree_path)
    untracked = run_cmd(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=worktree_path)
    ignored = run_cmd(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"], cwd=worktree_path)
    status_text = status.stdout if status.exit_code == 0 else ""
    diff_text = result.stdout if result.exit_code == 0 else ""
    untracked_parts: list[str] = []
    untracked_names: set[str] = set()
    if untracked.exit_code == 0:
        untracked_names.update(path for path in untracked.stdout.split("\0") if path)
    if ignored.exit_code == 0:
        untracked_names.update(path for path in ignored.stdout.split("\0") if path)
    for name in sorted(untracked_names):
        path = worktree_path / name
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        except OSError:
            digest = ""
        untracked_parts.append(f"{name}\0{digest}")
    return f"{status_text}\0{diff_text}\0{chr(0).join(untracked_parts)}"


def _run_verification_with_repairs(
    config: WorkerConfig,
    issue: Issue,
    worktree_path: Path,
    run_dir: Path,
    stamp: str,
) -> tuple[bool, VerifyResult, DiffSummary, str, str]:
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
        repair = _run_codex_session(
            config,
            worktree_path,
            repair_prompt_path,
            run_dir / f"codex-{repair_stamp}.log",
        )
        if not repair.success:
            return False, verify, diff, f"Repair attempt failed with exit code {repair.exit_code}.", "agent"
        verify_log = run_dir / f"verify-{repair_stamp}.log"
        verify = run_verifier(config.verify, worktree_path, verify_log)
        latest_verify.write_text(verify_log.read_text(encoding="utf-8"), encoding="utf-8")
        diff = inspect_diff(worktree_path, config.diff_policy)

    return verify.passed, verify, diff, "" if verify.passed else "Verification failed.", "verify"


def _run_review_loop(
    config: WorkerConfig,
    issue: Issue,
    repo_root: Path,
    worktree_path: Path,
    run_dir: Path,
    stamp: str,
    verify: VerifyResult,
    diff: DiffSummary,
) -> tuple[bool, VerifyResult, DiffSummary, str, str]:
    if not config.review.enabled:
        return True, verify, diff, "", ""

    fixes_completed = 0
    review_iteration = 0
    while True:
        review_iteration += 1
        review_stamp = f"{stamp}-review-{review_iteration}"
        review_prompt = build_review_prompt(issue, config, repo_root, diff, verify)
        review_prompt_path = run_dir / f"prompt-{review_stamp}.md"
        write_text_artifact(review_prompt_path, run_dir / "prompt.md", review_prompt)
        before_review = _diff_snapshot(worktree_path)
        review = _run_codex_session(
            config,
            worktree_path,
            review_prompt_path,
            run_dir / f"codex-{review_stamp}.log",
            command=config.review.command,
        )
        after_review = _diff_snapshot(worktree_path)
        review_output = (review.stdout.strip() or review.stderr.strip()).strip()
        write_text_artifact(run_dir / f"review-{review_stamp}.md", run_dir / "review.md", review_output)
        if not review.success:
            return False, verify, diff, f"Review attempt failed with exit code {review.exit_code}.", "agent"
        if after_review != before_review:
            return False, verify, diff, "Review attempt modified the worktree; review sessions must be read-only.", "agent"
        if not review_output:
            return False, verify, diff, "Review attempt completed without output.", "agent"

        priorities = blocking_review_priorities(review_output, config.review.fix_priorities)
        if not priorities:
            return True, verify, diff, "", ""

        if fixes_completed >= config.review.max_iterations:
            return (
                False,
                verify,
                diff,
                "Code review still reports blocking findings after "
                f"{config.review.max_iterations} fix iteration(s): {', '.join(priorities)}.\n\n"
                f"Latest review output:\n{review_output[-4000:]}",
                "review",
            )

        fixes_completed += 1
        fix_stamp = f"{stamp}-review-fix-{fixes_completed}"
        fix_prompt = build_review_fix_prompt(issue, review_output, diff, config.review.fix_priorities)
        fix_prompt_path = run_dir / f"prompt-{fix_stamp}.md"
        write_text_artifact(fix_prompt_path, run_dir / "prompt.md", fix_prompt)
        fix = _run_codex_session(
            config,
            worktree_path,
            fix_prompt_path,
            run_dir / f"codex-{fix_stamp}.log",
        )
        if not fix.success:
            return False, verify, diff, f"Review fix attempt failed with exit code {fix.exit_code}.", "agent"

        ok, verify, diff, error, failure_kind = _run_verification_with_repairs(
            config,
            issue,
            worktree_path,
            run_dir,
            fix_stamp,
        )
        if not ok:
            return False, verify, diff, f"Verification failed after review fix.\n\n{error}", failure_kind


def _run_agent_and_verify(
    config: WorkerConfig,
    issue: Issue,
    repo_root: Path,
    worktree_path: Path,
    run_dir: Path,
    stamp: str,
) -> tuple[bool, VerifyResult | None, DiffSummary, str, str]:
    prompt_text = build_prompt(issue, config, repo_root)
    prompt_path = run_dir / f"prompt-{stamp}.md"
    write_text_artifact(prompt_path, run_dir / "prompt.md", prompt_text)

    codex_log = run_dir / f"codex-{stamp}.log"
    agent = _run_codex_session(config, worktree_path, prompt_path, codex_log)
    if not agent.success:
        return False, None, inspect_diff(worktree_path, config.diff_policy), (
            f"Agent failed with exit code {agent.exit_code}."
            f"\n\nstdout:\n{agent.stdout[-2000:]}\n\nstderr:\n{agent.stderr[-2000:]}"
        ), "agent"

    ok, verify, diff, error, failure_kind = _run_verification_with_repairs(config, issue, worktree_path, run_dir, stamp)
    if not ok:
        return ok, verify, diff, error, failure_kind

    return _run_review_loop(config, issue, repo_root, worktree_path, run_dir, stamp, verify, diff)


def process_issue(config: WorkerConfig, plan: IssueWorkPlan, repo_root: Path, paths: dict[str, Path]) -> int:
    issue = plan.issue
    gh = GHClient(config.repo)
    stamp = utc_timestamp()
    branch = unique_branch_name(config.git, issue.number, issue.title)
    worktree_path = paths["worktree_root"] / f"issue-{issue.number}"
    run_dir = issue_run_dir(paths["run_root"], issue.number)
    run_dir.mkdir(parents=True, exist_ok=True)
    record = _record_for(issue, branch, worktree_path, plan)
    write_job_record(run_dir, record, stamp)

    gh.add_label(issue.number, config.issue_selection.working_label)
    record.status = "working"
    write_job_record(run_dir, record, stamp)

    try:
        add_worktree(worktree_path, branch, plan.base_branch)
    except GitError as exc:
        _finalize_issue_failure(gh, config, issue, run_dir, f"Git worktree setup failed:\n\n{exc}", add_failed_label=False)
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_GIT

    ok, verify, diff, error, failure_kind = _run_agent_and_verify(config, issue, repo_root, worktree_path, run_dir, stamp)
    record.changed_files = diff.changed_files
    record.verifier_passed = verify.passed if verify else None
    if not ok:
        if failure_kind == "agent":
            status = "agent_failed"
        elif failure_kind == "review":
            status = "review_failed"
        else:
            status = "verify_failed"
        _finalize_issue_failure(gh, config, issue, run_dir, error, add_failed_label=True)
        _write_record(run_dir, record, status, error)
        return EXIT_AGENT if failure_kind == "agent" else EXIT_VERIFY

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
        pr_url = gh.create_pr(plan.base_branch, branch, pr_title, pr_body_path, draft=config.pr.draft)
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


def run_once(config_path: Path, repo_root: Path | None = None, overrides: RunOverrides | None = None) -> int:
    root = repo_root or Path.cwd()
    try:
        config = apply_overrides(load_config(config_path), overrides)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG
    paths = configured_paths(config, root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    try:
        with FileLock(paths["runtime_root"] / "worker.lock"):
            try:
                check_dependencies(config, root=root, paths=paths)
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
                plan = select_work_plan(gh, issues, config.issue_selection, config.base_branch, paths)
                if plan is None:
                    return EXIT_OK
                issue = gh.view_issue(plan.issue.number)
                plan = IssueWorkPlan(
                    issue=issue,
                    base_branch=plan.base_branch,
                    stack_depth=plan.stack_depth,
                    blocker_issue_numbers=plan.blocker_issue_numbers,
                )
                return process_issue(config, plan, root, paths)
            except GHError as exc:
                print(f"GitHub error: {exc}")
                return EXIT_GH
    except LockHeld:
        print("lock already held")
        return EXIT_LOCK
