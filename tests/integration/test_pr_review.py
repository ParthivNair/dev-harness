"""End-to-end pr_review loop tests, driven entirely by in-memory fakes.

Proves the close-the-loop reviewer: select a harness-authored PR -> gate on
mergeable + green CI -> structured review -> merge (per the merge_to_main tier),
plus the fail-safe paths (CI pending defers, CI failure / unmergeable / changes
requested never merge), instance-scoped selection, and merge idempotency under the
runner's at-least-once execution contract.
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
from harness.domain.models import BreakerState, RunStatus, VerificationResponse
from harness.loops.pr_review import build_pr_review_loop
from harness.ports.executor import ClaudeResult
from harness.ports.github import ChecksState, PRState, ReviewEvent
from tests.fakes import RecordingNotifier

pytestmark = pytest.mark.integration

REPO = "acme/app"
INSTANCE = "this-machine"
PROJECT = ProjectConfig(id="sample", owner_instance=INSTANCE, repo=REPO)

APPROVE = {"recommendation": "approve", "summary": "looks good", "blocking": []}
REJECT = {"recommendation": "request_changes", "summary": "needs work",
          "blocking": [{"area": "tests", "issue": "no coverage"}]}

# Both writes opted in: the fully-closed-loop default for the dogfood repo.
AUTONOMOUS = {"review_pr": AutonomyTier.AUTONOMOUS, "merge_to_main": AutonomyTier.AUTONOMOUS}
GATED = {"review_pr": AutonomyTier.AUTONOMOUS, "merge_to_main": AutonomyTier.GATED}
FORBIDDEN = {"review_pr": AutonomyTier.AUTONOMOUS, "merge_to_main": AutonomyTier.FORBIDDEN}


class ReviewExecutor(EchoExecutor):
    """Returns a JSON review verdict as a real ``claude --json-schema`` call would."""

    def __init__(self, verdict: dict) -> None:
        super().__init__()
        self.verdict = verdict
        self.calls = 0

    def run_claude_task(
        self, *, project: ProjectConfig, prompt: str, json_schema: Optional[dict[str, Any]] = None
    ) -> ClaudeResult:
        self.calls += 1
        return ClaudeResult(
            result_text=json.dumps(self.verdict), session_id="rev-1", total_cost_usd=0.03
        )


def _seed_pr(
    gh: InMemoryGitHub,
    *,
    instance: str = INSTANCE,
    mergeable: Optional[bool] = True,
    checks: ChecksState = ChecksState.SUCCESS,
    changes_requested: bool = False,
    head: Optional[str] = None,
) -> tuple[int, int]:
    """Create a linked issue + an open draft PR on a harness branch. Returns (pr, issue)."""
    issue = gh.create_issue(
        repo=REPO, title="Add feature", body="do X",
        labels=[co.IN_PROGRESS, co.owner_label(instance)],
    )
    head = head or f"harness/{instance}/issue-{issue.number}"
    pr = gh.open_draft_pr(repo=REPO, head=head, base="main", title="Add feature", body=f"Resolves #{issue.number}")
    gh.set_pull(repo=REPO, number=pr.number, mergeable=mergeable)
    gh.set_pull_checks(repo=REPO, number=pr.number, state=checks)
    if changes_requested:
        gh.add_labels(repo=REPO, number=pr.number, labels=[co.CHANGES_REQUESTED])
    return pr.number, issue.number


def _runner(gh: InMemoryGitHub, *, taxonomy: dict, executor, tmp_path: Path, instance: str = INSTANCE):
    store = AtomicJsonRunStore(tmp_path / "state")
    loop = build_pr_review_loop(
        executor=executor, github=gh, guard=ActionGuard(taxonomy),
        project=PROJECT, instance_id=instance, project_root=tmp_path,
        artifacts_dir=store.root / "artifacts",
    )
    return store, LoopRunner(loop, store, RecordingNotifier())


def _create(runner: LoopRunner, *, pr: Optional[int] = None) -> str:
    data = {"pr_number": pr} if pr is not None else None
    return runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5), data=data
    ).run_id


def _answer(store: AtomicJsonRunStore, run_id: str, *, approved: bool) -> VerificationResponse:
    req = store.load(run_id).pending_request
    assert req is not None
    return VerificationResponse(
        request_id=req.request_id, run_id=run_id, step_id=req.step_id,
        answer={"approved": approved, "notes": ""}, approved=approved, via="test",
    )


def test_no_reviewable_pr_is_clean_noop(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    store, runner = _runner(gh, taxonomy=AUTONOMOUS, executor=ReviewExecutor(APPROVE), tmp_path=tmp_path)
    run_id = _create(runner)
    assert runner.run(run_id) is RunStatus.COMPLETED
    assert store.load(run_id).step_log["select_pr#1"].output["no_pr"] is True


def test_autonomous_approve_marks_ready_merges_and_marks_issue_done(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    pr_no, issue_no = _seed_pr(gh)
    exe = ReviewExecutor(APPROVE)
    store, runner = _runner(gh, taxonomy=AUTONOMOUS, executor=exe, tmp_path=tmp_path)

    assert runner.run(_create(runner)) is RunStatus.COMPLETED

    pull = gh.get_pull(repo=REPO, number=pr_no)
    assert pull.state is PRState.MERGED and pull.draft is False
    assert gh.merge_method_for(repo=REPO, number=pr_no) == "squash"
    # an APPROVE review was posted; the linked issue flipped to done.
    events = [e for e, _ in gh.reviews_for(repo=REPO, number=pr_no)]
    assert events == [ReviewEvent.APPROVE]
    assert co.state_of(gh.get_issue(repo=REPO, number=issue_no)) == co.DONE
    assert co.CHANGES_REQUESTED not in gh.get_pull(repo=REPO, number=pr_no).labels


def test_request_changes_posts_review_labels_and_does_not_merge(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    pr_no, issue_no = _seed_pr(gh)
    store, runner = _runner(gh, taxonomy=AUTONOMOUS, executor=ReviewExecutor(REJECT), tmp_path=tmp_path)

    assert runner.run(_create(runner)) is RunStatus.COMPLETED

    pull = gh.get_pull(repo=REPO, number=pr_no)
    assert pull.state is PRState.OPEN  # never merged
    assert [e for e, _ in gh.reviews_for(repo=REPO, number=pr_no)] == [ReviewEvent.REQUEST_CHANGES]
    assert co.CHANGES_REQUESTED in pull.labels
    assert co.state_of(gh.get_issue(repo=REPO, number=issue_no)) != co.DONE


def test_ci_pending_defers_without_reviewing_or_spending(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    pr_no, _ = _seed_pr(gh, checks=ChecksState.PENDING)
    exe = ReviewExecutor(APPROVE)
    store, runner = _runner(gh, taxonomy=AUTONOMOUS, executor=exe, tmp_path=tmp_path)

    run_id = _create(runner)
    assert runner.run(run_id) is RunStatus.COMPLETED
    # No review posted, no Claude spend, no sticky label -> retried cheaply next tick.
    assert exe.calls == 0
    assert gh.reviews_for(repo=REPO, number=pr_no) == []
    assert co.CHANGES_REQUESTED not in gh.get_pull(repo=REPO, number=pr_no).labels
    assert gh.get_pull(repo=REPO, number=pr_no).state is PRState.OPEN
    assert store.load(run_id).breakers.cumulative_cost_usd == 0.0


@pytest.mark.parametrize(
    "mergeable,checks",
    [(True, ChecksState.FAILURE), (False, ChecksState.SUCCESS)],
)
def test_failing_ci_or_unmergeable_labels_and_skips(tmp_path: Path, mergeable: bool, checks: ChecksState) -> None:
    gh = InMemoryGitHub()
    pr_no, _ = _seed_pr(gh, mergeable=mergeable, checks=checks)
    exe = ReviewExecutor(APPROVE)
    store, runner = _runner(gh, taxonomy=AUTONOMOUS, executor=exe, tmp_path=tmp_path)

    assert runner.run(_create(runner)) is RunStatus.COMPLETED
    assert exe.calls == 0  # gated before the paid review
    pull = gh.get_pull(repo=REPO, number=pr_no)
    assert pull.state is PRState.OPEN
    assert co.CHANGES_REQUESTED in pull.labels


def test_gated_merge_waits_then_approve_merges(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    pr_no, issue_no = _seed_pr(gh)
    store, runner = _runner(gh, taxonomy=GATED, executor=ReviewExecutor(APPROVE), tmp_path=tmp_path)

    run_id = _create(runner)
    assert runner.run(run_id) is RunStatus.WAITING  # suspends at the merge gate
    assert store.load(run_id).current_step == "merge"
    assert gh.get_pull(repo=REPO, number=pr_no).state is PRState.OPEN  # not yet merged

    # Resume through a FRESH store + runner, as another process/machine would.
    store2, runner2 = _runner(gh, taxonomy=GATED, executor=ReviewExecutor(APPROVE), tmp_path=tmp_path)
    assert runner2.resume(run_id, _answer(store2, run_id, approved=True)) is RunStatus.COMPLETED
    assert gh.get_pull(repo=REPO, number=pr_no).state is PRState.MERGED
    assert co.state_of(gh.get_issue(repo=REPO, number=issue_no)) == co.DONE


def test_gated_merge_reject_leaves_pr_open(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    pr_no, _ = _seed_pr(gh)
    store, runner = _runner(gh, taxonomy=GATED, executor=ReviewExecutor(APPROVE), tmp_path=tmp_path)
    run_id = _create(runner)
    assert runner.run(run_id) is RunStatus.WAITING
    assert runner.resume(run_id, _answer(store, run_id, approved=False)) is RunStatus.COMPLETED
    pull = gh.get_pull(repo=REPO, number=pr_no)
    assert pull.state is PRState.OPEN and pull.draft is True  # untouched (never marked ready)


def test_forbidden_tier_reviews_but_never_merges(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    pr_no, _ = _seed_pr(gh)
    store, runner = _runner(gh, taxonomy=FORBIDDEN, executor=ReviewExecutor(APPROVE), tmp_path=tmp_path)

    assert runner.run(_create(runner)) is RunStatus.COMPLETED
    pull = gh.get_pull(repo=REPO, number=pr_no)
    assert pull.state is PRState.OPEN  # reviewed only — the merge guard refused
    assert [e for e, _ in gh.reviews_for(repo=REPO, number=pr_no)] == [ReviewEvent.APPROVE]


def test_already_merged_pr_is_noop(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    pr_no, _ = _seed_pr(gh)
    gh.set_pull(repo=REPO, number=pr_no, state=PRState.MERGED)
    exe = ReviewExecutor(APPROVE)
    store, runner = _runner(gh, taxonomy=AUTONOMOUS, executor=exe, tmp_path=tmp_path)

    run_id = _create(runner, pr=pr_no)
    assert runner.run(run_id) is RunStatus.COMPLETED
    assert exe.calls == 0  # ready_check short-circuits a closed PR
    assert store.load(run_id).step_log["ready_check#1"].output["already_closed"] == "merged"


def test_merge_step_is_idempotent_on_at_least_once_reentry(tmp_path: Path) -> None:
    """Crash after merge, before the advance-save: the runner re-enters `merge`. It must
    see the PR already MERGED and finish without a second merge."""
    gh = InMemoryGitHub()
    pr_no, _ = _seed_pr(gh)
    store, runner = _runner(gh, taxonomy=AUTONOMOUS, executor=ReviewExecutor(APPROVE), tmp_path=tmp_path)
    run_id = _create(runner)
    assert runner.run(run_id) is RunStatus.COMPLETED
    assert gh.get_pull(repo=REPO, number=pr_no).state is PRState.MERGED

    # Simulate the crash-resume: force re-entry into the already-done merge step.
    rec = store.load(run_id)
    rec.status = RunStatus.RUNNING
    rec.current_step = "merge"
    store.save(rec)
    assert runner.run(run_id) is RunStatus.COMPLETED
    assert store.load(run_id).step_log["merge#1"].output["idempotent"] is True


def test_other_instance_and_nonharness_branches_not_autoselected(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    _seed_pr(gh, instance="other-machine")               # another machine's harness PR
    _seed_pr(gh, head="feature/hand-rolled")             # a human's branch
    store, runner = _runner(gh, taxonomy=AUTONOMOUS, executor=ReviewExecutor(APPROVE), tmp_path=tmp_path)

    run_id = _create(runner)
    assert runner.run(run_id) is RunStatus.COMPLETED
    assert store.load(run_id).step_log["select_pr#1"].output["no_pr"] is True


def test_changes_requested_pr_excluded_from_autoselect(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    _seed_pr(gh, changes_requested=True)
    store, runner = _runner(gh, taxonomy=AUTONOMOUS, executor=ReviewExecutor(APPROVE), tmp_path=tmp_path)
    run_id = _create(runner)
    assert runner.run(run_id) is RunStatus.COMPLETED
    assert store.load(run_id).step_log["select_pr#1"].output["no_pr"] is True
