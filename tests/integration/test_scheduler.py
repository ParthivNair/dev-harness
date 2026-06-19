"""Scheduler: attention-diversion across projects, with a deterministic clock.

Exercises the behaviours the user asked for: resume-before-start, per-project
cadence ("check the low-effort repo less often"), weighted selection under
contention, the global spend ceiling halting new starts, and arch_review cadence.
Uses a real FileNotifier inbox so the resume-inside-a-tick path is genuine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.adapters.executor.echo import EchoExecutor
from harness.adapters.github.fake import InMemoryGitHub
from harness.adapters.notifier.file import FileNotifier
from harness.adapters.state.json_store import AtomicJsonRunStore
from harness.application import coordination as co
from harness.application.action_guard import ActionGuard
from harness.application.loop_runner import LoopRunner
from harness.application.overseer import Overseer
from harness.application.scheduler import Scheduler, SchedulerLedger
from harness.config.models import (
    AutonomyTier,
    CircuitBreakers,
    HarnessConfig,
    InstanceInfo,
    ProjectConfig,
    ProjectOverrides,
    ProjectScheduling,
    SchedulingConfig,
)
from harness.domain.models import BreakerState, RunStatus, VerificationResponse
from harness.loops.arch_review import build_arch_review_loop
from harness.loops.dev_task import build_dev_loop
from harness.loops.pr_review import build_pr_review_loop
from harness.loops.triage import build_triage_loop
from harness.ports.executor import ClaudeResult, CommandResult
from harness.ports.github import ChecksState, PRState

pytestmark = pytest.mark.integration

INSTANCE = "this-machine"
TAXONOMY = {
    "open_draft_pr": AutonomyTier.AUTONOMOUS,
    "file_issue": AutonomyTier.AUTONOMOUS,
    "set_labels": AutonomyTier.AUTONOMOUS,
    "review_pr": AutonomyTier.AUTONOMOUS,
    "merge_to_main": AutonomyTier.AUTONOMOUS,
}


class ApproveExecutor(EchoExecutor):
    """Echo executor whose Claude call returns an approving PR-review verdict."""

    def run_claude_task(self, *, project, prompt, json_schema=None) -> ClaudeResult:
        import json as _json

        return ClaudeResult(
            result_text=_json.dumps({"recommendation": "approve", "summary": "ok", "blocking": []}),
            session_id="rev", total_cost_usd=0.01,
        )


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class AlwaysFailsTestExecutor(EchoExecutor):
    """EchoExecutor whose tests never pass, so a dev_task run loops to its
    max-iterations abort — used to exercise the overseer's handoff across a tick."""

    def run_test(self, *, project: ProjectConfig) -> CommandResult:
        return CommandResult(1, "1 failed", "assertion error", 0.01)


class FakeRegistry:
    def __init__(self, projects: list[ProjectConfig]) -> None:
        self._p = {p.id: p for p in projects}

    def list_projects(self):
        return list(self._p.values())

    def get(self, project_id):
        from harness.ports.project_registry import ProjectNotFound

        try:
            return self._p[project_id]
        except KeyError:
            raise ProjectNotFound(project_id) from None

    def list_owned(self, instance_id):
        return [p for p in self._p.values() if p.owner_instance == instance_id]

    def reload(self):
        pass


def _project(pid: str, *, priority="normal", weight=None, min_interval=0,
             arch_cadence=None, triage_cadence=None, pr_review_cadence=None,
             loops=("dev_task",)) -> ProjectConfig:
    return ProjectConfig(
        id=pid, owner_instance=INSTANCE, repo=f"acme/{pid}",
        scheduling=ProjectScheduling(
            priority=priority, weight=weight, min_poll_interval_seconds=min_interval,
            arch_review_cadence_seconds=arch_cadence,
            triage_cadence_seconds=triage_cadence,
            pr_review_cadence_seconds=pr_review_cadence, loops=list(loops),
        ),
    )


