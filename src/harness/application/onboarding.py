"""Onboarding: scaffold a :class:`ProjectConfig` for an arbitrary repo.

Pure logic (the only IO is *reading* the target directory to sniff its
ecosystem). It detects sensible build/test commands from the marker files a repo
already has — ``pyproject.toml``/``uv.lock`` -> ``uv run pytest``, ``package.json``
-> ``npm`` scripts, ``Cargo.toml`` -> ``cargo``, ``go.mod`` -> ``go`` — and returns
the in-memory config plus the ``harness.project.toml`` text to write into the repo.

The write side (appending the instance-config pointer and dropping the project
file on disk) belongs to
:meth:`harness.adapters.registry.file_registry.FileProjectRegistry.add_project`;
keeping this module IO-light makes the detection table trivially testable.
"""

from __future__ import annotations

from pathlib import Path

from harness.config.models import ProjectCommands, ProjectConfig, ProjectOverrides

# marker file -> (build, test) command argv. First marker that matches wins, so
# the order encodes precedence (a polyglot repo is detected by its primary stack).
_DETECTORS: list[tuple[str, list[str], list[str]]] = [
    ("pyproject.toml", [], ["uv", "run", "pytest"]),
    ("uv.lock", [], ["uv", "run", "pytest"]),
    ("package.json", ["npm", "run", "build"], ["npm", "test"]),
    ("Cargo.toml", ["cargo", "build"], ["cargo", "test"]),
    ("go.mod", ["go", "build", "./..."], ["go", "test", "./..."]),
]


def detect_commands(repo_dir: Path) -> ProjectCommands:
    """Sniff build/test commands from the repo's ecosystem markers.

    Returns empty command lists when nothing is recognised — the caller can still
    onboard the repo and the user fills the commands in later.
    """
    for marker, build, test in _DETECTORS:
        if (repo_dir / marker).is_file():
            return ProjectCommands(build=list(build), test=list(test), cwd=".")
    return ProjectCommands(build=[], test=[], cwd=".")


def scaffold_project(
    *,
    project_id: str,
    repo: str,
    owner_instance: str,
    path: str,
    repo_dir: Path,
    autonomous: bool = False,
) -> tuple[ProjectConfig, str]:
    """Build a :class:`ProjectConfig` for the repo at ``repo_dir`` and render the
    ``harness.project.toml`` text to write at its root.

    ``path`` is the pointer path the instance config will store (relative to
    ``harness.toml``); ``repo_dir`` is the resolved directory we inspect now.

    When ``autonomous`` is set, the project opts the normally-gated publishing
    actions (``verify_gate``, ``mark_pr_ready``, ``merge_to_main``) into
    autonomous via ``[overrides.autonomy]`` — the self-managed shape — instead of
    the safe human-gated instance defaults. Off by default: a freshly onboarded
    repo is human-reviewed until its owner deliberately closes the loop.
    """
    commands = detect_commands(Path(repo_dir))
    overrides = (
        ProjectOverrides.model_validate(
            {
                "autonomy": {
                    "verify_gate": "autonomous",
                    "mark_pr_ready": "autonomous",
                    "merge_to_main": "autonomous",
                }
            }
        )
        if autonomous
        else ProjectOverrides()
    )
    config = ProjectConfig(
        id=project_id,
        display_name=project_id,
        repo=repo,
        owner_instance=owner_instance,
        commands=commands,
        overrides=overrides,
    )
    return config, _render_project_toml(config, path=path, autonomous=autonomous)


# --------------------------------------------------------------------------- #
# TOML rendering (hand-rolled: avoids a writer dependency; the schema is small)
# --------------------------------------------------------------------------- #
def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def _command_value(value: list[str] | str) -> str:
    return _toml_str(value) if isinstance(value, str) else _toml_array(value)


def _render_project_toml(config: ProjectConfig, *, path: str, autonomous: bool) -> str:
    """Render a faithful, readable ``harness.project.toml`` for ``config``.

    Mirrors the layout of the in-repo example (``[project]`` metadata table, then
    ``[commands]``) so a human can keep editing it by hand afterwards.
    """
    lines = [
        "# Per-project config. It lives at the ROOT OF THE MANAGED REPO,"
        " version-controlled,",
        "# so any machine that checks out the repo reads the identical config."
        f" (scaffolded by `harness add-project {path}`)",
        f"schema_version = {config.schema_version}",
        "",
        "[project]",
        f"id = {_toml_str(config.id)}",
        f"display_name = {_toml_str(config.display_name)}",
        f"repo = {_toml_str(config.repo)}",
        f"owner_instance = {_toml_str(config.owner_instance)}"
        "    # only the install with this instance_id acts autonomously",
        "",
        "[commands]",
        f"build = {_command_value(config.commands.build)}",
        f"test = {_command_value(config.commands.test)}",
        f"cwd = {_toml_str(config.commands.cwd)}",
    ]
    if autonomous:
        lines += [
            "",
            "# SELF-MANAGED: reviewed at the PR by the pr_review agent, not a"
            " mid-run human gate.",
            "# Overrides ONLY this project; the instance defaults stay safe for"
            " human-reviewed repos.",
            "[overrides.autonomy]",
            'verify_gate = "autonomous"',
            'mark_pr_ready = "autonomous"',
            'merge_to_main = "autonomous"',
        ]
    return "\n".join(lines) + "\n"
