"""The scheduler — the "manager" that allocates attention across many repos.

One :meth:`Scheduler.tick` is a single, idempotent scheduling pass (what Windows
Task Scheduler / launchd / ``harness watch`` invoke on a cadence):

1. **Resume first.** Drain answered gates before adding new work — in-flight work
   outranks starting more, which keeps the human's gate backlog (and gate fatigue)
   bounded. This subsumes the old ``poll``.
2. **Global spend gate.** If this window's spend across all owned projects has hit
   ``global_spend_ceiling_usd``, start nothing new (running gates still resume).
3. **Eligibility.** A project may *start* a dev_task run iff: its
   ``min_poll_interval_seconds`` has elapsed (cadence — the "check the low-effort
   repo less often" knob), it has no active run already, there is queued work, and
   there is concurrency headroom.
4. **Weighted selection.** Among eligible projects, pick by weighted deficit
   round-robin (``weight / (1 + starts_this_window)``) so high-priority repos get
   proportionally more starts without starving the rest.
5. **arch_review cadence.** Separately, run a project's bounded arch_review at its
   (typically much lower) cadence; its filed issues become next tick's queue.

The only added state is a small JSON ledger (``.harness/scheduler.json``), atomic
-written, holding per-project cadence timestamps + this window's run ids. The
scheduler *starts* and *resumes* runs; it never holds a loop in memory — a started
run that hits a gate persists WAITING and the tick moves on (exit-and-resume).

The clock is injectable so cadence/window logic is deterministically testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field

from harness.application import coordination as co
from harness.application.loop_runner import LoopRunner
from harness.config.models import HarnessConfig, ProjectConfig
from harness.domain.models import BreakerState, RunRecord, RunStatus
from harness.ports.github import GitHubAdapter
from harness.ports.notifier import Notifier
from harness.ports.project_registry import ProjectNotFound, ProjectRegistry
from harness.ports.run_store import RunStore
from harness.util.atomic import atomic_write_text

_WINDOW_SECONDS = {"daily": 86_400.0, "rolling_24h": 86_400.0}

RunnerFactory = Callable[[str, Optional[ProjectConfig]], LoopRunner]
BreakersFactory = Callable[[ProjectConfig], BreakerState]


class SchedulerLedger(BaseModel):
    """Durable, plain-data scheduler state. Round-trips through JSON like RunRecord."""

    window_start: float = 0.0
    window_run_ids: list[str] = Field(default_factory=list)   # runs started this window
    last_started: dict[str, float] = Field(default_factory=dict)       # project -> epoch
    last_arch_review: dict[str, float] = Field(default_factory=dict)   # project -> epoch
    last_pr_review: dict[str, float] = Field(default_factory=dict)     # project -> epoch
    started_count: dict[str, int] = Field(default_factory=dict)        # project -> starts/window


@dataclass
class TickReport:
    resumed: list[tuple[str, str]]            # (run_id, status)
    started: list[tuple[str, str, str]]       # (project_id, run_id, status)
    window_spend_usd: float
    halted_for_spend: bool


class Scheduler:
    def __init__(
        self,
        *,
        cfg: HarnessConfig,
        store: RunStore,
        registry: ProjectRegistry,
        github: GitHubAdapter,
        notifier: Notifier,
        runner_factory: RunnerFactory,
        breakers_factory: BreakersFactory,
        ledger_path: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.registry = registry
        self.github = github
        self.notifier = notifier
        self.runner_factory = runner_factory
        self.breakers_factory = breakers_factory
        self.ledger_path = Path(ledger_path)
        self.clock = clock

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #
    def resume_waiting(self) -> list[tuple[str, str]]:
        """Resume every WAITING run whose answer has arrived. Idempotent."""
        out: list[tuple[str, str]] = []
        for record in self.store.list(status=RunStatus.WAITING):
            req = record.pending_request
            if req is None:
                continue
            response = self.notifier.collect(req)
            if response is None:
                continue
            runner = self.runner_factory(record.loop_name, self._project_for(record))
            status = runner.resume(record.run_id, response)
            archive = getattr(self.notifier, "archive", None)
            if archive is not None:
                archive(req.request_id)
            co.block_dev_issue_if_aborted(
                self.github, record=self.store.load(record.run_id), status=status
            )
            out.append((record.run_id, status.value))
        return out

    def tick(self) -> TickReport:
        now = self.clock()
        sc = self.cfg.scheduling
        ledger = self._load_ledger()
        self._roll_window(ledger, now, sc.spend_window)

        resumed = self.resume_waiting()

        started: list[tuple[str, str, str]] = []
        window_spend = self._window_spend(ledger)
        halted = window_spend >= sc.global_spend_ceiling_usd
        if not halted:
            started += self._start_dev_tasks(now, ledger)
            started += self._start_pr_reviews(now, ledger)
            started += self._start_arch_reviews(now, ledger)

        self._save_ledger(ledger)
        return TickReport(
            resumed=resumed,
            started=started,
            window_spend_usd=self._window_spend(ledger),
            halted_for_spend=halted,
        )

    # ------------------------------------------------------------------ #
    # Starting work
    # ------------------------------------------------------------------ #
    def _start_dev_tasks(self, now: float, ledger: SchedulerLedger) -> list[tuple[str, str, str]]:
        sc = self.cfg.scheduling
        instance_id = self.cfg.instance.instance_id
        active = self._active_runs()
        active_projects = {r.project_id for r in active}
        slots = sc.max_concurrent_runs - len(active)
        if slots <= 0:
            return []

        candidates: list[tuple[ProjectConfig, int]] = []
        for project in self.registry.list_owned(instance_id):
            psc = project.scheduling
            if "dev_task" not in psc.loops:
                continue
            if project.id in active_projects:           # one active run per project
                continue
            last = ledger.last_started.get(project.id)   # None => never started => due now
            if last is not None and now - last < psc.min_poll_interval_seconds:
                continue                                 # cadence: not due yet
            number = co.find_claimable(self.github, repo=project.repo, instance_id=instance_id)
            if number is None:                           # no queued work
                continue
            candidates.append((project, number))

        # weighted deficit round-robin: more weight + fewer starts this window => first
        candidates.sort(
            key=lambda pn: pn[0].scheduling.effective_weight(sc.default_weight)
            / (1 + ledger.started_count.get(pn[0].id, 0)),
            reverse=True,
        )

        started: list[tuple[str, str, str]] = []
        for project, number in candidates:
            if slots <= 0:
                break
            remaining = sc.global_spend_ceiling_usd - self._window_spend(ledger)
            if remaining <= 0:
                break
            # Claim now (confirm-read race-safe) before committing a run.
            claim = co.claim(self.github, repo=project.repo, number=number, instance_id=instance_id)
            if not claim.ok:
                continue
            breakers = self.breakers_factory(project)
            # A single run can never exceed the budget remaining in this window.
            breakers.budget_ceiling_usd = min(breakers.budget_ceiling_usd, remaining)
            runner = self.runner_factory("dev_task", project)
            record = runner.create_run(
                project_id=project.id,
                breakers=breakers,
                data={"issue_number": number, "repo": project.repo},
            )
            status = runner.run(record.run_id)
            co.block_dev_issue_if_aborted(
                self.github, record=self.store.load(record.run_id), status=status
            )
            ledger.last_started[project.id] = now
            ledger.started_count[project.id] = ledger.started_count.get(project.id, 0) + 1
            ledger.window_run_ids.append(record.run_id)
            slots -= 1
            started.append((project.id, record.run_id, status.value))
        return started

    def _start_pr_reviews(self, now: float, ledger: SchedulerLedger) -> list[tuple[str, str, str]]:
        """Start a pr_review run per owned project that has open work and is due.

        Like dev_task (and unlike the bounded arch_review) a pr_review can suspend on a
        gated merge, so it respects ``max_concurrent_runs`` and one-active-per-project.
        Cross-instance/cross-run dedup is structural: selection is scoped to THIS
        instance's ``harness/<instance>/issue-N`` PRs, so two machines never race.
        """
        sc = self.cfg.scheduling
        instance_id = self.cfg.instance.instance_id
        active = self._active_runs()
        active_projects = {r.project_id for r in active}
        slots = sc.max_concurrent_runs - len(active)
        out: list[tuple[str, str, str]] = []
        for project in self.registry.list_owned(instance_id):
            if slots <= 0:
                break
            cadence = project.scheduling.pr_review_cadence_seconds
            if cadence is None:                              # opt-in; None => never auto-run
                continue
            if project.id in active_projects:                # one active run per project
                continue
            last = ledger.last_pr_review.get(project.id)     # None => never => due now
            if last is not None and now - last < cadence:
                continue
            if self._window_spend(ledger) >= sc.global_spend_ceiling_usd:
                break
            if co.find_reviewable_pr(self.github, repo=project.repo, instance_id=instance_id) is None:
                continue                                     # nothing to review
            breakers = self.breakers_factory(project)
            remaining = sc.global_spend_ceiling_usd - self._window_spend(ledger)
            breakers.budget_ceiling_usd = min(breakers.budget_ceiling_usd, remaining)
            runner = self.runner_factory("pr_review", project)
            record = runner.create_run(project_id=project.id, breakers=breakers)
            status = runner.run(record.run_id)
            ledger.last_pr_review[project.id] = now
            ledger.window_run_ids.append(record.run_id)
            active_projects.add(project.id)
            slots -= 1
            out.append((project.id, record.run_id, status.value))
        return out

    def _start_arch_reviews(self, now: float, ledger: SchedulerLedger) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for project in self.registry.list_owned(self.cfg.instance.instance_id):
            cadence = project.scheduling.arch_review_cadence_seconds
            if cadence is None:
                continue
            last = ledger.last_arch_review.get(project.id)  # None => never reviewed => due now
            if last is not None and now - last < cadence:
                continue
            if self._window_spend(ledger) >= self.cfg.scheduling.global_spend_ceiling_usd:
                break
            runner = self.runner_factory("arch_review", project)
            record = runner.create_run(
                project_id=project.id, breakers=self.breakers_factory(project)
            )
            status = runner.run(record.run_id)  # bounded; never suspends
            ledger.last_arch_review[project.id] = now
            ledger.window_run_ids.append(record.run_id)
            out.append((project.id, record.run_id, status.value))
        return out

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _active_runs(self) -> list[RunRecord]:
        return self.store.list(status=RunStatus.WAITING) + self.store.list(status=RunStatus.RUNNING)

    def _window_spend(self, ledger: SchedulerLedger) -> float:
        total = 0.0
        for rid in ledger.window_run_ids:
            if self.store.exists(rid):
                total += self.store.load(rid).breakers.cumulative_cost_usd
        return total

    def _project_for(self, record: RunRecord) -> Optional[ProjectConfig]:
        if not record.project_id:
            return None
        try:
            return self.registry.get(record.project_id)
        except ProjectNotFound:
            return None

    @staticmethod
    def _roll_window(ledger: SchedulerLedger, now: float, spend_window: str) -> None:
        window_len = _WINDOW_SECONDS.get(spend_window, 86_400.0)
        if ledger.window_start <= 0.0 or now - ledger.window_start >= window_len:
            ledger.window_start = now
            ledger.window_run_ids = []
            ledger.started_count = {}

    def _load_ledger(self) -> SchedulerLedger:
        if self.ledger_path.is_file():
            return SchedulerLedger.model_validate_json(self.ledger_path.read_text("utf-8"))
        return SchedulerLedger()

    def _save_ledger(self, ledger: SchedulerLedger) -> None:
        atomic_write_text(self.ledger_path, ledger.model_dump_json(indent=2))
