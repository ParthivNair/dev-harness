from __future__ import annotations

import pytest

from harness.application.action_guard import (
    ActionGuard,
    ActionRequest,
    Decision,
    ForbiddenAction,
    GateRequired,
)
from harness.config.models import AutonomyTier

TAXONOMY = {
    "open_draft_pr": AutonomyTier.AUTONOMOUS,
    "mark_pr_ready": AutonomyTier.GATED,
    "force_push": AutonomyTier.FORBIDDEN,
}


def _req(action: str) -> ActionRequest:
    return ActionRequest(action=action, project_id="sample")


def test_autonomous_is_allowed() -> None:
    guard = ActionGuard(TAXONOMY)
    outcome = guard.admit(_req("open_draft_pr"))
    assert outcome.decision is Decision.ALLOW


def test_gated_raises_gate_required() -> None:
    guard = ActionGuard(TAXONOMY)
    with pytest.raises(GateRequired):
        guard.admit(_req("mark_pr_ready"))


def test_forbidden_raises_forbidden() -> None:
    guard = ActionGuard(TAXONOMY)
    with pytest.raises(ForbiddenAction):
        guard.admit(_req("force_push"))


def test_unknown_action_defaults_to_gated() -> None:
    guard = ActionGuard(TAXONOMY)
    assert guard.classify(_req("something_new")).decision is Decision.GATE
    with pytest.raises(GateRequired):
        guard.admit(_req("something_new"))
