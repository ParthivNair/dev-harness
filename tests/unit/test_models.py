from __future__ import annotations

from harness.domain.models import (
    RunRecord,
    RunStatus,
    TERMINAL_STATUSES,
    VerificationRequest,
    VerificationResponse,
)


def test_run_record_json_round_trip() -> None:
    record = RunRecord(loop_name="demo", project_id="sample")
    record.current_step = "verify_gate"
    record.status = RunStatus.WAITING
    record.pending_request = VerificationRequest(
        run_id=record.run_id,
        step_id="verify_gate#1",
        prompt="hear a tone?",
        answer_schema={"type": "object"},
    )
    record.answers.append(
        VerificationResponse(
            request_id="r1",
            run_id=record.run_id,
            step_id="verify_gate#1",
            answer={"approved": True},
            approved=True,
        )
    )

    blob = record.model_dump_json()
    restored = RunRecord.model_validate_json(blob)

    assert restored == record
    assert restored.status is RunStatus.WAITING
    assert restored.pending_request is not None
    assert restored.answers[0].approved is True


def test_terminal_statuses() -> None:
    assert RunStatus.COMPLETED in TERMINAL_STATUSES
    assert RunStatus.ABORTED in TERMINAL_STATUSES
    assert RunStatus.FAILED in TERMINAL_STATUSES
    assert RunStatus.RUNNING not in TERMINAL_STATUSES
    assert RunStatus.WAITING not in TERMINAL_STATUSES
