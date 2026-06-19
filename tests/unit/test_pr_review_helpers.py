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
        ("harness/win-desktop/issue-7", 7),
        ("harness/win-desktop/issue-123", 123),
        ("harness/other-machine/issue-7", None),   # different instance
        ("feature/win-desktop/issue-7", None),     # not a harness branch
        ("harness/win-desktop/issue-7x", None),    # trailing junk
        ("harness/win-desktop/hotfix", None),      # no issue-N
        ("", None),
        (None, None),
    ],
)
def test_harness_branch_issue_is_instance_scoped(head, expected) -> None:
    assert co.harness_branch_issue(head, INSTANCE) == expected


def test_find_reviewable_pr_picks_lowest_own_unflagged_open_pr() -> None:
    gh = InMemoryGitHub()
    # ours, reviewable (two — expect the lower number)
    a = gh.open_draft_pr(repo=REPO, head=f"harness/{INSTANCE}/issue-1", base="main", title="a", body="")
    b = gh.open_draft_pr(repo=REPO, head=f"harness/{INSTANCE}/issue-2", base="main", title="b", body="")
    # excluded: another instance, a human branch, and one we already flagged
    gh.open_draft_pr(repo=REPO, head="harness/other/issue-3", base="main", title="c", body="")
    gh.open_draft_pr(repo=REPO, head="feature/x", base="main", title="d", body="")
    flagged = gh.open_draft_pr(repo=REPO, head=f"harness/{INSTANCE}/issue-4", base="main", title="e", body="")
    gh.add_labels(repo=REPO, number=flagged.number, labels=[co.CHANGES_REQUESTED])

    assert co.find_reviewable_pr(gh, repo=REPO, instance_id=INSTANCE) == min(a.number, b.number)


def test_find_reviewable_pr_none_when_no_own_work() -> None:
    gh = InMemoryGitHub()
    gh.open_draft_pr(repo=REPO, head="harness/other/issue-1", base="main", title="x", body="")
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
