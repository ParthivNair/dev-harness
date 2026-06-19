"""SubprocessExecutor's git surface: prepare_branch (the clean trunk-rooted base)
and publish_branch (the only push path), plus their shared guardrails.

The structural-safety refusals (no trunk, no ambiguous branch) need no git. The
happy paths use a real temp repo + bare remote to prove the actual git plumbing:
a branch is cut from ``origin/main`` in an isolated worktree, and only the run's
own edit — never unrelated local dirt or a prior run's tip — reaches the branch.
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


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def _make_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A work repo on ``main`` with one commit, wired to a bare ``origin`` that
    already has ``main`` pushed — so ``origin/main`` exists to branch from."""
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
    _git(work, "push", "-u", "origin", "main")
    return work, bare


@pytest.mark.parametrize("bad", ["main", "master", "develop", "no-namespace"])
def test_publish_branch_refuses_protected_or_ambiguous(tmp_path: Path, bad: str) -> None:
    ex = SubprocessExecutor(project_root_resolver=lambda _pid: tmp_path)
    with pytest.raises(ExecutorError, match="refusing to publish"):
        ex.publish_branch(project=PROJECT, branch=bad, commit_message="m")


@pytest.mark.parametrize("bad", ["main", "master", "develop", "no-namespace"])
def test_prepare_branch_refuses_protected_or_ambiguous(tmp_path: Path, bad: str) -> None:
    ex = SubprocessExecutor(project_root_resolver=lambda _pid: tmp_path)
    with pytest.raises(ExecutorError, match="refusing to publish"):
        ex.prepare_branch(project=PROJECT, branch=bad)


@requires_git
def test_prepare_branch_cuts_from_origin_main_in_isolated_worktree(tmp_path: Path) -> None:
    work, _bare = _make_repo_with_remote(tmp_path)
    ex = SubprocessExecutor(project_root_resolver=lambda _pid: work)

    wt = ex.prepare_branch(project=PROJECT, branch="harness/win/issue-1")
    try:
        assert wt.exists() and wt != work  # an isolated worktree, NOT the live checkout
        # The worktree's branch points exactly at origin/main — a clean trunk base.
        head = _git(wt, "rev-parse", "HEAD").strip()
        origin_main = _git(work, "rev-parse", "origin/main").strip()
        assert head == origin_main
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=str(work))
        subprocess.run(["git", "branch", "-D", "harness/win/issue-1"], cwd=str(work))


@requires_git
def test_publish_commits_only_the_runs_edit_not_prior_tip_or_dirty_state(
    tmp_path: Path,
) -> None:
    # The bug this fixes: branches stacked on a prior run's tip, and a stray dirty
    # working-tree edit leaking into every branch via `git add -A` at HEAD. Prove
    # the worktree path cures both.
    work, bare = _make_repo_with_remote(tmp_path)
    ex = SubprocessExecutor(project_root_resolver=lambda _pid: work)

    # Pollute the live checkout the way a prior run would have: an extra LOCAL commit
    # that was never pushed to origin/main (the "prior branch tip")...
    (work / "stacked.txt").write_text("from a previous run, never on origin/main")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "prior run's local tip")
    # ...and an unrelated, uncommitted dirty file sitting in the working tree.
    (work / "dirty.txt").write_text("unstaged local junk")

    # Prepare cuts from origin/main into an isolated worktree, untouched by either.
    wt = ex.prepare_branch(project=PROJECT, branch="harness/win/issue-1")
    assert not (wt / "stacked.txt").exists()  # prior tip did not come along
    assert not (wt / "dirty.txt").exists()    # dirty live-checkout state did not either

    # The run makes its own edit in the worktree, then publishes.
    (wt / "feature.txt").write_text("the actual change")
    result = ex.publish_branch(
        project=PROJECT,
        branch="harness/win/issue-1",
        commit_message="harness: do it (#1)",
        worktree=wt,
    )
    assert result.ok

    # The branch reached the remote, and its diff against origin/main is ONLY the
    # run's own file — no prior tip, no dirty leak.
    listed = _git(work, "ls-remote", "--heads", str(bare), "harness/win/issue-1")
    assert "harness/win/issue-1" in listed
    diff = _git(work, "diff", "--name-only", "origin/main", "origin/harness/win/issue-1")
    changed = set(diff.split())
    assert changed == {"feature.txt"}
    assert "stacked.txt" not in changed and "dirty.txt" not in changed
    # The temp worktree + local branch were cleaned up after the push.
    assert not wt.exists()


@requires_git
def test_publish_branch_errors_when_nothing_changed(tmp_path: Path) -> None:
    work, _bare = _make_repo_with_remote(tmp_path)
    ex = SubprocessExecutor(project_root_resolver=lambda _pid: work)

    wt = ex.prepare_branch(project=PROJECT, branch="harness/win/issue-2")
    try:
        with pytest.raises(ExecutorError, match="nothing to publish"):
            ex.publish_branch(
                project=PROJECT, branch="harness/win/issue-2", commit_message="noop", worktree=wt
            )
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=str(work))
        subprocess.run(["git", "branch", "-D", "harness/win/issue-2"], cwd=str(work))