def _make(tmp_path: Path, projects, *, clock, gh=None, executor=None,
          max_concurrent=2, ceiling=100.0):
    gh = gh or InMemoryGitHub()
    executor = executor or EchoExecutor()
    store = AtomicJsonRunStore(tmp_path / "state")
    notifier = FileNotifier(tmp_path / "inbox")
    guard = ActionGuard(TAXONOMY)

    def runner_factory(loop_name, project):
        if loop_name == "dev_task":
            loop = build_dev_loop(
                executor=executor, github=gh, guard=guard, project=project,
                instance_id=INSTANCE, project_root=tmp_path, artifacts_dir=store.root / "art",
                store=store,
            )
        elif loop_name == "arch_review":
            loop = build_arch_review_loop(
                executor=executor, github=gh, guard=guard, project=project, project_root=tmp_path,
            )
        elif loop_name == "triage":
            loop = build_triage_loop(
                executor=executor, github=gh, guard=guard, project=project, project_root=tmp_path,
            )
        elif loop_name == "pr_review":
            loop = build_pr_review_loop(
                executor=executor, github=gh, guard=guard, project=project,
                instance_id=INSTANCE, project_root=tmp_path, artifacts_dir=store.root / "art",
            )
        else:
            raise ValueError(loop_name)
        return LoopRunner(loop, store, notifier)

    def breakers_factory(project):
        cb = project.effective_breakers(HarnessConfig(instance=InstanceInfo(instance_id=INSTANCE)).circuit_breakers)
        return BreakerState(max_iterations=cb.max_iterations, budget_ceiling_usd=cb.spend_ceiling_usd)

    cfg = HarnessConfig(
        instance=InstanceInfo(instance_id=INSTANCE),
        scheduling=SchedulingConfig(
            enabled=True, max_concurrent_runs=max_concurrent, global_spend_ceiling_usd=ceiling,
        ),
    )
    registry = FakeRegistry(projects)
    overseer = Overseer(
        cfg=cfg, store=store, github=gh, registry=registry, executor=executor,
        notifier=notifier, clock=clock,
    )
    sched = Scheduler(
        cfg=cfg, store=store, registry=registry, github=gh, notifier=notifier,
        runner_factory=runner_factory, breakers_factory=breakers_factory,
        overseer=overseer, ledger_path=tmp_path / "sched.json", clock=clock,
    )
    return sched, gh, store, notifier


def _queue(gh: InMemoryGitHub, repo: str, n: int) -> list[int]:
    # NB: InMemoryGitHub numbers issues with a GLOBAL counter, so numbers are unique
    # across repos — callers must use the returned numbers, not assume 1..n per repo.
    return [gh.create_issue(repo=repo, title=f"{repo}#{i}", body="x", labels=[co.QUEUED]).number
            for i in range(n)]


def _approve_all_waiting(store, notifier) -> None:
    for rec in store.list(status=RunStatus.WAITING):
        req = rec.pending_request
        notifier.write_response(VerificationResponse(
            request_id=req.request_id, run_id=rec.run_id, step_id=req.step_id,
            answer={"approved": True}, approved=True, via="test",
        ))


def test_tick_starts_eligible_work_and_resume_completes_it(tmp_path: Path) -> None:
    clock = Clock()
    sched, gh, store, notifier = _make(tmp_path, [_project("app")], clock=clock)
    _queue(gh, "acme/app", 1)

    r1 = sched.tick()
    assert len(r1.started) == 1
    assert store.load(r1.started[0][1]).status is RunStatus.WAITING  # gated

    _approve_all_waiting(store, notifier)
    r2 = sched.tick()
    assert r2.resumed  # the gate resumed inside the tick
    assert store.load(r2.resumed[0][0]).status is RunStatus.COMPLETED  # work finished
    # Exactly ONE draft PR now exists: the loop opens no per-issue PR (B1), so the only
    # PR is the overseer's aggregated wave PR (queue drained + the run is terminal ->
    # wave done). mark_pr_ready is gated here, so it stays a draft.
    pulls = gh.list_pulls(repo="acme/app")
    assert len(pulls) == 1 and pulls[0].draft is True
    assert r2.wave_pr is not None  # the aggregated wave PR was drafted this tick


