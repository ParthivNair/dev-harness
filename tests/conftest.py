from __future__ import annotations

from pathlib import Path

import pytest

from harness.adapters.state.json_store import AtomicJsonRunStore
from harness.config.models import ProjectConfig
from tests.fakes import RecordingNotifier

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path: Path) -> AtomicJsonRunStore:
    return AtomicJsonRunStore(tmp_path / "state")


@pytest.fixture
def sample_project() -> ProjectConfig:
    return ProjectConfig(id="sample", owner_instance="this-machine", repo="acme/sample")


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
