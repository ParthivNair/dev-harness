"""Bundled default prompts shipped inside the package.

A freshly-onboarded repo (and any ``pip install``ed copy of the harness) has no
project prompt files of its own yet, so the loops fall back to a default. These
generic, repo-agnostic prompts live in :mod:`harness.prompts` as package data and
are loaded via :mod:`importlib.resources` — so they resolve from an installed wheel
exactly as from a source checkout, not via a filesystem path relative to the repo.
"""

from __future__ import annotations

from importlib import resources

_PACKAGE = "harness.prompts"
_KNOWN = ("dev_task", "arch_review", "pr_review", "research", "triage")


def load_bundled_prompt(name: str) -> str | None:
    """The bundled default prompt text for ``name`` (e.g. ``"dev_task"``,
    ``"arch_review"``, ``"research"``, ``"pr_review"``, ``"triage"``), or ``None``
    for an unknown name or a name with no shipped ``<name>.md`` resource."""
    if name not in _KNOWN:
        return None
    resource = resources.files(_PACKAGE) / f"{name}.md"
    if not resource.is_file():
        return None
    return resource.read_text(encoding="utf-8")
