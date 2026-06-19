"""PyGithubAdapter: thin first implementation of the GitHubAdapter port.

Backed by PyGithub. Selected only when ``github.use_in_memory_fake = false`` and
a token is present, so it is not exercised by the credential-free M1 demo. The
import of ``github`` is lazy so the package stays optional at runtime.

Notes from research baked in here:
  * Reads use the live REST ``get_issues`` (never the Search API — its index lags).
  * ``open_draft_pr`` hard-wires ``draft=True``.
  * ``mark_pr_ready`` calls ``mark_ready_for_review()`` (GraphQL under the hood).
  * ``mergeable`` is computed asynchronously by GitHub, so it can be ``None``.
  * There is deliberately no merge / push / force-push method.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

from harness.ports.github import IssueRef, PRState, PullRef


def _to_issue_ref(issue: Any) -> IssueRef:
    return IssueRef(
        number=issue.number,
        title=issue.title,
        state=issue.state,
        labels=tuple(label.name for label in issue.labels),
        assignee=issue.assignee.login if issue.assignee else None,
        url=issue.html_url,
        body=issue.body or "",
    )


def _to_pull_ref(pr: Any) -> PullRef:
    state = PRState.MERGED if getattr(pr, "merged", False) else PRState(pr.state)
    return PullRef(
        number=pr.number,
        title=pr.title,
        state=state,
        draft=bool(getattr(pr, "draft", False)),
        mergeable=pr.mergeable,  # may be None (async)
        labels=tuple(label.name for label in pr.labels),
        url=pr.html_url,
    )


class PyGithubAdapter:
    def __init__(self, *, token: str, api_base: str = "https://api.github.com") -> None:
        from github import Auth, Github  # lazy import

        base = None if api_base.rstrip("/") == "https://api.github.com" else api_base
        self._gh = Github(auth=Auth.Token(token), base_url=base) if base else Github(
            auth=Auth.Token(token)
        )

    def _repo(self, repo: str) -> Any:
        return self._gh.get_repo(repo)

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
        gh_repo = self._repo(repo)
        kwargs: dict[str, Any] = {"state": state}
        if labels:
            kwargs["labels"] = [gh_repo.get_label(name) for name in labels]
        if assignee is not None:
            kwargs["assignee"] = assignee
        if since is not None:
            kwargs["since"] = since
        # GitHub returns PRs as issues too; filter to real issues.
        return [_to_issue_ref(i) for i in gh_repo.get_issues(**kwargs) if i.pull_request is None]

    def get_issue(self, *, repo: str, number: int) -> IssueRef:
        return _to_issue_ref(self._repo(repo).get_issue(number))

    def get_pull(self, *, repo: str, number: int) -> PullRef:
        return _to_pull_ref(self._repo(repo).get_pull(number))

    def list_pulls(self, *, repo: str, state: str = "open") -> list[PullRef]:
        return [_to_pull_ref(p) for p in self._repo(repo).get_pulls(state=state)]

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
        kwargs: dict[str, Any] = {"title": title, "body": body}
        if labels:
            kwargs["labels"] = list(labels)
        if assignee is not None:
            kwargs["assignee"] = assignee
        return _to_issue_ref(self._repo(repo).create_issue(**kwargs))

    def create_label(
        self,
        *,
        repo: str,
        name: str,
        color: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        from github import GithubException  # lazy import

        kwargs: dict[str, Any] = {}
        if description is not None:
            kwargs["description"] = description
        try:
            self._repo(repo).create_label(name=name, color=color or "ededed", **kwargs)
            return True
        except GithubException as exc:
            if exc.status == 422:  # label already exists -> idempotent no-op
                return False
            raise

    def set_labels(self, *, repo: str, number: int, labels: Sequence[str]) -> IssueRef:
        issue = self._repo(repo).get_issue(number)
        issue.set_labels(*labels)
        return _to_issue_ref(self._repo(repo).get_issue(number))

    def add_labels(self, *, repo: str, number: int, labels: Sequence[str]) -> IssueRef:
        issue = self._repo(repo).get_issue(number)
        issue.add_to_labels(*labels)
        return _to_issue_ref(self._repo(repo).get_issue(number))

    def assign_issue(self, *, repo: str, number: int, assignee: str) -> IssueRef:
        issue = self._repo(repo).get_issue(number)
        issue.add_to_assignees(assignee)
        return _to_issue_ref(self._repo(repo).get_issue(number))

    def open_draft_pr(
        self, *, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRef:
        pr = self._repo(repo).create_pull(
            title=title, body=body, head=head, base=base, draft=True
        )
        return _to_pull_ref(pr)

    # ---- gated write ----
    def mark_pr_ready(self, *, repo: str, number: int) -> PullRef:
        pr = self._repo(repo).get_pull(number)
        pr.mark_ready_for_review()
        return _to_pull_ref(self._repo(repo).get_pull(number))
