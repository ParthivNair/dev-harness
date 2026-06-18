"""SubprocessExecutor: the real Executor — the only place platform specifics live.

Invokes the ``claude`` CLI non-interactively and runs each project's build/test
commands by shelling out (``shell=False`` for cross-platform safety). The
``claude -p --output-format json`` response carries ``total_cost_usd`` and token
counts, which flow back to the spend circuit breaker via the loop.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from harness.config.models import ProjectConfig
from harness.ports.executor import ClaudeResult, CommandResult


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
        # Structural guardrail: a namespaced feature branch ONLY. Refuse trunks and
        # anything not clearly a feature branch; never force-push anywhere.
        if branch in _PROTECTED_BRANCHES or "/" not in branch:
            raise ExecutorError(
                f"refusing to publish '{branch}': only namespaced feature branches "
                f"(e.g. 'harness/<instance>/issue-N') may be pushed, never a trunk"
            )
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
