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


def test_handoff_issue_requeues_labels_and_comments() -> None:
    gh = InMemoryGitHub()
    n = _queued(gh, labels=(co.QUEUED, "sev:high"))
    co.claim(gh, repo=REPO, number=n, instance_id="A")
    co.handoff_issue(gh, repo=REPO, number=n, note="## Prior attempt(s)\nstrand")
    issue = gh.get_issue(repo=REPO, number=n)
    assert co.state_of(issue) == co.QUEUED          # back on the queue for a fresh run
    assert co.owner_of(issue) is None               # lease dropped so anyone can claim
    assert co.HANDOFF in issue.labels               # continuation marker stamped
    assert "sev:high" in issue.labels               # foreign labels preserved
    assert gh.comments[(REPO, n)] == ["## Prior attempt(s)\nstrand"]  # packet posted


def test_handoff_label_survives_a_later_transition() -> None:
    # HANDOFF is a foreign label (not a state label), so it must ride through the
    # next claim/transition just like a human tag.
    gh = InMemoryGitHub()
    n = _queued(gh)
    co.handoff_issue(gh, repo=REPO, number=n, note="packet")
    co.claim(gh, repo=REPO, number=n, instance_id="B")
    issue = gh.get_issue(repo=REPO, number=n)
    assert co.state_of(issue) == co.IN_PROGRESS
    assert co.HANDOFF in issue.labels


def test_fake_comment_on_issue_records_per_issue() -> None:
    gh = InMemoryGitHub()
    n = _queued(gh)
    gh.comment_on_issue(repo=REPO, number=n, body="first")
    gh.comment_on_issue(repo=REPO, number=n, body="second")
    assert gh.comments[(REPO, n)] == ["first", "second"]


# --------------------------------------------------------------------------- #
# C1: dependency-aware, severity-ordered claiming (Phase 2 §2a).
# --------------------------------------------------------------------------- #
def test_c1_find_claimable_picks_highest_severity_first() -> None:
    # Severity is the PRIMARY key: a later, higher-sev issue beats an earlier low one.
    gh = InMemoryGitHub()
    low = _queued(gh, title="low", labels=(co.QUEUED, "sev:low"))
    high = _queued(gh, title="high", labels=(co.QUEUED, "sev:high"))
    med = _queued(gh, title="med", labels=(co.QUEUED, "sev:med"))
    assert co.find_claimable(gh, repo=REPO, instance_id="ME") == high
    _ = (low, med)


def test_c1_effort_breaks_ties_at_equal_severity_preferring_smaller() -> None:
    # At equal severity, lower effort (quicker win) wins, regardless of number order.
    gh = InMemoryGitHub()
    big = _queued(gh, title="big", labels=(co.QUEUED, "sev:high", "effort:l"))
    small = _queued(gh, title="small", labels=(co.QUEUED, "sev:high", "effort:s"))
    assert co.find_claimable(gh, repo=REPO, instance_id="ME") == small
    _ = big


def test_c1_empty_labels_falls_back_to_number_order() -> None:
    # Regression-safe: no sev/effort labels anywhere => plain lowest-number order,
    # exactly the old min(number) behavior.
    gh = InMemoryGitHub()
    a = _queued(gh, title="a")
    _b = _queued(gh, title="b")
    assert co.find_claimable(gh, repo=REPO, instance_id="ME") == a


def test_c1_dependency_gate_skips_issue_whose_dep_is_still_open() -> None:
    # An issue declaring "Depends on #N" is NOT claimable while #N is open, even if it
    # is higher severity — so the dependency is worked first.
    gh = InMemoryGitHub()
    dep = _queued(gh, title="dependency", labels=(co.QUEUED, "sev:low"))
    blocked = _queued(
        gh, title="blocked", body=f"Depends on #{dep}", labels=(co.QUEUED, "sev:high")
    )
    # Despite higher severity, the blocked issue is gated -> the open dep is picked.
    assert co.find_claimable(gh, repo=REPO, instance_id="ME") == dep
    _ = blocked


def test_c1_dependency_releases_once_the_dep_closes() -> None:
    # Once the dependency is no longer open (resolved/merged), the gated issue becomes
    # claimable. Modeled by closing the dep issue's open state.
    gh = InMemoryGitHub()
    dep = _queued(gh, title="dependency", labels=(co.QUEUED, "sev:low"))
    blocked = _queued(
        gh, title="blocked", body=f"Depends on #{dep}", labels=(co.QUEUED, "sev:high")
    )
    # Close the dependency (it left the open set) -> the gate releases the blocked issue.
    closed = co.IssueRef(
        number=dep, title="dependency", state="closed", labels=(), assignee=None,
        url=f"https://github.com/{REPO}/issues/{dep}", body="",
    )
    gh._issues[(REPO, dep)] = closed  # simulate the dep being merged/closed
    assert co.find_claimable(gh, repo=REPO, instance_id="ME") == blocked


def test_c1_returns_none_when_only_dependency_blocked_work_remains() -> None:
    # The only queued issue depends on an OPEN non-queued issue -> nothing is ready.
    gh = InMemoryGitHub()
    other = gh.create_issue(repo=REPO, title="open feature", body="", labels=[]).number
    _queued(gh, title="blocked", body=f"Sequencing: #{other}")
    assert co.find_claimable(gh, repo=REPO, instance_id="ME") is None


def test_c1_depends_on_parses_multiple_refs_and_sequencing() -> None:
    gh = InMemoryGitHub()
    issue = gh.create_issue(
        repo=REPO, title="t", body="Depends on #3 and #4\nSequencing: #5", labels=[co.QUEUED]
    )
    assert co.depends_on(issue) == [3, 4, 5]
    assert co.depends_on(gh.create_issue(repo=REPO, title="x", body="no refs", labels=[])) == []


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