def test_higher_weight_wins_the_single_slot(tmp_path: Path) -> None:
    clock = Clock()
    hi = _project("hi", priority="high")
    lo = _project("lo", priority="low")
    sched, gh, store, _ = _make(tmp_path, [hi, lo], clock=clock, max_concurrent=1)
    _queue(gh, "acme/hi", 1)
    (lo_num,) = _queue(gh, "acme/lo", 1)

    report = sched.tick()
    assert [pid for pid, _, _ in report.started] == ["hi"]  # high priority took the only slot
    assert co.state_of(gh.get_issue(repo="acme/lo", number=lo_num)) == co.QUEUED  # lo untouched


def test_equal_weight_round_robin_is_fair(tmp_path: Path) -> None:
    clock = Clock()
    a, b = _project("a"), _project("b")
    sched, gh, store, notifier = _make(tmp_path, [a, b], clock=clock, max_concurrent=1)
    _queue(gh, "acme/a", 4)
    _queue(gh, "acme/b", 4)

    counts = {"a": 0, "b": 0}
    for _ in range(6):
        _approve_all_waiting(store, notifier)  # let the previous gate resume this tick
        for pid, _rid, _status in sched.tick().started:
            counts[pid] += 1
    assert abs(counts["a"] - counts["b"]) <= 1  # deficit round-robin alternates


def test_cadence_skips_low_effort_repo_until_interval_elapses(tmp_path: Path) -> None:
    clock = Clock(1000.0)
    proj = _project("app", min_interval=3600)  # only start at most hourly
    sched, gh, store, notifier = _make(tmp_path, [proj], clock=clock, max_concurrent=1)
    _queue(gh, "acme/app", 3)

    assert len(sched.tick().started) == 1          # first start
    _approve_all_waiting(store, notifier)
    clock.t = 1000.0 + 1800                          # 30 min later: under the 1h cadence
    assert sched.tick().started == []                # resumed only; no new start
    _approve_all_waiting(store, notifier)
    clock.t = 1000.0 + 3700                           # past the cadence
    assert len(sched.tick().started) == 1            # starts again


def test_global_spend_ceiling_halts_new_starts(tmp_path: Path) -> None:
    clock = Clock()
    # Ceiling below one generate's cost: the first run's spend trips the gate for the next tick.
    sched, gh, store, _ = _make(
        tmp_path, [_project("app")], clock=clock, max_concurrent=1, ceiling=0.005
    )
    _queue(gh, "acme/app", 3)

    sched.tick()  # starts one run; its generate records 0.01 > 0.005
    report = sched.tick()
    assert report.halted_for_spend is True
    assert report.started == []                       # no new work despite queued issues
    assert co.find_claimable(gh, repo="acme/app", instance_id=INSTANCE) is not None  # work remains


