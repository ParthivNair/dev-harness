"""SubprocessExecutor.publish_branch — the only push path, and its guardrails.

The structural-safety refusals (no trunk, no ambiguous branch) need no git. The
happy path uses a real temp repo + bare remote to prove the actual git plumbing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harness.adapters.executor.subprocess_executor import ExecutorError, SubprocessExecutor
from harness.config.models import ProjectConfig

PROJECT = ProjectConfig(id="p", owner_instance="x")

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.mark.parametrize("bad", ["main", "master", "develop", "no-namespace"])
def test_publish_branch_refuses_protected_or_ambiguous(tmp_path: Path, bad: str) -> None:
    ex = SubprocessExecutor(project_root_resolver=lambda _pid: tmp_path)
    with pytest.raises(ExecutorError, match="refusing to publish"):
        ex.publish_branch(project=PROJECT, branch=bad, commit_message="m")


@requires_git
def test_publish_branch_commits_and_pushes_feature_branch(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Tester")
    (work / "README.md").write_text("init")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    # an uncommitted change, as Claude would have left in the working tree
    (work / "feature.txt").write_text("the change")

    ex = SubprocessExecutor(project_root_resolver=lambda _pid: work)
    result = ex.publish_branch(
        project=PROJECT, branch="harness/win/issue-1", commit_message="harness: do it (#1)"
    )
    assert result.ok
    listed = subprocess.run(
        ["git", "-C", str(bare), "branch", "--list", "harness/win/issue-1"],
        capture_output=True, text=True,
    )
    assert "harness/win/issue-1" in listed.stdout  # the feature branch reached the remote


@requires_git
def test_publish_branch_errors_when_nothing_changed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Tester")
    (work / "README.md").write_text("init")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))

    ex = SubprocessExecutor(project_root_resolver=lambda _pid: work)
    with pytest.raises(ExecutorError, match="nothing to publish"):
        ex.publish_branch(project=PROJECT, branch="harness/win/issue-2", commit_message="noop")
