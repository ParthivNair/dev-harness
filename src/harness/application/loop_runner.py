"""LoopRunner: the re-entrant state machine at the heart of the harness.

Design contract (exit-and-resume, NOT block-and-wait):

* A loop is a set of named step functions plus a starting step.
* Each step receives a :class:`RunContext` and returns a :class:`StepOutcome`,
  OR raises :class:`VerificationRequired` to suspend for the human.
* The runner persists the full :class:`RunRecord` AFTER EVERY step, so a reboot
  resumes from a clean boundary. State is plain data — the resume position is
  ``current_step`` (a string), never a frozen stack frame.
* Hitting a gate persists ``WAITING`` and RETURNS. The process may die. A later
  :meth:`resume` call (driven by the human, a poller, or the other machine)
  reloads the record, validates the answer, and re-enters the saved step.

Idempotency: completing a step and advancing ``current_step`` happen in ONE
atomic save. So a crash leaves ``current_step`` pointing at a not-yet-recorded
step, which is simply re-run (steps must tolerate at-least-once execution).
Step instances are keyed ``"{step_name}#{loop_count}"`` so a reject->re-run cycle
never collides in the step log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, NoReturn, Optional

import jsonschema

from harness.domain.models import (
    BreakerState,
    RunRecord,
    RunStatus,
    StepRecord,
    TERMINAL_STATUSES,
    VerificationRequest,
    VerificationRequired,
    VerificationResponse,
    utcnow_iso,
)
from harness.ports.notifier import Notifier
from harness.ports.run_store import RunStore


class InvalidResume(RuntimeError):
    """Tried to resume a run that is not WAITING, or with no pending request."""


@dataclass
class StepOutcome:
    """The normal (non-suspending) result of a step."""

    next_step: Optional[str] = None        # None => the loop is complete
    state_patch: dict[str, Any] = field(default_factory=dict)  # merged into record.data
    output: Any = None                     # recorded in the step log


class RunContext:
    """Handed to each step. Reads/writes ``data``; reads recorded answers; records cost."""

    def __init__(self, record: RunRecord, step_id: str) -> None:
        self._record = record
        self.step_id = step_id
        self.run_id = record.run_id
        self.loop_count = record.breakers.loop_count
        self.data = record.data  # the live working dict

    def answer_for(self, step_id: str) -> Optional[VerificationResponse]:
        """The recorded answer for a specific gate firing, or None if not answered."""
        for answer in reversed(self._record.answers):
            if answer.step_id == step_id:
                return answer
        return None

    def last_answer(self) -> Optional[VerificationResponse]:
        return self._record.answers[-1] if self._record.answers else None

    def record_cost(
        self, total_cost_usd: float, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        b = self._record.breakers
        b.cumulative_cost_usd += total_cost_usd
        b.cumulative_input_tokens += input_tokens
        b.cumulative_output_tokens += output_tokens

    def require_verification(
        self,
        *,
        prompt: str,
        answer_schema: dict[str, Any],
        artifact_path: Optional[str] = None,
        default_answer: Optional[dict[str, Any]] = None,
        timeout_seconds: Optional[int] = None,
    ) -> NoReturn:
        """Build a :class:`VerificationRequest` for THIS step and suspend."""
        request = VerificationRequest(
            run_id=self.run_id,
            step_id=self.step_id,
            prompt=prompt,
            answer_schema=answer_schema,
            artifact_path=artifact_path,
            default_answer=default_answer,
            timeout_seconds=timeout_seconds,
        )
        raise VerificationRequired(request)


StepFn = Callable[[RunContext], StepOutcome]


@dataclass
class LoopDefinition:
    name: str
    start_step: str
    steps: dict[str, StepFn]


class LoopRunner:
    def __init__(self, loop: LoopDefinition, store: RunStore, notifier: Notifier) -> None:
        self.loop = loop
        self.store = store
        self.notifier = notifier

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def create_run(
        self,
        *,
        project_id: Optional[str] = None,
        breakers: Optional[BreakerState] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> RunRecord:
        record = RunRecord(loop_name=self.loop.name, project_id=project_id)
        if breakers is not None:
            record.breakers = breakers
        if data:
            record.data.update(data)  # seed loop inputs (e.g. issue_number, repo)
        self.store.create(record)
        return record

    def run(self, run_id: str) -> RunStatus:
        """Start a CREATED run or continue a crash-interrupted RUNNING one.

        Returns the resulting status. A run that hits a gate returns ``WAITING``
        and the caller (process) is free to exit.
        """
        record = self.store.load(run_id)
        if record.status in TERMINAL_STATUSES:
            return record.status
        if record.status == RunStatus.WAITING:
            # Cannot run a waiting run; it needs an answer via resume(). For an
            # interactive notifier, try to collect one inline.
            if getattr(self.notifier, "interactive", False) and record.pending_request:
                response = self.notifier.collect(record.pending_request)
                if response is not None:
                    return self.resume(run_id, response)
            return RunStatus.WAITING

        if record.status == RunStatus.CREATED:
            record.status = RunStatus.RUNNING
            self._enter_iteration(record)  # first iteration: loop_count 0 -> 1
            if record.status in TERMINAL_STATUSES:
                return record.status
        else:  # RUNNING — resume after a crash, from the persisted current_step
            record.status = RunStatus.RUNNING
            self.store.save(record)

        return self._dispatch(record)

    def resume(self, run_id: str, response: VerificationResponse) -> RunStatus:
        """Deliver a human answer to a WAITING run and continue dispatching."""
        record = self.store.load(run_id)
        if record.status != RunStatus.WAITING:
            raise InvalidResume(f"run {run_id} is {record.status.value}, not WAITING")
        request = record.pending_request
        if request is None:
            raise InvalidResume(f"run {run_id} is WAITING but has no pending request")

        # 1. Correlation guard — a stale/cross-run answer must not resume this run.
        if response.request_id != request.request_id or response.run_id != record.run_id:
            return RunStatus.WAITING  # ignored; run stays suspended

        # 2. Validate the answer against the request's JSON Schema.
        try:
            jsonschema.validate(response.answer, request.answer_schema)
        except jsonschema.ValidationError:
            self.notifier.notify(request)  # re-ask
            return RunStatus.WAITING

        # 3. Normalize, record, clear, flip to RUNNING.
        response.step_id = request.step_id
        if "approved" in response.answer:
            response.approved = bool(response.answer["approved"])
        record.answers.append(response)
        record.pending_request = None
        record.status = RunStatus.RUNNING
        self.store.save(record)

        return self._dispatch(record)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _step_id(self, step_name: str, record: RunRecord) -> str:
        return f"{step_name}#{record.breakers.loop_count}"

    def _enter_iteration(self, record: RunRecord) -> None:
        """Begin a new loop iteration at the start step. Increments and commits the
        iteration counter BEFORE any work, then enforces the max-iterations cap."""
        record.breakers.loop_count += 1
        record.current_step = self.loop.start_step
        self.store.save(record)
        if record.breakers.loop_count > record.breakers.max_iterations:
            self._abort(record, f"max_iterations ({record.breakers.max_iterations}) exceeded")

    def _dispatch(self, record: RunRecord) -> RunStatus:
        while True:
            step_name = record.current_step
            if step_name is None:
                return self._complete(record)

            b = record.breakers
            # Pre-step breaker checks (cheap; the cost check is read back from disk).
            if b.consecutive_failures >= b.max_consecutive_failures:
                return self._abort(record, f"consecutive failures reached {b.consecutive_failures}")
            if b.cumulative_cost_usd >= b.budget_ceiling_usd:
                return self._abort(record, f"spend ceiling ${b.budget_ceiling_usd:.2f} reached")

            step_id = self._step_id(step_name, record)
            record.attempt_counts[step_name] = record.attempt_counts.get(step_name, 0) + 1
            ctx = RunContext(record, step_id)
            started_at = utcnow_iso()

            try:
                outcome = self.loop.steps[step_name](ctx)
            except VerificationRequired as signal:
                # Suspend. Persist WAITING FIRST so the path is durable even if an
                # interactive inline-collect is interrupted (Ctrl-C / crash).
                record.pending_request = signal.request
                record.status = RunStatus.WAITING
                record.current_step = step_name  # re-enter THIS step on resume
                self.store.save(record)
                self.notifier.notify(signal.request)
                if getattr(self.notifier, "interactive", False):
                    response = self.notifier.collect(signal.request)
                    if response is not None:
                        return self.resume(record.run_id, response)
                return RunStatus.WAITING
            except Exception as exc:  # noqa: BLE001 — runner deliberately catches step errors
                b.consecutive_failures += 1
                record.step_log[step_id] = StepRecord(
                    step_id=step_id,
                    step_name=step_name,
                    status="failed",
                    output=None,
                    started_at=started_at,
                    finished_at=utcnow_iso(),
                    error=repr(exc),
                )
                self.store.save(record)
                if b.consecutive_failures >= b.max_consecutive_failures:
                    return self._abort(record, f"step '{step_name}' failed: {exc!r}")
                continue  # retry the same step (current_step unchanged)

            # Success: reset failure streak, merge data, record output, ADVANCE —
            # all in one atomic save so position and log move together.
            b.consecutive_failures = 0
            record.data.update(outcome.state_patch)
            record.step_log[step_id] = StepRecord(
                step_id=step_id,
                step_name=step_name,
                status="done",
                output=outcome.output,
                started_at=started_at,
                finished_at=utcnow_iso(),
            )
            next_step = outcome.next_step
            crossed_cap = False
            if next_step is not None and next_step == self.loop.start_step:
                b.loop_count += 1  # looping back = a new iteration
                crossed_cap = b.loop_count > b.max_iterations
            record.current_step = next_step
            self.store.save(record)

            if crossed_cap:
                return self._abort(record, f"max_iterations ({b.max_iterations}) exceeded")
            if next_step is None:
                return self._complete(record)
            # else: continue the dispatch loop

    def _abort(self, record: RunRecord, reason: str) -> RunStatus:
        record.status = RunStatus.ABORTED
        record.terminal_reason = reason
        record.current_step = None
        self.store.save(record)
        return RunStatus.ABORTED

    def _complete(self, record: RunRecord) -> RunStatus:
        record.status = RunStatus.COMPLETED
        record.current_step = None
        self.store.save(record)
        return RunStatus.COMPLETED
