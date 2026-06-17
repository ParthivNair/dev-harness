"""ActionGuard: enforce the autonomy taxonomy at the boundary.

The principle the brief demands: the LLM *proposes* an action by name; **code**,
not the model, classifies and admits or refuses it. The taxonomy is data (a
``dict[str, AutonomyTier]`` from config); this guard is the only path the engine
uses to reach a side effect.

This is the primary enforcement layer. The secondary, structural layer is the
adapter interfaces themselves (e.g. there is no ``force_push`` method to call).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from harness.config.models import AutonomyTier


class Decision(str, Enum):
    ALLOW = "allow"    # proceed autonomously
    GATE = "gate"      # suspend; a human must approve via a verification gate
    REFUSE = "refuse"  # hard stop; never executes


@dataclass(frozen=True)
class ActionRequest:
    action: str                 # canonical name, e.g. "open_draft_pr"
    project_id: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GuardOutcome:
    decision: Decision
    tier: AutonomyTier
    reason: str


class ForbiddenAction(RuntimeError):
    """A forbidden action was attempted; nothing executes."""


class GateRequired(Exception):
    """A gated action needs human approval. The runner converts this into a
    verification gate (suspend -> persist -> resume on approval)."""

    def __init__(self, request: ActionRequest, outcome: GuardOutcome) -> None:
        self.request = request
        self.outcome = outcome
        super().__init__(f"action '{request.action}' is gated: {outcome.reason}")


class ActionGuard:
    def __init__(self, taxonomy: dict[str, AutonomyTier]) -> None:
        self._taxonomy = taxonomy

    def tier_for(self, action: str) -> AutonomyTier:
        # Unknown actions default to GATED — fail safe, never silently autonomous.
        return self._taxonomy.get(action, AutonomyTier.GATED)

    def classify(self, request: ActionRequest) -> GuardOutcome:
        tier = self.tier_for(request.action)
        if tier is AutonomyTier.AUTONOMOUS:
            return GuardOutcome(Decision.ALLOW, tier, "autonomous tier")
        if tier is AutonomyTier.GATED:
            return GuardOutcome(Decision.GATE, tier, "requires human approval")
        return GuardOutcome(Decision.REFUSE, tier, "forbidden by taxonomy")

    def admit(self, request: ActionRequest) -> GuardOutcome:
        """Call immediately before performing a side effect.

        * ALLOW  -> returns the outcome; caller proceeds.
        * GATE   -> raises :class:`GateRequired`.
        * REFUSE -> raises :class:`ForbiddenAction`.
        """
        outcome = self.classify(request)
        if outcome.decision is Decision.ALLOW:
            return outcome
        if outcome.decision is Decision.GATE:
            raise GateRequired(request, outcome)
        raise ForbiddenAction(f"{request.action}: {outcome.reason}")
