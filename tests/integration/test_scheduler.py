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
from harness.application.scheduler import Scheduler
from harness.config.models import (
    AutonomyTier,
    HarnessConfig,
    InstanceInfo,
    ProjectConfig,
    ProjectScheduling,
    SchedulingConfig,
)
from harness.domain.models import BreakerState, RunStatus, VerificationResponse
from harness.loops.arch_review import build_arch_review_loop
from harness.loops.dev_task import build_dev_loop

pytestmark = pytest.mark.integration

INSTANCE = "this-machine"
TAXONOMY = {"open_draft_pr": AutonomyTier.AUTONOMOUS, "file_issue": AutonomyTier.AUTONOMOUS}


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


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
             arch_cadence=None, loops=("dev_task",)) -> ProjectConfig:
    return ProjectConfig(
        id=pid, owner_instance=INSTANCE, repo=f"acme/{pid}",
        scheduling=ProjectScheduling(
            priority=priority, weight=weight, min_poll_interval_seconds=min_interval,
            arch_review_cadence_seconds=arch_cadence, loops=list(loops),
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
            )
        elif loop_name == "arch_review":
            loop = build_arch_review_loop(
                executor=executor, github=gh, guard=guard, project=project, project_root=tmp_path,
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
    sched = Scheduler(
        cfg=cfg, store=store, registry=FakeRegistry(projects), github=gh, notifier=notifier,
        runner_factory=runner_factory, breakers_factory=breakers_factory,
        ledger_path=tmp_path / "sched.json", clock=clock,
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
    assert len(gh.list_pulls(repo="acme/app")) == 1  # draft PR opened


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


def test_tick_reconciles_stale_lease_from_crashed_owner(tmp_path: Path) -> None:
    clock = Clock()
    sched, gh, store, _ = _make(tmp_path, [_project("app")], clock=clock, max_concurrent=1)
    (n,) = _queue(gh, "acme/app", 1)
    # Simulate a crash mid-run: we hold the lease (in-progress, our owner label) but
    # no run was ever persisted, so nothing would otherwise requeue it.
    co.claim(gh, repo="acme/app", number=n, instance_id=INSTANCE)
    assert co.state_of(gh.get_issue(repo="acme/app", number=n)) == co.IN_PROGRESS

    report = sched.tick()
    assert report.reconciled == [("app", n)]  # the reconciler requeued our orphan


def test_tick_leaves_a_live_run_lease_untouched(tmp_path: Path) -> None:
    clock = Clock()
    sched, gh, store, _ = _make(tmp_path, [_project("app")], clock=clock, max_concurrent=1)
    _queue(gh, "acme/app", 1)
    sched.tick()                      # starts a run that gates -> WAITING (a live lease)
    report = sched.tick()             # second pass: the run is still active
    assert report.reconciled == []    # a lease backed by an active run is not reclaimed


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
