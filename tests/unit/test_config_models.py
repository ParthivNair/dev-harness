from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.adapters.registry.file_registry import FileProjectRegistry
from harness.application.ownership import owns
from harness.config.loader import (
    load_harness_config,
    load_project_config,
    resolve_project_config_path,
)
from harness.config.models import AutonomyTier, HarnessConfig, ProjectConfig


def test_repo_harness_config_loads(repo_root: Path) -> None:
    cfg = load_harness_config(repo_root / "harness.toml")
    assert cfg.instance.instance_id == "WindowsDesktop"
    assert cfg.github.account == "ParthivNair"
    assert cfg.autonomy["force_push"] is AutonomyTier.FORBIDDEN
    assert cfg.autonomy["open_draft_pr"] is AutonomyTier.AUTONOMOUS
    assert {p.id for p in cfg.projects} >= {"sample", "dev-harness"}


def test_b4_instance_defaults_gate_verify_and_mark_ready(repo_root: Path) -> None:
    # The DAW-safe defaults: both stay gated at the instance level so a human-reviewed
    # repo opts INTO autonomy rather than out of a gate.
    cfg = load_harness_config(repo_root / "harness.toml")
    assert cfg.autonomy["verify_gate"] is AutonomyTier.GATED
    assert cfg.autonomy["mark_pr_ready"] is AutonomyTier.GATED


def test_b4_self_managed_project_overrides_verify_and_mark_ready_autonomous(
    repo_root: Path,
) -> None:
    # The dev-harness project (self-managed) opts both into autonomous via
    # [overrides.autonomy]; effective_autonomy layers it over the gated instance default.
    cfg = load_harness_config(repo_root / "harness.toml")
    pointer = next(p for p in cfg.projects if p.id == "dev-harness")
    project = load_project_config(resolve_project_config_path(pointer, repo_root))
    eff = project.effective_autonomy(cfg.autonomy)
    assert eff["verify_gate"] is AutonomyTier.AUTONOMOUS
    assert eff["mark_pr_ready"] is AutonomyTier.AUTONOMOUS
    # Other actions still come from the instance taxonomy (override is additive).
    assert eff["force_push"] is AutonomyTier.FORBIDDEN


def test_sample_project_loads_and_is_owned(repo_root: Path) -> None:
    cfg = load_harness_config(repo_root / "harness.toml")
    registry = FileProjectRegistry(cfg.projects, repo_root)
    sample = registry.get("sample")
    assert sample.owner_instance == "WindowsDesktop"
    assert owns(sample, cfg.instance) is True


def test_invalid_autonomy_tier_rejected() -> None:
    with pytest.raises(ValidationError):
        HarnessConfig.model_validate(
            {
                "instance": {"instance_id": "x"},
                "autonomy": {"merge_to_main": "totally-fine"},  # not a valid tier
            }
        )


def test_effective_breakers_override() -> None:
    from harness.config.models import CircuitBreakers

    default = CircuitBreakers(max_iterations=8, spend_ceiling_usd=5.0)
    project = ProjectConfig(
        id="x",
        owner_instance="me",
        overrides={"circuit_breakers": {"max_iterations": 3, "spend_ceiling_usd": 1.0}},
    )
    eff = project.effective_breakers(default)
    assert eff.max_iterations == 3
    assert eff.spend_ceiling_usd == 1.0


def test_scheduling_and_discord_defaults_are_backward_compatible() -> None:
    # A minimal instance config (no [scheduling]/[discord]) must still validate,
    # with the new subsystems defaulting OFF so M1 behaviour is unchanged.
    cfg = HarnessConfig.model_validate({"instance": {"instance_id": "x"}})
    assert cfg.scheduling.enabled is False
    assert cfg.scheduling.global_spend_ceiling_usd == 20.0
    assert cfg.discord.enabled is False
    assert cfg.discord.token is None


def test_project_scheduling_priority_to_weight() -> None:
    low = ProjectConfig(id="x", owner_instance="me", scheduling={"priority": "low"})
    assert low.scheduling.effective_weight() == 0.25
    high = ProjectConfig(id="y", owner_instance="me", scheduling={"priority": "high"})
    assert high.scheduling.effective_weight() == 4.0
    # explicit weight overrides the priority mapping
    pinned = ProjectConfig(
        id="z", owner_instance="me", scheduling={"priority": "low", "weight": 2.0}
    )
    assert pinned.scheduling.effective_weight() == 2.0


def test_invalid_priority_rejected() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(
            {"id": "x", "owner_instance": "me", "scheduling": {"priority": "urgent"}}
        )