def test_pr_review_runs_on_cadence_and_closes_the_loop(tmp_path: Path) -> None:
    clock = Clock(1000.0)
    proj = _project("app", pr_review_cadence=3600)  # auto review+merge its own wave PR ~hourly
    sched, gh, store, _ = _make(tmp_path, [proj], clock=clock, executor=ApproveExecutor())
    # An open, mergeable, green-CI WAVE PR (overseer-style) for THIS instance: its issue
    # is at PR_OPEN (where dev_task.publish leaves it), referenced as a checked body row.
    issue = gh.create_issue(repo="acme/app", title="x", body="",
                            labels=[co.PR_OPEN, co.owner_label(INSTANCE)])
    pr = gh.open_draft_pr(
        repo="acme/app", head=f"harness/{INSTANCE}/wave-abc123", base="main", title="wave",
        body=f"- [x] #{issue.number} — x (`harness/{INSTANCE}/issue-{issue.number}`)",
    )
    gh.set_pull(repo="acme/app", number=pr.number, mergeable=True)
    gh.set_pull_checks(repo="acme/app", number=pr.number, state=ChecksState.SUCCESS)

    report = sched.tick()
    assert any(pid == "app" for pid, _, _ in report.started)            # a pr_review run started
    assert gh.get_pull(repo="acme/app", number=pr.number).state is PRState.MERGED
    assert co.state_of(gh.get_issue(repo="acme/app", number=issue.number)) == co.DONE

    clock.t = 1000.0 + 1800                                             # under cadence, PR gone
    assert sched.tick().started == []                                   # nothing to do


def test_arch_review_runs_on_its_own_cadence(tmp_path: Path) -> None:
    clock = Clock(1000.0)
    proj = _project("app", arch_cadence=3600)  # daily-ish review; no dev work queued
    sched, gh, store, _ = _make(tmp_path, [proj], clock=clock)

    def arch_runs() -> int:
        return sum(1 for r in store.list() if r.loop_name == "arch_review")

    sched.tick()
    assert arch_runs() == 1
    clock.t = 1000.0 + 1800
    sched.tick()
    assert arch_runs() == 1            # cadence not elapsed -> no second review
    clock.t = 1000.0 + 3700
    sched.tick()
    assert arch_runs() == 2            # elapsed -> reviews again


# --------------------------------------------------------------------------- #
# C3: triage cadence fires (mirrors arch_review); C1: deploy highest-priority first.
# --------------------------------------------------------------------------- #
def test_c3_triage_runs_on_its_own_cadence(tmp_path: Path) -> None:
    clock = Clock(1000.0)
    proj = _project("app", triage_cadence=3600)  # groom hourly; no dev work queued
    sched, gh, store, _ = _make(tmp_path, [proj], clock=clock)

    def triage_runs() -> int:
        return sum(1 for r in store.list() if r.loop_name == "triage")

    sched.tick()
    assert triage_runs() == 1
    clock.t = 1000.0 + 1800
    sched.tick()
    assert triage_runs() == 1          # cadence not elapsed -> no second triage
    clock.t = 1000.0 + 3700
    sched.tick()
    assert triage_runs() == 2          # elapsed -> triages again


class TriageJudgementExecutor(EchoExecutor):
    """Returns fixed triage judgements so a scheduled triage run labels the queue,
    letting the same tick's dev_task selection order by those labels."""

    def __init__(self, judgements: list[dict]) -> None:
        super().__init__()
        self._judgements = judgements

    def run_claude_task(self, *, project, prompt, json_schema=None):  # type: ignore[no-untyped-def]
        import json

        # Only the triage call carries the triage schema; everything else is echo.
        if json_schema is not None and "judgements" in json_schema.get("properties", {}):
            return ClaudeResult(
                result_text=json.dumps({"judgements": self._judgements}),
                session_id="triage", total_cost_usd=0.01,
            )
        return super().run_claude_task(project=project, prompt=prompt, json_schema=json_schema)


