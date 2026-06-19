"""Executor port: runs the actual work and is the ONLY home of platform specifics.

It invokes Claude Code on a task and runs the project's build/test commands by
shelling out to the local toolchain. The engine sees only this Protocol and the
result dataclasses; it never branches on the OS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

# Avoid a hard import cycle at type-check time; ProjectConfig is only used for typing.
from harness.config.models import ProjectConfig


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
class WaveAssembly:
    """Result of aggregating a wave's per-issue branches onto one wave branch.

    ``included``/``skipped`` are the source branch names that cherry-picked cleanly
    vs. those dropped on conflict, so the overseer can report what made the PR.
    """

    branch: str
    head_sha: str
    included: list[str]
    skipped: list[str]


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
    def prepare_branch(self, *, project: ProjectConfig, branch: str) -> Path:
        """Establish a clean, trunk-rooted base for a run BEFORE any editing.

        Fetch ``origin`` and create ``branch`` from ``origin/main`` inside a fresh,
        ISOLATED ``git worktree`` (never the human's live checkout), so dirty or
        concurrent local state cannot leak into the branch and successive runs are
        not stacked on each other's tips. Returns the worktree path; the run's
        ``run_claude_task``/``run_build``/``run_test``/``publish_branch`` calls then
        operate in that worktree. Same guard as the push paths: a namespaced feature
        branch ONLY, never a trunk."""

    def run_claude_task(
        self,
        *,
        project: ProjectConfig,
        prompt: str,
        json_schema: Optional[dict[str, Any]] = None,
        worktree: Optional[Path] = None,
    ) -> ClaudeResult:
        """Invoke Claude Code non-interactively; return its result + cost. When
        ``worktree`` is given the agent edits there (the prepared feature branch)
        instead of the project root."""

    def run_build(
        self, *, project: ProjectConfig, worktree: Optional[Path] = None
    ) -> CommandResult: ...

    def run_test(
        self, *, project: ProjectConfig, worktree: Optional[Path] = None
    ) -> CommandResult: ...

    def publish_branch(
        self,
        *,
        project: ProjectConfig,
        branch: str,
        commit_message: str,
        worktree: Optional[Path] = None,
    ) -> CommandResult:
        """Commit the run's edits and push the ALREADY-PREPARED ``branch`` so a draft
        PR can reference it. ``branch`` must have been created by
        :meth:`prepare_branch` (from ``origin/main`` in ``worktree``); this method no
        longer cuts the branch from an arbitrary HEAD. It is deliberately narrow:
        implementations MUST refuse ``main``/``master`` and MUST NOT force-push. The
        GitHubAdapter still has no merge/push — only this guarded feature-branch push
        exists, and a human still merges."""

    def assemble_wave_branch(
        self,
        *,
        project: ProjectConfig,
        wave_branch: str,
        source_branches: list[str],
        base: str = "origin/main",
    ) -> WaveAssembly:
        """Aggregate one wave's per-issue ``source_branches`` onto ``wave_branch``.

        Branch ``wave_branch`` from ``base`` and cherry-pick each source branch's
        ``base..<branch>`` commits onto it, skipping (and recording) any branch that
        conflicts, then push the wave branch so a single draft PR can reference it.
        Like ``publish_branch`` this is a guarded namespaced-branch push only — never
        a trunk, never a force-push, and the GitHubAdapter still has no merge."""
