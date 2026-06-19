"""Notifier port: how a waiting gate reaches the human and how the answer returns.

First implementations are console (interactive stdin) and file (write a request
file, an answer file appears later). A Discord bot is a LATER implementation of
this same Protocol — the engine never changes when it slots in.

The ``interactive`` flag is the only thing the runner branches on: an interactive
notifier may be ``collect()``-ed inline in the same process (a developer
ergonomic); a non-interactive one publishes the request and the run goes durably
``WAITING`` until something external delivers the answer via ``resume()``.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from harness.domain.models import VerificationRequest, VerificationResponse


@runtime_checkable
class Notifier(Protocol):
    #: True => the runner may block-collect an answer in this same process.
    interactive: bool

    def notify(self, request: VerificationRequest) -> None:
        """Alert the human that a gate is waiting. MUST NOT block on the answer."""

    def collect(self, request: VerificationRequest) -> Optional[VerificationResponse]:
        """Attempt to obtain an answer for THIS request, else return ``None``.

        Interactive notifiers may block on input here; non-interactive ones do a
        non-blocking check (e.g. "is the response file present yet?").
        """

    def warn(self, message: str) -> None:
        """Surface an autonomous-path advisory (no answer expected, never blocks).

        Used by the overseer when a best-effort step is skipped — e.g. a wave PR
        whose branches all conflicted, or a per-repo drafting error swallowed so the
        tick survives. An OPTIONAL capability: callers tolerate a notifier without it
        (``getattr(notifier, "warn", None)``), mirroring ``archive``.
        """
