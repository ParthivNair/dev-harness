"""Test doubles. The in-memory adapters under ``harness.adapters`` ARE the
production default wiring, so most fakes are reused from there; this package adds
only what's test-specific."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from harness.domain.models import VerificationRequest, VerificationResponse


class RecordingNotifier:
    """Non-interactive notifier that records requests and never auto-collects.
    Tests drive ``LoopRunner.resume`` directly to deliver answers."""

    interactive = False

    def __init__(self) -> None:
        self.requests: list[VerificationRequest] = []

    def notify(self, request: VerificationRequest) -> None:
        self.requests.append(request)

    def collect(self, request: VerificationRequest) -> Optional[VerificationResponse]:
        return None


class FakeRegistry:
    """A one-project ProjectRegistry for the use-case + dashboard tests."""

    def __init__(self, project) -> None:  # type: ignore[no-untyped-def]
        self._p = project

    def list_projects(self):  # type: ignore[no-untyped-def]
        return [self._p]

    def get(self, project_id: str):  # type: ignore[no-untyped-def]
        from harness.ports.project_registry import ProjectNotFound

        if project_id == self._p.id:
            return self._p
        raise ProjectNotFound(project_id)

    def list_owned(self, instance_id: str):  # type: ignore[no-untyped-def]
        return [self._p] if self._p.owner_instance == instance_id else []

    def reload(self) -> None:
        pass


def make_container(
    tmp_path: Path,
    *,
    instance: str = "this-machine",
    repo: str = "acme/app",
    project_id: str = "sample",
    owner: Optional[str] = None,
    github=None,  # type: ignore[no-untyped-def]
    notifier=None,  # type: ignore[no-untyped-def]
):
    """Build a real :class:`~harness.container.Container` wired with in-memory fakes —
    the production-default adapters — so use-cases run exactly as in production."""
    from harness.adapters.executor.echo import EchoExecutor
    from harness.adapters.github.fake import InMemoryGitHub
    from harness.adapters.notifier.file import FileNotifier
    from harness.adapters.state.json_store import AtomicJsonRunStore
    from harness.application.action_guard import ActionGuard
    from harness.config.models import (
        AutonomyTier,
        HarnessConfig,
        InstanceInfo,
        ProjectCommands,
        ProjectConfig,
        ProjectPointer,
    )
    from harness.container import Container

    project = ProjectConfig(
        id=project_id,
        owner_instance=owner or instance,
        repo=repo,
        commands=ProjectCommands(build=["true"], test=["true"]),
    )
    cfg = HarnessConfig(
        instance=InstanceInfo(instance_id=instance),
        projects=[ProjectPointer(id=project_id, path=str(tmp_path))],
    )
    return Container(
        cfg=cfg,
        base_dir=Path(tmp_path),
        registry=FakeRegistry(project),
        store=AtomicJsonRunStore(tmp_path / "state"),
        notifier=notifier or FileNotifier(tmp_path / "inbox"),
        executor=EchoExecutor(),
        github=github or InMemoryGitHub(),
        guard=ActionGuard(
            {
                "open_draft_pr": AutonomyTier.AUTONOMOUS,
                "file_issue": AutonomyTier.AUTONOMOUS,
            }
        ),
    )
