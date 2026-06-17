"""Ownership: which install acts on which project.

This is the whole "no central dispatcher" mechanism, expressed as a one-line
comparison. A project declares ``owner_instance``; only the install whose
``instance_id`` matches acts autonomously on it. Any install may *read* a
project's GitHub state — that is what keeps a project legible from the machine
that is not running it.
"""

from __future__ import annotations

from harness.config.models import InstanceInfo, ProjectConfig


def owns(project: ProjectConfig, instance: InstanceInfo) -> bool:
    return project.owner_instance == instance.instance_id
