"""SubprocessExecutor: the real Executor — the only place platform specifics live.

Invokes the ``claude`` CLI non-interactively and runs each project's build/test
commands by shelling out (``shell=False`` for cross-platform safety). The
``claude -p --output-format json`` response carries ``total_cost_usd`` and token
counts, which flow back to the spend circuit breaker via the loop.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from harness.config.models import ProjectConfig
from harness.ports.executor import ClaudeResult, CommandResult, WaveAssembly


class ExecutorError(RuntimeError):
    pass


# Branches the harness must never publish onto. The autonomous path may push a
# namespaced feature branch only — never a trunk, never a force-push.
_PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "develop", "HEAD"})


class SubprocessExecutor:
    def __init__(
        self,
        *,
        project_root_resolver: Callable[[str], Path],
        claude_bin: str = "claude",
    ) -> None:
        self._root_of = project_root_resolver
        self._claude = claude_bin

    @staticmethod
    def _as_argv(command: list[str] | str) -> list[str]:
        return command if isinstance(command, list) else command.split()

    @staticmethod
    def _guard_feature_branch(branch: str) -> None:
        """Structural guardrail: a namespaced feature branch ONLY. Refuse trunks and
        anything not clearly a feature branch; never force-push anywhere."""
        if branch in _PROTECTED_BRANCHES or "/" not in branch:
            raise ExecutorError(
                f"refusing to publish '{branch}': only namespaced feature branches "
                f"(e.g. 'harness/<instance>/issue-N') may be pushed, never a trunk"
            )

    def _run(self, argv: list[str], cwd: Path) -> CommandResult:
        start = time.monotonic()
        proc = subprocess.run(
            argv, cwd=str(cwd), shell=False, capture_output=True, text=True
        )
        return CommandResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_s=time.monotonic() - start,
        )

    def _cwd(self, project: ProjectConfig, worktree: Optional[Path] = None) -> Path:
        # An isolated run operates in its prepared worktree; otherwise the project root.
        root = Path(worktree) if worktree is not None else self._root_of(project.id)
        return root / project.commands.cwd

    @staticmethod
    def _worktree_path(project: ProjectConfig, branch: str) -> Path:
        """A stable on-disk location for a run's isolated worktree. Stable (not a
        random temp dir) so a run suspended at the verify gate can resume in a fresh
        process and still find the worktree it prepared."""
        safe = branch.replace("/", "-").replace("\\", "-")
        return Path(tempfile.gettempdir()) / "harness-worktrees" / project.id / safe

    def prepare_branch(self, *, project: ProjectConfig, branch: str) -> Path:
        # Same push guard: refuse trunks / ambiguous names before touching git.
        self._guard_feature_branch(branch)
        repo_root = self._root_of(project.id)
        self._git(["fetch", "origin"], repo_root)
        wt = self._worktree_path(project, branch)
        wt.parent.mkdir(parents=True, exist_ok=True)
        # Idempotent: clear any stale worktree/branch from a crashed prior attempt so a
        # re-run (or at-least-once step retry) doesn't wedge on "already exists" (A6).
        self._run(["git", "worktree", "remove", "--force", str(wt)], repo_root)
        self._run(["git", "branch", "-D", branch], repo_root)
        # Cut the feature branch from the TRUNK as published, in an isolated worktree —
        # never the human's live checkout, so dirty/concurrent local state cannot leak
        # in and successive runs are not stacked on each other's tips.
        self._git(["worktree", "add", "-B", branch, str(wt), "origin/main"], repo_root)
        return wt

    def run_build(
        self, *, project: ProjectConfig, worktree: Optional[Path] = None
    ) -> CommandResult:
        return self._run(self._as_argv(project.commands.build), self._cwd(project, worktree))

    def run_test(
        self, *, project: ProjectConfig, worktree: Optional[Path] = None
    ) -> CommandResult:
        return self._run(self._as_argv(project.commands.test), self._cwd(project, worktree))

    def publish_branch(
        self,
        *,
        project: ProjectConfig,
        branch: str,
        commit_message: str,
        worktree: Optional[Path] = None,
    ) -> CommandResult:
        self._guard_feature_branch(branch)
        cwd = Path(worktree) if worktree is not None else self._cwd(project)
        # The branch was already cut from origin/main by prepare_branch and is checked
        # out HERE; we only commit the run's edits and push. The worktree began clean,
        # so `add -A` stages exactly this run's changes (no blanket HEAD checkout, no
        # leak of unrelated working-tree dirt). Push WITHOUT --force; a human merges.
        self._git(["add", "-A"], cwd)
        commit = self._run(["git", "commit", "-m", commit_message], cwd)
        if not commit.ok and "nothing to commit" in (commit.stdout + commit.stderr).lower():
            raise ExecutorError("nothing to publish: the task produced no file changes")
        if not commit.ok:
            raise ExecutorError(f"git commit failed ({commit.exit_code}): {commit.stderr[:300]}")
        pushed = self._git(["push", "-u", "origin", branch], cwd)
        if worktree is not None:
            # The branch now lives on origin; detach the temp worktree and drop the
            # local ref so it can't wedge the next run (mirrors assemble_wave_branch).
            repo_root = self._root_of(project.id)
            self._run(["git", "worktree", "remove", "--force", str(worktree)], repo_root)
            self._run(["git", "branch", "-D", branch], repo_root)
        return pushed

    def assemble_wave_branch(
        self,
        *,
        project: ProjectConfig,
        wave_branch: str,
        source_branches: list[str],
        base: str = "origin/main",
    ) -> WaveAssembly:
        # Same push guard as publish_branch: a namespaced feature branch ONLY.
        self._guard_feature_branch(wave_branch)
        repo_root = self._cwd(project)
        self._git(["fetch", "origin"], repo_root)

        # Do the assembly in a throwaway worktree so the project's working tree and
        # checked-out branch are never disturbed (cherry-picks happen off in temp).
        with tempfile.TemporaryDirectory(prefix="harness-wave-") as tmp:
            wt = Path(tmp) / "wt"
            # Pre-clean any stale local ref so a retry after a crashed run is idempotent
            # (the temp worktree is gone but the branch could survive — A6).
            self._run(["git", "worktree", "remove", "--force", str(wt)], repo_root)
            self._run(["git", "branch", "-D", wave_branch], repo_root)
            # Create the wave branch at `base` and check it out in the temp worktree.
            self._git(["worktree", "add", "-b", wave_branch, str(wt), base], repo_root)
            try:
                included, skipped = self._cherry_pick_branches(
                    wt, base=base, source_branches=source_branches
                )
                # Guarded push (no --force); a human still merges the draft PR.
                self._git(["push", "-u", "origin", wave_branch], wt)
                head = self._git(["rev-parse", "HEAD"], wt).stdout.strip()
            finally:
                # Always detach the temp worktree AND delete the local branch — the
                # branch now lives in origin, and a surviving local ref would wedge the
                # next attempt at "branch already exists" (A6).
                self._run(["git", "worktree", "remove", "--force", str(wt)], repo_root)
                self._run(["git", "branch", "-D", wave_branch], repo_root)
        return WaveAssembly(
            branch=wave_branch, head_sha=head, included=included, skipped=skipped
        )

    def _cherry_pick_branches(
        self, wt: Path, *, base: str, source_branches: list[str]
    ) -> tuple[list[str], list[str]]:
        """Cherry-pick each source branch's own commits onto the wave branch, one
        commit at a time, in the given order.

        Picking ``base..branch`` as a single range double-applies the commits a
        STACKED branch shares with an earlier source (issue-13 ⊂ issue-5 ⊂ …), which
        conflicts and drops everything but the first branch. Instead we replay
        ``rev-list --reverse base..branch`` commit-by-commit: a commit already on the
        wave branch cherry-picks empty (``--skip``), a genuinely new one applies, and
        only a real content conflict aborts THIS branch (recorded in ``skipped``) while
        leaving the wave intact for the rest. Correct for both independent (post-#18)
        and stacked branches."""
        included: list[str] = []
        skipped: list[str] = []
        for branch in source_branches:
            commits = self._git(
                ["rev-list", "--reverse", f"{base}..{branch}"], wt
            ).stdout.split()
            conflicted = False
            for sha in commits:
                pick = self._run(["git", "cherry-pick", sha], wt)
                if pick.ok:
                    continue
                combined = (pick.stdout + pick.stderr).lower()
                # An empty pick = the commit is already applied (a shared ancestor of a
                # stacked branch). Skip it and keep replaying the branch.
                if "empty" in combined or "nothing to commit" in combined:
                    self._run(["git", "cherry-pick", "--skip"], wt)
                    continue
                # A real conflict: abandon just this branch, recover the wave tree, and
                # move on so the remaining branches can still aggregate.
                self._run(["git", "cherry-pick", "--abort"], wt)
                conflicted = True
                break
            if conflicted:
                skipped.append(branch)
            else:
                included.append(branch)
        return included, skipped

    def _git(self, args: list[str], cwd: Path) -> CommandResult:
        result = self._run(["git", *args], cwd)
        if not result.ok:
            raise ExecutorError(f"git {args[0]} failed ({result.exit_code}): {result.stderr[:300]}")
        return result

    def run_claude_task(
        self,
        *,
        project: ProjectConfig,
        prompt: str,
        json_schema: Optional[dict[str, Any]] = None,
        worktree: Optional[Path] = None,
    ) -> ClaudeResult:
        c = project.claude
        argv = [
            self._claude,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            c.model,
            "--permission-mode",
            c.permission_mode,
        ]
        for tool in c.allowed_tools:
            argv += ["--allowedTools", tool]
        for tool in c.disallowed_tools:
            argv += ["--disallowedTools", tool]
        for extra_dir in c.add_dirs:
            argv += ["--add-dir", extra_dir]
        if json_schema is not None:
            argv += ["--json-schema", json.dumps(json_schema)]

        cwd = Path(worktree) if worktree is not None else self._root_of(project.id)
        result = self._run(argv, cwd)
        if not result.ok:
            raise ExecutorError(
                f"claude exited {result.exit_code}: {result.stderr[:500]}"
            )
        payload = json.loads(result.stdout)
        usage = payload.get("usage", {})
        return ClaudeResult(
            result_text=payload.get("result", ""),
            session_id=payload.get("session_id"),
            total_cost_usd=float(payload.get("total_cost_usd", 0.0)),
            input_tokens=int(usage.get("input_tokens", payload.get("input_tokens", 0))),
            output_tokens=int(usage.get("output_tokens", payload.get("output_tokens", 0))),
            raw=payload,
        )
