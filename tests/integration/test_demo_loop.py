"""End-to-end demo-loop tests.

The headline test proves the central primitive: a run suspends, persists, and is
resumed through FRESH store + runner instances — i.e. as if a different process
(or machine) picked it up from disk. Nothing is held in memory between steps.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.adapters.executor.echo import EchoExecutor
from harness.adapters.notifier.file import FileNotifier
from harness.adapters.state.json_store import AtomicJsonRunStore
from harness.application.loop_runner import LoopDefinition, LoopRunner, RunContext, StepOutcome
from harness.config.models import ProjectConfig
from harness.domain.models import BreakerState, RunStatus, VerificationResponse
from harness.loops.demo import build_demo_loop
from tests.fakes import RecordingNotifier

pytestmark = pytest.mark.integration

PROJECT = ProjectConfig(id="sample", owner_instance="this-machine", repo="acme/sample")


def _runner(root: Path, notifier, *, breakers: BreakerState | None = None):
    """A fresh store + runner pair, as a new process would build them."""
    store = AtomicJsonRunStore(root)
    loop = build_demo_loop(
        executor=EchoExecutor(), artifacts_dir=store.root / "artifacts", project=PROJECT
    )
    return store, LoopRunner(loop, store, notifier)


def _answer(store: AtomicJsonRunStore, run_id: str, *, approved: bool, notes: str = ""):
    req = store.load(run_id).pending_request
    assert req is not None
    return VerificationResponse(
        request_id=req.request_id,
        run_id=run_id,
        step_id=req.step_id,
        answer={"approved": approved, "notes": notes},
        approved=approved,
        via="test",
    )


def test_suspend_persist_resume_across_fresh_instances(tmp_path: Path) -> None:
    root = tmp_path / "state"
    notifier = RecordingNotifier()

    # --- process 1: start; run suspends at the gate and "exits" ---
    store1, runner1 = _runner(root, notifier)
    record = runner1.create_run(project_id="sample", breakers=BreakerState(max_iterations=5))
    run_id = record.run_id
    assert runner1.run(run_id) is RunStatus.WAITING

    on_disk = store1.load(run_id)
    assert on_disk.status is RunStatus.WAITING
    assert on_disk.current_step == "verify_gate"
    assert on_disk.pending_request is not None
    assert "build#1" in on_disk.step_log

    # --- process 2 (fresh objects): reject -> loops back, waits again ---
    store2, runner2 = _runner(root, notifier)
    assert runner2.resume(run_id, _answer(store2, run_id, approved=False, notes="crackle")) is RunStatus.WAITING
    mid = store2.load(run_id)
    assert mid.breakers.loop_count == 2
    assert len(mid.answers) == 1

    # --- process 3 (fresh objects): approve -> completes ---
    store3, runner3 = _runner(root, notifier)
    assert runner3.resume(run_id, _answer(store3, run_id, approved=True, notes="clean")) is RunStatus.COMPLETED

    final = store3.load(run_id)
    assert final.status is RunStatus.COMPLETED
    assert final.breakers.loop_count == 2
    assert len(final.answers) == 2
    assert set(final.step_log) >= {"build#1", "verify_gate#1", "build#2", "verify_gate#2", "finish#2"}


def test_correlation_guard_ignores_stale_answer(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store, runner = _runner(root, RecordingNotifier())
    record = runner.create_run(project_id="sample")
    run_id = record.run_id
    runner.run(run_id)

    stale = _answer(store, run_id, approved=True)
    stale.request_id = "not-the-pending-request"
    assert runner.resume(run_id, stale) is RunStatus.WAITING
    assert store.load(run_id).status is RunStatus.WAITING  # untouched


def test_invalid_answer_keeps_waiting(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store, runner = _runner(root, RecordingNotifier())
    run_id = runner.create_run(project_id="sample").run_id
    runner.run(run_id)

    req = store.load(run_id).pending_request
    assert req is not None
    bad = VerificationResponse(
        request_id=req.request_id,
        run_id=run_id,
        step_id=req.step_id,
        answer={"approved": "yes-please"},  # wrong type; violates the schema
    )
    assert runner.resume(run_id, bad) is RunStatus.WAITING


def test_max_iterations_aborts(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store, runner = _runner(root, RecordingNotifier(), breakers=BreakerState(max_iterations=2))
    # NB: breakers passed to create_run, not the runner.
    run_id = runner.create_run(project_id="sample", breakers=BreakerState(max_iterations=2)).run_id
    runner.run(run_id)

    runner.resume(run_id, _answer(store, run_id, approved=False))   # -> iteration 2, waiting
    status = runner.resume(run_id, _answer(store, run_id, approved=False))  # would be iteration 3
    assert status is RunStatus.ABORTED
    final = store.load(run_id)
    assert final.status is RunStatus.ABORTED
    assert "max_iterations" in (final.terminal_reason or "")


def test_file_notifier_request_and_response_cycle(tmp_path: Path) -> None:
    """Exercise the actual FileNotifier publish/collect/archive path."""
    root = tmp_path / "state"
    inbox = tmp_path / "inbox"
    notifier = FileNotifier(inbox)
    store, runner = _runner(root, notifier)
    run_id = runner.create_run(project_id="sample").run_id
    runner.run(run_id)

    req = store.load(run_id).pending_request
    assert req is not None
    # The request file was published by notify().
    assert (inbox / f"{req.request_id}.request.json").exists()

    # A human/bridge drops a response; collect() reads it; resume() finishes it.
    notifier.write_response(_answer(store, run_id, approved=True, notes="clean"))
    collected = notifier.collect(req)
    assert collected is not None
    assert runner.resume(run_id, collected) is RunStatus.COMPLETED
    notifier.archive(req.request_id)
    assert (inbox / "done" / f"{req.request_id}.response.json").exists()


def test_spend_ceiling_aborts(tmp_path: Path) -> None:
    """A custom self-looping costed step trips the spend breaker."""
    root = tmp_path / "state"
    store = AtomicJsonRunStore(root)

    def work(ctx: RunContext) -> StepOutcome:
        ctx.record_cost(3.0)
        return StepOutcome(next_step="work")  # loop forever (until a breaker stops it)

    loop = LoopDefinition(name="costly", start_step="work", steps={"work": work})
    runner = LoopRunner(loop, store, RecordingNotifier())
    run_id = runner.create_run(
        breakers=BreakerState(max_iterations=100, budget_ceiling_usd=5.0)
    ).run_id

    status = runner.run(run_id)
    assert status is RunStatus.ABORTED
    final = store.load(run_id)
    assert "spend ceiling" in (final.terminal_reason or "")
    assert final.breakers.cumulative_cost_usd >= 5.0
