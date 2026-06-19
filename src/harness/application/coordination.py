"""GitHub-native coordination: the "shared ticketing board".

Issues are the task queue, a mutually-exclusive ``harness:<state>`` label is the
status, and an **owner-bearing label** (``harness:owner:<instance_id>``) is a
single-writer lease so two machines never double-claim the same issue.

Why the lease works with only GitHub primitives: ``set_labels`` REPLACES the
whole label set, so the owner label is *last-writer-wins*. After two racing
claims, each instance re-reads the issue and only the one whose owner label
survived proceeds; the loser yields. This is an optimistic lease with a
confirm-read tiebreak — GitHub has no atomic compare-and-set, and the Search API
lags, so the live REST read (``list_issues``/``get_issue``) is the source of truth.

State transitions preserve the owner lease and any *foreign* labels (human tags,
``sev:high``, …); they only swap the one ``harness:<state>`` label. The assignee
is set too, but purely as GitHub-native UX — the label, not the assignee, is the
authoritative lease token (real GitHub assignment is additive; label replacement
is not).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from harness.ports.github import GitHubAdapter, IssueRef

if TYPE_CHECKING:
    from harness.domain.models import RunRecord, RunStatus

# Mutually-exclusive status labels. The label IS the state machine:
#   queued -> in-progress -> needs-verification -> pr-open -> done
#   (any) -> blocked   (a circuit breaker tripped; a human must triage)
QUEUED = "harness:queued"
IN_PROGRESS = "harness:in-progress"
NEEDS_VERIFICATION = "harness:needs-verification"
PR_OPEN = "harness:pr-open"
BLOCKED = "harness:blocked"
DONE = "harness:done"

STATE_LABELS = frozenset(
    {QUEUED, IN_PROGRESS, NEEDS_VERIFICATION, PR_OPEN, BLOCKED, DONE}
)
OWNER_PREFIX = "harness:owner:"

# A non-state marker applied to a PULL (not an issue): the pr_review loop tags a PR
# it reviewed but would not merge (changes requested, failing CI, or unmergeable), so
# the next selection pass skips it instead of re-reviewing the same unchanged PR.
# Deliberately NOT in STATE_LABELS — it lives on PRs, outside the issue state machine.
CHANGES_REQUESTED = "harness:changes-requested"


def owner_label(instance_id: str) -> str:
    return f"{OWNER_PREFIX}{instance_id}"


def owner_of(issue: IssueRef) -> Optional[str]:
    """The instance id holding the lease, or None if unclaimed."""
    for label in issue.labels:
        if label.startswith(OWNER_PREFIX):
            return label[len(OWNER_PREFIX):]
    return None


def state_of(issue: IssueRef) -> Optional[str]:
    """The current ``harness:<state>`` label, or None if the issue is outside the
    harness state machine (e.g. a plain human-filed issue)."""
    for label in issue.labels:
        if label in STATE_LABELS:
            return label
    return None


def _compute_labels(issue: IssueRef, *, to_state: str, owner: Optional[str]) -> list[str]:
    """New label set = foreign labels (kept) + owner lease (if any) + one state."""
    kept = [
        label
        for label in issue.labels
        if label not in STATE_LABELS and not label.startswith(OWNER_PREFIX)
    ]
    if owner is not None:
        kept.append(owner_label(owner))
    kept.append(to_state)
    return list(dict.fromkeys(kept))  # dedupe, preserve order


def transition(
    github: GitHubAdapter, *, repo: str, number: int, to_state: str, owner: Optional[str] = None
) -> IssueRef:
    """Move the issue to ``to_state``, preserving the lease + foreign labels.

    ``owner=None`` keeps whatever owner the issue already has; pass an explicit id
    to (re)assert a lease as part of the transition.
    """
    issue = github.get_issue(repo=repo, number=number)
    effective_owner = owner if owner is not None else owner_of(issue)
    labels = _compute_labels(issue, to_state=to_state, owner=effective_owner)
    return github.set_labels(repo=repo, number=number, labels=labels)


def release(github: GitHubAdapter, *, repo: str, number: int) -> IssueRef:
    """Drop the lease and requeue — a yield, or reclaiming a dead machine's work."""
    issue = github.get_issue(repo=repo, number=number)
    labels = _compute_labels(issue, to_state=QUEUED, owner=None)
    return github.set_labels(repo=repo, number=number, labels=labels)


