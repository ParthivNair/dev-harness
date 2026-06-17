"""ProjectRegistry port: the set of registered projects.

Each project's config + prompt set lives with/near its own repo; the registry
discovers and reads them. The owning-instance field (read by
:func:`harness.application.ownership.owns`) decides which install acts.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.config.models import ProjectConfig


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
