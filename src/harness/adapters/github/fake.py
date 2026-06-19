"""InMemoryGitHub: a deterministic in-memory fake of the GitHubAdapter port.

It is the M1 default so the demo runs with no token or network, AND it is the
double used in tests. ``open_draft_pr`` asserts the draft invariant structurally
(it can only ever create a draft), mirroring the real adapter's guarantee.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from harness.ports.github import IssueRef, PRState, PullRef


class InMemoryGitHub:
    def __init__(self) -> None:
        self._issues: dict[tuple[str, int], IssueRef] = {}
        self._pulls: dict[tuple[str, int], PullRef] = {}
        self._labels: dict[str, set[str]] = {}  # repo -> created repo-level label names
        self._next_issue = 1
        self._next_pull = 1000

    # ---- reads ----
    def list_issues(
        self,
        *,
        repo: str,
        state: str = "open",
        labels: Sequence[str] = (),
        assignee: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> list[IssueRef]:
        out = []
        for (r, _), issue in self._issues.items():
            if r != repo:
                continue
            if state != "all" and issue.state != state:
                continue
            if labels and not set(labels).issubset(set(issue.labels)):
                continue
            if assignee is not None and issue.assignee != assignee:
                continue
            out.append(issue)
        return out

    def get_issue(self, *, repo: str, number: int) -> IssueRef:
        return self._issues[(repo, number)]

    def get_pull(self, *, repo: str, number: int) -> PullRef:
        return self._pulls[(repo, number)]

    def list_pulls(self, *, repo: str, state: str = "open") -> list[PullRef]:
        return [
            pr
            for (r, _), pr in self._pulls.items()
            if r == repo and (state == "all" or pr.state.value == state)
        ]

    # ---- autonomous writes ----
    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: Sequence[str] = (),
        assignee: Optional[str] = None,
    ) -> IssueRef:
        number = self._next_issue
        self._next_issue += 1
        issue = IssueRef(
            number=number,
            title=title,
            state="open",
            labels=tuple(labels),
            assignee=assignee,
            url=f"https://github.com/{repo}/issues/{number}",
            body=body,
        )
        self._issues[(repo, number)] = issue
        return issue

    def create_label(
        self,
        *,
        repo: str,
        name: str,
        color: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        created = self._labels.setdefault(repo, set())
        if name in created:
            return False
        created.add(name)
        return True

    def set_labels(self, *, repo: str, number: int, labels: Sequence[str]) -> IssueRef:
        issue = self._issues[(repo, number)]
        updated = IssueRef(
            number=issue.number,
            title=issue.title,
            state=issue.state,
            labels=tuple(labels),
            assignee=issue.assignee,
            url=issue.url,
            body=issue.body,
        )
        self._issues[(repo, number)] = updated
        return updated

    def add_labels(self, *, repo: str, number: int, labels: Sequence[str]) -> IssueRef:
        issue = self._issues[(repo, number)]
        merged = tuple(dict.fromkeys((*issue.labels, *labels)))
        return self.set_labels(repo=repo, number=number, labels=merged)

    def assign_issue(self, *, repo: str, number: int, assignee: str) -> IssueRef:
        issue = self._issues[(repo, number)]
        updated = IssueRef(
            number=issue.number,
            title=issue.title,
            state=issue.state,
            labels=issue.labels,
            assignee=assignee,
            url=issue.url,
            body=issue.body,
        )
        self._issues[(repo, number)] = updated
        return updated

    def open_draft_pr(
        self, *, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRef:
        number = self._next_pull
        self._next_pull += 1
        pr = PullRef(
            number=number,
            title=title,
            state=PRState.OPEN,
            draft=True,  # structural invariant: always a draft
            mergeable=None,
            labels=(),
            url=f"https://github.com/{repo}/pull/{number}",
        )
        self._pulls[(repo, number)] = pr
        return pr

    # ---- gated write ----
    def mark_pr_ready(self, *, repo: str, number: int) -> PullRef:
        pr = self._pulls[(repo, number)]
        updated = PullRef(
            number=pr.number,
            title=pr.title,
            state=pr.state,
            draft=False,
            mergeable=pr.mergeable,
            labels=pr.labels,
            url=pr.url,
        )
        self._pulls[(repo, number)] = updated
        return updated
