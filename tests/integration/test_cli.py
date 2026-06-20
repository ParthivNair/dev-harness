"""End-to-end CLI wiring tests for the onboarding/research commands.

These drive the real Typer app through ``typer.testing.CliRunner`` against a
temp instance config wired to the in-memory fakes (``use_in_memory_fake = true``
=> InMemoryGitHub + EchoExecutor), so they exercise the actual command wiring —
argument parsing, the instance-config-path plumbing, exit codes — without any
network or ``claude`` spend. Loop behavior itself is covered by the per-loop
integration tests; the point here is the CLI surface added for "point at any repo".
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app

runner = CliRunner()

_INSTANCE_TOML = """\
schema_version = 1

[instance]
instance_id = "test-inst"

[github]
use_in_memory_fake = true
"""


def _combined_output(result) -> str:  # type: ignore[no-untyped-def]
    text = result.output or ""
    try:  # click may capture stderr separately depending on version
        text += result.stderr or ""
    except (ValueError, AttributeError):
        pass
    return text


def _make_instance(tmp_path: Path, name: str = "myinstance.toml") -> Path:
    cfg = tmp_path / name
    cfg.write_text(_INSTANCE_TOML, encoding="utf-8")
    return cfg


def _make_target_repo(tmp_path: Path, name: str = "myapp") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    # A python marker so onboarding.detect_commands picks `uv run pytest`.
    (repo / "pyproject.toml").write_text("[project]\nname='myapp'\n", encoding="utf-8")
    return repo


def test_add_project_writes_pointer_to_the_loaded_config_not_a_default(tmp_path: Path) -> None:
    """Regression: `add-project --config <non-default>.toml` must append the pointer
    to THAT file, not a hardcoded harness.toml (which would silently drop it)."""
    cfg = _make_instance(tmp_path)
    repo = _make_target_repo(tmp_path)

    result = runner.invoke(
        app,
        ["add-project", str(repo), "--repo", "acme/myapp", "--id", "myapp", "--config", str(cfg)],
    )
    assert result.exit_code == 0, _combined_output(result)

    # The pointer landed in the config we actually loaded ...
    assert "myapp" in cfg.read_text("utf-8")
    # ... NOT in a stray default-named file.
    assert not (tmp_path / "harness.toml").exists()
    # ... and the target repo got its scaffolded project config.
    assert (repo / "harness.project.toml").exists()

    # The newly registered project is immediately visible to other commands.
    listed = runner.invoke(app, ["projects", "--config", str(cfg)])
    assert listed.exit_code == 0, _combined_output(listed)
    assert "myapp" in listed.output


def test_onboard_then_labels_then_research_flow(tmp_path: Path) -> None:
    """The headline workflow: add-project -> labels-init -> research, all green."""
    cfg = _make_instance(tmp_path)
    repo = _make_target_repo(tmp_path)
    runner.invoke(
        app,
        ["add-project", str(repo), "--repo", "acme/myapp", "--id", "myapp", "--config", str(cfg)],
    )

    labels = runner.invoke(app, ["labels-init", "myapp", "--config", str(cfg)])
    assert labels.exit_code == 0, _combined_output(labels)

    # EchoExecutor -> no parseable findings -> a clean no-op research run.
    research = runner.invoke(app, ["research", "myapp", "--config", str(cfg)])
    assert research.exit_code == 0, _combined_output(research)
    assert "research run" in research.output


def test_labels_init_unknown_project_errors_cleanly(tmp_path: Path) -> None:
    """An unknown id is a clean error + exit 1, not a raw ProjectNotFound traceback."""
    cfg = _make_instance(tmp_path)
    result = runner.invoke(app, ["labels-init", "ghost", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "no such project" in _combined_output(result)


def test_research_unknown_project_errors_cleanly(tmp_path: Path) -> None:
    cfg = _make_instance(tmp_path)
    result = runner.invoke(app, ["research", "ghost", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "no such project" in _combined_output(result)


def test_doctor_reports_not_ready_on_fake_wiring(tmp_path: Path) -> None:
    """doctor against the fakes should print the readiness report and exit non-zero,
    and must distinguish 'installed' from 'login' (no over-claiming READY)."""
    cfg = _make_instance(tmp_path)
    result = runner.invoke(app, ["doctor", "--config", str(cfg)])
    assert result.exit_code == 1  # echo executor + fake github => NOT READY
    out = result.output
    assert "claude installed:" in out
    assert "claude login:" in out
    assert "NOT READY" in out
