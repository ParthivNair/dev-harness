"""Use-case layer tests — the shared engine path behind the CLI and the dashboard.

Driven by the production-default in-memory fakes (via :func:`tests.fakes.make_container`),
so these exercise the real ``start / answer / abort / overview`` behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import operations
from harness.application import coordination as co
from harness.adapters.github.fake import InMemoryGitHub
from harness.domain.models import RunStatus
from tests.fakes import make_container

REPO = "acme/app"


def _seed(gh: InMemoryGitHub, title: str = "Add feature") -> int:
    return gh.create_issue(repo=REPO, title=title, body="do X", labels=[co.QUEUED]).number


def _to_gate(tmp_path: Path):  # type: ignore[no-untyped-def]
    gh = InMemoryGitHub()
    number = _seed(gh)
    c = make_container(tmp_path, repo=REPO, github=gh)
    record = operations.create_run_for(c, loop="dev_task", project_id="sample")
    assert record.data["issue_number"] == number
    status = operations.execute_run(c, loop_name="dev_task", project_id="sample", run_id=record.run_id)
    assert status is RunStatus.WAITING
    return c, gh, record.run_id, number


def test_create_run_for_dev_task_reaches_gate(tmp_path: Path) -> None:
    c, gh, run_id, number = _to_gate(tmp_path)
    rec = c.store.load(run_id)
    assert rec.status is RunStatus.WAITING
    assert rec.current_step == "verify_gate"
    assert co.state_of(gh.get_issue(repo=REPO, number=number)) == co.NEEDS_VERIFICATION


def test_answer_run_approves_to_completion(tmp_path: Path) -> None:
    c, gh, run_id, number = _to_gate(tmp_path)
    status = operations.answer_run(c, run_id=run_id, approved=True, notes="looks good")
    assert status is RunStatus.COMPLETED
    final = c.store.load(run_id)
    assert final.data["pr_url"]
    assert co.state_of(gh.get_issue(repo=REPO, number=number)) == co.PR_OPEN
    pulls = gh.list_pulls(repo=REPO)
    assert len(pulls) == 1 and pulls[0].draft is True


def test_answer_run_reject_loops_back_to_waiting(tmp_path: Path) -> None:
    c, _gh, run_id, _ = _to_gate(tmp_path)
    status = operations.answer_run(c, run_id=run_id, approved=False, notes="crackle")
    assert status is RunStatus.WAITING
    mid = c.store.load(run_id)
    assert mid.breakers.loop_count == 2
    assert mid.data["last_failure"]["phase"] == "verify"


def test_answer_run_on_non_waiting_raises(tmp_path: Path) -> None:
    c = make_container(tmp_path)
    rec = operations.create_run_for(c, loop="demo", project_id="sample")
    # demo run is CREATED, not WAITING
    with pytest.raises(operations.InvalidAnswer):
        operations.answer_run(c, run_id=rec.run_id, approved=True)


def test_abort_waiting_run_marks_blocked(tmp_path: Path) -> None:
    c, gh, run_id, number = _to_gate(tmp_path)
    status = operations.abort_run(c, run_id=run_id, reason="operator stop")
    assert status is RunStatus.ABORTED
    rec = c.store.load(run_id)
    assert rec.status is RunStatus.ABORTED
    assert rec.terminal_reason == "operator stop"
    assert rec.current_step is None
    # dev_task abort leaves the issue blocked for human triage
    assert co.state_of(gh.get_issue(repo=REPO, number=number)) == co.BLOCKED


def test_abort_is_idempotent_on_terminal(tmp_path: Path) -> None:
    c, _gh, run_id, _ = _to_gate(tmp_path)
    operations.answer_run(c, run_id=run_id, approved=True)  # -> COMPLETED
    assert operations.abort_run(c, run_id=run_id) is RunStatus.COMPLETED  # unchanged


def test_not_owned_raises(tmp_path: Path) -> None:
    c = make_container(tmp_path, instance="this-machine", owner="other-machine")
    with pytest.raises(operations.NotOwned):
        operations.create_run_for(c, loop="demo", project_id="sample")


def test_no_queued_work_raises(tmp_path: Path) -> None:
    c = make_container(tmp_path, github=InMemoryGitHub())  # no issues seeded
    with pytest.raises(operations.NoQueuedWork):
        operations.create_run_for(c, loop="dev_task", project_id="sample")


def test_overview_aggregates_active_and_spend(tmp_path: Path) -> None:
    c, _gh, run_id, _ = _to_gate(tmp_path)
    ov = operations.overview(c)
    assert ov["totals"]["active"] == 1
    assert ov["totals"]["waiting"] == 1
    assert ov["active"][0]["run_id"] == run_id
    assert ov["active"][0]["has_gate"] is True
    assert ov["counts"]["WAITING"] == 1
    assert ov["spend"]["ceiling_usd"] > 0


def test_board_reports_queue_depth(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    _seed(gh, "one")
    _seed(gh, "two")
    c = make_container(tmp_path, repo=REPO, github=gh)
    bd = operations.board(c)
    assert bd["projects"][0]["queued"] == 2


def test_github_snapshot_lists_deployable_issues(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    queued = _seed(gh, "queued work")
    plain = gh.create_issue(repo=REPO, title="human bug", body="x", labels=["bug"]).number
    # An issue already claimed + in-progress by ANOTHER instance is not deployable here.
    foreign = gh.create_issue(repo=REPO, title="theirs", body="x", labels=[co.QUEUED]).number
    co.claim(gh, repo=REPO, number=foreign, instance_id="other-machine")

    c = make_container(tmp_path, instance="this-machine", repo=REPO, github=gh)
    snap = operations.github_snapshot(c)
    proj = snap["projects"][0]

    assert proj["queued"] == 1 and proj["in_progress"] == 1
    views = {i["number"]: i for i in proj["issues"]}
    assert views[queued]["deployable"] is True        # harness:queued, unclaimed
    assert views[plain]["deployable"] is True          # plain human issue, claimable
    assert views[foreign]["deployable"] is False       # owned by another instance
    assert views[foreign]["owner"] == "other-machine"
    # Deployable work is ordered ahead of in-progress/claimed work.
    assert proj["issues"][0]["deployable"] is True


def test_github_snapshot_isolates_a_failing_repo(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    _seed(gh)

    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("token missing")

    gh.list_issues = boom  # type: ignore[method-assign]
    c = make_container(tmp_path, repo=REPO, github=gh)
    proj = operations.github_snapshot(c)["projects"][0]
    assert "token missing" in proj["error"]
    assert "issues" not in proj or proj["issues"] == []  # never a confident empty/zero
