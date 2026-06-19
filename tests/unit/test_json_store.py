from __future__ import annotations

import json

import pytest

from harness.adapters.state.json_store import AtomicJsonRunStore
from harness.domain.models import RunRecord, RunStatus
from harness.ports.run_store import RunAlreadyExists, RunNotFound, VersionConflict


def test_create_load_round_trip(store: AtomicJsonRunStore) -> None:
    record = RunRecord(loop_name="demo")
    store.create(record)
    loaded = store.load(record.run_id)
    assert loaded.run_id == record.run_id
    assert loaded.loop_name == "demo"
    assert loaded.machine_id  # stamped on save


def test_create_is_not_an_upsert(store: AtomicJsonRunStore) -> None:
    record = RunRecord(loop_name="demo")
    store.create(record)
    with pytest.raises(RunAlreadyExists):
        store.create(record)


def test_load_missing_raises(store: AtomicJsonRunStore) -> None:
    with pytest.raises(RunNotFound):
        store.load("does-not-exist")


def test_save_overwrites_atomically(store: AtomicJsonRunStore) -> None:
    record = RunRecord(loop_name="demo")
    store.create(record)
    record.status = RunStatus.RUNNING
    record.current_step = "build"
    store.save(record)
    loaded = store.load(record.run_id)
    assert loaded.status is RunStatus.RUNNING
    assert loaded.current_step == "build"


def test_list_filters_by_status(store: AtomicJsonRunStore) -> None:
    a = RunRecord(loop_name="demo", status=RunStatus.WAITING)
    b = RunRecord(loop_name="demo", status=RunStatus.COMPLETED)
    store.create(a)
    store.create(b)
    waiting = store.list(status=RunStatus.WAITING)
    assert [r.run_id for r in waiting] == [a.run_id]
    assert len(store.list()) == 2


def test_created_record_loads_as_version_zero(store: AtomicJsonRunStore) -> None:
    record = RunRecord(loop_name="demo")
    store.create(record)
    assert store.load(record.run_id).version == 0


def test_sequential_load_save_increments_version(store: AtomicJsonRunStore) -> None:
    record = RunRecord(loop_name="demo")
    store.create(record)

    first = store.load(record.run_id)
    assert first.version == 0
    store.save(first)
    assert store.load(record.run_id).version == 1

    second = store.load(record.run_id)
    store.save(second)
    assert store.load(record.run_id).version == 2


def test_concurrent_save_raises_version_conflict(store: AtomicJsonRunStore) -> None:
    record = RunRecord(loop_name="demo")
    store.create(record)

    # Two callers load the same record at the same version.
    first = store.load(record.run_id)
    second = store.load(record.run_id)

    first.current_step = "build"
    store.save(first)  # wins; on-disk version is now 1

    second.current_step = "ship"
    with pytest.raises(VersionConflict):
        store.save(second)

    # The loser's write never landed: the winner's data and version stand.
    reloaded = store.load(record.run_id)
    assert reloaded.current_step == "build"
    assert reloaded.version == 1


def test_back_compat_record_without_version(store: AtomicJsonRunStore) -> None:
    record = RunRecord(loop_name="demo")
    store.create(record)

    # Simulate a file written before the version field existed.
    path = store._path(record.run_id)
    raw = json.loads(path.read_text("utf-8"))
    del raw["version"]
    path.write_text(json.dumps(raw), "utf-8")

    loaded = store.load(record.run_id)
    assert loaded.version == 0
    store.save(loaded)  # treats missing on-disk version as 0; no conflict
    assert store.load(record.run_id).version == 1
