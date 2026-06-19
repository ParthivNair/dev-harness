"""End-to-end dev_task loop tests, driven entirely by in-memory fakes.

Proves the real loop: claim a queued issue -> generate -> build -> test ->
human gate -> publish branch, plus loop-back on failure and the max-iterations
abort (which leaves the issue ``harness:blocked``). The loop opens NO PR of its
own (B1) — a COMPLETED run means "branch published"; the overseer aggregates the
published branches into one wave PR. Resumes go through FRESH store + runner
instances, as a separate process/machine would.
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
from harness.domain.models import BreakerState, RunRecord, RunStatus, VerificationResponse
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

    def run_build(self, *, project: ProjectConfig, worktree=None) -> CommandResult:
        if self._fail_builds > 0:
            self._fail_builds -= 1
            return CommandResult(1, "", "build broke", 0.01)
        return super().run_build(project=project, worktree=worktree)

    def run_test(self, *, project: ProjectConfig, worktree=None) -> CommandResult:
        if self._fail_tests > 0:
            self._fail_tests -= 1
            return CommandResult(1, "1 failed", "assertion error", 0.01)
        return super().run_test(project=project, worktree=worktree)


class WorktreeRecordingExecutor(ScriptedExecutor):
    """Records prepare_branch calls and the ``worktree`` threaded into each step, so
    the loop's clean-base wiring (branch cut once from origin/main, then edited/built/
    published in that worktree) can be asserted without real git."""

    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self.prepared: list[str] = []
        self.claude_worktrees: list = []
        self.build_worktrees: list = []
        self.test_worktrees: list = []
        self.publish_worktree = None

    def prepare_branch(self, *, project, branch):  # type: ignore[no-untyped-def]
        self.prepared.append(branch)
        return super().prepare_branch(project=project, branch=branch)

    def run_claude_task(self, *, project, prompt, json_schema=None, worktree=None):  # type: ignore[no-untyped-def]
        self.claude_worktrees.append(worktree)
        return super().run_claude_task(
            project=project, prompt=prompt, json_schema=json_schema, worktree=worktree
        )

    def run_build(self, *, project, worktree=None):  # type: ignore[no-untyped-def]
        self.build_worktrees.append(worktree)
        return super().run_build(project=project, worktree=worktree)

    def run_test(self, *, project, worktree=None):  # type: ignore[no-untyped-def]
        self.test_worktrees.append(worktree)
        return super().run_test(project=project, worktree=worktree)

    def publish_branch(self, *, project, branch, commit_message, worktree=None):  # type: ignore[no-untyped-def]
        self.publish_worktree = worktree
        return super().publish_branch(
            project=project, branch=branch, commit_message=commit_message, worktree=worktree
        )


def _seed_queued_issue(gh: InMemoryGitHub, *, title: str = "Add feature", body: str = "do X") -> int:
    return gh.create_issue(repo=REPO, title=title, body=body, labels=[co.QUEUED]).number


def _runner(root: Path, gh: InMemoryGitHub, notifier, executor=None, *, tmp: Path, taxonomy=None):
    store = AtomicJsonRunStore(root)
    loop = build_dev_loop(
        executor=executor or EchoExecutor(),
        github=gh,
        guard=ActionGuard(taxonomy if taxonomy is not None else TAXONOMY),
        project=PROJECT,
        instance_id=INSTANCE,
        project_root=tmp,
        artifacts_dir=store.root / "artifacts",
        store=store,
    )
    return store, LoopRunner(loop, store, notifier)


def _answer(store: AtomicJsonRunStore, run_id: str, *, approved: bool, notes: str = "") -> VerificationResponse:
    req = store.load(run_id).pending_request
    assert req is not None
    return VerificationResponse(
        request_id=req.request_id, run_id=run_id, step_id=req.step_id,
        answer={"approved": approved, "notes": notes}, approved=approved, via="test",
    )


def test_happy_path_claims_builds_gates_then_publishes_no_pr(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    notifier = RecordingNotifier()
    root = tmp_path / "state"

    store, runner = _runner(root, gh, notifier, tmp=tmp_path)
    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5),
        data={"issue_number": number, "repo": REPO},
    ).run_id

    # Runs claim -> generate -> build -> test, then suspends at the (gated) verify gate.
    assert runner.run(run_id) is RunStatus.WAITING
    issue = gh.get_issue(repo=REPO, number=number)
    assert co.owner_of(issue) == INSTANCE
    assert co.state_of(issue) == co.NEEDS_VERIFICATION
    rec = store.load(run_id)
    assert rec.current_step == "verify_gate"
    assert rec.data["artifact_path"]  # a perceptual artifact was written

    # Approve through a fresh runner -> publishes the branch and finishes. The loop
    # opens NO PR (B1): the overseer aggregates the published branch into a wave PR.
    store2, runner2 = _runner(root, gh, notifier, tmp=tmp_path)
    assert runner2.resume(run_id, _answer(store2, run_id, approved=True, notes="looks good")) is RunStatus.COMPLETED

    final = store2.load(run_id)
    assert final.status is RunStatus.COMPLETED
    assert final.data.get("published") is True       # branch published = the work product
    assert final.data.get("pr_url") is None          # no per-issue PR opened by the loop
    assert "publish#1" in final.step_log             # the branch was published
    assert "open_pr#1" not in final.step_log         # the open_pr step is gone
    assert gh.list_pulls(repo=REPO) == []            # the loop opened no PR
    assert co.state_of(gh.get_issue(repo=REPO, number=number)) == co.PR_OPEN  # "published"


def test_prepare_branch_cuts_clean_base_then_threads_worktree_through(tmp_path: Path) -> None:
    # The fix: a prepare_branch step cuts the feature branch from origin/main in an
    # isolated worktree BEFORE generate, and that worktree is threaded into the
    # claude/build/test/publish calls so the run never edits the live checkout.
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    root = tmp_path / "state"
    executor = WorktreeRecordingExecutor()
    taxonomy = {**TAXONOMY, "verify_gate": AutonomyTier.AUTONOMOUS}  # run straight through
    store, runner = _runner(root, gh, RecordingNotifier(), executor, tmp=tmp_path, taxonomy=taxonomy)
    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5),
        data={"issue_number": number, "repo": REPO},
    ).run_id

    assert runner.run(run_id) is RunStatus.COMPLETED
    final = store.load(run_id)

    # The branch was prepared exactly once, before generate, and recorded in state.
    assert executor.prepared == [f"harness/{INSTANCE}/issue-{number}"]
    assert "prepare_branch#1" in final.step_log
    wt = final.data["worktree"]
    assert wt and final.data["branch"] == f"harness/{INSTANCE}/issue-{number}"
    # Every editing/verifying/publishing step ran in that prepared worktree.
    assert [str(w) for w in executor.claude_worktrees] == [wt]
    assert [str(w) for w in executor.build_worktrees] == [wt]
    assert [str(w) for w in executor.test_worktrees] == [wt]
    assert str(executor.publish_worktree) == wt


def test_loop_back_does_not_recut_the_branch(tmp_path: Path) -> None:
    # A build/test failure loops back to keep editing the SAME prepared worktree (its
    # accumulated work + the failure log); the branch must not be re-cut from
    # origin/main on the second iteration, or partial progress would be wiped.
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    root = tmp_path / "state"
    executor = WorktreeRecordingExecutor(fail_tests=1)  # iter 1 fails, iter 2 passes
    taxonomy = {**TAXONOMY, "verify_gate": AutonomyTier.AUTONOMOUS}
    store, runner = _runner(root, gh, RecordingNotifier(), executor, tmp=tmp_path, taxonomy=taxonomy)
    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5),
        data={"issue_number": number, "repo": REPO},
    ).run_id

    assert runner.run(run_id) is RunStatus.COMPLETED
    rec = store.load(run_id)
    assert rec.breakers.loop_count == 2          # it did loop back
    assert executor.prepared == [f"harness/{INSTANCE}/issue-{number}"]  # cut ONCE only
    # The step re-runs on the loop-back, but short-circuits (no new branch in output)
    # because the worktree is already prepared — so accumulated work isn't wiped.
    assert "prepare_branch#2" in rec.step_log
    assert rec.step_log["prepare_branch#2"].output is None


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

    # Approve the second gate -> publishes the branch, completes (no per-issue PR).
    assert runner.resume(run_id, _answer(store, run_id, approved=True)) is RunStatus.COMPLETED
    assert gh.list_pulls(repo=REPO) == []
    assert store.load(run_id).data.get("published") is True


# --------------------------------------------------------------------------- #
# B2: verify_gate via the autonomy tier.
# --------------------------------------------------------------------------- #
def test_b2_autonomous_verify_gate_flows_to_publish_without_a_gate(tmp_path: Path) -> None:
    # A project that marks verify_gate autonomous (review-at-PR-only) skips the human
    # gate and runs straight through to publish — and STILL writes the perceptual
    # artifact + test output in `test`, so the record stays complete.
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    root = tmp_path / "state"
    taxonomy = {**TAXONOMY, "verify_gate": AutonomyTier.AUTONOMOUS}
    store, runner = _runner(root, gh, RecordingNotifier(), tmp=tmp_path, taxonomy=taxonomy)
    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5),
        data={"issue_number": number, "repo": REPO},
    ).run_id

    # No gate: the run completes in a single pass (never WAITING).
    assert runner.run(run_id) is RunStatus.COMPLETED
    final = store.load(run_id)
    assert final.data.get("published") is True
    assert final.data["artifact_path"]               # the artifact was still written
    assert final.data["test_stdout"]                 # and the test output recorded
    assert "verify_gate#1" in final.step_log
    assert final.step_log["verify_gate#1"].output.get("auto_verified") is True
    # The issue was never parked in needs-verification — it went straight to published.
    assert co.state_of(gh.get_issue(repo=REPO, number=number)) == co.PR_OPEN
    assert gh.list_pulls(repo=REPO) == []            # still no per-issue PR (B1)


def test_b2_gated_verify_gate_still_suspends_for_a_human(tmp_path: Path) -> None:
    # The safe default: verify_gate absent from the taxonomy => GATED => the run still
    # suspends WAITING at the human gate (the DAW-style review-mid-run path).
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    root = tmp_path / "state"
    store, runner = _runner(root, gh, RecordingNotifier(), tmp=tmp_path)  # TAXONOMY: no verify_gate
    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5),
        data={"issue_number": number, "repo": REPO},
    ).run_id

    assert runner.run(run_id) is RunStatus.WAITING
    rec = store.load(run_id)
    assert rec.current_step == "verify_gate"
    assert co.state_of(gh.get_issue(repo=REPO, number=number)) == co.NEEDS_VERIFICATION


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


class PromptCapturingExecutor(EchoExecutor):
    """Records the prompt fed to ``run_claude_task`` so the handoff-context
    injection (prior-attempt block) can be asserted."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def run_claude_task(self, *, project, prompt, json_schema=None, worktree=None):  # type: ignore[no-untyped-def]
        self.prompts.append(prompt)
        return super().run_claude_task(
            project=project, prompt=prompt, json_schema=json_schema, worktree=worktree
        )


