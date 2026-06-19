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

# A FOREIGN marker label (deliberately NOT in STATE_LABELS) so it rides through
# state transitions like any human tag. Stamped when a stranded/aborted run is
# requeued for a fresh attempt, so a continuation is visible on the board.
HANDOFF = "harness:handoff"

# Prioritization labels (foreign — set by the triage loop, read by find_claimable).
# Severity is the primary ordering key; effort is the "prefer quicker wins" tiebreak.
SEVERITY_SCORES: dict[str, int] = {"high": 3, "med": 2, "low": 1}
EFFORT_ORDER: dict[str, int] = {"s": 0, "m": 1, "l": 2}
SEV_PREFIX = "sev:"
EFFORT_PREFIX = "effort:"

# ``Depends on #12`` / ``Sequencing: #12, #13`` lines in an issue body declare a
# dependency on another issue. An issue is not claimable while any such issue is
# still OPEN (unmerged), so e.g. #18 -> #22 self-order without manual scheduling.
_DEPENDS_RE = re.compile(
    r"^\s*(?:depends on|sequencing)\b[:\s]*(.+)$", re.IGNORECASE | re.MULTILINE
)
_ISSUE_REF_RE = re.compile(r"#(\d+)")


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


def handoff_issue(
    github: GitHubAdapter, *, repo: str, number: int, note: str
) -> IssueRef:
    """Requeue a stranded/aborted issue for a FRESH continuation attempt.

    Unlike :func:`release` (a plain yield), a handoff also stamps the foreign
    ``HANDOFF`` label and posts ``note`` as a comment carrying the handoff packet
    (what was attempted, why it ended, spend) so the next run continues the work
    instead of restarting cold. The label rides through future transitions because
    it is not a state label. Returns the requeued issue.
    """
    issue = release(github, repo=repo, number=number)
    issue = github.add_labels(repo=repo, number=number, labels=[HANDOFF])
    github.comment_on_issue(repo=repo, number=number, body=note)
    return issue


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


def _label_value(issue: IssueRef, prefix: str) -> Optional[str]:
    """The suffix of the first ``<prefix><value>`` label on the issue, or None."""
    for label in issue.labels:
        if label.startswith(prefix):
            return label[len(prefix):]
    return None


def severity_score(issue: IssueRef) -> int:
    """Triage severity as a sortable score (high=3 / med=2 / low=1). Unlabelled
    issues score 0 so they sort *below* any labelled issue but among themselves keep
    plain number order (the regression-safe fallback)."""
    return SEVERITY_SCORES.get(_label_value(issue, SEV_PREFIX) or "", 0)


def effort_rank(issue: IssueRef) -> int:
    """Triage effort as a sortable rank (s=0 < m=1 < l=2); unlabelled sorts last so a
    quicker, sized task is preferred over an unsized one at equal severity."""
    return EFFORT_ORDER.get(_label_value(issue, EFFORT_PREFIX) or "", len(EFFORT_ORDER))


def depends_on(issue: IssueRef) -> list[int]:
    """Issue numbers this issue's body declares it ``Depends on`` / ``Sequencing`` to.

    Parses lines like ``Depends on #12`` or ``Sequencing: #12, #13``; deterministic
    and tolerant (no match => no dependencies). Pure — it reads only the body text.
    """
    out: list[int] = []
    for line in _DEPENDS_RE.findall(issue.body or ""):
        out += [int(n) for n in _ISSUE_REF_RE.findall(line)]
    return list(dict.fromkeys(out))  # dedupe, preserve order


def find_claimable(
    github: GitHubAdapter, *, repo: str, instance_id: str
) -> Optional[int]:
    """The highest-priority READY open ``queued`` issue claimable by this instance.

    Replaces a plain ``min(number)`` with dependency-aware, severity-ordered
    selection (Phase 2 §2a):

    * **Dependency gate.** An issue whose body declares ``Depends on #N`` /
      ``Sequencing`` to an issue still OPEN (unmerged) is NOT ready — it is dropped
      this pass and released once its dependency closes. So #18 -> #22 self-order.
    * **Priority.** Among ready candidates, sort by severity score desc, then lower
      effort (quicker wins), then lower number. With NO sev/effort labels this is
      pure number order (regression-safe — nothing changes for an untriaged queue).

    Pure/deterministic over the live REST list (never the Search API). Returns the
    issue number to pass to :func:`claim`, or None if there is no ready work.
    """
    issues = github.list_issues(repo=repo, state="open", labels=[QUEUED])
    candidates = [i for i in issues if owner_of(i) in (None, instance_id)]
    if not candidates:
        return None

    # The set of issue numbers still open in this repo — a dependency on any of them
    # gates the dependent issue. One live read covers every candidate's deps.
    open_numbers = {i.number for i in github.list_issues(repo=repo, state="open")}
    ready = [
        i for i in candidates if not any(dep in open_numbers for dep in depends_on(i))
    ]
    if not ready:
        return None

    # severity desc, effort asc, number asc — deterministic and total.
    ready.sort(key=lambda i: (-severity_score(i), effort_rank(i), i.number))
    return ready[0].number
