"""GitHubAdapter port: GitHub as the durable cross-machine coordination substrate.

Issues = the task queue, labels + PR state = status, draft PRs = work product.

Autonomy is structural here: there is intentionally **no** ``merge_pr``,
``push``, ``force_push``, or ``update main`` method. The autonomous path cannot
call what the interface does not expose. ``open_draft_pr`` always opens a *draft*.
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


@dataclass(frozen=True)
class IssueRef:
    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    assignee: Optional[str]
    url: str


@dataclass(frozen=True)
class PullRef:
    number: int
    title: str
    state: PRState
    draft: bool
    mergeable: Optional[bool]  # None-safe: GitHub computes this asynchronously
    labels: tuple[str, ...]
    url: str


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

    # ---- GATED WRITE (autonomy tier: gated) — must pass the ActionGuard ----
    def mark_pr_ready(self, *, repo: str, number: int) -> PullRef:
        """Draft -> ready-for-review (GraphQL under the hood). Crosses into human
        territory, so the engine routes it through the gate, never autonomously."""

    # NOTE: deliberately NO merge_pr / push / force_push / update_ref(main).
