"""Domain models for the harness engine.

These are the durable, serializable types. They contain no I/O and no platform
branches. The :class:`RunRecord` is the single document persisted by a
:class:`~harness.ports.run_store.RunStore`; its schema is pinned by
``SCHEMA_VERSION`` so a reboot — or the other machine — can load it safely.

The cardinal rule: everything here must round-trip through JSON. We never
serialize a coroutine, generator, closure, or live object; the resume position
is plain data (``current_step`` + ``data`` dict), never a frozen stack frame.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, NoReturn, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


def utcnow_iso() -> str:
    """Timezone-aware UTC timestamp, ISO-8601, used for every ``*_at`` field."""
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    """A fresh correlation id (uuid4 hex)."""
    return uuid.uuid4().hex


class RunStatus(str, Enum):
    """Lifecycle of a single run. ``str`` mixin keeps it JSON-friendly."""

    CREATED = "CREATED"      # record exists, never dispatched
    RUNNING = "RUNNING"      # actively executing (also the crash-recovery state)
    WAITING = "WAITING"      # suspended on a VerificationRequest; no live process
    COMPLETED = "COMPLETED"  # loop finished normally (terminal)
    ABORTED = "ABORTED"      # a circuit breaker tripped (terminal)
    FAILED = "FAILED"        # unhandled step error past the failure threshold (terminal)


TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.ABORTED, RunStatus.FAILED})


# --------------------------------------------------------------------------- #
# Control signals (not errors — interpreted by the LoopRunner)
# --------------------------------------------------------------------------- #
class ControlSignal(Exception):
    """Base for non-error control flow the runner interprets.

    Steps must never ``except Exception`` broadly around the point where they
    raise one of these, or the runner will treat a suspension as a failure.
    """


class VerificationRequired(ControlSignal):
    """Raised by a step that needs the human perceptual oracle.

    Carries the structured :class:`VerificationRequest`; the runner persists
    ``WAITING`` and returns control to the caller (the process may then exit).
    """

    def __init__(self, request: "VerificationRequest") -> None:
        self.request = request
        super().__init__(f"verification required: {request.request_id}")


# --------------------------------------------------------------------------- #
# Verification request / response (the human-in-the-loop contract)
# --------------------------------------------------------------------------- #
class VerificationRequest(BaseModel):
    """A structured ask handed to the human. Persisted in the run record and,
    for the file notifier, written verbatim to ``<request_id>.request.json``.

    ``answer_schema`` is a JSON Schema (not a Python type) on purpose: it crosses
    the process/machine boundary as pure data, so a Discord bot or a human
    editing a file knows the expected shape with zero Python.
    """

    schema_version: int = SCHEMA_VERSION
    request_id: str = Field(default_factory=new_id)
    run_id: str
    step_id: str
    prompt: str
    answer_schema: dict[str, Any]
    artifact_path: Optional[str] = None
    default_answer: Optional[dict[str, Any]] = None
    timeout_seconds: Optional[int] = None
    created_at: str = Field(default_factory=utcnow_iso)
    expires_at: Optional[str] = None


class VerificationResponse(BaseModel):
    """The human's validated answer. Echoes ``request_id``/``run_id`` so a stale
    answer can never resume the wrong run.
    """

    schema_version: int = SCHEMA_VERSION
    request_id: str
    run_id: str
    step_id: str
    answer: dict[str, Any]
    approved: bool = False           # convenience; derived from answer["approved"] if present
    answered_at: str = Field(default_factory=utcnow_iso)
    via: str = "unknown"             # "console" | "file" | "cli" | "discord" | "default"
    timed_out: bool = False


# --------------------------------------------------------------------------- #
# Step bookkeeping + circuit breakers
# --------------------------------------------------------------------------- #
class StepRecord(BaseModel):
    """The recorded result of one completed (or failed) step instance.

    Keyed in :attr:`RunRecord.step_log` by ``step_id`` = ``"{step_name}#{loop_count}"``,
    so a reject→re-run cycle produces distinct entries (``build#1``, ``build#2``).
    """

    step_id: str
    step_name: str
    status: str  # "done" | "failed"
    output: Any = None
    started_at: str
    finished_at: str
    error: Optional[str] = None


class BreakerState(BaseModel):
    """All circuit-breaker counters. Persisted with the run and read back BEFORE
    acting on resume, so a crash can't reset a budget or escape the cap.
    """

    loop_count: int = 0
    max_iterations: int = 5
    consecutive_failures: int = 0
    max_consecutive_failures: int = 3
    cumulative_cost_usd: float = 0.0
    budget_ceiling_usd: float = 5.0
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0


class RunRecord(BaseModel):
    """The single durable document per run. This is the source of truth a reboot
    or the other machine reloads to know exactly where a loop is.
    """

    schema_version: int = SCHEMA_VERSION
    run_id: str = Field(default_factory=new_id)
    loop_name: str
    project_id: Optional[str] = None
    status: RunStatus = RunStatus.CREATED
    current_step: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
    step_log: dict[str, StepRecord] = Field(default_factory=dict)
    pending_request: Optional[VerificationRequest] = None
    answers: list[VerificationResponse] = Field(default_factory=list)
    breakers: BreakerState = Field(default_factory=BreakerState)
    attempt_counts: dict[str, int] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)
    terminal_reason: Optional[str] = None
    machine_id: str = ""
    version: int = 0  # monotonic CAS token; bumped by RunStore.save on each write


__all__ = [
    "SCHEMA_VERSION",
    "utcnow_iso",
    "new_id",
    "RunStatus",
    "TERMINAL_STATUSES",
    "ControlSignal",
    "VerificationRequired",
    "VerificationRequest",
    "VerificationResponse",
    "StepRecord",
    "BreakerState",
    "RunRecord",
    "NoReturn",
]
