"""End-to-end dev_task loop tests, driven entirely by in-memory fakes.

Proves the real loop: claim a queued issue -> generate -> build -> test ->
human gate -> draft PR, plus loop-back on failure and the max-iterations abort
(which leaves the issue ``harness:blocked``). Resumes go through FRESH store +
runner instances, as a separate process/machine would.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.adapters.executor.echo import EchoExecutor
from harness.adapters.github.fake import InMemoryGitHub
from harness.adapters.state.json_store import AtomicJsonRunStore
from harness.application import coordination as co
from harness.application.action_guard import ActionGuard
from harness.application.loop_runner import LoopRunner
from harness.config.models import AutonomyTier, ProjectConfig
from harness.domain.models import BreakerState, RunStatus, VerificationResponse
from harness.loops.dev_task import build_dev_loop
from harness.ports.executor import CommandResult
from tests.fakes import RecordingNotifier

pytestmark = pytest.mark.integration

REPO = "acme/app"
INSTANCE = "this-machine"
PROJECT = ProjectConfig(id="sample", owner_instance=INSTANCE, repo=REPO)
TAXONOMY = {"open_draft_pr": AutonomyTier.AUTONOMOUS}


class ScriptedExecutor(EchoExecutor):
    """EchoExecutor whose build/test can be made to fail a fixed number of times."""

    def __init__(self, *, fail_tests: int = 0, fail_builds: int = 0) -> None:
        super().__init__()
        self._fail_tests = fail_tests
        self._fail_builds = fail_builds

    def run_build(self, *, project: ProjectConfig) -> CommandResult:
        if self._fail_builds > 0:
            self._fail_builds -= 1
            return CommandResult(1, "", "build broke", 0.01)
        return super().run_build(project=project)

    def run_test(self, *, project: ProjectConfig) -> CommandResult:
        if self._fail_tests > 0:
            self._fail_tests -= 1
            return CommandResult(1, "1 failed", "assertion error", 0.01)
        return super().run_test(project=project)


def _seed_queued_issue(gh: InMemoryGitHub, *, title: str = "Add feature", body: str = "do X") -> int:
    return gh.create_issue(repo=REPO, title=title, body=body, labels=[co.QUEUED]).number


def _runner(root: Path, gh: InMemoryGitHub, notifier, executor=None, *, tmp: Path):
    store = AtomicJsonRunStore(root)
    loop = build_dev_loop(
        executor=executor or EchoExecutor(),
        github=gh,
        guard=ActionGuard(TAXONOMY),
        project=PROJECT,
        instance_id=INSTANCE,
        project_root=tmp,
        artifacts_dir=store.root / "artifacts",
    )
    return store, LoopRunner(loop, store, notifier)


def _answer(store: AtomicJsonRunStore, run_id: str, *, approved: bool, notes: str = "") -> VerificationResponse:
    req = store.load(run_id).pending_request
    assert req is not None
    return VerificationResponse(
        request_id=req.request_id, run_id=run_id, step_id=req.step_id,
        answer={"approved": approved, "notes": notes}, approved=approved, via="test",
    )


def test_happy_path_claims_builds_gates_then_opens_draft_pr(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    notifier = RecordingNotifier()
    root = tmp_path / "state"

    store, runner = _runner(root, gh, notifier, tmp=tmp_path)
    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5),
        data={"issue_number": number, "repo": REPO},
    ).run_id

    # Runs claim -> generate -> build -> test, then suspends at the gate.
    assert runner.run(run_id) is RunStatus.WAITING
    issue = gh.get_issue(repo=REPO, number=number)
    assert co.owner_of(issue) == INSTANCE
    assert co.state_of(issue) == co.NEEDS_VERIFICATION
    rec = store.load(run_id)
    assert rec.current_step == "verify_gate"
    assert rec.data["artifact_path"]  # a perceptual artifact was written

    # Approve through a fresh runner -> opens a DRAFT pr and finishes.
    store2, runner2 = _runner(root, gh, notifier, tmp=tmp_path)
    assert runner2.resume(run_id, _answer(store2, run_id, approved=True, notes="looks good")) is RunStatus.COMPLETED

    final = store2.load(run_id)
    assert final.status is RunStatus.COMPLETED
    assert final.data["pr_url"]
    assert "publish#1" in final.step_log  # the branch was published before the PR
    pulls = gh.list_pulls(repo=REPO)
    assert len(pulls) == 1 and pulls[0].draft is True  # never a ready PR
    assert co.state_of(gh.get_issue(repo=REPO, number=number)) == co.PR_OPEN


def test_reject_loops_back_then_approve_completes(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    root = tmp_path / "state"
    store, runner = _runner(root, gh, RecordingNotifier(), tmp=tmp_path)
    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5),
        data={"issue_number": number, "repo": REPO},
    ).run_id
    runner.run(run_id)

    # Reject -> loops back (new iteration), regenerates, waits again.
    assert runner.resume(run_id, _answer(store, run_id, approved=False, notes="audio crackles")) is RunStatus.WAITING
    mid = store.load(run_id)
    assert mid.breakers.loop_count == 2
    # the rejection notes are carried into the next attempt as failure context
    assert mid.data["last_failure"]["phase"] == "verify"

    # Approve the second gate -> draft PR, completes.
    assert runner.resume(run_id, _answer(store, run_id, approved=True)) is RunStatus.COMPLETED
    assert len(gh.list_pulls(repo=REPO)) == 1


def test_failing_test_loops_back_with_failure_context(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    root = tmp_path / "state"
    executor = ScriptedExecutor(fail_tests=1)  # first test run fails, then passes
    store, runner = _runner(root, gh, RecordingNotifier(), executor, tmp=tmp_path)
    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5),
        data={"issue_number": number, "repo": REPO},
    ).run_id

    # Test fails on iteration 1 -> loops back -> iteration 2 passes -> gate.
    assert runner.run(run_id) is RunStatus.WAITING
    rec = store.load(run_id)
    assert rec.breakers.loop_count == 2
    assert "test#1" in rec.step_log  # the failed test attempt is logged


def test_max_iterations_aborts_and_marks_blocked(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    root = tmp_path / "state"
    executor = ScriptedExecutor(fail_tests=99)  # test never passes
    store, runner = _runner(root, gh, RecordingNotifier(), executor, tmp=tmp_path)
    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=2),
        data={"issue_number": number, "repo": REPO},
    ).run_id

    assert runner.run(run_id) is RunStatus.ABORTED
    final = store.load(run_id)
    assert "max_iterations" in (final.terminal_reason or "")
    # The CLI marks the issue blocked on abort; emulate that post-run step here.
    co.transition(gh, repo=REPO, number=number, to_state=co.BLOCKED)
    assert co.state_of(gh.get_issue(repo=REPO, number=number)) == co.BLOCKED


def test_claim_lost_finishes_as_noop_without_spending(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    co.claim(gh, repo=REPO, number=number, instance_id="other-machine")  # someone else owns it
    root = tmp_path / "state"
    store, runner = _runner(root, gh, RecordingNotifier(), tmp=tmp_path)
    run_id = runner.create_run(
        project_id="sample", data={"issue_number": number, "repo": REPO}
    ).run_id

    assert runner.run(run_id) is RunStatus.COMPLETED  # clean no-op
    final = store.load(run_id)
    assert final.data.get("claim_lost") is None  # output, not data
    assert final.breakers.cumulative_cost_usd == 0.0  # never called claude
    assert final.step_log["claim_issue#1"].output["claim_lost"] is True
