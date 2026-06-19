"""InMemoryGitHub: a deterministic in-memory fake of the GitHubAdapter port.

It is the M1 default so the demo runs with no token or network, AND it is the
double used in tests. ``open_draft_pr`` asserts the draft invariant structurally
(it can only ever create a draft), mirroring the real adapter's guarantee.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Optional, Sequence

from harness.ports.github import (
    ChecksState,
    IssueRef,
    PRState,
    PullChecks,
    PullRef,
    ReviewEvent,
)


class InMemoryGitHub:
    def __init__(self) -> None:
        self._issues: dict[tuple[str, int], IssueRef] = {}
        self._pulls: dict[tuple[str, int], PullRef] = {}
        self._next_issue = 1
        self._next_pull = 1000
        # pr_review state: per-PR review diff, CI rollup, and posted reviews.
        self._diffs: dict[tuple[str, int], str] = {}
        self._checks: dict[tuple[str, int], ChecksState] = {}
        self._reviews: dict[tuple[str, int], list[tuple[ReviewEvent, str]]] = {}
        self._merges: dict[tuple[str, int], str] = {}  # (repo, number) -> merge method used

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
        key = (repo, number)
        if key in self._issues:
            issue = self._issues[key]
            merged = tuple(dict.fromkeys((*issue.labels, *labels)))
            return self.set_labels(repo=repo, number=number, labels=merged)
        # GitHub shares the issue/PR number space, so add_labels also targets PRs
        # (the pr_review loop tags a PR ``harness:changes-requested``). The fake keeps
        # them in separate dicts, so resolve to the pull and synthesize the IssueRef view.
        if key in self._pulls:
            pr = self._pulls[key]
            merged = tuple(dict.fromkeys((*pr.labels, *labels)))
            self._pulls[key] = replace(pr, labels=merged)
            return IssueRef(
                number=pr.number, title=pr.title, state=pr.state.value,
                labels=merged, assignee=None, url=pr.url,
            )
        raise KeyError(key)

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
            head=head,
        )
        self._pulls[(repo, number)] = pr
        return pr

    def get_pull_diff(self, *, repo: str, number: int) -> str:
        return self._diffs.get((repo, number), f"[fake] diff for PR #{number}")

    def get_pull_checks(self, *, repo: str, number: int) -> PullChecks:
        # Default: NONE (no CI configured) — passes the green-CI bar vacuously.
        return PullChecks(state=self._checks.get((repo, number), ChecksState.NONE))

    def review_pull(
        self, *, repo: str, number: int, body: str, event: ReviewEvent
    ) -> None:
        self._reviews.setdefault((repo, number), []).append((event, body))

    # ---- gated / opt-in writes ----
    def mark_pr_ready(self, *, repo: str, number: int) -> PullRef:
        pr = self._pulls[(repo, number)]
        updated = replace(pr, draft=False)
        self._pulls[(repo, number)] = updated
        return updated

    def merge_pull(
        self, *, repo: str, number: int, method: str = "squash"
    ) -> PullRef:
        pr = self._pulls[(repo, number)]
        updated = replace(pr, state=PRState.MERGED, draft=False)
        self._pulls[(repo, number)] = updated
        self._merges[(repo, number)] = method
        return updated

    # ---- test helpers (not part of the port) ----
    def set_pull(
        self,
        *,
        repo: str,
        number: int,
        mergeable: Optional[bool] = None,
        draft: Optional[bool] = None,
        state: Optional[PRState] = None,
    ) -> PullRef:
        """Mutate a PR's merge-relevant fields so tests can drive the loop."""
        pr = self._pulls[(repo, number)]
        updated = replace(
            pr,
            mergeable=pr.mergeable if mergeable is None else mergeable,
            draft=pr.draft if draft is None else draft,
            state=pr.state if state is None else state,
        )
        self._pulls[(repo, number)] = updated
        return updated

    def set_pull_checks(self, *, repo: str, number: int, state: ChecksState) -> None:
        self._checks[(repo, number)] = state

    def set_pull_diff(self, *, repo: str, number: int, diff: str) -> None:
        self._diffs[(repo, number)] = diff

    def reviews_for(self, *, repo: str, number: int) -> list[tuple[ReviewEvent, str]]:
        return list(self._reviews.get((repo, number), []))

    def merge_method_for(self, *, repo: str, number: int) -> Optional[str]:
        return self._merges.get((repo, number))
