"""Use-cases shared by every driving surface (CLI + web dashboard).

These functions are the single implementation of "start a run", "answer a gate",
"abort a run", "tick the scheduler", and the read-side aggregates the dashboard
renders. The Typer commands and the FastAPI endpoints both delegate here, so the
engine logic lives in exactly one place and is testable without Typer or HTTP.

Everything routes through the same durable :class:`~harness.domain.models.RunRecord`
writes the CLI has always used; there is no second, weaker path to engine state.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from harness.application import coordination as co
from harness.application.ownership import owns
from harness.application.scheduler import SchedulerLedger, TickReport
from harness.container import (
    Container,
    breakers_for,
    build_runner,
    build_scheduler,
    mark_blocked_if_aborted,
)
from harness.domain.models import (
    RunRecord,
    RunStatus,
    TERMINAL_STATUSES,
    VerificationResponse,
    utcnow_iso,
)
from harness.ports.project_registry import ProjectNotFound
from harness.ports.run_store import RunNotFound


class NotOwned(RuntimeError):
    """This install does not own the project, so it may read but not act on it."""


class NoQueuedWork(RuntimeError):
    """A dev_task start found no claimable queued issue."""


class InvalidAnswer(RuntimeError):
    """Tried to answer a run that is not WAITING."""


_ACTIVE = (RunStatus.RUNNING, RunStatus.WAITING)
_RECENT_LIMIT = 15


# --------------------------------------------------------------------------- #
# Write use-cases
# --------------------------------------------------------------------------- #
def create_run_for(
    c: Container, *, loop: str, project_id: str, issue: Optional[int] = None
) -> RunRecord:
    """Validate ownership, resolve dev_task work, and persist a CREATED run.

    Split from :func:`execute_run` so a caller (the web server) can return the
    new run id immediately and dispatch the (blocking) execution in the background.
    """
    proj = c.registry.get(project_id)
    if not owns(proj, c.cfg.instance):
        raise NotOwned(
            f"instance '{c.cfg.instance.instance_id}' does not own project "
            f"'{project_id}' (owner: {proj.owner_instance})"
        )

    data: Optional[dict[str, Any]] = None
    if loop == "dev_task":
        number = issue if issue is not None else co.find_claimable(
            c.github, repo=proj.repo, instance_id=c.cfg.instance.instance_id
        )
        if number is None:
            raise NoQueuedWork(f"no queued work for '{project_id}' ({proj.repo})")
        data = {"issue_number": number, "repo": proj.repo}

    runner = build_runner(c, loop, proj)
    return runner.create_run(project_id=project_id, breakers=breakers_for(c.cfg, proj), data=data)


def execute_run(c: Container, *, loop_name: str, project_id: Optional[str], run_id: str) -> RunStatus:
    """Dispatch a CREATED/RUNNING run to its next gate or terminal state.

    Blocks (claude + build + test can take minutes). The CLI calls this inline;
    the web server calls it on a background thread and lets the UI poll.
    """
    proj = c.registry.get(project_id) if project_id else None
    runner = build_runner(c, loop_name, proj)
    status = runner.run(run_id)
    mark_blocked_if_aborted(c, run_id, status)
    return status


def start_run(
    c: Container, *, loop: str, project_id: str, issue: Optional[int] = None
) -> tuple[RunRecord, RunStatus]:
    """create + execute, inline. Convenience for the CLI's one-shot ``run`` command."""
    record = create_run_for(c, loop=loop, project_id=project_id, issue=issue)
    status = execute_run(c, loop_name=loop, project_id=project_id, run_id=record.run_id)
    return record, status


def answer_run(c: Container, *, run_id: str, approved: bool, notes: str = "") -> RunStatus:
    """Deliver a verification answer to a WAITING run and resume it.

    Mirrors the ``answer`` CLI command exactly: persist the answer (for the file
    notifier, so a concurrent poller sees it too), resume, archive, relabel on abort.
    """
    record = c.store.load(run_id)
    if record.status is not RunStatus.WAITING or record.pending_request is None:
        raise InvalidAnswer(f"run {run_id} is {record.status.value}; nothing to answer")
    req = record.pending_request
    response = VerificationResponse(
        request_id=req.request_id,
        run_id=record.run_id,
        step_id=req.step_id,
        answer={"approved": approved, "notes": notes},
        approved=approved,
        via="ui",
    )
    runner = build_runner(c, record.loop_name, _project_for(c, record.project_id))
    write_response = getattr(c.notifier, "write_response", None)
    if write_response is not None:
        write_response(response)
    status = runner.resume(run_id, response)
    archive = getattr(c.notifier, "archive", None)
    if archive is not None:
        archive(req.request_id)
    mark_blocked_if_aborted(c, run_id, status)
    return status


