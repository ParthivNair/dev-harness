"""FastAPI app factory for the observer dashboard.

``create_app(container)`` wires read + action endpoints over the shared use-cases
and mounts the static SPA. Two design points worth calling out:

* **Actions run in the background.** ``runner.run``/``resume`` block until the next
  gate or terminal state (claude + build + test can take minutes), so start / answer
  / tick dispatch on a background thread and return immediately; the page reflects
  progress by polling ``/api/overview``. Durability makes this safe — a server crash
  mid-run leaves the record RUNNING and resumable. The background runner is injectable
  so tests run inline and stay deterministic.
* **GitHub board reads are cached.** ``/api/overview`` is polled often; the local run
  state is cheap file I/O (always fresh) but the GitHub-derived board is network-bound,
  so it is cached with a short TTL.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import Response
from starlette.types import Scope

from harness import operations
from harness.container import Container
from harness.domain.models import RunStatus
from harness.ports.run_store import RunNotFound

STATIC_DIR = Path(__file__).parent / "static"

BackgroundRunner = Callable[[Callable[[], Any]], None]


class _NoCacheStaticFiles(StaticFiles):
    """Serve the SPA with ``Cache-Control: no-cache`` so the browser revalidates
    every asset. The files are tiny and local — caching them buys nothing and risks
    a fresh ``index.html`` running a *stale cached* ``app.js`` (the queued count
    updates but the deploy queue stays blank and freshness sticks on "connecting").
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class AnswerBody(BaseModel):
    approved: bool
    notes: str = ""


class StartBody(BaseModel):
    loop: str
    project: str
    issue: Optional[int] = None
    pr: Optional[int] = None


def _default_background(fn: Callable[[], Any]) -> None:
    """Run ``fn`` on a daemon thread, logging (never crashing) on failure."""

    def _guarded() -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — a background failure must not kill the server
            print(f"[harness ui] background task failed: {exc!r}", file=sys.stderr)

    threading.Thread(target=_guarded, daemon=True).start()


def create_app(
    container: Container,
    *,
    allow_actions: bool = True,
    poll_interval_ms: int = 1500,
    board_ttl_seconds: int = 20,
    background_runner: Optional[BackgroundRunner] = None,
) -> FastAPI:
    app = FastAPI(title="dev-harness dashboard", docs_url=None, redoc_url=None)
    background = background_runner or _default_background
    board_cache: dict[str, Any] = {"at": 0.0, "data": None}

    def cached_board(force: bool = False) -> dict[str, Any]:
        """The GitHub snapshot (counts + deployable issues), cached with a short TTL.

        ``force`` re-fetches now — the dashboard's manual refresh, and after any
        action that changes the queue (a deploy) so the user never reads a stale
        count right after acting.
        """
        now = time.monotonic()
        if force or board_cache["data"] is None or (now - board_cache["at"]) > board_ttl_seconds:
            board_cache["data"] = operations.github_snapshot(container)
            board_cache["at"] = now
        return board_cache["data"]

    def invalidate_board() -> None:
        board_cache["data"] = None

    def _require_actions() -> None:
        if not allow_actions:
            raise HTTPException(status_code=403, detail="this dashboard is read-only ([ui].allow_actions=false)")

    def _load_or_404(run_id: str):  # type: ignore[no-untyped-def]
        try:
            return container.store.load(run_id)
        except RunNotFound:
            raise HTTPException(status_code=404, detail=f"no run {run_id}") from None

    # ----- reads ---------------------------------------------------------- #
    @app.get("/api/overview")
    def overview(fresh: bool = False) -> dict[str, Any]:
        data = operations.overview(container)
        data["board"] = cached_board(force=fresh)
        data["config"] = {"allow_actions": allow_actions, "poll_interval_ms": poll_interval_ms}
        return data

    @app.get("/api/runs")
    def runs(status: Optional[str] = None) -> dict[str, Any]:
        filt: Optional[RunStatus] = None
        if status:
            try:
                filt = RunStatus(status)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"unknown status '{status}'") from None
        return {"runs": [operations.run_summary(r) for r in container.store.list(filt)]}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        return _load_or_404(run_id).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/artifact")
    def artifact(run_id: str) -> FileResponse:
        record = _load_or_404(run_id)
        rel = record.pending_request.artifact_path if record.pending_request else None
        rel = rel or record.data.get("artifact_path")
        if not rel:
            raise HTTPException(status_code=404, detail="no artifact for this run")
        artifacts_root = (container.store.root / "artifacts").resolve()
        target = Path(rel)
        if not target.is_absolute():
            target = (container.base_dir / target)
        target = target.resolve()
        # Path-safety: only ever serve files under the artifacts directory.
        if not target.is_relative_to(artifacts_root):
            raise HTTPException(status_code=403, detail="artifact path escapes the artifacts dir")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="artifact file missing")
        return FileResponse(target)

    # ----- actions (mutate engine state; gated by allow_actions) ----------- #
    @app.post("/api/runs")
    def start(body: StartBody) -> dict[str, Any]:
        _require_actions()
        try:
            record = operations.create_run_for(
                container, loop=body.loop, project_id=body.project, issue=body.issue, pr=body.pr
            )
        except operations.NotOwned as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except operations.NoQueuedWork as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except KeyError as exc:  # unknown project id
            raise HTTPException(status_code=404, detail=f"unknown project {exc}") from None
        background(
            lambda: operations.execute_run(
                container, loop_name=body.loop, project_id=body.project, run_id=record.run_id
            )
        )
        invalidate_board()  # the deployed issue is leaving the queue — refetch next poll
        return {"run_id": record.run_id, "status": record.status.value}

    @app.post("/api/runs/{run_id}/answer")
    def answer(run_id: str, body: AnswerBody) -> dict[str, Any]:
        _require_actions()
        record = _load_or_404(run_id)
        if record.status is not RunStatus.WAITING or record.pending_request is None:
            raise HTTPException(status_code=409, detail=f"run {run_id} is {record.status.value}; nothing to answer")
        background(
            lambda: operations.answer_run(
                container, run_id=run_id, approved=body.approved, notes=body.notes
            )
        )
        return {"run_id": run_id, "accepted": True}

    @app.post("/api/runs/{run_id}/abort")
    def abort(run_id: str) -> dict[str, Any]:
        _require_actions()
        _load_or_404(run_id)
        status = operations.abort_run(container, run_id=run_id, reason="aborted via dashboard")
        return {"run_id": run_id, "status": status.value}

    @app.post("/api/scheduler/tick")
    def tick() -> dict[str, Any]:
        _require_actions()
        background(lambda: operations.tick_once(container))
        return {"accepted": True}

    # ----- static SPA (mounted last so /api/* routes win) ------------------ #
    app.mount("/", _NoCacheStaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app
