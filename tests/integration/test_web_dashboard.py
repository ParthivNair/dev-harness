"""Dashboard HTTP surface — exercised end-to-end with the FastAPI TestClient.

Skips cleanly if the optional ``web`` extra (FastAPI) or httpx is absent. Actions
run inline (``background_runner=lambda fn: fn()``) so assertions are deterministic;
in production those same calls dispatch on a background thread.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from harness.application import coordination as co  # noqa: E402
from harness.adapters.github.fake import InMemoryGitHub  # noqa: E402
from tests.fakes import make_container  # noqa: E402

pytestmark = pytest.mark.integration

REPO = "acme/app"


def _seed(gh: InMemoryGitHub, title: str = "Add feature") -> int:
    return gh.create_issue(repo=REPO, title=title, body="do X", labels=[co.QUEUED]).number


def _client(c, *, allow_actions: bool = True) -> TestClient:  # type: ignore[no-untyped-def]
    from harness.web.server import create_app

    app = create_app(c, allow_actions=allow_actions, background_runner=lambda fn: fn())
    return TestClient(app)


def _start_dev_task(client: TestClient) -> str:
    res = client.post("/api/runs", json={"loop": "dev_task", "project": "sample"})
    assert res.status_code == 200, res.text
    return res.json()["run_id"]


def test_overview_endpoint_shape(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    _seed(gh)
    client = _client(make_container(tmp_path, repo=REPO, github=gh))
    body = client.get("/api/overview").json()
    assert "totals" in body and "spend" in body and "board" in body
    assert body["config"]["allow_actions"] is True
    assert body["board"]["projects"][0]["queued"] == 1


def test_overview_board_lists_deployable_issues(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed(gh)
    client = _client(make_container(tmp_path, repo=REPO, github=gh))
    proj = client.get("/api/overview").json()["board"]["projects"][0]
    issues = proj["issues"]
    assert [i["number"] for i in issues] == [number]
    assert issues[0]["deployable"] is True
    assert issues[0]["title"] == "Add feature"


def test_deploy_specific_issue_then_fresh_refetch(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed(gh)
    client = _client(make_container(tmp_path, repo=REPO, github=gh))

    # Deploy an agent on THIS issue (the one-click queue button posts exactly this).
    res = client.post("/api/runs", json={"loop": "dev_task", "project": "sample", "issue": number})
    assert res.status_code == 200, res.text

    # A plain poll may serve the cached snapshot; ?fresh=1 forces a refetch so the
    # just-deployed issue is no longer offered as deployable.
    proj = client.get("/api/overview", params={"fresh": "true"}).json()["board"]["projects"][0]
    view = next(i for i in proj["issues"] if i["number"] == number)
    assert view["deployable"] is False
    assert view["state"] == co.NEEDS_VERIFICATION  # echo loop drove it to the gate


def test_start_then_run_detail_is_waiting(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    _seed(gh)
    client = _client(make_container(tmp_path, repo=REPO, github=gh))
    run_id = _start_dev_task(client)
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "WAITING"
    assert detail["current_step"] == "verify_gate"
    # overview now shows one active gated run
    ov = client.get("/api/overview").json()
    assert ov["totals"]["waiting"] == 1
    assert ov["active"][0]["has_gate"] is True


def test_answer_endpoint_completes_run(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed(gh)
    client = _client(make_container(tmp_path, repo=REPO, github=gh))
    run_id = _start_dev_task(client)
    res = client.post(f"/api/runs/{run_id}/answer", json={"approved": True, "notes": "ok"})
    assert res.status_code == 200
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "COMPLETED"
    assert co.state_of(gh.get_issue(repo=REPO, number=number)) == co.PR_OPEN


def test_answer_on_non_waiting_is_409(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    _seed(gh)
    c = make_container(tmp_path, repo=REPO, github=gh)
    client = _client(c)
    run_id = _start_dev_task(client)
    client.post(f"/api/runs/{run_id}/answer", json={"approved": True})  # -> COMPLETED
    again = client.post(f"/api/runs/{run_id}/answer", json={"approved": True})
    assert again.status_code == 409


def test_abort_endpoint(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    number = _seed(gh)
    client = _client(make_container(tmp_path, repo=REPO, github=gh))
    run_id = _start_dev_task(client)
    res = client.post(f"/api/runs/{run_id}/abort")
    assert res.status_code == 200 and res.json()["status"] == "ABORTED"
    assert co.state_of(gh.get_issue(repo=REPO, number=number)) == co.BLOCKED


def test_artifact_served_then_traversal_rejected(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    _seed(gh)
    c = make_container(tmp_path, repo=REPO, github=gh)
    client = _client(c)
    run_id = _start_dev_task(client)

    # The dev gate wrote a real artifact under .harness/artifacts -> served.
    ok = client.get(f"/api/runs/{run_id}/artifact")
    assert ok.status_code == 200

    # Point the pending request at a file OUTSIDE the artifacts dir -> rejected.
    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    rec = c.store.load(run_id)
    assert rec.pending_request is not None
    rec.pending_request.artifact_path = str(secret)
    c.store.save(rec)
    blocked = client.get(f"/api/runs/{run_id}/artifact")
    assert blocked.status_code == 403


def test_unknown_run_is_404(tmp_path: Path) -> None:
    client = _client(make_container(tmp_path))
    assert client.get("/api/runs/nope").status_code == 404


def test_read_only_blocks_actions(tmp_path: Path) -> None:
    gh = InMemoryGitHub()
    _seed(gh)
    client = _client(make_container(tmp_path, repo=REPO, github=gh), allow_actions=False)
    assert client.post("/api/runs", json={"loop": "dev_task", "project": "sample"}).status_code == 403
    assert client.post("/api/scheduler/tick").status_code == 403
    assert client.get("/api/overview").json()["config"]["allow_actions"] is False
