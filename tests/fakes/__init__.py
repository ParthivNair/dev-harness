"""Test doubles. The in-memory adapters under ``harness.adapters`` ARE the
production default wiring, so most fakes are reused from there; this package adds
only what's test-specific."""

from __future__ import annotations

from typing import Optional

from harness.domain.models import VerificationRequest, VerificationResponse


class RecordingNotifier:
    """Non-interactive notifier that records requests and never auto-collects.
    Tests drive ``LoopRunner.resume`` directly to deliver answers."""

    interactive = False

    def __init__(self) -> None:
        self.requests: list[VerificationRequest] = []

    def notify(self, request: VerificationRequest) -> None:
        self.requests.append(request)

    def collect(self, request: VerificationRequest) -> Optional[VerificationResponse]:
        return None
