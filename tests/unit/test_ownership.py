from __future__ import annotations

from harness.application.ownership import owns
from harness.config.models import InstanceInfo, ProjectConfig


def test_owns_true_when_instance_matches() -> None:
    project = ProjectConfig(id="macdaw", owner_instance="mac-laptop")
    instance = InstanceInfo(instance_id="mac-laptop")
    assert owns(project, instance) is True


def test_owns_false_when_instance_differs() -> None:
    project = ProjectConfig(id="macdaw", owner_instance="mac-laptop")
    instance = InstanceInfo(instance_id="win-desktop")
    assert owns(project, instance) is False
