from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.adapters.registry.file_registry import FileProjectRegistry
from harness.application.ownership import owns
from harness.config.loader import load_harness_config
from harness.config.models import AutonomyTier, HarnessConfig, ProjectConfig


def test_repo_harness_config_loads(repo_root: Path) -> None:
    cfg = load_harness_config(repo_root / "harness.toml")
    assert cfg.instance.instance_id == "this-machine"
    assert cfg.github.use_in_memory_fake is True
    assert cfg.autonomy["force_push"] is AutonomyTier.FORBIDDEN
    assert cfg.autonomy["open_draft_pr"] is AutonomyTier.AUTONOMOUS
    assert any(p.id == "sample" for p in cfg.projects)


def test_sample_project_loads_and_is_owned(repo_root: Path) -> None:
    cfg = load_harness_config(repo_root / "harness.toml")
    registry = FileProjectRegistry(cfg.projects, repo_root)
    sample = registry.get("sample")
    assert sample.owner_instance == "this-machine"
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
