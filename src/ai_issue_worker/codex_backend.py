from __future__ import annotations

import shlex
import time
from pathlib import Path

from .agent import AgentBackend
from .models import AgentResult
from .shell import run_cmd


class CodexBackend(AgentBackend):
    def __init__(self, command: str = "codex", log_path: Path | None = None):
        self.command = command
        self.log_path = log_path

    def run(self, worktree_path: Path, prompt_path: Path, timeout_sec: int) -> AgentResult:
        prompt = prompt_path.read_text(encoding="utf-8")
        started = time.monotonic()
        args = codex_command_args(self.command)
        result = run_cmd(args, cwd=worktree_path, timeout=timeout_sec, input_text=prompt)
        duration = time.monotonic() - started
        timed_out = result.exit_code == 124
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text(
                f"$ {result.command}\n\n## stdout\n{result.stdout}\n\n## stderr\n{result.stderr}\n",
                encoding="utf-8",
            )
        return AgentResult(
            success=result.exit_code == 0,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_sec=duration,
            timed_out=timed_out,
        )


def codex_command_args(command: str) -> list[str]:
    args = shlex.split(command)
    if args == ["codex"]:
        return ["codex", "exec", "--full-auto", "-"]
    if len(args) >= 2 and args[0] == "codex" and args[1] == "exec" and "-" not in args:
        return [*args, "-"]
    return args