def test_c1_scheduler_deploys_highest_priority_ready_issue_first(tmp_path: Path) -> None:
    # End-to-end: a scheduled triage labels the queue, then dev_task claims the
    # highest-severity ready issue first (not merely the lowest-numbered).
    clock = Clock()
    proj = _project("app", triage_cadence=3600, min_interval=0)
    low, high = _queue(gh := InMemoryGitHub(), "acme/app", 2)
    sched, gh, store, _ = _make(
        tmp_path, [proj], clock=clock, gh=gh, max_concurrent=1,
        executor=TriageJudgementExecutor([
            {"number": low, "severity": "low", "effort": "m"},
            {"number": high, "severity": "high", "effort": "s"},
        ]),
    )

    report = sched.tick()
    # Triage labelled both; dev_task then claimed the high-severity issue, not `low`.
    # `high` left the queue (it was deployed and is mid-run); `low` is still queued.
    assert co.state_of(gh.get_issue(repo="acme/app", number=high)) != co.QUEUED
    assert co.state_of(gh.get_issue(repo="acme/app", number=low)) == co.QUEUED
    dev_started = [(pid, rid) for pid, rid, _ in report.started
                   if store.load(rid).loop_name == "dev_task"]
    assert len(dev_started) == 1
    assert store.load(dev_started[0][1]).data["issue_number"] == high  # the high-sev one


# --------------------------------------------------------------------------- #
# Overseer wiring (layer 3): wave bookkeeping, supervise, and handoff per tick.
# --------------------------------------------------------------------------- #
def _load_ledger(tmp_path: Path) -> SchedulerLedger:
    return SchedulerLedger.model_validate_json((tmp_path / "sched.json").read_text("utf-8"))


def test_tick_opens_a_wave_stamps_wave_id_and_registers_the_run(tmp_path: Path) -> None:
    clock = Clock()
    sched, gh, store, _ = _make(tmp_path, [_project("app")], clock=clock)
    _queue(gh, "acme/app", 1)

    report = sched.tick()
    assert len(report.started) == 1
    run_id = report.started[0][1]

    # The wave opened and the started run was stamped + registered into it.
    ledger = _load_ledger(tmp_path)
    assert ledger.current_wave is not None
    assert ledger.current_wave.run_ids == [run_id]
    assert store.load(run_id).data["wave_id"] == ledger.current_wave.wave_id


def test_tick_report_carries_overseer_fields(tmp_path: Path) -> None:
    # Even an empty tick (no work) returns the new fields with their defaults — proof
    # supervise() ran and its (empty) result was folded into the report.
    clock = Clock()
    sched, _gh, _store, _ = _make(tmp_path, [_project("app")], clock=clock)

    report = sched.tick()
    assert report.reconciled == []
    assert report.handed_off == []
    assert report.wave_pr is None


def test_aborted_dev_task_issue_is_handed_off_across_a_tick(tmp_path: Path) -> None:
    clock = Clock()
    # Tests never pass -> the dev_task run exhausts max_iterations and ABORTS within
    # the start phase of the tick; supervise() then hands the issue off (under cap).
    sched, gh, store, _ = _make(
        tmp_path, [_project("app")], clock=clock, executor=AlwaysFailsTestExecutor(),
    )
    (number,) = _queue(gh, "acme/app", 1)

    report = sched.tick()
    assert len(report.started) == 1
    run_id = report.started[0][1]
    assert store.load(run_id).status is RunStatus.ABORTED

    # The overseer requeued it for a fresh continuation (NOT blocked) under the cap.
    assert report.handed_off == [(number, "aborted")]
    issue = gh.get_issue(repo="acme/app", number=number)
    assert co.state_of(issue) == co.QUEUED       # back on the queue, fresh attempt
    assert co.owner_of(issue) is None            # lease dropped
    assert co.HANDOFF in issue.labels            # continuation marker stamped
    assert gh.comments[("acme/app", number)]     # a handoff packet was posted


def _reject_all_waiting(store, notifier) -> None:
    for rec in store.list(status=RunStatus.WAITING):
        req = rec.pending_request
        notifier.write_response(VerificationResponse(
            request_id=req.request_id, run_id=rec.run_id, step_id=req.step_id,
            answer={"approved": False, "notes": "no"}, approved=False, via="test",
        ))


