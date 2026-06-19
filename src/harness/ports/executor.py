"""Executor port: runs the actual work and is the ONLY home of platform specifics.

It invokes Claude Code on a task and runs the project's build/test commands by
shelling out to the local toolchain. The engine sees only this Protocol and the
result dataclasses; it never branches on the OS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

# Avoid a hard import cycle at type-check time; ProjectConfig is only used for typing.
from harness.config.models import ProjectConfig


class ExecutorError(RuntimeError):
    """Raised by an Executor when the work itself fails in a way the engine should
    react to (e.g. a guarded ``publish_branch`` refuses a branch, finds nothing to
    commit, or a git command errors). Part of the port contract so the engine can
    catch it without importing an adapter."""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class ClaudeResult:
    result_text: str
    session_id: Optional[str]
    total_cost_usd: float  # feeds the spend circuit breaker
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Executor(Protocol):
    def run_claude_task(
        self,
        *,
        project: ProjectConfig,
        prompt: str,
        json_schema: Optional[dict[str, Any]] = None,
    ) -> ClaudeResult:
        """Invoke Claude Code non-interactively; return its result + cost."""

    def run_build(self, *, project: ProjectConfig) -> CommandResult: ...

    def run_test(self, *, project: ProjectConfig) -> CommandResult: ...

    def publish_branch(
        self, *, project: ProjectConfig, branch: str, commit_message: str
    ) -> CommandResult:
        """Commit the working tree and push it to a FEATURE branch so a draft PR can
        reference it. The autonomous path needs this, but it is deliberately narrow:
        implementations MUST refuse ``main``/``master`` and MUST NOT force-push. The
        GitHubAdapter still has no merge/push — only this guarded feature-branch push
        exists, and a human still merges."""
