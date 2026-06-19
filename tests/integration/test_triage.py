"""Triage loop: JSON-schema judgements -> refined sev:/effort: labels on the queue.

The LLM judges severity/effort (here a scripted executor stands in for the
JSON-Schema-constrained call); the loop applies the judgement as labels the
deterministic claimer (find_claimable) then orders work by. Skips non-queued
issues and no-ops on a malformed result.
"""

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
from harness.loops.triage import build_triage_loop
from tests.fakes import RecordingNotifier

pytestmark = pytest.mark.integration

REPO = "acme/app"
PROJECT = ProjectConfig(id="sample", owner_instance="this-machine", repo=REPO)
TAXONOMY = {"set_labels": AutonomyTier.AUTONOMOUS}


class JudgementExecutor(EchoExecutor):
    """Returns a JSON judgements payload as a real `claude --json-schema` call would."""

    def __init__(self, judgements: list[dict]) -> None:
        super().__init__()
        self._judgements = judgements

    def run_claude_task(
        self, *, project: ProjectConfig, prompt: str, json_schema: Optional[dict[str, Any]] = None
    ) -> "Any":
        from harness.ports.executor import ClaudeResult

        return ClaudeResult(
            result_text=json.dumps({"judgements": self._judgements}),
            session_id="triage-1",
            total_cost_usd=0.02,
        )


def _run(gh: InMemoryGitHub, executor, tmp_path: Path) -> tuple[AtomicJsonRunStore, RunStatus]:
    store = AtomicJsonRunStore(tmp_path / "state")
    loop = build_triage_loop(
        executor=executor, github=gh, guard=ActionGuard(TAXONOMY),
        project=PROJECT, project_root=tmp_path,
    )
    runner = LoopRunner(loop, store, RecordingNotifier())
    run_id = runner.create_run(project_id="sample").run_id
    return store, runner.run(run_id)


def _queued(gh: InMemoryGitHub, *, title: str, labels=(co.QUEUED,)) -> int:
    return gh.create_issue(repo=REPO, title=title, body="x", labels=list(labels)).number


def test_empty_queue_exits_clean(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    _store, status = _run(gh, EchoExecutor(), tmp_path)
    assert status is RunStatus.COMPLETED


def test_malformed_result_applies_nothing(tmp_path: Path) -> None:
    # EchoExecutor returns non-JSON text -> parsed as zero judgements -> clean no-op.
    gh = InMemoryGitHub()
    n = _queued(gh, title="task")
    _store, status = _run(gh, EchoExecutor(), tmp_path)
    assert status is RunStatus.COMPLETED
    issue = gh.get_issue(repo=REPO, number=n)
    assert not any(label.startswith(co.SEV_PREFIX) for label in issue.labels)
    assert not any(label.startswith(co.EFFORT_PREFIX) for label in issue.labels)


def test_triage_applies_severity_and_effort_labels(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    n = _queued(gh, title="needs grooming", labels=(co.QUEUED, "bug"))
    judgements = [
        {"number": n, "severity": "high", "effort": "s", "rationale": "broken build"},
    ]
    _store, status = _run(gh, JudgementExecutor(judgements), tmp_path)
    assert status is RunStatus.COMPLETED
    issue = gh.get_issue(repo=REPO, number=n)
    assert "sev:high" in issue.labels and "effort:s" in issue.labels
    assert co.QUEUED in issue.labels and "bug" in issue.labels  # state + foreign kept
    assert any("broken build" in c for c in gh.comments[(REPO, n)])  # rationale comment


def test_triage_refines_an_existing_severity_label(tmp_path: Path) -> None:
    # A re-triage must MOVE sev:* (set-or-refine), not pile up two severity labels.
    gh = InMemoryGitHub()
    n = _queued(gh, title="reassessed", labels=(co.QUEUED, "sev:low"))
    judgements = [{"number": n, "severity": "high", "effort": "m"}]
    _store, _status = _run(gh, JudgementExecutor(judgements), tmp_path)
    issue = gh.get_issue(repo=REPO, number=n)
    sev_labels = [label for label in issue.labels if label.startswith(co.SEV_PREFIX)]
    assert sev_labels == ["sev:high"]              # refined, exactly one severity
    assert "effort:m" in issue.labels


def test_triage_skips_non_queued_issues(tmp_path: Path) -> None:
    # A still-queued issue is triaged; a claimed (in-progress) issue the model also
    # judged is skipped by apply()'s state guard so its labels stay the dev loop's.
    gh = InMemoryGitHub()
    queued = _queued(gh, title="still queued")
    claimed = _queued(gh, title="claimed")
    co.claim(gh, repo=REPO, number=claimed, instance_id="someone")  # in-progress now
    judgements = [
        {"number": queued, "severity": "med", "effort": "s"},
        {"number": claimed, "severity": "high", "effort": "l"},  # judged but no longer queued
    ]
    _store, status = _run(gh, JudgementExecutor(judgements), tmp_path)
    assert status is RunStatus.COMPLETED
    assert "sev:med" in gh.get_issue(repo=REPO, number=queued).labels    # triaged
    claimed_issue = gh.get_issue(repo=REPO, number=claimed)
    assert "sev:high" not in claimed_issue.labels                        # skipped
    assert co.state_of(claimed_issue) == co.IN_PROGRESS                  # untouched
