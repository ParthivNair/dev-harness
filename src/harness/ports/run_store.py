"""RunStore port: durable persistence of run records to EXTERNAL storage.

"External" = survives the process. The first implementation
(:class:`~harness.adapters.state.json_store.AtomicJsonRunStore`) is one JSON file
per run; a SQLite implementation can slot in behind the same Protocol later.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from harness.domain.models import RunRecord, RunStatus


class RunStoreError(RuntimeError):
    """Base class for store errors."""


class RunNotFound(RunStoreError, KeyError):
    """No run with the given id exists."""


class RunAlreadyExists(RunStoreError):
    """A run with the given id already exists (create is not an upsert)."""


class IncompatibleSchema(RunStoreError):
    """The stored document's schema_version is newer than this engine understands."""


@runtime_checkable
class RunStore(Protocol):
    def create(self, record: RunRecord) -> None:
        """Persist a brand-new run. Raises :class:`RunAlreadyExists` on collision."""

    def load(self, run_id: str) -> RunRecord:
        """Load and deserialize. Raises :class:`RunNotFound` / :class:`IncompatibleSchema`."""

    def save(self, record: RunRecord) -> None:
        """Atomically overwrite the run document (stamps updated_at + machine_id)."""

    def exists(self, run_id: str) -> bool:
        ...

    def list(self, status: Optional[RunStatus] = None) -> list[RunRecord]:
        """All runs, optionally filtered by status. Used by ``list-runs`` and ``poll``."""
