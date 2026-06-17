"""Loading + discovery for the two config levels.

Uses the stdlib ``tomllib`` (Python 3.11+) plus pydantic validation. The GitHub
token is layered in from the environment so it never lives in a TOML file.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from harness.config.models import HarnessConfig, ProjectConfig, ProjectPointer

DEFAULT_CONFIG_NAME = "harness.toml"
TOKEN_ENV_VAR = "HARNESS_GITHUB_TOKEN"
_PROJECT_CONFIG_CANDIDATES = ("harness.project.toml", ".harness/project.toml")


def find_config(start: Path | None = None) -> Path:
    """Locate ``harness.toml`` by walking up from ``start`` (default: cwd)."""
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        candidate = d / DEFAULT_CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"could not find {DEFAULT_CONFIG_NAME} in {cur} or any parent directory"
    )


def load_harness_config(path: Path) -> HarnessConfig:
    path = Path(path)
    raw = tomllib.loads(path.read_text("utf-8"))
    cfg = HarnessConfig.model_validate(raw)
    token = os.environ.get(TOKEN_ENV_VAR)
    if token:
        cfg.github.token = token
    return cfg


def resolve_project_config_path(pointer: ProjectPointer, base_dir: Path) -> Path:
    """Find a project's config file given its pointer, relative to ``base_dir``
    (the directory containing ``harness.toml``)."""
    ptr_path = Path(pointer.path)
    base = ptr_path if ptr_path.is_absolute() else (base_dir / ptr_path)
    if pointer.config_file:
        return base / pointer.config_file
    for candidate in _PROJECT_CONFIG_CANDIDATES:
        if (base / candidate).is_file():
            return base / candidate
    raise FileNotFoundError(
        f"no project config under {base} for pointer '{pointer.id}' "
        f"(looked for {', '.join(_PROJECT_CONFIG_CANDIDATES)})"
    )


def load_project_config(path: Path) -> ProjectConfig:
    raw = tomllib.loads(Path(path).read_text("utf-8"))
    # The TOML keeps metadata under a readable [project] table; the model is flat.
    # Merge that table up to the top level (siblings: commands/prompts/claude/overrides).
    flat = {k: v for k, v in raw.items() if k != "project"}
    flat.update(raw.get("project", {}))
    return ProjectConfig.model_validate(flat)


def resolve_under(base_dir: Path, value: str) -> Path:
    """Resolve a possibly-relative config path against the config directory."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base_dir / p)
