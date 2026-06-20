from __future__ import annotations

import tomllib
from pathlib import Path

from harness.application.onboarding import detect_commands, scaffold_project
from harness.config.loader import load_project_config
from harness.config.models import AutonomyTier


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_detect_python_repo_uses_uv_run_pytest(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    cmds = detect_commands(tmp_path)
    assert cmds.test == ["uv", "run", "pytest"]


def test_detect_node_repo_uses_npm(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", "{}\n")
    cmds = detect_commands(tmp_path)
    assert cmds.test == ["npm", "test"]
    assert cmds.build == ["npm", "run", "build"]


def test_detect_rust_and_go(tmp_path: Path) -> None:
    rust = tmp_path / "rust"
    go = tmp_path / "go"
    _write(rust / "Cargo.toml")
    _write(go / "go.mod")
    assert detect_commands(rust).test == ["cargo", "test"]
    assert detect_commands(go).test == ["go", "test", "./..."]


def test_detect_unknown_repo_returns_empty_commands(tmp_path: Path) -> None:
    cmds = detect_commands(tmp_path)
    assert cmds.build == []
    assert cmds.test == []


def test_scaffold_returns_config_and_parseable_toml(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml")
    config, toml_text = scaffold_project(
        project_id="acme",
        repo="acme/widgets",
        owner_instance="WindowsDesktop",
        path="../acme",
        repo_dir=tmp_path,
    )
    assert config.id == "acme"
    assert config.repo == "acme/widgets"
    assert config.owner_instance == "WindowsDesktop"
    assert config.commands.test == ["uv", "run", "pytest"]

    # The rendered TOML must be valid AND round-trip through the real loader.
    raw = tomllib.loads(toml_text)
    assert raw["project"]["id"] == "acme"
    reparsed = load_project_config_from_text(tmp_path, toml_text)
    assert reparsed.id == "acme"
    assert reparsed.commands.test == ["uv", "run", "pytest"]
    # A non-autonomous onboard carries no overrides.
    assert reparsed.overrides.autonomy == {}


def test_scaffold_autonomous_sets_overrides(tmp_path: Path) -> None:
    config, toml_text = scaffold_project(
        project_id="self",
        repo="me/self",
        owner_instance="WindowsDesktop",
        path=".",
        repo_dir=tmp_path,
        autonomous=True,
    )
    assert config.overrides.autonomy["merge_to_main"] is AutonomyTier.AUTONOMOUS
    reparsed = load_project_config_from_text(tmp_path, toml_text)
    eff = reparsed.overrides.autonomy
    assert eff["verify_gate"] is AutonomyTier.AUTONOMOUS
    assert eff["mark_pr_ready"] is AutonomyTier.AUTONOMOUS
    assert eff["merge_to_main"] is AutonomyTier.AUTONOMOUS


def load_project_config_from_text(repo_dir: Path, toml_text: str):
    """Write the rendered TOML and load it back through the production loader,
    proving the scaffold output is exactly what the registry would read on reload."""
    project_file = repo_dir / "harness.project.toml"
    project_file.write_text(toml_text, encoding="utf-8")
    return load_project_config(project_file)
