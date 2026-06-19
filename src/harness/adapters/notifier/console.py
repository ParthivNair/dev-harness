"""ConsoleNotifier: interactive stdin notifier for single-session/dev use.

``interactive = True`` lets the runner collect the answer inline in the same
process. The run is still persisted WAITING before this blocks, so a Ctrl-C
leaves a durable, resumable run — the interactivity is pure ergonomics layered
on the durable substrate.

For M1 it speaks the common approve/notes answer shape. A richer free-form mode
(raw JSON entry against ``answer_schema``) is a later refinement.
"""

from __future__ import annotations

from typing import Optional

from harness.domain.models import VerificationRequest, VerificationResponse


class ConsoleNotifier:
    interactive = True

    def warn(self, message: str) -> None:
        print(f"  [overseer] {message}")

    def notify(self, request: VerificationRequest) -> None:
        print("\n" + "=" * 70)
        print("  VERIFICATION GATE - your perception is required")
        print("=" * 70)
        print(f"  {request.prompt}")
        if request.artifact_path:
            print(f"  artifact: {request.artifact_path}")
        print(f"  (run {request.run_id} / request {request.request_id})")
        print("-" * 70)

    def collect(self, request: VerificationRequest) -> Optional[VerificationResponse]:
        raw = input("  Approve? [y/N] ").strip().lower()
        approved = raw in ("y", "yes")
        notes = input("  Notes (optional): ").strip()
        answer: dict[str, object] = {"approved": approved}
        if notes:
            answer["notes"] = notes
        return VerificationResponse(
            request_id=request.request_id,
            run_id=request.run_id,
            step_id=request.step_id,
            answer=answer,
            approved=approved,
            via="console",
        )
