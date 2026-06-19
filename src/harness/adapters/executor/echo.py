"""EchoExecutor: a deterministic, dependency-free Executor for the M1 demo + tests.

No subprocess, no network, no ``claude`` CLI. It returns canned build/test output
and a tiny non-zero cost so the spend circuit breaker is genuinely exercised.
Swapping it for :class:`~harness.adapters.executor.subprocess_executor.SubprocessExecutor`
is a one-line change in the composition root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from harness.config.models import ProjectConfig
from harness.ports.executor import ClaudeResult, CommandResult, WaveAssembly


class EchoExecutor:
    def __init__(self, *, claude_cost_usd: float = 0.01) -> None:
        self._calls = 0
        self._claude_cost_usd = claude_cost_usd

    def prepare_branch(self, *, project: ProjectConfig, branch: str) -> Path:
        # No git: hand back a deterministic stand-in worktree path. Build/test/publish
        # ignore it, but the loop still threads it through exactly as in production.
        return Path(f"/echo-worktree/{project.id}/{branch.replace('/', '-')}")

    def run_build(
        self, *, project: ProjectConfig, worktree: Optional[Path] = None
    ) -> CommandResult:
        return CommandResult(0, f"[echo] build {project.id} OK", "", 0.01)

    def run_test(
        self, *, project: ProjectConfig, worktree: Optional[Path] = None
    ) -> CommandResult:
        return CommandResult(0, f"[echo] tests {project.id} passed", "", 0.01)

    def publish_branch(
        self,
        *,
        project: ProjectConfig,
        branch: str,
        commit_message: str,
        worktree: Optional[Path] = None,
    ) -> CommandResult:
        return CommandResult(0, f"[echo] published {branch}", "", 0.01)

    def assemble_wave_branch(
        self,
        *,
        project: ProjectConfig,
        wave_branch: str,
        source_branches: list[str],
        base: str = "origin/main",
    ) -> WaveAssembly:
        # No git: pretend every source branch cherry-picked cleanly.
        return WaveAssembly(
            branch=wave_branch,
            head_sha="echo-sha",
            included=list(source_branches),
            skipped=[],
        )

    def run_claude_task(
        self,
        *,
        project: ProjectConfig,
        prompt: str,
        json_schema: Optional[dict[str, Any]] = None,
        worktree: Optional[Path] = None,
    ) -> ClaudeResult:
        self._calls += 1
        return ClaudeResult(
            result_text=f"[echo] handled: {prompt[:60]}",
            session_id=f"echo-{self._calls}",
            total_cost_usd=self._claude_cost_usd,
        )