def abort_run(c: Container, *, run_id: str, reason: str = "aborted by operator") -> RunStatus:
    """Operator-initiated abort. New primitive — the engine only aborts via breakers.

    Meaningful for a WAITING run (cancel one awaiting a gate) and for a stale
    RUNNING record (a crashed mid-step run). A *live* RUNNING run in another
    process cannot be signal-killed from here — there is no shared kill channel —
    and that process would overwrite this record on its next save. We mark the
    durable record and relabel the issue best-effort; we never claim to kill work.
    """
    record = c.store.load(run_id)
    if record.status in TERMINAL_STATUSES:
        return record.status
    record.status = RunStatus.ABORTED
    record.terminal_reason = reason
    record.current_step = None
    record.updated_at = utcnow_iso()
    c.store.save(record)
    mark_blocked_if_aborted(c, run_id, RunStatus.ABORTED)
    return RunStatus.ABORTED


def tick_once(c: Container) -> TickReport:
    """One scheduling pass (resume answered gates, then start eligible work)."""
    return build_scheduler(c).tick()


# --------------------------------------------------------------------------- #
# Read-side aggregates (for the dashboard)
# --------------------------------------------------------------------------- #
def overview(c: Container) -> dict[str, Any]:
    """Always-fresh local state: active + recent runs, counts, window spend.

    Pure file reads (cheap), so the dashboard may poll this often. GitHub-derived
    board data is computed separately by :func:`board` and cached by the caller.
    """
    runs = c.store.list()
    active = [r for r in runs if r.status in _ACTIVE]
    recent = [r for r in reversed(runs) if r.status in TERMINAL_STATUSES][:_RECENT_LIMIT]
    counts = Counter(r.status.value for r in runs)
    ledger = _load_ledger(c)
    return {
        "instance": c.cfg.instance.instance_id,
        "active": [run_summary(r) for r in active],
        "recent": [run_summary(r) for r in recent],
        "counts": {s.value: counts.get(s.value, 0) for s in RunStatus},
        "totals": {
            "active": len(active),
            "waiting": sum(1 for r in active if r.status is RunStatus.WAITING),
            "runs": len(runs),
        },
        "spend": {
            "window_usd": _window_spend(c, ledger),
            "ceiling_usd": c.cfg.scheduling.global_spend_ceiling_usd,
            "window": c.cfg.scheduling.spend_window,
        },
        "scheduling_enabled": c.cfg.scheduling.enabled,
    }


def board(c: Container) -> dict[str, Any]:
    """GitHub-derived queue/board state per owned project. Network-bound — the web
    server caches this with a short TTL so fast polling never hammers the API."""
    projects: list[dict[str, Any]] = []
    for p in c.registry.list_owned(c.cfg.instance.instance_id):
        entry: dict[str, Any] = {"id": p.id, "repo": p.repo}
        try:
            issues = c.github.list_issues(repo=p.repo, state="open")
            states = Counter(co.state_of(i) for i in issues)
            pulls = c.github.list_pulls(repo=p.repo, state="open")
            entry.update(
                queued=states.get(co.QUEUED, 0),
                in_progress=states.get(co.IN_PROGRESS, 0),
                needs_verification=states.get(co.NEEDS_VERIFICATION, 0),
                pr_open=states.get(co.PR_OPEN, 0),
                blocked=states.get(co.BLOCKED, 0),
                open_prs=len(pulls),
            )
        except Exception as exc:  # noqa: BLE001 — a board read must never 500 the page
            entry["error"] = str(exc)
        projects.append(entry)
    return {"projects": projects}


def run_summary(r: RunRecord) -> dict[str, Any]:
    """The compact projection the minimized dashboard view renders per run."""
    return {
        "run_id": r.run_id,
        "loop": r.loop_name,
        "project": r.project_id,
        "status": r.status.value,
        "current_step": r.current_step,
        "iter": r.breakers.loop_count,
        "max_iter": r.breakers.max_iterations,
        "cost_usd": round(r.breakers.cumulative_cost_usd, 4),
        "budget_usd": r.breakers.budget_ceiling_usd,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "has_gate": r.pending_request is not None,
        "gate_prompt": r.pending_request.prompt if r.pending_request else None,
        "issue": r.data.get("issue_number"),
        "repo": r.data.get("repo"),
        "branch": r.data.get("branch"),
        "pr_url": r.data.get("pr_url"),
        "sweep_id": r.data.get("sweep_id"),  # forward-compat: groups a sweep's fan-out
        "terminal_reason": r.terminal_reason,
        "machine_id": r.machine_id,
    }


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _project_for(c: Container, project_id: Optional[str]):  # type: ignore[no-untyped-def]
    if not project_id:
        return None
    try:
        return c.registry.get(project_id)
    except ProjectNotFound:
        return None


def _load_ledger(c: Container) -> SchedulerLedger:
    path = c.store.root / "scheduler.json"
    if path.is_file():
        return SchedulerLedger.model_validate_json(path.read_text("utf-8"))
    return SchedulerLedger()


def _window_spend(c: Container, ledger: SchedulerLedger) -> float:
    """Sum cost across this window's runs. Mirrors ``Scheduler._window_spend`` (kept
    here so the read path needs no fully-wired scheduler)."""
    total = 0.0
    for rid in ledger.window_run_ids:
        try:
            total += c.store.load(rid).breakers.cumulative_cost_usd
        except RunNotFound:
            continue
    return round(total, 4)
