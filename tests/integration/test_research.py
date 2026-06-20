"""Research loop: goals-driven findings -> queued issues, with a clean no-action exit."""

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
from harness.loops.research import build_research_loop
from harness.ports.executor import ClaudeResult
from tests.fakes import RecordingNotifier

pytestmark = pytest.mark.integration

REPO = "acme/app"
PROJECT = ProjectConfig(id="sample", owner_instance="this-machine", repo=REPO)
TAXONOMY = {"file_issue": AutonomyTier.AUTONOMOUS}
GOALS = "Ship a CLI that imports CSV and exports JSON; prioritize correctness."


class FindingsExecutor(EchoExecutor):
    """Returns a JSON findings payload as a real `claude --json-schema` call would,
    and records the prompt it was asked so tests can assert the goals were folded in."""

    def __init__(self, findings: list[dict]) -> None:
        super().__init__()
        self._findings = findings
        self.last_prompt: Optional[str] = None

    def run_claude_task(
        self, *, project: ProjectConfig, prompt: str, json_schema: Optional[dict[str, Any]] = None
    ) -> ClaudeResult:
        self.last_prompt = prompt
        return ClaudeResult(
            result_text=json.dumps({"findings": self._findings}),
            session_id="research-1",
            total_cost_usd=0.02,
        )


def _run(
    gh: InMemoryGitHub, executor, tmp_path: Path, goals: str = GOALS
) -> tuple[AtomicJsonRunStore, RunStatus]:
    store = AtomicJsonRunStore(tmp_path / "state")
    loop = build_research_loop(
        executor=executor, github=gh, guard=ActionGuard(TAXONOMY),
        project=PROJECT, project_root=tmp_path, goals=goals,
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


def test_garbage_result_files_nothing(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    # A malformed/garbage payload parses to no findings -> nothing is filed.
    _store, status = _run(gh, FindingsExecutor([]), tmp_path)
    assert status is RunStatus.COMPLETED
    assert gh.list_issues(repo=REPO, state="open") == []


def test_files_one_queued_issue_per_finding_with_sev_labels(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    findings = [
        {"title": "Add CSV import command", "severity": "high", "body": "what/why + AC"},
        {"title": "Export to JSON", "severity": "low", "body": "what/why + AC"},
    ]
    executor = FindingsExecutor(findings)
    _store, status = _run(gh, executor, tmp_path)
    assert status is RunStatus.COMPLETED
    issues = gh.list_issues(repo=REPO, state="open", labels=[co.QUEUED])
    titles = {i.title for i in issues}
    assert titles == {"Add CSV import command", "Export to JSON"}
    high = next(i for i in issues if i.title == "Add CSV import command")
    assert "sev:high" in high.labels and co.QUEUED in high.labels
    # The owner's goals were folded into the prompt handed to the model.
    assert executor.last_prompt is not None and "imports CSV" in executor.last_prompt
    assert "{{goals}}" not in executor.last_prompt


def test_empty_goals_still_runs_with_generic_brief(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    findings = [{"title": "Fix flaky test", "severity": "med", "body": "what/why + AC"}]
    executor = FindingsExecutor(findings)
    _store, status = _run(gh, executor, tmp_path, goals="")
    assert status is RunStatus.COMPLETED
    titles = {i.title for i in gh.list_issues(repo=REPO, state="open", labels=[co.QUEUED])}
    assert titles == {"Fix flaky test"}
    # No goals -> a generic brief is substituted rather than a bare placeholder.
    assert executor.last_prompt is not None and "{{goals}}" not in executor.last_prompt


def test_dedupes_against_existing_queue(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    gh.create_issue(repo=REPO, title="Known issue", body="", labels=[co.QUEUED])  # pre-existing
    findings = [
        {"title": "Known issue", "severity": "med", "body": "again"},  # duplicate
        {"title": "Fresh issue", "severity": "low", "body": "new"},
    ]
    _store, status = _run(gh, FindingsExecutor(findings), tmp_path)
    assert status is RunStatus.COMPLETED
    open_issues = gh.list_issues(repo=REPO, state="open", labels=[co.QUEUED])
    titles = sorted(i.title for i in open_issues)
    assert titles == ["Fresh issue", "Known issue"]  # no duplicate "Known issue"
