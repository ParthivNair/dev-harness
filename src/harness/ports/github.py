"""GitHubAdapter port: GitHub as the durable cross-machine coordination substrate.

Issues = the task queue, labels + PR state = status, draft PRs = work product.

Autonomy used to be *purely* structural here: the port exposed no way to merge, so
the autonomous path could not call what the interface did not expose. As of the
``pr_review`` (close-the-loop) milestone, ``merge_pull`` and ``review_pull`` exist —
but the structural guard is preserved by where the boundary now sits: **merge is
reachable only through the** :class:`~harness.application.action_guard.ActionGuard`
**with a per-repo opt-in** (``merge_to_main`` is ``forbidden`` in the instance
default; a repo raises it to ``gated``/``autonomous`` in its own
``harness.project.toml``). This is the same admit-before-side-effect boundary the
existing *gated* ``mark_pr_ready`` already crosses. There is still **no** ``push`` /
``force_push`` / ``update main`` here, and the Executor still refuses to push trunks
locally — ``main`` is reached ONLY via a reviewed, guard-admitted API merge.
``open_draft_pr`` always opens a *draft*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Protocol, Sequence, runtime_checkable


class PRState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class ReviewEvent(str, Enum):
    """The GitHub PR-review verbs ``review_pull`` posts (maps 1:1 to the REST API)."""

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


class ChecksState(str, Enum):
    """Rolled-up CI status for a PR's head commit — the pre-merge "green CI" gate.

    ``NONE`` means no checks/statuses are configured on the repo, so the green-CI bar
    is vacuously satisfied (nothing can fail). ``PENDING`` means checks are still
    running — the loop defers (retries next tick) rather than merging blind.
    """

    SUCCESS = "success"
    PENDING = "pending"
    FAILURE = "failure"
    NONE = "none"


@dataclass(frozen=True)
class PullChecks:
    """The CI rollup for one PR's head SHA. ``state`` is the pre-merge gate."""

    state: ChecksState


@dataclass(frozen=True)
class IssueRef:
    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    assignee: Optional[str]
    url: str
    body: str = ""  # the task description a dev loop feeds to Claude (default-last, additive)


@dataclass(frozen=True)
class PullRef:
    number: int
    title: str
    state: PRState
    draft: bool
    mergeable: Optional[bool]  # None-safe: GitHub computes this asynchronously
    labels: tuple[str, ...]
    url: str
    head: str = ""  # head branch ref, e.g. "harness/<instance>/issue-7" (default-last, additive)


@runtime_checkable
class GitHubAdapter(Protocol):
    # ---- READS (always allowed, even for projects this install does not own) ----
    def list_issues(
        self,
        *,
        repo: str,
        state: str = "open",
        labels: Sequence[str] = (),
        assignee: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> list[IssueRef]:
        """Live REST list (never the Search API — its index lags by seconds/minutes)."""

    def get_issue(self, *, repo: str, number: int) -> IssueRef: ...

    def get_pull(self, *, repo: str, number: int) -> PullRef: ...

    def list_pulls(self, *, repo: str, state: str = "open") -> list[PullRef]: ...

    def get_pull_diff(self, *, repo: str, number: int) -> str:
        """The PR's unified diff (concatenated per-file patches). Read-only; fed to
        Claude as review context. Implementations cap the size and skip binaries."""

    def get_pull_checks(self, *, repo: str, number: int) -> PullChecks:
        """Rolled-up CI status for the PR's head commit — the pre-merge green-CI gate."""

    # ---- AUTONOMOUS WRITES (autonomy tier: autonomous) ----
    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: Sequence[str] = (),
        assignee: Optional[str] = None,
    ) -> IssueRef: ...

    def set_labels(self, *, repo: str, number: int, labels: Sequence[str]) -> IssueRef: ...

    def add_labels(self, *, repo: str, number: int, labels: Sequence[str]) -> IssueRef: ...

    def assign_issue(self, *, repo: str, number: int, assignee: str) -> IssueRef: ...

    def open_draft_pr(
        self, *, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRef:
        """Open a PR. ALWAYS a draft — implementations hard-wire ``draft=True``."""

    def review_pull(
        self, *, repo: str, number: int, body: str, event: ReviewEvent
    ) -> None:
        """Post a PR review (APPROVE / REQUEST_CHANGES / COMMENT). Low-risk write —
        autonomy action ``review_pr`` (autonomous by default; a repo may forbid it)."""

    # ---- GATED / OPT-IN WRITES — must pass the ActionGuard ----
    def mark_pr_ready(self, *, repo: str, number: int) -> PullRef:
        """Draft -> ready-for-review (GraphQL under the hood). Crosses into human
        territory, so the engine routes it through the gate, never autonomously."""

    def merge_pull(
        self, *, repo: str, number: int, method: str = "squash"
    ) -> PullRef:
        """Merge the PR into its base (``main``). The ONE method that reaches a trunk.

        Autonomy action ``merge_to_main`` — ``forbidden`` in the instance default, so
        this is unreachable unless a repo opts in (``gated``/``autonomous``) and the
        :class:`~harness.application.action_guard.ActionGuard` admits the call. ``method``
        is "squash" | "merge" | "rebase". Returns the fresh (merged) ref.
        """

    # NOTE: still deliberately NO push / force_push / update_ref(main) — merge is the
    # only trunk-touching write, and only via the guarded, reviewed path above.
