"""`harness queue PROJECT` — the read-only board view, exercised through Typer.

Drives the real CLI command with ``CliRunner`` over a :class:`Container` wired to
``InMemoryGitHub`` (the production-default fake), so the command runs token-free
and the grouped/owner output is asserted exactly as an operator would see it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import harness.cli.main as main
from harness.adapters.github.fake import InMemoryGitHub
from harness.application import coordination as co
from tests.fakes import make_container

pytestmark = pytest.mark.integration

REPO = "acme/app"
runner = CliRunner()


def _seed(gh: InMemoryGitHub, *, title: str, labels: list[str]) -> int:
    return gh.create_issue(repo=REPO, title=title, body="x", labels=labels).number


def _run(monkeypatch, tmp_path: Path, gh: InMemoryGitHub, *args: str):
    c = make_container(tmp_path, repo=REPO, github=gh)
    monkeypatch.setattr(main, "build_container", lambda *a, **k: c)
    return runner.invoke(main.app, ["queue", "sample", *args])


def test_queue_groups_issues_by_state_with_owner(tmp_path: Path, monkeypatch) -> None:
    gh = InMemoryGitHub()
    n_queued = _seed(gh, title="Add queue command", labels=[co.QUEUED])
    n_inprog = _seed(gh, title="Wire scheduler", labels=[co.IN_PROGRESS, co.owner_label("box-2")])
    _seed(gh, title="Verify gate", labels=[co.NEEDS_VERIFICATION])
    _seed(gh, title="Open draft", labels=[co.PR_OPEN])
    _seed(gh, title="plain human ticket", labels=["bug"])  # no harness:<state> -> omitted

    result = _run(monkeypatch, tmp_path, gh)

    assert result.exit_code == 0, result.output
    out = result.output
    assert "queue: sample (acme/app)" in out
    assert "harness:queued (1)" in out
    assert "harness:in-progress (1)" in out
    assert "harness:needs-verification (1)" in out
    assert "harness:pr-open (1)" in out
    assert f"#{n_queued} Add queue command  [unclaimed]" in out
    assert f"#{n_inprog} Wire scheduler  [box-2]" in out
    # empty states and the unlabeled human ticket are not printed in the full board
    assert "harness:done" not in out
    assert "harness:blocked" not in out
    assert "plain human ticket" not in out


def test_queue_state_filter_and_zero_states(tmp_path: Path, monkeypatch) -> None:
    gh = InMemoryGitHub()
    _seed(gh, title="Add queue command", labels=[co.QUEUED])
    _seed(gh, title="Wire scheduler", labels=[co.IN_PROGRESS])

    # --state narrows to exactly one group.
    filtered = _run(monkeypatch, tmp_path, gh, "--state", "queued")
    assert filtered.exit_code == 0, filtered.output
    assert "harness:queued (1)" in filtered.output
    assert "harness:in-progress" not in filtered.output

    # A filtered state with no issues prints a clean zero-state line, not an error.
    empty_state = _run(monkeypatch, tmp_path, gh, "--state", "done")
    assert empty_state.exit_code == 0, empty_state.output
    assert "harness:done (0)" in empty_state.output
    assert "(none)" in empty_state.output

    # An empty repo prints a zero-state line for the whole board.
    empty_repo = _run(monkeypatch, tmp_path, InMemoryGitHub())
    assert empty_repo.exit_code == 0, empty_repo.output
    assert "(no harness-labeled issues)" in empty_repo.output

    # An unknown state name is a clean error, not a crash.
    bad = _run(monkeypatch, tmp_path, gh, "--state", "nonsense")
    assert bad.exit_code == 1
