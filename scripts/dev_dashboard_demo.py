"""Dev-only: serve the observer dashboard against seeded in-memory fakes.

Lets us SEE the real rendered UI (issues, deploy, live runs, gates) without a
GitHub token or network. Not shipped behavior — a manual verification harness.

    uv run python scripts/dev_dashboard_demo.py            # normal seeded demo
    uv run python scripts/dev_dashboard_demo.py --board-error  # simulate GitHub down

Then open http://127.0.0.1:8799
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # make `tests` importable for make_container

from harness import operations  # noqa: E402
from harness.adapters.github.fake import InMemoryGitHub  # noqa: E402
from harness.application import coordination as co  # noqa: E402
from tests.fakes import make_container  # noqa: E402

REPO = "ParthivNair/dev-harness"


def seed(board_error: bool):  # type: ignore[no-untyped-def]
    gh = InMemoryGitHub()
    # A realistic mix of queued work + a few human-filed issues with no harness label.
    gh.create_issue(repo=REPO, title="Cache board reads behind an ETag", body="Avoid re-listing issues every poll; honor GitHub ETags.", labels=[co.QUEUED])
    gh.create_issue(repo=REPO, title="Add a stale-lease reconciler", body="Reclaim issues whose owning instance has gone dark for >24h.", labels=[co.QUEUED])
    gh.create_issue(repo=REPO, title="SQLite RunStore backend", body="Add a sqlite-backed store alongside the JSON one; same port.", labels=[co.QUEUED])
    gh.create_issue(repo=REPO, title="Per-project Discord gate channels", body="Route gates to a channel chosen per project.", labels=[co.QUEUED])
    gh.create_issue(repo=REPO, title="Typo in README quickstart", body="`uv sync --exta web` should be `--extra`.", labels=[])  # plain human issue
    gh.create_issue(repo=REPO, title="Flaky scheduler test on Windows", body="test_scheduler intermittently fails on win runners.", labels=["bug"])  # plain human issue

    tmp = Path(tempfile.mkdtemp(prefix="harness_dash_"))
    c = make_container(tmp, instance="WindowsDesktop", repo=REPO, project_id="dev-harness", github=gh)

    # Two live runs: one parked at a verification gate, one driven to completion.
    r1 = operations.create_run_for(c, loop="dev_task", project_id="dev-harness", issue=1)
    operations.execute_run(c, loop_name="dev_task", project_id="dev-harness", run_id=r1.run_id)  # -> WAITING (gate)
    r2 = operations.create_run_for(c, loop="dev_task", project_id="dev-harness", issue=2)
    operations.execute_run(c, loop_name="dev_task", project_id="dev-harness", run_id=r2.run_id)  # -> WAITING (gate)
    operations.answer_run(c, run_id=r2.run_id, approved=True, notes="looks good")  # -> COMPLETED (recent)

    if board_error:
        def boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
            raise RuntimeError("HARNESS_GITHUB_TOKEN is not set")

        gh.list_issues = boom  # type: ignore[method-assign]
    return c


def main() -> None:
    import uvicorn

    from harness.web.server import create_app

    board_error = "--board-error" in sys.argv
    c = seed(board_error)
    app = create_app(c, allow_actions=True, background_runner=lambda fn: fn())
    print("dashboard demo: http://127.0.0.1:8799  (Ctrl-C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=8799, log_level="warning")


if __name__ == "__main__":
    main()
