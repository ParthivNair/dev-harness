"""AtomicJsonRunStore: one JSON file per run, written atomically.

The M1 durable store. Local file is the source of truth; the cross-machine
substrate is GitHub, not a shared filesystem (so two machines never own the same
run id at once). A SQLite store can replace this behind the same Protocol later.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Optional

from harness.domain.models import SCHEMA_VERSION, RunRecord, RunStatus, utcnow_iso
from harness.ports.run_store import (
    IncompatibleSchema,
    RunAlreadyExists,
    RunNotFound,
    VersionConflict,
)
from harness.util.atomic import atomic_write_text

MACHINE_ID = socket.gethostname()


class AtomicJsonRunStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def create(self, record: RunRecord) -> None:
        if self._path(record.run_id).exists():
            raise RunAlreadyExists(record.run_id)
        self.save(record)

    def load(self, run_id: str) -> RunRecord:
        path = self._path(run_id)
        if not path.exists():
            raise RunNotFound(run_id)
        raw = json.loads(path.read_text("utf-8"))
        stored_version = raw.get("schema_version", 0)
        if stored_version > SCHEMA_VERSION:
            raise IncompatibleSchema(
                f"run {run_id} has schema_version {stored_version}; "
                f"this engine understands {SCHEMA_VERSION}"
            )
        return RunRecord.model_validate(raw)

    def save(self, record: RunRecord) -> None:
        record.updated_at = utcnow_iso()
        record.machine_id = MACHINE_ID
        path = self._path(record.run_id)
        if path.exists():
            # Compare-and-set: the version loaded must still be on disk, else a
            # concurrent writer raced us. .get(..., 0) keeps pre-version files compatible.
            disk_version = json.loads(path.read_text("utf-8")).get("version", 0)
            if disk_version != record.version:
                raise VersionConflict(
                    f"run {record.run_id} changed on disk: expected version "
                    f"{record.version}, found {disk_version}"
                )
            record.version += 1
        atomic_write_text(path, record.model_dump_json(indent=2))

    def exists(self, run_id: str) -> bool:
        return self._path(run_id).exists()

    def list(self, status: Optional[RunStatus] = None) -> list[RunRecord]:
        records: list[RunRecord] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                record = self.load(path.stem)
            except Exception:  # noqa: BLE001 — skip unreadable/foreign files
                continue
            if status is None or record.status == status:
                records.append(record)
        records.sort(key=lambda r: r.created_at)
        return records
