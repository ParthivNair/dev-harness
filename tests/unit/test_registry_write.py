from __future__ import annotations

import tomllib
from pathlib import Path

from harness.adapters.registry.file_registry import FileProjectRegistry
from harness.config.models import ProjectPointer

# A minimal instance config with no projects yet — the onboarding starting point.
_INSTANCE_TOML = """\
schema_version = 1

[instance]
instance_id = "WindowsDesktop"
"""

# A self-contained project config we can register a pointer at.
_PROJECT_TOML = """\
schema_version = 1

[project]
id = "acme"
repo = "acme/widgets"
owner_instance = "WindowsDesktop"
"""


def _instance(tmp_path: Path) -> Path:
    path = tmp_path / "harness.toml"
    path.write_text(_INSTANCE_TOML, encoding="utf-8")
    return path


def _registry(instance_path: Path, pointers: list[ProjectPointer]) -> FileProjectRegistry:
    return FileProjectRegistry(
        pointers, instance_path.parent, instance_config_path=instance_path
    )


def test_add_project_appends_pointer_and_writes_project_file(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    repo_dir = tmp_path / "repos" / "acme"
    repo_dir.mkdir(parents=True)

    registry = _registry(instance, [])
    registry.add_project(
        pointer=ProjectPointer(id="acme", path="repos/acme"),
        project_config_toml=_PROJECT_TOML,
    )

    # The project file was written into the target repo...
    project_file = repo_dir / "harness.project.toml"
    assert project_file.is_file()
    assert project_file.read_text("utf-8") == _PROJECT_TOML

    # ...the pointer was appended to harness.toml (still valid TOML)...
    raw = tomllib.loads(instance.read_text("utf-8"))
    ids = [p["id"] for p in raw["projects"]]
    assert ids == ["acme"]
    assert raw["projects"][0]["path"] == "repos/acme"
    # ...the original [instance] table is untouched...
    assert raw["instance"]["instance_id"] == "WindowsDesktop"
    # ...and the project is immediately visible (reloaded).
    assert registry.get("acme").repo == "acme/widgets"


def test_add_project_is_idempotent_on_id(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    repo_dir = tmp_path / "repos" / "acme"
    repo_dir.mkdir(parents=True)

    registry = _registry(instance, [])
    pointer = ProjectPointer(id="acme", path="repos/acme")
    registry.add_project(pointer=pointer, project_config_toml=_PROJECT_TOML)
    # Re-adding the same id must not duplicate the [[projects]] entry.
    registry.add_project(pointer=pointer, project_config_toml=_PROJECT_TOML)

    raw = tomllib.loads(instance.read_text("utf-8"))
    ids = [p["id"] for p in raw["projects"]]
    assert ids == ["acme"]


def test_add_project_without_toml_assumes_existing_project_file(tmp_path: Path) -> None:
    # When the repo already carries a harness.project.toml, add_project just wires
    # the pointer — it must not overwrite the existing file.
    instance = _instance(tmp_path)
    repo_dir = tmp_path / "repos" / "acme"
    repo_dir.mkdir(parents=True)
    (repo_dir / "harness.project.toml").write_text(_PROJECT_TOML, encoding="utf-8")

    registry = _registry(instance, [])
    registry.add_project(pointer=ProjectPointer(id="acme", path="repos/acme"))

    raw = tomllib.loads(instance.read_text("utf-8"))
    assert [p["id"] for p in raw["projects"]] == ["acme"]
    assert registry.get("acme").repo == "acme/widgets"


def test_add_project_does_not_clobber_existing_project_file(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    repo_dir = tmp_path / "repos" / "acme"
    repo_dir.mkdir(parents=True)
    existing = "# hand-edited\n" + _PROJECT_TOML
    (repo_dir / "harness.project.toml").write_text(existing, encoding="utf-8")

    registry = _registry(instance, [])
    # Even passed fresh TOML, an existing file (id already known) is left alone.
    registry.add_project(
        pointer=ProjectPointer(id="acme", path="repos/acme"),
        project_config_toml=_PROJECT_TOML,
    )
    assert (repo_dir / "harness.project.toml").read_text("utf-8") == existing


def test_add_second_project_keeps_the_first(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    for name in ("acme", "beta"):
        d = tmp_path / "repos" / name
        d.mkdir(parents=True)

    registry = _registry(instance, [])
    registry.add_project(
        pointer=ProjectPointer(id="acme", path="repos/acme"),
        project_config_toml=_PROJECT_TOML.replace("acme", "acme"),
    )
    registry.add_project(
        pointer=ProjectPointer(id="beta", path="repos/beta"),
        project_config_toml=_PROJECT_TOML.replace("acme", "beta").replace(
            "widgets", "beta"
        ),
    )

    raw = tomllib.loads(instance.read_text("utf-8"))
    assert [p["id"] for p in raw["projects"]] == ["acme", "beta"]
    assert {p.id for p in registry.list_projects()} == {"acme", "beta"}
