"""Label provisioning: ensure_labels makes a fresh repo's state machine well-formed.

Exercised against the InMemoryGitHub fake (the production-default wiring), so the
idempotency and "return only what was created" contract the integrator's
``labels-init`` command relies on is the real one.
"""

from __future__ import annotations

from harness.adapters.github.fake import InMemoryGitHub
from harness.application import coordination as co

REPO = "acme/app"

# The full label set a fresh target repo needs: the harness:<state> machine plus a
# sample owner lease label and the sev:* prioritization labels.
COORDINATION_LABELS = [
    co.QUEUED,
    co.IN_PROGRESS,
    co.NEEDS_VERIFICATION,
    co.PR_OPEN,
    co.BLOCKED,
    co.DONE,
    co.CHANGES_REQUESTED,
    co.HANDOFF,
    "sev:high",
    "sev:med",
    "sev:low",
]


def test_ensure_labels_creates_all_missing_on_a_fresh_repo() -> None:
    gh = InMemoryGitHub()
    created = gh.ensure_labels(repo=REPO, labels=COORDINATION_LABELS)
    assert set(created) == set(COORDINATION_LABELS)  # all were missing -> all created
    assert gh.labels_on(repo=REPO) == set(COORDINATION_LABELS)


def test_ensure_labels_is_idempotent() -> None:
    gh = InMemoryGitHub()
    gh.ensure_labels(repo=REPO, labels=COORDINATION_LABELS)
    # A second pass creates nothing — re-running labels-init is safe.
    assert gh.ensure_labels(repo=REPO, labels=COORDINATION_LABELS) == []
    assert gh.labels_on(repo=REPO) == set(COORDINATION_LABELS)


def test_ensure_labels_returns_only_the_newly_created() -> None:
    gh = InMemoryGitHub()
    gh.ensure_labels(repo=REPO, labels=[co.QUEUED, co.DONE])
    created = gh.ensure_labels(repo=REPO, labels=[co.QUEUED, co.BLOCKED, "sev:high"])
    assert set(created) == {co.BLOCKED, "sev:high"}  # QUEUED already existed
    assert gh.labels_on(repo=REPO) == {co.QUEUED, co.DONE, co.BLOCKED, "sev:high"}


def test_labels_are_scoped_per_repo() -> None:
    gh = InMemoryGitHub()
    gh.ensure_labels(repo="a/one", labels=[co.QUEUED])
    # A different repo starts empty — provisioning is independent.
    assert gh.ensure_labels(repo="b/two", labels=[co.QUEUED]) == [co.QUEUED]
    assert gh.labels_on(repo="a/one") == {co.QUEUED}
    assert gh.labels_on(repo="b/two") == {co.QUEUED}


def test_create_label_then_ensure_skips_it() -> None:
    gh = InMemoryGitHub()
    gh.create_label(repo=REPO, name=co.QUEUED, color="ededed", description="task is queued")
    # ensure_labels sees the pre-created label and does not recreate it.
    assert gh.ensure_labels(repo=REPO, labels=[co.QUEUED, co.IN_PROGRESS]) == [co.IN_PROGRESS]


def test_whoami_returns_a_stub_login() -> None:
    gh = InMemoryGitHub()
    assert gh.whoami() == "harness-bot"
