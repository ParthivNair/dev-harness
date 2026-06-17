"""The Milestone-1 demo loop: build -> verification gate -> finish (or loop back).

Its only job is to exercise suspend -> persist -> resume across a real process
boundary. The "build" step is a placeholder that writes a dummy artifact to
"perceive"; the gate asks the human a structured question; on approve the loop
finishes, on reject it loops back and rebuilds (a fresh iteration, so the cap and
the per-iteration step ids are exercised too).
"""

from __future__ import annotations

from pathlib import Path

from harness.application.loop_runner import LoopDefinition, RunContext, StepOutcome
from harness.config.models import ProjectConfig
from harness.ports.executor import Executor

DEMO_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["approved"],
    "additionalProperties": False,
}


def build_demo_loop(
    *, executor: Executor, artifacts_dir: Path, project: ProjectConfig
) -> LoopDefinition:
    artifacts_dir = Path(artifacts_dir)

    def build(ctx: RunContext) -> StepOutcome:
        result = executor.run_build(project=project)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifacts_dir / f"{ctx.run_id}_iter{ctx.loop_count}.wav"
        # A placeholder "build artifact" the human is asked to perceive.
        artifact.write_bytes(b"RIFF....placeholder-tone....")
        return StepOutcome(
            next_step="verify_gate",
            state_patch={"artifact_path": str(artifact), "build_stdout": result.stdout},
            output={"artifact_path": str(artifact), "exit_code": result.exit_code},
        )

    def verify_gate(ctx: RunContext) -> StepOutcome:
        answer = ctx.answer_for(ctx.step_id)
        if answer is None:
            # First time here for this iteration: suspend and ask the human.
            ctx.require_verification(
                prompt="You should hear a sustained 440 Hz tone with no crackle. Do you?",
                answer_schema=DEMO_ANSWER_SCHEMA,
                artifact_path=ctx.data.get("artifact_path"),
                default_answer={"approved": False, "notes": "timed out: rejected by default"},
                timeout_seconds=86_400,
            )
        # Resumed with an answer for this gate firing.
        notes = answer.answer.get("notes", "")
        if answer.approved:
            return StepOutcome(
                next_step="finish",
                state_patch={"final_notes": notes},
                output=answer.answer,
            )
        return StepOutcome(
            next_step="build",  # loop back -> new iteration
            state_patch={"last_reject_notes": notes},
            output=answer.answer,
        )

    def finish(ctx: RunContext) -> StepOutcome:
        return StepOutcome(next_step=None, state_patch={"completed": True}, output={"ok": True})

    return LoopDefinition(
        name="demo",
        start_step="build",
        steps={"build": build, "verify_gate": verify_gate, "finish": finish},
    )
