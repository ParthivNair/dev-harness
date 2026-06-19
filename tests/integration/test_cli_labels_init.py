"""`harness labels-init PROJECT` — bootstrap the harness:* label set, via Typer.

Drives the real CLI command with ``CliRunner`` over a :class:`Container` wired to
``InMemoryGitHub`` (the production-default fake), so the command runs token-free.
Asserts a fresh repo gets every state label, a re-run is a clean no-op, and an
unowned project is refused — exactly as an operator would see it.
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


def _run(monkeypatch, c, *args: str):
    monkeypatch.setattr(main, "build_container", lambda *a, **k: c)
    return runner.invoke(main.app, ["labels-init", "sample", *args])


def test_labels_init_creates_then_no_ops(tmp_path: Path, monkeypatch) -> None:
    gh = InMemoryGitHub()
    c = make_container(tmp_path, repo=REPO, github=gh)

    first = _run(monkeypatch, c)
    assert first.exit_code == 0, first.output
    assert f"created {len(co.STATE_LABELS)} label(s)" in first.output
    assert gh._labels[REPO] == set(co.STATE_LABELS)

    # Re-running creates nothing and does not error.
    second = _run(monkeypatch, c)
    assert second.exit_code == 0, second.output
    assert "all harness labels already present" in second.output
    assert gh._labels[REPO] == set(co.STATE_LABELS)


def test_labels_init_refuses_unowned_project(tmp_path: Path, monkeypatch) -> None:
    gh = InMemoryGitHub()
    c = make_container(
        tmp_path, instance="this-machine", owner="other-machine", repo=REPO, github=gh
    )
    result = _run(monkeypatch, c)
    assert result.exit_code == 1
    assert "refusing" in result.output
    assert gh._labels == {}  # refused before any write
