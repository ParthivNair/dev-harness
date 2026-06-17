from __future__ import annotations

import pytest

from harness.adapters.state.json_store import AtomicJsonRunStore
from harness.domain.models import RunRecord, RunStatus
from harness.ports.run_store import RunAlreadyExists, RunNotFound


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
