"""PyGithubAdapter: field mapping + the draft-PR invariant against a fake client.

PyGithubAdapter is the production GitHubAdapter, but its ``__init__`` builds a
real ``Github`` client (lazy ``from github import ...``). We never want that here,
so we subclass and feed a fake repo through the ``_repo`` seam — the same
override trick as ``AssignFails`` in test_coordination.py. No network, no PyGithub.
"""

from __future__ import annotations

from typing import Any

from harness.adapters.github.pygithub_adapter import (
    PyGithubAdapter,
    _to_issue_ref,
    _to_pull_ref,
)
from harness.ports.github import PRState


# ---- minimal PyGithub-shaped fakes (attribute access is all the mappers touch) ----
class _Label:
    def __init__(self, name: str) -> None:
        self.name = name


class _User:
    def __init__(self, login: str) -> None:
        self.login = login


class _Issue:
    def __init__(
        self,
        *,
        number: int = 1,
        title: str = "an issue",
        state: str = "open",
        labels: tuple[str, ...] = (),
        assignee: Any = None,
        html_url: str = "https://github.com/acme/x/issues/1",
        body: Any = "",
        pull_request: Any = None,
    ) -> None:
        self.number = number
        self.title = title
        self.state = state
        self.labels = [_Label(n) for n in labels]
        self.assignee = assignee
        self.html_url = html_url
        self.body = body
        self.pull_request = pull_request  # non-None on the GitHub-PR-as-issue rows


class _Pull:
    def __init__(
        self,
        *,
        number: int = 2,
        title: str = "a pull",
        state: str = "open",
        merged: bool = False,
        draft: bool = False,
        mergeable: Any = None,
        labels: tuple[str, ...] = (),
        html_url: str = "https://github.com/acme/x/pull/2",
    ) -> None:
        self.number = number
        self.title = title
        self.state = state
        self.merged = merged
        self.draft = draft
        self.mergeable = mergeable
        self.labels = [_Label(n) for n in labels]
        self.html_url = html_url
        self.marked_ready = False

    def mark_ready_for_review(self) -> None:
        self.marked_ready = True


class _Repo:
    """Records the writes the adapter makes; serves the reads it asks for."""

    def __init__(self, *, issues: list[_Issue] = (), pull: _Pull | None = None) -> None:
        self._issues = list(issues)
        self._pull = pull
        self.create_pull_kwargs: dict[str, Any] | None = None

    def get_issues(self, **_kwargs: Any) -> list[_Issue]:
        return self._issues

    def get_pull(self, _number: int) -> _Pull:
        assert self._pull is not None
        return self._pull

    def create_pull(self, **kwargs: Any) -> _Pull:
        self.create_pull_kwargs = kwargs
        return _Pull(number=7, title=kwargs["title"], draft=kwargs.get("draft", False))


class _Adapter(PyGithubAdapter):
    """PyGithubAdapter wired to a fake repo, skipping the real Github client."""

    def __init__(self, repo: _Repo) -> None:  # noqa: D401 - deliberately no super().__init__
        self._fake_repo = repo

    def _repo(self, repo: str) -> _Repo:
        return self._fake_repo


# ---- mappers ----
def test_to_issue_ref_maps_every_field() -> None:
    ref = _to_issue_ref(
        _Issue(
            number=11,
            title="bug",
            state="open",
            labels=("harness:queued", "bug"),
            assignee=_User("octocat"),
            html_url="https://github.com/acme/x/issues/11",
            body="details",
        )
    )
    assert ref.number == 11
    assert ref.title == "bug"
    assert ref.state == "open"
    assert ref.labels == ("harness:queued", "bug")  # tuple of label names
    assert ref.assignee == "octocat"  # assignee.login
    assert ref.url == "https://github.com/acme/x/issues/11"  # html_url
    assert ref.body == "details"


def test_to_issue_ref_none_assignee_and_none_body() -> None:
    ref = _to_issue_ref(_Issue(assignee=None, body=None))
    assert ref.assignee is None  # no assignee -> None, not an attribute error
    assert ref.body == ""  # body or "" coalesces None -> ''


def test_to_pull_ref_merged_wins_over_state() -> None:
    # GitHub leaves merged PRs with state="closed"; merged=True must map to MERGED.
    ref = _to_pull_ref(_Pull(state="closed", merged=True))
    assert ref.state is PRState.MERGED


def test_to_pull_ref_open_and_closed_map_through_prstate() -> None:
    assert _to_pull_ref(_Pull(state="open", merged=False)).state is PRState.OPEN
    assert _to_pull_ref(_Pull(state="closed", merged=False)).state is PRState.CLOSED


def test_to_pull_ref_maps_draft_mergeable_labels() -> None:
    ref = _to_pull_ref(
        _Pull(number=3, draft=True, mergeable=None, labels=("wip",), html_url="u")
    )
    assert ref.number == 3
    assert ref.draft is True
    assert ref.mergeable is None  # async-None passes through untouched
    assert ref.labels == ("wip",)
    assert ref.url == "u"


# ---- list_issues: PRs masquerade as issues and must be filtered out ----
def test_list_issues_filters_out_pull_requests() -> None:
    real = _Issue(number=1, pull_request=None)
    disguised_pr = _Issue(number=2, pull_request=object())  # non-None => it's a PR
    adapter = _Adapter(_Repo(issues=[real, disguised_pr]))

    refs = adapter.list_issues(repo="acme/x")

    assert [r.number for r in refs] == [1]


# ---- the draft invariant + ready-for-review gated write ----
def test_open_draft_pr_hardwires_draft_true() -> None:
    repo = _Repo()
    adapter = _Adapter(repo)

    ref = adapter.open_draft_pr(
        repo="acme/x", head="feature", base="main", title="t", body="b"
    )

    assert repo.create_pull_kwargs == {
        "title": "t",
        "body": "b",
        "head": "feature",
        "base": "main",
        "draft": True,  # the structural autonomy invariant
    }
    assert ref.draft is True


def test_mark_pr_ready_calls_mark_ready_for_review() -> None:
    pull = _Pull(number=9, state="open", draft=True)
    adapter = _Adapter(_Repo(pull=pull))

    ref = adapter.mark_pr_ready(repo="acme/x", number=9)

    assert pull.marked_ready is True
    assert ref.number == 9
