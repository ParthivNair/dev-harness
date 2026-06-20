"""Unit tests for the bundled-prompt loader and the loops' prompt fallback order.

A freshly-onboarded repo ships no prompt files of its own, so the loops must fall
back to the generic prompts packaged with the harness (loaded via importlib.resources
so they resolve from an installed wheel too), not to the terse one-line DEFAULT_*
constants. These tests pin both the loader and that fallback wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.config.models import ProjectConfig, PromptSet
from harness.loops import arch_review, dev_task, pr_review, triage
from harness.util.prompts import load_bundled_prompt


@pytest.mark.parametrize("name", ["dev_task", "arch_review", "pr_review", "triage"])
def test_load_bundled_prompt_returns_text_for_known_names(name: str) -> None:
    text = load_bundled_prompt(name)
    assert isinstance(text, str)
    assert text.strip()  # non-empty, real prompt content


def test_load_bundled_prompt_is_repo_agnostic() -> None:
    # The bundled defaults must not leak the dev-harness/ParthivNair specifics they
    # were generalized from — a packaged install onboards an ARBITRARY repo.
    for name in ("dev_task", "arch_review", "pr_review", "triage"):
        text = (load_bundled_prompt(name) or "").lower()
        assert "dev-harness" not in text
        assert "parthivnair" not in text


def test_load_bundled_prompt_unknown_name_returns_none() -> None:
    assert load_bundled_prompt("not_a_real_prompt") is None


def test_dev_loop_falls_back_to_bundled_prompt(tmp_path: Path) -> None:
    # No prompt file on disk for this project -> the dev loop reads the bundled default,
    # NOT the terse inline DEFAULT_DEV_PROMPT.
    project = ProjectConfig(id="p", owner_instance="me", prompts=PromptSet(dev_task=None))
    prompt = dev_task._read_prompt(tmp_path, project)
    assert prompt == load_bundled_prompt("dev_task")
    assert prompt != dev_task.DEFAULT_DEV_PROMPT


def test_dev_loop_prefers_project_prompt_file_over_bundled(tmp_path: Path) -> None:
    # A project's own prompt file on disk still wins over the bundled default.
    rel = ".harness/prompts/dev_task.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("custom project prompt", encoding="utf-8")
    project = ProjectConfig(id="p", owner_instance="me", prompts=PromptSet(dev_task=rel))
    assert dev_task._read_prompt(tmp_path, project) == "custom project prompt"


def test_arch_review_falls_back_to_bundled_rubric(tmp_path: Path) -> None:
    project = ProjectConfig(id="p", owner_instance="me", prompts=PromptSet(arch_review=None))
    rubric = arch_review._read_rubric(tmp_path, project)
    assert rubric == load_bundled_prompt("arch_review")
    assert rubric != arch_review.DEFAULT_RUBRIC


def test_triage_falls_back_to_bundled_prompt(tmp_path: Path) -> None:
    project = ProjectConfig(id="p", owner_instance="me", prompts=PromptSet(triage=None))
    prompt = triage._read_prompt(tmp_path, project)
    assert prompt == load_bundled_prompt("triage")
    assert prompt != triage.DEFAULT_PROMPT


def test_pr_review_falls_back_to_bundled_prompt(tmp_path: Path) -> None:
    project = ProjectConfig(id="p", owner_instance="me", prompts=PromptSet(pr_review=None))
    prompt = pr_review._read_prompt(tmp_path, project)
    assert prompt == load_bundled_prompt("pr_review")
    assert prompt != pr_review.DEFAULT_REVIEW_PROMPT
