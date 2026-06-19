"""The overseer — one supervisor invoked each scheduler tick.

Where the :class:`~harness.application.scheduler.Scheduler` allocates *attention*
(which project to start next), the overseer watches the runs that are already
in flight and keeps the GitHub board honest. It is pure orchestration over the
same ports the rest of the engine uses (store, github, registry, executor,
notifier); it owns no git or network of its own and is deterministic via an
injected clock, so a tick is fully testable against in-memory fakes.

Each :meth:`supervise` pass does three things, in order:

1. **Reconcile stranded leases.** An issue this instance owns and left
   ``in-progress`` whose run is neither active nor recoverable (terminal or
   gone) is a lease nobody is working — release it back to ``queued`` so it can
   be picked up again. (This subsumes the old standalone stale-lease reconciler.)
2. **Handoff aborted/failed work.** A dev_task run in the current wave that ended
   ABORTED/FAILED, whose issue is still owned/in-progress, is requeued for a
   FRESH continuation via :func:`coordination.handoff_issue` (it gets a fresh
   per-run budget and the prior attempt's context). Capped at ``HANDOFF_CAP``
   prior attempts — past the cap the issue is left ``blocked`` for a human.
3. **Draft the wave PR.** When every run in the open wave is terminal AND no
   owned repo has claimable work left, the wave is done: aggregate one commit
   per COMPLETED run onto a wave branch and open ONE draft PR. The PR is then
   promoted to ready-for-review iff the project's autonomy taxonomy admits
   ``mark_pr_ready`` (a self-managed repo opts in; the safe default stays a draft
   for a human). The harness never merges — promotion to ready is the most it
   does. Idempotent — once ``status == "pr_drafted"`` a later tick is a no-op.

The wave itself is plain data on the scheduler ledger (:class:`WaveState`); the
overseer only opens it, registers runs into it, and closes it with a PR.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

from harness.application import coordination as co
from harness.application.action_guard import (
    ActionGuard,
    ActionRequest,
    ForbiddenAction,
    GateRequired,
)
from harness.config.models import HarnessConfig, ProjectConfig
from harness.domain.models import RunRecord, RunStatus, TERMINAL_STATUSES, new_id
from harness.ports.executor import Executor, WaveAssembly
from harness.ports.github import GitHubAdapter
from harness.ports.notifier import Notifier
from harness.ports.project_registry import ProjectRegistry
from harness.ports.run_store import RunStore

if TYPE_CHECKING:
    from harness.application.scheduler import SchedulerLedger, WaveState


@dataclass
class OverseerReport:
    """What one :meth:`Overseer.supervise` pass did, for the tick report / CLI."""

    reconciled: list[int] = field(default_factory=list)          # issue numbers requeued
    handed_off: list[tuple[int, str]] = field(default_factory=list)  # (issue, reason)
    wave_pr: Optional[str] = None                                # draft PR url, if drafted


#: Harness states from which a wave run's abort/fail is still handed off: the issue
#: is actively being worked by us. A verify-gate timeout aborts the run while the
#: issue sits in NEEDS_VERIFICATION, so that counts too. Any OTHER state (QUEUED =
#: a prior pass already requeued it; BLOCKED/DONE/PR_OPEN = already resolved) means
#: there is nothing left to hand off (idempotent).
_HANDOFF_FROM_STATES = frozenset({co.IN_PROGRESS, co.NEEDS_VERIFICATION})


class Overseer:
    """Supervises in-flight runs and the current wave; see the module docstring."""

    #: Max number of prior terminal attempts before an issue is left ``blocked``
    #: instead of handed off again (2 handoffs => 3 total tries).
    HANDOFF_CAP = 2

    def __init__(
        self,
        *,
        cfg: HarnessConfig,
        store: RunStore,
        github: GitHubAdapter,
        registry: ProjectRegistry,
        executor: Executor,
        notifier: Notifier,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.github = github
        self.registry = registry
        self.executor = executor
        self.notifier = notifier
        self.clock = clock

    # ------------------------------------------------------------------ #
    # Wave bookkeeping (plain-data mutation of the ledger)
    # ------------------------------------------------------------------ #
    def current_or_new_wave(self, ledger: "SchedulerLedger") -> str:
        """Return the open wave's id, opening a fresh wave if none is open."""
        from harness.application.scheduler import WaveState

        if ledger.current_wave is None:
            ledger.current_wave = WaveState(wave_id=new_id(), opened_at=self.clock())
        return ledger.current_wave.wave_id

    def register_run(self, ledger: "SchedulerLedger", wave_id: str, run_id: str) -> None:
        """Add a started run to the wave it belongs to (idempotent)."""
        wave = ledger.current_wave
        if wave is None or wave.wave_id != wave_id:
            return
        if run_id not in wave.run_ids:
            wave.run_ids.append(run_id)

    # ------------------------------------------------------------------ #
    # The per-tick pass
    # ------------------------------------------------------------------ #
    def supervise(
        self, ledger: "SchedulerLedger", active_run_ids: set[str]
    ) -> OverseerReport:
        report = OverseerReport()
        instance_id = self.cfg.instance.instance_id
        owned = self.registry.list_owned(instance_id)

        # Handoff first: a current-wave ABORTED/FAILED run is a deliberate
        # continuation (requeue + context), NOT a lease to silently release. Then
        # reconcile mops up any remaining stranded leases not owned by the wave.
        report.handed_off = self._handoff_terminal(ledger, instance_id)
        report.reconciled = self._reconcile_stranded(
            ledger, owned, instance_id, active_run_ids
        )
        report.wave_pr = self._maybe_draft_wave_pr(ledger, owned, instance_id)
        return report

    # ------------------------------------------------------------------ #
    # (a) reconcile stranded leases
    # ------------------------------------------------------------------ #
    def _reconcile_stranded(
        self,
        ledger: "SchedulerLedger",
        owned: list[ProjectConfig],
        instance_id: str,
        active_run_ids: set[str],
    ) -> list[int]:
        """Release ``in-progress`` issues we own whose run is not active and whose
        latest run is terminal or missing — nobody is working that lease.

        Current-wave runs are deliberately excluded: an ABORTED/FAILED wave run was
        already handed off (or blocked) above, so releasing it here would undo that.
        Reconcile is the safety net for leases the wave does not account for (e.g. a
        crash from a prior process/wave that left a lease behind)."""
        wave_run_ids = set(ledger.current_wave.run_ids) if ledger.current_wave else set()
        reconciled: list[int] = []
        for project in owned:
            for issue in self.github.list_issues(
                repo=project.repo, state="open", labels=[co.IN_PROGRESS]
            ):
                if co.owner_of(issue) != instance_id:
                    continue
                latest = self._latest_dev_run(project.repo, issue.number)
                if latest is not None and (
                    latest.run_id in active_run_ids
                    or latest.run_id in wave_run_ids
                    or latest.status not in TERMINAL_STATUSES
                ):
                    continue  # active, owned by the wave, or still in-flight
                co.release(self.github, repo=project.repo, number=issue.number)
                reconciled.append(issue.number)
        return reconciled

    # ------------------------------------------------------------------ #
    # (b) handoff aborted/failed dev_task runs in the current wave
    # ------------------------------------------------------------------ #
    def _handoff_terminal(
        self, ledger: "SchedulerLedger", instance_id: str
    ) -> list[tuple[int, str]]:
        wave = ledger.current_wave
        if wave is None:
            return []
        handed_off: list[tuple[int, str]] = []
        done: set[int] = set()  # one decision per issue per pass
        for run_id in wave.run_ids:
            if not self.store.exists(run_id):
                continue
            record = self.store.load(run_id)
            if record.loop_name != "dev_task":
                continue
            repo = record.data.get("repo")
            number = record.data.get("issue_number")
            if not (repo and number):
                continue
            number = int(number)
            if number in done:
                continue
            done.add(number)  # one decision per issue, regardless of which run we hit first
            # Decide from the LATEST terminal dev_task run for this issue, not whichever
            # wave run happened to come first in run_ids: after handoff -> re-claim ->
            # re-abort the run_ids carry the stale older record too, and the packet/abort
            # reason must reflect the most recent attempt.
            latest = self._latest_dev_run(repo, number)
            if latest is None or latest.status not in (RunStatus.ABORTED, RunStatus.FAILED):
                continue
            # Only act while the issue is still ours AND in a worked state (in-progress
            # or needs-verification — a verify-gate timeout aborts from the latter); if a
            # human or a prior pass already requeued/blocked/resolved it (or it's gone),
            # leave it be (idempotent).
            try:
                issue = self.github.get_issue(repo=repo, number=number)
            except KeyError:
                continue
            if (
                co.owner_of(issue) != instance_id
                or co.state_of(issue) not in _HANDOFF_FROM_STATES
            ):
                continue

            prior_attempts = self._count_prior_attempts(repo, number)
            if prior_attempts <= self.HANDOFF_CAP:
                co.handoff_issue(
                    self.github,
                    repo=repo,
                    number=number,
                    note=self._handoff_packet(latest, prior_attempts),
                )
                handed_off.append((number, latest.status.value.lower()))
            else:
                co.transition(self.github, repo=repo, number=number, to_state=co.BLOCKED)
                handed_off.append((number, "blocked: handoff cap reached"))
        return handed_off

    # ------------------------------------------------------------------ #
    # (c) wave completion -> one aggregated draft PR
    # ------------------------------------------------------------------ #
    def _maybe_draft_wave_pr(
        self, ledger: "SchedulerLedger", owned: list[ProjectConfig], instance_id: str
    ) -> Optional[str]:
        wave = ledger.current_wave
        if wave is None or wave.status != "open":
            return None  # no open wave, or its PR is already drafted (idempotent)
        if not wave.run_ids:
            return None
        # Every run in the wave must be terminal...
        if not all(self._is_terminal(rid) for rid in wave.run_ids):
            return None
        # ...and no owned repo may still have claimable work (the backlog is drained).
        for project in owned:
            if co.find_claimable(
                self.github, repo=project.repo, instance_id=instance_id
            ) is not None:
                return None

        wave_records = [
            self.store.load(rid) for rid in wave.run_ids if self.store.exists(rid)
        ]
        completed = [
            r
            for r in wave_records
            if r.status is RunStatus.COMPLETED and r.loop_name == "dev_task"
        ]
        # Group completed runs by repo and draft ONE wave PR per repo: the drain check
        # spans every owned project, so a wave may carry work from several repos —
        # branches from repo A can't be cherry-picked onto repo B's wave branch.
        by_repo: dict[str, list[RunRecord]] = {}
        for r in completed:
            repo = r.data.get("repo")
            if repo and r.data.get("branch"):
                by_repo.setdefault(repo, []).append(r)

        first_url: Optional[str] = None
        # Whatever happens below, the wave is done being supervised: recycle it so the
        # next backlog drain opens a FRESH wave instead of re-joining this dead one.
        # (Without this, post-draft starts join a wave that already drafted and never
        # get a PR of their own — the A1 recycle bug.)
        for repo, repo_completed in by_repo.items():
            # Each repo's drafting is isolated: an executor/GitHub error on one repo's
            # wave PR is logged and skipped, never crashing supervise()/tick().
            try:
                url = self._draft_one_repo_wave_pr(
                    wave, repo, repo_completed, owned, instance_id
                )
            except Exception as exc:  # noqa: BLE001 — a PR failure must not crash the tick
                self.notifier.warn(
                    f"wave PR drafting failed for {repo}: {exc!r}"
                )
                continue
            if url is not None and first_url is None:
                first_url = url
        ledger.current_wave = None
        return first_url

    def _draft_one_repo_wave_pr(
        self,
        wave: "WaveState",
        repo: str,
        completed: list[RunRecord],
        owned: list[ProjectConfig],
        instance_id: str,
    ) -> Optional[str]:
        """Assemble + open + mark ONE repo's aggregated wave PR. Returns the url, or
        None if the project is no longer owned or nothing assembled cleanly."""
        wave_project = self._project_by_repo(owned, repo)
        if wave_project is None:
            return None  # the completed run's project is no longer owned here
        branches = [r.data["branch"] for r in completed if r.data.get("branch")]
        # Aggregate onto a namespaced wave branch (the executor guard requires it).
        wave_branch = f"harness/{instance_id}/wave-{wave.wave_id[:8]}"
        assembly = self.executor.assemble_wave_branch(
            project=wave_project,
            wave_branch=wave_branch,
            source_branches=branches,
        )
        if not assembly.included:
            # Every branch was skipped (all conflicted) — opening a 0-change PR would be
            # noise (and GitHub would reject it). Warn and draft nothing for this repo.
            self.notifier.warn(
                f"wave {wave.wave_id[:8]}: every branch skipped on {repo}; no PR drafted"
            )
            return None
        pr = self.github.open_draft_pr(
            repo=repo,
            head=assembly.branch,
            base="main",
            title=f"harness wave {wave.wave_id[:8]}: {len(assembly.included)} change(s)",
            body=self._wave_pr_body(completed, assembly),
        )
        # The invariant is preserved (we always OPEN a draft); promotion to ready is a
        # separate, gated step. A self-managed project that marks `mark_pr_ready`
        # autonomous gets a regular PR a reviewing agent can pick up; the safe default
        # (gated/forbidden) leaves it a draft for a human. Merge is always external.
        guard = ActionGuard(wave_project.effective_autonomy(self.cfg.autonomy))
        ready = False
        try:
            guard.admit(ActionRequest("mark_pr_ready", wave_project.id))
            self.github.mark_pr_ready(repo=repo, number=pr.number)
            ready = True
        except (GateRequired, ForbiddenAction):
            pass  # not autonomous for this project -> leave the wave PR a draft
        self.github.comment_on_issue(
            repo=repo,
            number=completed[0].data["issue_number"],
            body=(
                f"Wave PR {'opened (ready for review)' if ready else 'drafted'} "
                f"aggregating {len(assembly.included)} change(s): {pr.url}"
            ),
        )
        wave.pr_url = pr.url
        wave.status = "pr_drafted"
        return pr.url

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _is_terminal(self, run_id: str) -> bool:
        if not self.store.exists(run_id):
            return True  # a vanished run can never become active again
        return self.store.load(run_id).status in TERMINAL_STATUSES

    def _dev_runs_for_issue(self, repo: str, number: int) -> list[RunRecord]:
        """All dev_task runs for this (repo, issue), oldest-first by creation."""
        out = [
            r
            for r in self.store.list()
            if r.loop_name == "dev_task"
            and r.data.get("repo") == repo
            and r.data.get("issue_number") == number
        ]
        out.sort(key=lambda r: r.created_at)
        return out

    def _latest_dev_run(self, repo: str, number: int) -> Optional[RunRecord]:
        runs = self._dev_runs_for_issue(repo, number)
        return runs[-1] if runs else None

    def _count_prior_attempts(self, repo: str, number: int) -> int:
        """Count terminal dev_task runs already recorded for this issue."""
        return sum(
            1
            for r in self._dev_runs_for_issue(repo, number)
            if r.status in TERMINAL_STATUSES
        )

    @staticmethod
    def _project_by_repo(
        owned: list[ProjectConfig], repo: str
    ) -> Optional[ProjectConfig]:
        for project in owned:
            if project.repo == repo:
                return project
        return None

    @staticmethod
    def _handoff_packet(record: RunRecord, prior_attempts: int) -> str:
        """The continuation note posted on the requeued issue: what was attempted,
        why it ended, and the spend, so the fresh run continues instead of cold-starting."""
        b = record.breakers
        lines = [
            "## Prior attempt(s)",
            f"Attempt {prior_attempts + 1} ended **{record.status.value}** "
            f"(reason: {record.terminal_reason or 'n/a'}).",
            f"- branch: `{record.data.get('branch', '?')}`",
            f"- spend: ${b.cumulative_cost_usd:.2f} over {b.loop_count} iteration(s)",
        ]
        last_failure = record.data.get("last_failure")
        if last_failure:
            lines.append(f"- last failure: {last_failure.get('phase', '?')}")
        claude_result = record.data.get("claude_result")
        if claude_result:
            lines += ["", "### What was tried", str(claude_result)[:2000]]
        lines += [
            "",
            "A fresh run is requeued with this context to **continue** the work "
            "(fresh per-run budget). Do not restart cold.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _wave_pr_body(completed: list[RunRecord], assembly: WaveAssembly) -> str:
        rows = []
        for r in completed:
            number = r.data.get("issue_number")
            title = r.data.get("issue_title") or f"issue #{number}"
            mark = "x" if r.data.get("branch") in assembly.included else " "
            rows.append(f"- [{mark}] #{number} — {title} (`{r.data.get('branch', '?')}`)")
        skipped = (
            ["", f"Skipped on conflict: {', '.join(assembly.skipped)}"]
            if assembly.skipped
            else []
        )
        return "\n".join(
            [
                f"Aggregated wave PR — one commit per completed issue ({len(assembly.included)} included).",
                "",
                *rows,
                *skipped,
                "",
                "---",
                "Draft opened autonomously by the harness overseer. A human reviews "
                "and merges — the harness cannot.",
            ]
        )
