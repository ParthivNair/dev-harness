"""ProjectRegistry port: the set of registered projects.

Each project's config + prompt set lives with/near its own repo; the registry
discovers and reads them. The owning-instance field (read by
:func:`harness.application.ownership.owns`) decides which install acts.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.config.models import ProjectConfig, ProjectPointer


class ProjectNotFound(KeyError):
    """No registered project with the given id."""


@runtime_checkable
class ProjectRegistry(Protocol):
    def list_projects(self) -> list[ProjectConfig]: ...

    def get(self, project_id: str) -> ProjectConfig:
        """Raises :class:`ProjectNotFound` if unknown."""

    def list_owned(self, instance_id: str) -> list[ProjectConfig]: ...

    def reload(self) -> None:
        """Re-read project configs from disk."""

    def add_project(
        self, *, pointer: ProjectPointer, project_config_toml: str | None = None
    ) -> None:
        """Register a new project: append ``pointer`` to the instance config and,
        if ``project_config_toml`` is given, write it as the target repo's
        ``harness.project.toml``. Idempotent on the pointer id (re-adding a known
        id is a no-op) and reloads so the new project is immediately visible."""
