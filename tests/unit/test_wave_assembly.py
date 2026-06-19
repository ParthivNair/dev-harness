"""assemble_wave_branch — aggregating a wave's per-issue branches onto one branch.

The echo stub proves the port contract with no git. The subprocess tests use a
real temp repo + bare remote (mirroring test_subprocess_executor) to prove the
actual cherry-pick plumbing, including conflict-skip.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harness.adapters.executor.echo import EchoExecutor
from harness.adapters.executor.subprocess_executor import ExecutorError, SubprocessExecutor
from harness.config.models import ProjectConfig

PROJECT = ProjectConfig(id="p", owner_instance="x")

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def test_echo_assemble_wave_branch_stubs_all_included() -> None:
    ex = EchoExecutor()
    wave = ex.assemble_wave_branch(
        project=PROJECT,
        wave_branch="harness/win/wave-1",
        source_branches=["harness/win/issue-1", "harness/win/issue-2"],
    )
    assert wave.branch == "harness/win/wave-1"
    assert wave.head_sha == "echo-sha"
    assert wave.included == ["harness/win/issue-1", "harness/win/issue-2"]
    assert wave.skipped == []


@pytest.mark.parametrize("bad", ["main", "master", "develop", "no-namespace"])
def test_assemble_wave_branch_refuses_protected_or_ambiguous(tmp_path: Path, bad: str) -> None:
    ex = SubprocessExecutor(project_root_resolver=lambda _pid: tmp_path)
    with pytest.raises(ExecutorError, match="refusing to publish"):
        ex.assemble_wave_branch(project=PROJECT, wave_branch=bad, source_branches=[])


def _seed_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A work tree on `main` with one initial commit, wired to a bare origin."""
    work = tmp_path / "work"
    work.mkdir()
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Tester")
    (work / "README.md").write_text("init\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work, bare


@requires_git
def test_assemble_wave_branch_cherry_picks_independent_branches(tmp_path: Path) -> None:
    work, bare = _seed_repo_with_remote(tmp_path)
    # Two independent feature branches off main, each touching a different file.
    _git(work, "checkout", "-b", "harness/win/issue-1", "main")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "add a")
    _git(work, "push", "-u", "origin", "harness/win/issue-1")

    _git(work, "checkout", "-b", "harness/win/issue-2", "main")
    (work / "b.txt").write_text("b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "add b")
    _git(work, "push", "-u", "origin", "harness/win/issue-2")
    _git(work, "checkout", "main")

    ex = SubprocessExecutor(project_root_resolver=lambda _pid: work)
    wave = ex.assemble_wave_branch(
        project=PROJECT,
        wave_branch="harness/win/wave-1",
        source_branches=["harness/win/issue-1", "harness/win/issue-2"],
        base="origin/main",
    )
    assert wave.included == ["harness/win/issue-1", "harness/win/issue-2"]
    assert wave.skipped == []
    assert wave.head_sha  # a real SHA
    # the wave branch reached the remote with BOTH changes aggregated
    listed = subprocess.run(
        ["git", "-C", str(bare), "branch", "--list", "harness/win/wave-1"],
        capture_output=True, text=True,
    )
    assert "harness/win/wave-1" in listed.stdout
    show = subprocess.run(
        ["git", "-C", str(bare), "ls-tree", "-r", "--name-only", "harness/win/wave-1"],
        capture_output=True, text=True,
    )
    assert "a.txt" in show.stdout and "b.txt" in show.stdout


@requires_git
def test_assemble_wave_branch_skips_conflicting_branch(tmp_path: Path) -> None:
    work, bare = _seed_repo_with_remote(tmp_path)
    # Both branches edit the SAME file from the same base => the second conflicts.
    _git(work, "checkout", "-b", "harness/win/issue-1", "main")
    (work / "shared.txt").write_text("from one\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "one edits shared")
    _git(work, "push", "-u", "origin", "harness/win/issue-1")

    _git(work, "checkout", "-b", "harness/win/issue-2", "main")
    (work / "shared.txt").write_text("from two\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "two edits shared")
    _git(work, "push", "-u", "origin", "harness/win/issue-2")
    _git(work, "checkout", "main")

    ex = SubprocessExecutor(project_root_resolver=lambda _pid: work)
    wave = ex.assemble_wave_branch(
        project=PROJECT,
        wave_branch="harness/win/wave-2",
        source_branches=["harness/win/issue-1", "harness/win/issue-2"],
        base="origin/main",
    )
    # first cherry-picks clean; second conflicts and is recorded as skipped
    assert wave.included == ["harness/win/issue-1"]
    assert wave.skipped == ["harness/win/issue-2"]
    # the conflict-skip left a clean wave (the abort recovered the working tree)
    listed = subprocess.run(
        ["git", "-C", str(bare), "branch", "--list", "harness/win/wave-2"],
        capture_output=True, text=True,
    )
    assert "harness/win/wave-2" in listed.stdout


@requires_git
def test_a4_assemble_stacked_branches_applies_each_change_exactly_once(tmp_path: Path) -> None:
    # A4: branch-2 is STACKED on branch-1 (issue-2 was cut from issue-1, not main), so
    # `main..issue-2` includes issue-1's commit too. The old whole-range cherry-pick
    # double-applied that shared commit and conflicted, dropping issue-2. Replaying
    # commit-by-commit skips the already-applied ancestor and applies only the new one.
    work, bare = _seed_repo_with_remote(tmp_path)
    _git(work, "checkout", "-b", "harness/win/issue-1", "main")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "add a")
    _git(work, "push", "-u", "origin", "harness/win/issue-1")
    # issue-2 is cut from issue-1 (stacked), not from main.
    _git(work, "checkout", "-b", "harness/win/issue-2", "harness/win/issue-1")
    (work / "b.txt").write_text("b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "add b on top of a")
    _git(work, "push", "-u", "origin", "harness/win/issue-2")
    _git(work, "checkout", "main")

    ex = SubprocessExecutor(project_root_resolver=lambda _pid: work)
    wave = ex.assemble_wave_branch(
        project=PROJECT,
        wave_branch="harness/win/wave-stacked",
        source_branches=["harness/win/issue-1", "harness/win/issue-2"],
        base="origin/main",
    )
    # Both branches aggregated cleanly — no spurious conflict from the shared ancestor.
    assert wave.included == ["harness/win/issue-1", "harness/win/issue-2"]
    assert wave.skipped == []
    # The wave tree carries BOTH changes...
    tree = subprocess.run(
        ["git", "-C", str(bare), "ls-tree", "-r", "--name-only", "harness/win/wave-stacked"],
        capture_output=True, text=True,
    )
    assert "a.txt" in tree.stdout and "b.txt" in tree.stdout
    # ...and "add a" appears EXACTLY ONCE (the shared ancestor was not double-applied).
    # NB: in the BARE remote the trunk ref is just `main` (no `origin/` remote-tracking).
    log = subprocess.run(
        ["git", "-C", str(bare), "log", "--oneline", "main..harness/win/wave-stacked"],
        capture_output=True, text=True,
    )
    assert log.stdout.count("add a") == 1
    assert log.stdout.count("add b on top of a") == 1


@requires_git
def test_a6_assemble_is_idempotent_when_the_local_wave_branch_survives(tmp_path: Path) -> None:
    # A6: a crashed prior run can leave the local wave branch behind (the temp worktree
    # is gone but the ref survives), wedging a retry at "branch already exists". The
    # pre-clean + finally-delete make a second assemble of the SAME wave branch succeed.
    work, bare = _seed_repo_with_remote(tmp_path)
    _git(work, "checkout", "-b", "harness/win/issue-1", "main")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "add a")
    _git(work, "push", "-u", "origin", "harness/win/issue-1")
    _git(work, "checkout", "main")

    # Simulate a stale local ref left over from a crashed prior attempt.
    _git(work, "branch", "harness/win/wave-1", "main")

    ex = SubprocessExecutor(project_root_resolver=lambda _pid: work)
    # The assemble must NOT fail with "branch already exists" despite the stale local
    # ref — the pre-clean drops it before `worktree add`, so the op is idempotent.
    wave = ex.assemble_wave_branch(
        project=PROJECT,
        wave_branch="harness/win/wave-1",
        source_branches=["harness/win/issue-1"],
        base="origin/main",
    )
    assert wave.included == ["harness/win/issue-1"]
    # The finally-block also deletes the local ref so the NEXT attempt starts clean too
    # (no leftover to wedge a future retry).
    refs = subprocess.run(
        ["git", "-C", str(work), "branch", "--list", "harness/win/wave-1"],
        capture_output=True, text=True,
    )
    assert refs.stdout.strip() == ""
