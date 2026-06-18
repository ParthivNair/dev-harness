"""Arch-review loop: structured-findings -> issues, with a clean no-action exit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from harness.adapters.executor.echo import EchoExecutor
from harness.adapters.github.fake import InMemoryGitHub
from harness.adapters.state.json_store import AtomicJsonRunStore
from harness.application import coordination as co
from harness.application.action_guard import ActionGuard
from harness.application.loop_runner import LoopRunner
from harness.config.models import AutonomyTier, ProjectConfig
from harness.domain.models import RunStatus
from harness.loops.arch_review import build_arch_review_loop
from harness.ports.executor import ClaudeResult
from tests.fakes import RecordingNotifier

pytestmark = pytest.mark.integration

REPO = "acme/app"
PROJECT = ProjectConfig(id="sample", owner_instance="this-machine", repo=REPO)
TAXONOMY = {"file_issue": AutonomyTier.AUTONOMOUS}


class FindingsExecutor(EchoExecutor):
    """Returns a JSON findings payload as a real `claude --json-schema` call would."""

    def __init__(self, findings: list[dict]) -> None:
        super().__init__()
        self._findings = findings

    def run_claude_task(
        self, *, project: ProjectConfig, prompt: str, json_schema: Optional[dict[str, Any]] = None
    ) -> ClaudeResult:
        return ClaudeResult(
            result_text=json.dumps({"findings": self._findings}),
            session_id="arch-1",
            total_cost_usd=0.02,
        )


def _run(gh: InMemoryGitHub, executor, tmp_path: Path) -> tuple[AtomicJsonRunStore, RunStatus]:
    store = AtomicJsonRunStore(tmp_path / "state")
    loop = build_arch_review_loop(
        executor=executor, github=gh, guard=ActionGuard(TAXONOMY),
        project=PROJECT, project_root=tmp_path,
    )
    runner = LoopRunner(loop, store, RecordingNotifier())
    run_id = runner.create_run(project_id="sample").run_id
    return store, runner.run(run_id)


def test_no_findings_exits_clean(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    # EchoExecutor returns non-JSON text -> parsed as zero findings -> clean exit.
    _store, status = _run(gh, EchoExecutor(), tmp_path)
    assert status is RunStatus.COMPLETED
    assert gh.list_issues(repo=REPO, state="open") == []


def test_files_one_issue_per_finding_with_queue_labels(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    findings = [
        {"title": "God object in loop_runner", "severity": "high", "rationale": "too big"},
        {"title": "No SQLite store", "severity": "low", "rationale": "json only"},
    ]
    _store, status = _run(gh, FindingsExecutor(findings), tmp_path)
    assert status is RunStatus.COMPLETED
    issues = gh.list_issues(repo=REPO, state="open", labels=[co.QUEUED])
    titles = {i.title for i in issues}
    assert titles == {"God object in loop_runner", "No SQLite store"}
    high = next(i for i in issues if i.title == "God object in loop_runner")
    assert "sev:high" in high.labels and co.QUEUED in high.labels


def test_dedupes_against_existing_queue(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    gh.create_issue(repo=REPO, title="Known issue", body="", labels=[co.QUEUED])  # pre-existing
    findings = [
        {"title": "Known issue", "severity": "med", "rationale": "again"},  # duplicate
        {"title": "Fresh issue", "severity": "low", "rationale": "new"},
    ]
    _store, status = _run(gh, FindingsExecutor(findings), tmp_path)
    assert status is RunStatus.COMPLETED
    open_issues = gh.list_issues(repo=REPO, state="open", labels=[co.QUEUED])
    titles = sorted(i.title for i in open_issues)
    assert titles == ["Fresh issue", "Known issue"]  # no duplicate "Known issue"
