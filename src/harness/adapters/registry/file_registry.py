"""FileProjectRegistry: loads each registered project's config from disk.

Given the instance config's project pointers and the directory that holds
``harness.toml``, it discovers and validates each project's
``harness.project.toml``. The pointer id must match the project's declared id —
a mismatch is a loud configuration error, not a silent surprise.
"""

from __future__ import annotations

from pathlib import Path

from harness.config.loader import load_project_config, resolve_project_config_path
from harness.config.models import ProjectConfig, ProjectPointer
from harness.ports.project_registry import ProjectNotFound


class ProjectConfigError(ValueError):
    pass


class FileProjectRegistry:
    def __init__(self, pointers: list[ProjectPointer], base_dir: Path | str) -> None:
        self._pointers = pointers
        self._base_dir = Path(base_dir)
        self._by_id: dict[str, ProjectConfig] = {}
        self.reload()

    def reload(self) -> None:
        loaded: dict[str, ProjectConfig] = {}
        for pointer in self._pointers:
            path = resolve_project_config_path(pointer, self._base_dir)
            config = load_project_config(path)
            if config.id != pointer.id:
                raise ProjectConfigError(
                    f"pointer id '{pointer.id}' != project id '{config.id}' in {path}"
                )
            loaded[config.id] = config
        self._by_id = loaded

    def list_projects(self) -> list[ProjectConfig]:
        return list(self._by_id.values())

    def get(self, project_id: str) -> ProjectConfig:
        try:
            return self._by_id[project_id]
        except KeyError:
            raise ProjectNotFound(project_id) from None

    def list_owned(self, instance_id: str) -> list[ProjectConfig]:
        return [c for c in self._by_id.values() if c.owner_instance == instance_id]
