"""Coordination substrate: label state machine + the owner-bearing issue lease.

Exercised against the InMemoryGitHub fake (the production default wiring), so the
last-writer-wins ``set_labels`` semantics the lease relies on are the real ones.
"""

from __future__ import annotations

from harness.adapters.github.fake import InMemoryGitHub
from harness.application import coordination as co

REPO = "acme/app"


def _queued(gh: InMemoryGitHub, *, title: str = "task", body: str = "", labels=(co.QUEUED,)) -> int:
    return gh.create_issue(repo=REPO, title=title, body=body, labels=list(labels)).number


def test_claim_happy_path_takes_lease_and_moves_to_in_progress() -> None:
    gh = InMemoryGitHub()
    n = _queued(gh, body="implement X")
    result = co.claim(gh, repo=REPO, number=n, instance_id="win")
    assert result.ok
    issue = gh.get_issue(repo=REPO, number=n)
    assert co.owner_of(issue) == "win"
    assert co.state_of(issue) == co.IN_PROGRESS
    assert co.QUEUED not in issue.labels
    assert issue.assignee == "win"
    assert issue.body == "implement X"  # body survives the transition


def test_second_instance_yields_on_already_owned() -> None:
    gh = InMemoryGitHub()
    n = _queued(gh)
    assert co.claim(gh, repo=REPO, number=n, instance_id="A").ok
    b = co.claim(gh, repo=REPO, number=n, instance_id="B")
    assert not b.ok
    assert "already owned by A" in b.reason
    assert co.owner_of(gh.get_issue(repo=REPO, number=n)) == "A"  # lease unchanged


def test_reclaiming_own_issue_is_idempotent() -> None:
    gh = InMemoryGitHub()
    n = _queued(gh)
    assert co.claim(gh, repo=REPO, number=n, instance_id="A").ok
    # e.g. the dev loop re-asserting the lease on a loop-back
    assert co.claim(gh, repo=REPO, number=n, instance_id="A").ok
    assert co.owns_issue(gh, repo=REPO, number=n, instance_id="A")


def test_simultaneous_claim_last_writer_wins_loser_can_detect() -> None:
    # Model a true race: both instances read the unclaimed issue, then both write.
    # set_labels is last-writer-wins, so the confirm-read picks a single winner and
    # the loser's owns_issue() check returns False -> it yields.
    gh = InMemoryGitHub()
    n = _queued(gh)
    co.transition(gh, repo=REPO, number=n, to_state=co.IN_PROGRESS, owner="A")
    co.transition(gh, repo=REPO, number=n, to_state=co.IN_PROGRESS, owner="B")  # last writer
    assert co.owner_of(gh.get_issue(repo=REPO, number=n)) == "B"
    assert not co.owns_issue(gh, repo=REPO, number=n, instance_id="A")
    assert co.owns_issue(gh, repo=REPO, number=n, instance_id="B")


def test_transition_preserves_owner_and_foreign_labels() -> None:
    gh = InMemoryGitHub()
    n = _queued(gh, labels=(co.QUEUED, "sev:high", "bug"))
    co.claim(gh, repo=REPO, number=n, instance_id="A")
    co.transition(gh, repo=REPO, number=n, to_state=co.NEEDS_VERIFICATION)
    issue = gh.get_issue(repo=REPO, number=n)
    assert co.state_of(issue) == co.NEEDS_VERIFICATION
    assert co.owner_of(issue) == "A"               # lease preserved
    assert "sev:high" in issue.labels and "bug" in issue.labels  # foreign labels kept
    assert co.IN_PROGRESS not in issue.labels      # old state dropped


def test_release_drops_lease_and_requeues() -> None:
    gh = InMemoryGitHub()
    n = _queued(gh, labels=(co.QUEUED, "bug"))
    co.claim(gh, repo=REPO, number=n, instance_id="A")
    co.release(gh, repo=REPO, number=n)
    issue = gh.get_issue(repo=REPO, number=n)
    assert co.state_of(issue) == co.QUEUED
    assert co.owner_of(issue) is None
    assert "bug" in issue.labels


def test_reconcile_requeues_stale_lease_no_active_run() -> None:
    # We crashed mid-run: the issue is in-progress + owned by us, but no run backs it.
    gh = InMemoryGitHub()
    n = _queued(gh, labels=(co.QUEUED, "sev:high", "bug"))
    co.claim(gh, repo=REPO, number=n, instance_id="win")
    reclaimed = co.reconcile_orphans(
        gh, repo=REPO, instance_id="win", active_run_issue_numbers=set()
    )
    assert reclaimed == [n]
    issue = gh.get_issue(repo=REPO, number=n)
    assert co.state_of(issue) == co.QUEUED         # back to the queue
    assert co.owner_of(issue) is None              # lease dropped
    assert "sev:high" in issue.labels and "bug" in issue.labels  # foreign labels kept


def test_reconcile_leaves_live_lease_with_active_run_alone() -> None:
    gh = InMemoryGitHub()
    n = _queued(gh)
    co.claim(gh, repo=REPO, number=n, instance_id="win")
    reclaimed = co.reconcile_orphans(
        gh, repo=REPO, instance_id="win", active_run_issue_numbers={n}
    )
    assert reclaimed == []
    assert co.state_of(gh.get_issue(repo=REPO, number=n)) == co.IN_PROGRESS  # untouched


def test_reconcile_never_steals_another_instances_lease() -> None:
    gh = InMemoryGitHub()
    n = _queued(gh)
    co.claim(gh, repo=REPO, number=n, instance_id="other")
    reclaimed = co.reconcile_orphans(
        gh, repo=REPO, instance_id="win", active_run_issue_numbers=set()
    )
    assert reclaimed == []
    assert co.owner_of(gh.get_issue(repo=REPO, number=n)) == "other"  # foreign lease intact


def test_reconcile_is_idempotent() -> None:
    gh = InMemoryGitHub()
    n = _queued(gh)
    co.claim(gh, repo=REPO, number=n, instance_id="win")
    assert co.reconcile_orphans(gh, repo=REPO, instance_id="win", active_run_issue_numbers=set()) == [n]
    # Second pass: the issue is queued again, so there is nothing left to reclaim.
    assert co.reconcile_orphans(gh, repo=REPO, instance_id="win", active_run_issue_numbers=set()) == []


def test_find_claimable_returns_lowest_queued_and_skips_claimed() -> None:
    gh = InMemoryGitHub()
    a = _queued(gh, title="a")
    b = _queued(gh, title="b")
    # claim the lower one for someone else; it leaves the queue -> next pick is b
    co.claim(gh, repo=REPO, number=a, instance_id="OTHER")
    assert co.find_claimable(gh, repo=REPO, instance_id="ME") == b


def test_find_claimable_none_when_no_work() -> None:
    gh = InMemoryGitHub()
    assert co.find_claimable(gh, repo=REPO, instance_id="ME") is None


def test_claim_succeeds_even_if_assignee_write_fails() -> None:
    # instance_id is usually not a real GitHub user, so assign can 403/422. The
    # owner LABEL is the real lease, so a failed assignee must not abort the claim.
    class AssignFails(InMemoryGitHub):
        def assign_issue(self, *, repo, number, assignee):
            raise RuntimeError("403 Forbidden: not a collaborator")

    gh = AssignFails()
    n = gh.create_issue(repo=REPO, title="t", body="", labels=[co.QUEUED]).number
    result = co.claim(gh, repo=REPO, number=n, instance_id="WindowsDesktop")
    assert result.ok
    assert co.owner_of(gh.get_issue(repo=REPO, number=n)) == "WindowsDesktop"
