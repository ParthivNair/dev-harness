"""PyGithubAdapter: thin first implementation of the GitHubAdapter port.

Backed by PyGithub. Selected only when ``github.use_in_memory_fake = false`` and
a token is present, so it is not exercised by the credential-free M1 demo. The
import of ``github`` is lazy so the package stays optional at runtime.

Notes from research baked in here:
  * Reads use the live REST ``get_issues`` (never the Search API — its index lags).
  * ``open_draft_pr`` hard-wires ``draft=True``.
  * ``mark_pr_ready`` calls ``mark_ready_for_review()`` (GraphQL under the hood).
  * ``mergeable`` is computed asynchronously by GitHub, so it can be ``None``.
  * ``merge_pull`` is the ONLY trunk-touching write; it is reachable only via the
    guarded, reviewed pr_review path (see the port docstring). No push / force-push.
  * ``review_pull`` downgrades APPROVE -> COMMENT if GitHub refuses (a PAT can't
    approve a PR it authored), so a harness self-review is still recorded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

from harness.ports.github import (
    ChecksState,
    IssueRef,
    PRState,
    PullChecks,
    PullRef,
    ReviewEvent,
)

# Check-run conclusions that count as a hard CI failure (block the merge gate).
_CHECK_FAILURE = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "stale", "startup_failure"}
)


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
    head = getattr(getattr(pr, "head", None), "ref", "") or ""
    return PullRef(
        number=pr.number,
        title=pr.title,
        state=state,
        draft=bool(getattr(pr, "draft", False)),
        mergeable=pr.mergeable,  # may be None (async)
        labels=tuple(label.name for label in pr.labels),
        url=pr.html_url,
        head=head,
        body=pr.body or "",
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

    def get_pull_diff(self, *, repo: str, number: int) -> str:
        """Unified diff = per-file patches concatenated (skip binaries; capped)."""
        pr = self._repo(repo).get_pull(number)
        parts: list[str] = []
        total = 0
        cap = 30_000  # keep the review prompt bounded
        for f in pr.get_files():
            patch = getattr(f, "patch", None)
            if not patch:  # binary / no textual diff
                continue
            chunk = f"--- {f.filename}\n{patch}\n"
            parts.append(chunk)
            total += len(chunk)
            if total >= cap:
                parts.append(f"\n[diff truncated at {cap} chars]\n")
                break
        return "".join(parts)

    def get_pull_checks(self, *, repo: str, number: int) -> PullChecks:
        pr = self._repo(repo).get_pull(number)
        commit = self._repo(repo).get_commit(pr.head.sha)
        combined = commit.get_combined_status()
        runs = list(commit.get_check_runs())
        if combined.total_count == 0 and not runs:
            return PullChecks(state=ChecksState.NONE)
        if combined.total_count > 0 and combined.state in ("failure", "error"):
            return PullChecks(state=ChecksState.FAILURE)
        if any(getattr(r, "conclusion", None) in _CHECK_FAILURE for r in runs):
            return PullChecks(state=ChecksState.FAILURE)
        if combined.total_count > 0 and combined.state == "pending":
            return PullChecks(state=ChecksState.PENDING)
        if any(getattr(r, "status", None) != "completed" for r in runs):
            return PullChecks(state=ChecksState.PENDING)
        return PullChecks(state=ChecksState.SUCCESS)

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

    def comment_on_issue(self, *, repo: str, number: int, body: str) -> None:
        self._repo(repo).get_issue(number).create_comment(body)

    def open_draft_pr(
        self, *, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRef:
        pr = self._repo(repo).create_pull(
            title=title, body=body, head=head, base=base, draft=True
        )
        return _to_pull_ref(pr)

    def review_pull(
        self, *, repo: str, number: int, body: str, event: ReviewEvent
    ) -> None:
        from github import GithubException  # lazy import

        pr = self._repo(repo).get_pull(number)
        try:
            pr.create_review(body=body, event=event.value)
        except GithubException:
            # GitHub forbids approving/requesting-changes on a PR you authored, and the
            # harness PAT authors its own PRs. Downgrade to a plain COMMENT so the
            # review is still recorded; the merge itself carries the real authorization.
            if event is ReviewEvent.COMMENT:
                raise
            pr.create_review(body=f"[{event.value}]\n\n{body}", event=ReviewEvent.COMMENT.value)

    # ---- gated / opt-in writes ----
    def mark_pr_ready(self, *, repo: str, number: int) -> PullRef:
        pr = self._repo(repo).get_pull(number)
        pr.mark_ready_for_review()
        return _to_pull_ref(self._repo(repo).get_pull(number))

    def merge_pull(
        self, *, repo: str, number: int, method: str = "squash"
    ) -> PullRef:
        pr = self._repo(repo).get_pull(number)
        if getattr(pr, "merged", False):  # already merged (retry / TOCTOU race): idempotent
            return _to_pull_ref(pr)
        pr.merge(merge_method=method)
        return _to_pull_ref(self._repo(repo).get_pull(number))
