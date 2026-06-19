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
from harness.config.models import AutonomyTier, ProjectConfig, ProjectScheduling
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


class NonJsonExecutor(EchoExecutor):
    """Returns non-JSON text (e.g. a rate-limit message) as a degraded `claude` call would."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def run_claude_task(
        self, *, project: ProjectConfig, prompt: str, json_schema: Optional[dict[str, Any]] = None
    ) -> ClaudeResult:
        return ClaudeResult(result_text=self._text, session_id="arch-1", total_cost_usd=0.02)


def _run(
    gh: InMemoryGitHub, executor, tmp_path: Path, project: ProjectConfig = PROJECT
) -> tuple[AtomicJsonRunStore, RunStatus]:
    store = AtomicJsonRunStore(tmp_path / "state")
    loop = build_arch_review_loop(
        executor=executor, github=gh, guard=ActionGuard(TAXONOMY),
        project=project, project_root=tmp_path,
    )
    runner = LoopRunner(loop, store, RecordingNotifier())
    run_id = runner.create_run(project_id="sample").run_id
    return store, runner.run(run_id)


def _file_findings_output(store: AtomicJsonRunStore) -> dict:
    """The step output recorded for the single run's `file_findings` step."""
    (record,) = store.list()
    step = next(s for s in record.step_log.values() if s.step_name == "file_findings")
    return step.output


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


def test_non_json_scan_result_is_preserved(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    # A rate-limit / truncated response is non-JSON: it parses as zero findings
    # (clean exit), but the raw text must survive so `harness show` can diagnose it.
    raw = "Error: rate limit exceeded, please retry"
    store, status = _run(gh, NonJsonExecutor(raw), tmp_path)
    assert status is RunStatus.COMPLETED
    filed = gh.list_issues(repo=REPO, state="open", labels=[co.QUEUED])
    assert len(filed) == 0
    (record,) = store.list()
    assert record.data["raw_scan_result"] == raw


def test_caps_findings_per_run_filing_highest_severity_first(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    # Mixed severities in deliberately non-severity order; cap=2 keeps the two highest.
    findings = [
        {"title": "low A", "severity": "low", "rationale": "l"},
        {"title": "high A", "severity": "high", "rationale": "h"},
        {"title": "med A", "severity": "med", "rationale": "m"},
        {"title": "high B", "severity": "high", "rationale": "h"},
    ]
    project = ProjectConfig(
        id="sample", owner_instance="this-machine", repo=REPO,
        scheduling=ProjectScheduling(max_findings_per_run=2),
    )
    store, status = _run(gh, FindingsExecutor(findings), tmp_path, project)
    assert status is RunStatus.COMPLETED
    filed = gh.list_issues(repo=REPO, state="open", labels=[co.QUEUED])
    titles = {i.title for i in filed}
    assert len(filed) == 2
    assert titles == {"high A", "high B"}  # the two highest-severity findings
    out = _file_findings_output(store)
    assert out["deferred"] == ["med A", "low A"]  # remainder, severity-ordered, not filed
    assert out["skipped"] == []  # cut-by-cap is NOT a dedup skip


def test_no_cap_files_all_non_duplicate_findings(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    findings = [
        {"title": "one", "severity": "low", "rationale": "1"},
        {"title": "two", "severity": "high", "rationale": "2"},
        {"title": "three", "severity": "med", "rationale": "3"},
    ]
    # Default project: max_findings_per_run is None => no cap, no regression.
    store, status = _run(gh, FindingsExecutor(findings), tmp_path)
    assert status is RunStatus.COMPLETED
    filed = gh.list_issues(repo=REPO, state="open", labels=[co.QUEUED])
    assert {i.title for i in filed} == {"one", "two", "three"}
    out = _file_findings_output(store)
    assert out["deferred"] == []