def owns_issue(github: GitHubAdapter, *, repo: str, number: int, instance_id: str) -> bool:
    """Does ``instance_id`` currently hold this issue's lease?"""
    return owner_of(github.get_issue(repo=repo, number=number)) == instance_id


@dataclass(frozen=True)
class ClaimResult:
    ok: bool
    issue: Optional[IssueRef]
    reason: str = ""


def claim(
    github: GitHubAdapter, *, repo: str, number: int, instance_id: str
) -> ClaimResult:
    """Optimistically claim a specific issue, with a confirm-read tiebreak.

    Returns ``ok=True`` only if THIS instance holds the lease *after* the write —
    so a racing winner makes us yield. Idempotent: re-claiming an issue we already
    own succeeds and re-asserts ``in-progress``.
    """
    issue = github.get_issue(repo=repo, number=number)
    current = owner_of(issue)
    if current is not None and current != instance_id:
        return ClaimResult(False, issue, f"already owned by {current}")

    # Write the lease. The owner LABEL is authoritative (last-writer-wins). The
    # assignee is best-effort GitHub UX only: instance_id is usually NOT a real
    # GitHub user, so GitHub ignores or rejects it — that must never abort the claim.
    # A label-write failure (e.g. the PAT lacks Issues:write) still propagates.
    try:
        github.assign_issue(repo=repo, number=number, assignee=instance_id)
    except Exception:  # noqa: BLE001 — assignee is decorative; the label is the lease
        pass
    transition(github, repo=repo, number=number, to_state=IN_PROGRESS, owner=instance_id)

    # Confirm-read: did our owner label survive a possible simultaneous claim?
    confirmed = github.get_issue(repo=repo, number=number)
    winner = owner_of(confirmed)
    if winner == instance_id:
        return ClaimResult(True, confirmed)
    return ClaimResult(False, confirmed, f"lost race to {winner}")


def block_dev_issue_if_aborted(
    github: GitHubAdapter, *, record: "RunRecord", status: "RunStatus"
) -> bool:
    """If a ``dev_task`` run aborted (a circuit breaker tripped), leave its issue
    ``harness:blocked`` so a human can triage. Best-effort and idempotent — the run
    is already durably ABORTED regardless. Returns True iff it relabeled.

    Lives here (not in the loop) because the abort happens in the generic runner,
    after the loop's steps; both the CLI and the scheduler call it post-run.
    """
    from harness.domain.models import RunStatus  # local import keeps this module light

    if status is not RunStatus.ABORTED or record.loop_name != "dev_task":
        return False
    repo = record.data.get("repo")
    number = record.data.get("issue_number")
    if not (repo and number):
        return False
    try:
        transition(github, repo=repo, number=int(number), to_state=BLOCKED)
        return True
    except Exception:  # noqa: BLE001 — labeling is advisory; never mask the run result
        return False


def find_claimable(
    github: GitHubAdapter, *, repo: str, instance_id: str
) -> Optional[int]:
    """Lowest-numbered open ``queued`` issue not already owned by another instance.

    Uses the live REST list (never the Search API). Returns the issue number to
    pass to :func:`claim`, or None if there is no available work.
    """
    issues = github.list_issues(repo=repo, state="open", labels=[QUEUED])
    candidates = [i for i in issues if owner_of(i) in (None, instance_id)]
    if not candidates:
        return None
    return min(i.number for i in candidates)


def harness_branch_issue(head: Optional[str], instance_id: str) -> Optional[int]:
    """The issue number encoded in a head branch ``harness/<instance_id>/issue-N``,
    or None if the branch is not one THIS instance authored. The single source of
    truth for "is this PR ours to review/merge" — instance-scoped so two machines
    never race and a human's branch is never matched."""
    m = re.match(rf"^harness/{re.escape(instance_id)}/issue-(\d+)$", head or "")
    return int(m.group(1)) if m else None


def find_reviewable_pr(
    github: GitHubAdapter, *, repo: str, instance_id: str
) -> Optional[int]:
    """Lowest open PR authored by THIS instance (``harness/<instance>/issue-N``) that
    is not already flagged :data:`CHANGES_REQUESTED`. The pr_review analogue of
    :func:`find_claimable`; returns the PR number, or None if there's nothing to review."""
    numbers = [
        pr.number
        for pr in github.list_pulls(repo=repo, state="open")
        if CHANGES_REQUESTED not in pr.labels
        and harness_branch_issue(pr.head, instance_id) is not None
    ]
    return min(numbers) if numbers else None