def _capped_project(pid: str, *, max_iterations: int) -> ProjectConfig:
    """A dev_task project whose per-run iteration cap is tiny, so a single reject
    loop-back trips max_iterations and ABORTS on the resume path."""
    return ProjectConfig(
        id=pid, owner_instance=INSTANCE, repo=f"acme/{pid}",
        scheduling=ProjectScheduling(min_poll_interval_seconds=0, loops=["dev_task"]),
        overrides=ProjectOverrides(circuit_breakers=CircuitBreakers(max_iterations=max_iterations)),
    )


def test_a3_wave_run_that_aborts_on_resume_is_handed_off_not_blocked(tmp_path: Path) -> None:
    # A3: a wave run that goes WAITING at verify_gate and then ABORTS on resume (a
    # reject loops back past max_iterations) must NOT be blocked by resume_waiting —
    # supervise() owns the abort -> handoff decision for wave runs. With the bug the
    # resume path blocked it first, losing the handoff even on attempt 1.
    clock = Clock()
    proj = _capped_project("app", max_iterations=1)  # one iteration only
    sched, gh, store, notifier = _make(tmp_path, [proj], clock=clock)
    (number,) = _queue(gh, "acme/app", 1)

    # Tick 1: the run reaches verify_gate and suspends WAITING (iteration 1 of 1).
    r1 = sched.tick()
    assert len(r1.started) == 1
    run_id = r1.started[0][1]
    assert store.load(run_id).status is RunStatus.WAITING

    # Reject -> the loop-back to claim_issue would be iteration 2 > max(1) -> abort.
    _reject_all_waiting(store, notifier)
    r2 = sched.tick()

    # The run aborted during resume...
    assert store.load(run_id).status is RunStatus.ABORTED
    # ...and was HANDED OFF by supervise this same tick, NOT left blocked.
    assert r2.handed_off == [(number, "aborted")]
    issue = gh.get_issue(repo="acme/app", number=number)
    assert co.state_of(issue) == co.QUEUED        # requeued for a fresh run (handoff)
    assert co.state_of(issue) != co.BLOCKED       # the bug would have blocked it here
    assert co.HANDOFF in issue.labels


def test_handoff_cap_boundary_via_real_cross_tick_cycles(tmp_path: Path) -> None:
    # The CAP boundary driven by REAL handoff cycles (not hand-seeded counts): tests
    # never pass, so each tick claims the issue, starts a run that ABORTS at
    # max_iterations, and supervise hands it off — until prior terminal attempts exceed
    # HANDOFF_CAP (2), at which point the 3rd cycle BLOCKS instead.
    clock = Clock()
    proj = _capped_project("app", max_iterations=2)
    sched, gh, store, _ = _make(
        tmp_path, [proj], clock=clock, executor=AlwaysFailsTestExecutor(),
    )
    (number,) = _queue(gh, "acme/app", 1)

    # Tick 1: attempt #1 aborts -> 1 prior terminal run (<= cap) -> handoff.
    r1 = sched.tick()
    assert r1.handed_off == [(number, "aborted")]
    assert co.state_of(gh.get_issue(repo="acme/app", number=number)) == co.QUEUED

    # Tick 2: attempt #2 aborts -> 2 prior terminal runs (== cap) -> still handoff.
    r2 = sched.tick()
    assert r2.handed_off == [(number, "aborted")]
    assert co.state_of(gh.get_issue(repo="acme/app", number=number)) == co.QUEUED

    # Tick 3: attempt #3 aborts -> 3 prior terminal runs (> cap) -> BLOCKED for a human.
    r3 = sched.tick()
    assert r3.handed_off == [(number, "blocked: handoff cap reached")]
    issue = gh.get_issue(repo="acme/app", number=number)
    assert co.state_of(issue) == co.BLOCKED       # no further requeue
    # Three real dev_task runs were recorded for this issue across the three ticks.
    runs = [r for r in store.list() if r.loop_name == "dev_task"
            and r.data.get("issue_number") == number]
    assert len(runs) == 3 and all(r.status is RunStatus.ABORTED for r in runs)
