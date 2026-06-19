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

    def _cwd(self, project: ProjectConfig) -> Path:
        return self._root_of(project.id) / project.commands.cwd

    def run_build(self, *, project: ProjectConfig) -> CommandResult:
        return self._run(self._as_argv(project.commands.build), self._cwd(project))

    def run_test(self, *, project: ProjectConfig) -> CommandResult:
        return self._run(self._as_argv(project.commands.test), self._cwd(project))

    def publish_branch(
        self, *, project: ProjectConfig, branch: str, commit_message: str
    ) -> CommandResult:
        self._guard_feature_branch(branch)
        cwd = self._cwd(project)
        # Create/reset the feature branch at HEAD (working-tree edits are preserved),
        # commit everything, and push WITHOUT --force. A human still merges.
        self._git(["checkout", "-B", branch], cwd)
        self._git(["add", "-A"], cwd)
        commit = self._run(["git", "commit", "-m", commit_message], cwd)
        if not commit.ok and "nothing to commit" in (commit.stdout + commit.stderr).lower():
            raise ExecutorError("nothing to publish: the task produced no file changes")
        if not commit.ok:
            raise ExecutorError(f"git commit failed ({commit.exit_code}): {commit.stderr[:300]}")
        return self._git(["push", "-u", "origin", branch], cwd)

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

        result = self._run(argv, self._root_of(project.id))
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