def test_generate_injects_prior_attempt_context_when_a_prior_terminal_run_exists(
    tmp_path: Path,
) -> None:
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    root = tmp_path / "state"
    executor = PromptCapturingExecutor()
    store, runner = _runner(root, gh, RecordingNotifier(), executor, tmp=tmp_path)

    # A prior TERMINAL dev_task run for this same issue, recorded in the store —
    # what a handed-off attempt leaves behind for the fresh run to continue.
    store.create(RunRecord(
        loop_name="dev_task",
        project_id="sample",
        status=RunStatus.ABORTED,
        terminal_reason="max_iterations (2) exceeded",
        data={
            "repo": REPO, "issue_number": number,
            "branch": "harness/this-machine/issue-prior",
            "claude_result": "got partway: wrote the parser",
        },
        created_at="2026-01-01T00:00:00+00:00",
    ))

    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5),
        data={"issue_number": number, "repo": REPO},
    ).run_id
    runner.run(run_id)

    assert executor.prompts, "generate should have called the executor"
    prompt = executor.prompts[0]
    assert "## Prior attempt(s)" in prompt        # the handoff block was injected
    assert "ABORTED" in prompt
    assert "got partway: wrote the parser" in prompt  # last attempt's work carried in
    assert "do not restart from scratch" in prompt.lower()


def test_generate_omits_prior_attempt_context_on_a_first_attempt(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed_queued_issue(gh)
    root = tmp_path / "state"
    executor = PromptCapturingExecutor()
    store, runner = _runner(root, gh, RecordingNotifier(), executor, tmp=tmp_path)
    run_id = runner.create_run(
        project_id="sample", breakers=BreakerState(max_iterations=5),
        data={"issue_number": number, "repo": REPO},
    ).run_id
    runner.run(run_id)

    # No prior terminal run for this issue -> no handoff block (the live run itself
    # is RUNNING, not terminal, so it is not counted as a prior attempt).
    assert executor.prompts
    assert "## Prior attempt(s)" not in executor.prompts[0]
