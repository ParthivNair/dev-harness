"""Unit tests for the pr_review building blocks: instance-scoped branch parsing,
reviewable-PR selection, the per-repo merge opt-in, and the merge guard tiers."""

from __future__ import annotations

import pytest

from harness.adapters.github.fake import InMemoryGitHub
from harness.application import coordination as co
from harness.application.action_guard import (
    ActionGuard,
    ActionRequest,
    Decision,
    ForbiddenAction,
    GateRequired,
)
from harness.config.models import AutonomyTier, ProjectConfig, ProjectOverrides

REPO = "acme/app"
INSTANCE = "win-desktop"


@pytest.mark.parametrize(
    "head,expected",
    [
        ("harness/win-desktop/wave-ab12cd", True),
        ("harness/win-desktop/wave-0", True),
        ("harness/other-machine/wave-ab12cd", False),   # different instance
        ("harness/win-desktop/issue-7", False),         # a per-issue branch, not a wave
        ("feature/win-desktop/wave-ab12cd", False),     # not a harness branch
        ("", False),
        (None, False),
    ],
)
def test_is_harness_wave_pr_is_instance_scoped(head, expected) -> None:
    assert co.is_harness_wave_pr(head, INSTANCE) is expected


def test_find_reviewable_pr_picks_lowest_own_unflagged_wave_pr() -> None:
    gh = InMemoryGitHub()
    # ours, reviewable wave PRs (two — expect the lower number)
    a = gh.open_draft_pr(repo=REPO, head=f"harness/{INSTANCE}/wave-aaa", base="main", title="a", body="")
    b = gh.open_draft_pr(repo=REPO, head=f"harness/{INSTANCE}/wave-bbb", base="main", title="b", body="")
    # excluded: another instance's wave, a per-issue branch, a human branch, a flagged wave
    gh.open_draft_pr(repo=REPO, head="harness/other/wave-ccc", base="main", title="c", body="")
    gh.open_draft_pr(repo=REPO, head=f"harness/{INSTANCE}/issue-3", base="main", title="d", body="")
    gh.open_draft_pr(repo=REPO, head="feature/x", base="main", title="e", body="")
    flagged = gh.open_draft_pr(repo=REPO, head=f"harness/{INSTANCE}/wave-ddd", base="main", title="f", body="")
    gh.add_labels(repo=REPO, number=flagged.number, labels=[co.CHANGES_REQUESTED])

    assert co.find_reviewable_pr(gh, repo=REPO, instance_id=INSTANCE) == min(a.number, b.number)


def test_find_reviewable_pr_none_when_no_own_wave() -> None:
    gh = InMemoryGitHub()
    gh.open_draft_pr(repo=REPO, head="harness/other/wave-aaa", base="main", title="x", body="")
    gh.open_draft_pr(repo=REPO, head=f"harness/{INSTANCE}/issue-1", base="main", title="y", body="")
    assert co.find_reviewable_pr(gh, repo=REPO, instance_id=INSTANCE) is None


def test_effective_autonomy_merges_per_repo_merge_optin() -> None:
    instance_default = {"merge_to_main": AutonomyTier.FORBIDDEN, "review_pr": AutonomyTier.AUTONOMOUS}
    project = ProjectConfig(
        id="p", owner_instance=INSTANCE, repo=REPO,
        overrides=ProjectOverrides(autonomy={"merge_to_main": AutonomyTier.AUTONOMOUS}),
    )
    eff = project.effective_autonomy(instance_default)
    assert eff["merge_to_main"] is AutonomyTier.AUTONOMOUS   # opted in per-repo
    assert eff["review_pr"] is AutonomyTier.AUTONOMOUS       # inherited
    # The instance default is untouched — other repos keep the forbidden ceiling.
    assert instance_default["merge_to_main"] is AutonomyTier.FORBIDDEN


def test_merge_guard_tiers() -> None:
    req = ActionRequest("merge_to_main", "p")
    assert ActionGuard({"merge_to_main": AutonomyTier.AUTONOMOUS}).admit(req).decision is Decision.ALLOW
    with pytest.raises(GateRequired):
        ActionGuard({"merge_to_main": AutonomyTier.GATED}).admit(req)
    with pytest.raises(ForbiddenAction):
        ActionGuard({"merge_to_main": AutonomyTier.FORBIDDEN}).admit(req)
    # Unknown action (no merge opt-in at all) fails safe to GATED, never autonomous.
    with pytest.raises(GateRequired):
        ActionGuard({}).admit(req)
