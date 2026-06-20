"""FileProjectRegistry: loads each registered project's config from disk.

Given the instance config's project pointers and the directory that holds
``harness.toml``, it discovers and validates each project's
``harness.project.toml``. The pointer id must match the project's declared id —
a mismatch is a loud configuration error, not a silent surprise.

It is also the write side of onboarding: :meth:`add_project` appends a
``[[projects]]`` pointer to the on-disk ``harness.toml`` and (optionally) writes
the target repo's ``harness.project.toml``, so registering a repo no longer means
hand-editing TOML.
"""

from __future__ import annotations

from pathlib import Path

from harness.config.loader import (
    DEFAULT_CONFIG_NAME,
    load_project_config,
    resolve_project_config_path,
)
from harness.config.models import ProjectConfig, ProjectPointer
from harness.ports.project_registry import ProjectNotFound


class ProjectConfigError(ValueError):
    pass


class FileProjectRegistry:
    def __init__(
        self,
        pointers: list[ProjectPointer],
        base_dir: Path | str,
        *,
        instance_config_path: Path | str | None = None,
    ) -> None:
        self._pointers = pointers
        self._base_dir = Path(base_dir)
        # The instance config the pointers came from. The container builds the
        # registry from ``harness.toml``'s directory, so default to that file;
        # tests may point it elsewhere.
        self._instance_config_path = (
            Path(instance_config_path)
            if instance_config_path is not None
            else self._base_dir / DEFAULT_CONFIG_NAME
        )
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

    def add_project(
        self, *, pointer: ProjectPointer, project_config_toml: str | None = None
    ) -> None:
        """Append ``pointer`` to ``harness.toml`` and, if given, write the target
        repo's ``harness.project.toml``; then reload so the project is visible.

        Idempotent: re-adding a pointer whose id is already registered does not
        duplicate the ``[[projects]]`` entry (and leaves an existing project file
        untouched), so an interrupted onboarding is safe to retry.
        """
        already_registered = pointer.id in self._existing_pointer_ids()

        if project_config_toml is not None:
            # The pointer path is relative to the instance config's directory.
            ptr_path = Path(pointer.path)
            repo_dir = ptr_path if ptr_path.is_absolute() else (
                self._instance_config_path.parent / ptr_path
            )
            project_file = repo_dir / (pointer.config_file or "harness.project.toml")
            if not project_file.exists():
                project_file.parent.mkdir(parents=True, exist_ok=True)
                project_file.write_text(project_config_toml, encoding="utf-8")

        if not already_registered:
            self._append_pointer(pointer)
            self._pointers = [*self._pointers, pointer]

        self.reload()

    # -- internals ---------------------------------------------------------- #
    def _existing_pointer_ids(self) -> set[str]:
        """Ids already present, read from disk (the source of truth) when the
        instance config exists, else from the in-memory pointers."""
        import tomllib

        if self._instance_config_path.is_file():
            raw = tomllib.loads(self._instance_config_path.read_text("utf-8"))
            return {p.get("id") for p in raw.get("projects", []) if p.get("id")}
        return {p.id for p in self._pointers}

    def _append_pointer(self, pointer: ProjectPointer) -> None:
        text = (
            self._instance_config_path.read_text("utf-8")
            if self._instance_config_path.is_file()
            else ""
        )
        block = _render_pointer_block(pointer)
        sep = "" if text == "" or text.endswith("\n\n") else (
            "\n" if text.endswith("\n") else "\n\n"
        )
        self._instance_config_path.parent.mkdir(parents=True, exist_ok=True)
        self._instance_config_path.write_text(text + sep + block, encoding="utf-8")


def _toml_str(value: str) -> str:
    """A minimal TOML basic-string literal (quotes + escapes for our field set)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_pointer_block(pointer: ProjectPointer) -> str:
    """Hand-render a ``[[projects]]`` array-of-tables entry. We avoid adding a
    TOML writer dependency: a pointer is three simple string fields."""
    lines = ["[[projects]]", f"id = {_toml_str(pointer.id)}", f"path = {_toml_str(pointer.path)}"]
    if pointer.config_file:
        lines.append(f"config_file = {_toml_str(pointer.config_file)}")
    return "\n".join(lines) + "\n"
