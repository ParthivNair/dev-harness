"""The overseer: stranded-lease reconcile, capped handoff, and the wave PR.

Pure orchestration over the in-memory fakes (the production-default wiring) plus a
real AtomicJsonRunStore, with an injected clock so a supervise() pass is fully
deterministic. No git/network: the wave PR path uses the EchoExecutor stub.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.executor.echo import EchoExecutor
from harness.adapters.github.fake import InMemoryGitHub
from harness.adapters.notifier.file import FileNotifier
from harness.adapters.state.json_store import AtomicJsonRunStore
from harness.application import coordination as co
from harness.application.overseer import Overseer
from harness.application.scheduler import SchedulerLedger, WaveState
from harness.config.models import (
    AutonomyTier,
    HarnessConfig,
    InstanceInfo,
    ProjectConfig,
    ProjectOverrides,
)
from harness.domain.models import RunRecord, RunStatus

INSTANCE = "this-machine"
REPO = "acme/app"


class _Registry:
    def __init__(self, projects: list[ProjectConfig]) -> None:
        self._p = {p.id: p for p in projects}

    def list_projects(self):  # type: ignore[no-untyped-def]
        return list(self._p.values())

    def get(self, project_id):  # type: ignore[no-untyped-def]
        from harness.ports.project_registry import ProjectNotFound

        try:
            return self._p[project_id]
        except KeyError:
            raise ProjectNotFound(project_id) from None

    def list_owned(self, instance_id):  # type: ignore[no-untyped-def]
        return [p for p in self._p.values() if p.owner_instance == instance_id]

    def reload(self):  # type: ignore[no-untyped-def]
        pass


def _project(pid: str = "app", repo: str = REPO) -> ProjectConfig:
    return ProjectConfig(id=pid, owner_instance=INSTANCE, repo=repo)


def _overseer(
    tmp_path: Path, projects: list[ProjectConfig], *, gh=None, executor=None, clock=None
):  # type: ignore[no-untyped-def]
    gh = gh or InMemoryGitHub()
    store = AtomicJsonRunStore(tmp_path / "state")
    ov = Overseer(
        cfg=HarnessConfig(instance=InstanceInfo(instance_id=INSTANCE)),
        store=store,
        github=gh,
        registry=_Registry(projects),
        executor=executor or EchoExecutor(),
        notifier=FileNotifier(tmp_path / "inbox"),
        clock=clock or (lambda: 1000.0),
    )
    return ov, gh, store


def _seed_run(
    store: AtomicJsonRunStore,
    *,
    repo: str = REPO,
    number: int,
    status: RunStatus,
    branch: str | None = None,
    created_at: str,
    loop_name: str = "dev_task",
    terminal_reason: str | None = None,
    claude_result: str | None = None,
) -> RunRecord:
    data: dict = {"repo": repo, "issue_number": number, "branch": branch or f"b-{number}"}
    if claude_result is not None:
        data["claude_result"] = claude_result
    rec = RunRecord(
        loop_name=loop_name,
        project_id="app",
        status=status,
        data=data,
        created_at=created_at,
        terminal_reason=terminal_reason,
    )
    store.create(rec)
    return rec


def _in_progress_issue(gh: InMemoryGitHub, *, owner: str = INSTANCE) -> int:
    n = gh.create_issue(repo=REPO, title="t", body="x", labels=[co.QUEUED]).number
    co.claim(gh, repo=REPO, number=n, instance_id=owner)
    return n


# --------------------------------------------------------------------------- #
# (a) reconcile stranded leases
# --------------------------------------------------------------------------- #
def test_reconcile_releases_stranded_lease_whose_run_is_terminal(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()])
    n = _in_progress_issue(gh)
    # A run for it that has ended terminally but the issue is still in-progress:
    rec = _seed_run(store, number=n, status=RunStatus.FAILED, created_at="2026-01-01T00:00:00+00:00")

    rep = ov.supervise(SchedulerLedger(), active_run_ids=set())
    assert rep.reconciled == [n]
    issue = gh.get_issue(repo=REPO, number=n)
    assert co.state_of(issue) == co.QUEUED      # back on the queue
    assert co.owner_of(issue) is None           # lease dropped
    _ = rec


def test_reconcile_skips_lease_with_an_active_run(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()])
    n = _in_progress_issue(gh)
    rec = _seed_run(store, number=n, status=RunStatus.RUNNING, created_at="2026-01-01T00:00:00+00:00")

    # The run id is in the active set -> the lease is genuinely being worked.
    rep = ov.supervise(SchedulerLedger(), active_run_ids={rec.run_id})
    assert rep.reconciled == []
    assert co.state_of(gh.get_issue(repo=REPO, number=n)) == co.IN_PROGRESS


def test_reconcile_ignores_issues_owned_by_another_instance(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()])
    n = _in_progress_issue(gh, owner="other-machine")
    _seed_run(store, number=n, status=RunStatus.FAILED, created_at="2026-01-01T00:00:00+00:00")

    rep = ov.supervise(SchedulerLedger(), active_run_ids=set())
    assert rep.reconciled == []
    assert co.owner_of(gh.get_issue(repo=REPO, number=n)) == "other-machine"


# --------------------------------------------------------------------------- #
# (b) handoff under / over the cap
# --------------------------------------------------------------------------- #
def test_handoff_requeues_aborted_run_under_cap(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()])
    n = _in_progress_issue(gh)
    rec = _seed_run(store, number=n, status=RunStatus.ABORTED, created_at="2026-01-01T00:00:00+00:00")
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[rec.run_id]))

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.handed_off == [(n, "aborted")]
    issue = gh.get_issue(repo=REPO, number=n)
    assert co.state_of(issue) == co.QUEUED          # requeued for a fresh run
    assert co.owner_of(issue) is None
    assert co.HANDOFF in issue.labels               # continuation marker stamped
    assert gh.comments[(REPO, n)]                    # a handoff packet was posted
    assert "## Prior attempt(s)" in gh.comments[(REPO, n)][0]


def test_handoff_over_cap_blocks_instead_of_requeue(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()])
    n = _in_progress_issue(gh)
    # CAP=2 prior terminal attempts already on record; the wave's failing run is the 3rd.
    _seed_run(store, number=n, status=RunStatus.FAILED, created_at="2026-01-01T00:00:01+00:00")
    _seed_run(store, number=n, status=RunStatus.ABORTED, created_at="2026-01-01T00:00:02+00:00")
    rec = _seed_run(store, number=n, status=RunStatus.FAILED, created_at="2026-01-01T00:00:03+00:00")
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[rec.run_id]))

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.handed_off == [(n, "blocked: handoff cap reached")]
    issue = gh.get_issue(repo=REPO, number=n)
    assert co.state_of(issue) == co.BLOCKED         # left for a human, NOT requeued
    assert co.HANDOFF not in issue.labels
    assert (REPO, n) not in gh.comments              # no fresh handoff packet


def test_handoff_at_cap_boundary_still_requeues(tmp_path: Path) -> None:
    # Exactly CAP (2) prior terminal attempts => still <= cap => one more handoff.
    ov, gh, store = _overseer(tmp_path, [_project()])
    n = _in_progress_issue(gh)
    _seed_run(store, number=n, status=RunStatus.FAILED, created_at="2026-01-01T00:00:01+00:00")
    rec = _seed_run(store, number=n, status=RunStatus.ABORTED, created_at="2026-01-01T00:00:02+00:00")
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[rec.run_id]))

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.handed_off == [(n, "aborted")]
    assert co.state_of(gh.get_issue(repo=REPO, number=n)) == co.QUEUED


def test_completed_run_is_not_handed_off(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()])
    n = _in_progress_issue(gh)
    rec = _seed_run(store, number=n, status=RunStatus.COMPLETED, created_at="2026-01-01T00:00:00+00:00")
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[rec.run_id]))

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.handed_off == []


# --------------------------------------------------------------------------- #
# (c) wave completion -> ONE aggregated draft PR, idempotent
# --------------------------------------------------------------------------- #
def test_wave_does_not_draft_while_a_run_is_non_terminal(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()])
    done = _seed_run(store, number=1, status=RunStatus.COMPLETED, branch="b1", created_at="2026-01-01T00:00:01+00:00")
    live = _seed_run(store, number=2, status=RunStatus.RUNNING, branch="b2", created_at="2026-01-01T00:00:02+00:00")
    ledger = SchedulerLedger(
        current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[done.run_id, live.run_id])
    )

    rep = ov.supervise(ledger, active_run_ids={live.run_id})
    assert rep.wave_pr is None
    assert ledger.current_wave.status == "open"
    assert gh.list_pulls(repo=REPO) == []


def test_wave_does_not_draft_while_queue_has_claimable_work(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()])
    done = _seed_run(store, number=1, status=RunStatus.COMPLETED, branch="b1", created_at="2026-01-01T00:00:01+00:00")
    # All runs terminal, but a fresh queued issue means the backlog is NOT drained.
    gh.create_issue(repo=REPO, title="more", body="x", labels=[co.QUEUED])
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[done.run_id]))

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.wave_pr is None
    assert ledger.current_wave.status == "open"


def test_wave_drafts_one_pr_when_drained_and_all_terminal(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()])
    # An open issue the completed run resolved (so find_claimable is None — not queued).
    n1 = gh.create_issue(repo=REPO, title="one", body="x", labels=[co.PR_OPEN]).number
    a = _seed_run(store, number=n1, status=RunStatus.COMPLETED, branch="harness/win/issue-1", created_at="2026-01-01T00:00:01+00:00")
    b = _seed_run(store, number=2, status=RunStatus.COMPLETED, branch="harness/win/issue-2", created_at="2026-01-01T00:00:02+00:00")
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="wave1234", opened_at=1000.0, run_ids=[a.run_id, b.run_id]))

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.wave_pr is not None
    pulls = gh.list_pulls(repo=REPO)
    assert len(pulls) == 1 and pulls[0].draft is True   # one aggregated draft PR
    # After a successful draft the wave is RECYCLED (A1): current_wave drops back to
    # None so the next backlog drain opens a fresh wave instead of re-joining a dead,
    # already-drafted one. (The drafted url is what the report carries.)
    assert ledger.current_wave is None


def test_wave_pr_is_idempotent_drafts_only_once(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()])
    gh.create_issue(repo=REPO, title="one", body="x", labels=[co.PR_OPEN])
    a = _seed_run(store, number=1, status=RunStatus.COMPLETED, branch="harness/win/issue-1", created_at="2026-01-01T00:00:01+00:00")
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="wave1234", opened_at=1000.0, run_ids=[a.run_id]))

    first = ov.supervise(ledger, active_run_ids=set())
    second = ov.supervise(ledger, active_run_ids=set())
    assert first.wave_pr is not None
    # The first draft recycled the wave to None (A1); the second pass has no open wave
    # to draft, so it is a no-op and exactly one PR ever exists.
    assert second.wave_pr is None
    assert len(gh.list_pulls(repo=REPO)) == 1            # exactly one PR ever


def test_wave_with_no_completed_runs_closes_without_pr(tmp_path: Path) -> None:
    # All runs ended terminal but none COMPLETED (all aborted/failed) -> no branches
    # to aggregate -> close the wave so the next start opens a fresh one.
    ov, gh, store = _overseer(tmp_path, [_project()])
    a = _seed_run(store, number=1, status=RunStatus.ABORTED, branch="b1", created_at="2026-01-01T00:00:01+00:00")
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[a.run_id]))
    # number 1 is not open/queued, so the backlog is drained.

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.wave_pr is None
    assert ledger.current_wave is None                   # wave closed
    assert gh.list_pulls(repo=REPO) == []


# --------------------------------------------------------------------------- #
# wave bookkeeping helpers
# --------------------------------------------------------------------------- #
def test_current_or_new_wave_opens_then_reuses(tmp_path: Path) -> None:
    ov, _, _ = _overseer(tmp_path, [_project()], clock=lambda: 4242.0)
    ledger = SchedulerLedger()
    wid = ov.current_or_new_wave(ledger)
    assert ledger.current_wave is not None
    assert ledger.current_wave.opened_at == 4242.0
    assert ov.current_or_new_wave(ledger) == wid          # same wave reused


def test_register_run_adds_once(tmp_path: Path) -> None:
    ov, _, _ = _overseer(tmp_path, [_project()])
    ledger = SchedulerLedger()
    wid = ov.current_or_new_wave(ledger)
    ov.register_run(ledger, wid, "r1")
    ov.register_run(ledger, wid, "r1")                    # idempotent
    ov.register_run(ledger, "stale-wave", "r2")           # wrong wave id ignored
    assert ledger.current_wave.run_ids == ["r1"]


# --------------------------------------------------------------------------- #
# A1: wave recycle — a SECOND wave drafts after the first already did.
# --------------------------------------------------------------------------- #
def test_a1_a_new_issue_after_a_drafted_wave_drafts_a_second_wave_pr(tmp_path: Path) -> None:
    # The wave-recycle bug: after the first wave drafts, current_wave was left set, so
    # the next drain re-joined a dead wave and NO second wave PR ever drafted. With the
    # fix the first draft recycles current_wave to None, so a fresh issue's completed
    # run opens + drafts a brand-new wave.
    ov, gh, store = _overseer(tmp_path, [_project()])
    # Wave 1: one completed run, queue drained -> drafts a PR and recycles.
    gh.create_issue(repo=REPO, title="one", body="x", labels=[co.PR_OPEN])
    a = _seed_run(store, number=1, status=RunStatus.COMPLETED, branch="harness/win/issue-1", created_at="2026-01-01T00:00:01+00:00")
    wid1 = ov.current_or_new_wave(SchedulerLedger())  # just to mint an id shape
    ledger = SchedulerLedger(current_wave=WaveState(wave_id=wid1, opened_at=1000.0, run_ids=[a.run_id]))
    first = ov.supervise(ledger, active_run_ids=set())
    assert first.wave_pr is not None
    assert ledger.current_wave is None                    # recycled

    # A NEW issue is worked and completes; the scheduler would open a fresh wave for it.
    gh.create_issue(repo=REPO, title="two", body="x", labels=[co.PR_OPEN])
    b = _seed_run(store, number=2, status=RunStatus.COMPLETED, branch="harness/win/issue-2", created_at="2026-01-02T00:00:01+00:00")
    wid2 = ov.current_or_new_wave(ledger)                 # opens a brand-new wave
    assert wid2 != wid1
    ov.register_run(ledger, wid2, b.run_id)

    second = ov.supervise(ledger, active_run_ids=set())
    assert second.wave_pr is not None and second.wave_pr != first.wave_pr
    assert len(gh.list_pulls(repo=REPO)) == 2             # a SECOND wave PR drafted


# --------------------------------------------------------------------------- #
# A2: handoff packet is built from the LATEST terminal run, not the oldest.
# --------------------------------------------------------------------------- #
def test_a2_handoff_packet_reflects_the_latest_terminal_run(tmp_path: Path) -> None:
    # Two terminal runs for one issue (handoff -> re-claim -> re-abort): run_ids carry
    # both, oldest first. The packet must come from the NEWER record, not the stale R1.
    ov, gh, store = _overseer(tmp_path, [_project()])
    n = _in_progress_issue(gh)
    r1 = _seed_run(
        store, number=n, status=RunStatus.FAILED, created_at="2026-01-01T00:00:01+00:00",
        terminal_reason="stale-old-reason", claude_result="OLD attempt notes",
    )
    r2 = _seed_run(
        store, number=n, status=RunStatus.ABORTED, created_at="2026-01-01T00:00:09+00:00",
        terminal_reason="fresh-new-reason", claude_result="NEW attempt notes",
    )
    # Both runs are in the wave, oldest-first (the bug acted on r1).
    ledger = SchedulerLedger(
        current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[r1.run_id, r2.run_id])
    )

    rep = ov.supervise(ledger, active_run_ids=set())
    # The reason in the report is the LATEST run's status (aborted), not r1's (failed).
    assert rep.handed_off == [(n, "aborted")]
    packet = gh.comments[(REPO, n)][0]
    assert "fresh-new-reason" in packet and "NEW attempt notes" in packet
    assert "stale-old-reason" not in packet and "OLD attempt notes" not in packet


# --------------------------------------------------------------------------- #
# A5: empty wave (every branch skipped) drafts NO PR; a GitHub error never crashes.
# --------------------------------------------------------------------------- #
class _AllSkippedExecutor(EchoExecutor):
    """Assembly where every source branch conflicts -> included is empty."""

    def assemble_wave_branch(self, *, project, wave_branch, source_branches, base="origin/main"):  # type: ignore[no-untyped-def]
        from harness.ports.executor import WaveAssembly

        return WaveAssembly(branch=wave_branch, head_sha="x", included=[], skipped=list(source_branches))


def test_a5_empty_assembly_drafts_no_pr_and_recycles(tmp_path: Path) -> None:
    ov, gh, store = _overseer(tmp_path, [_project()], executor=_AllSkippedExecutor())
    gh.create_issue(repo=REPO, title="one", body="x", labels=[co.PR_OPEN])
    a = _seed_run(store, number=1, status=RunStatus.COMPLETED, branch="harness/win/issue-1", created_at="2026-01-01T00:00:01+00:00")
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[a.run_id]))

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.wave_pr is None                            # no 0-change PR opened
    assert gh.list_pulls(repo=REPO) == []
    assert ledger.current_wave is None                    # wave still recycled
    notifier = ov.notifier
    assert any("every branch skipped" in w for w in notifier.warnings)  # warned


class _PRRaisesGitHub(InMemoryGitHub):
    """A GitHub fake whose draft-PR open blows up like a real API error would."""

    def open_draft_pr(self, *, repo, head, base, title, body):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated GitHub 422: no commits between main and head")


def test_a5_github_error_during_drafting_does_not_crash_supervise(tmp_path: Path) -> None:
    gh = _PRRaisesGitHub()
    ov, gh, store = _overseer(tmp_path, [_project()], gh=gh)
    gh.create_issue(repo=REPO, title="one", body="x", labels=[co.PR_OPEN])
    a = _seed_run(store, number=1, status=RunStatus.COMPLETED, branch="harness/win/issue-1", created_at="2026-01-01T00:00:01+00:00")
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[a.run_id]))

    # The GitHub error is caught + warned, NOT raised — supervise() returns normally.
    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.wave_pr is None
    assert ledger.current_wave is None                    # still recycled despite the error
    assert any("wave PR drafting failed" in w for w in ov.notifier.warnings)


# --------------------------------------------------------------------------- #
# A7: multi-repo wave -> ONE wave PR per repo.
# --------------------------------------------------------------------------- #
def test_a7_multi_repo_wave_drafts_one_pr_per_repo(tmp_path: Path) -> None:
    repo_a, repo_b = "acme/app-a", "acme/app-b"
    projects = [_project("a", repo=repo_a), _project("b", repo=repo_b)]
    ov, gh, store = _overseer(tmp_path, projects)
    # A completed run in EACH repo, both with the backlog drained (PR_OPEN, not queued).
    gh.create_issue(repo=repo_a, title="a", body="x", labels=[co.PR_OPEN])
    gh.create_issue(repo=repo_b, title="b", body="x", labels=[co.PR_OPEN])
    ra = _seed_run(store, repo=repo_a, number=1, status=RunStatus.COMPLETED, branch="harness/win/issue-1", created_at="2026-01-01T00:00:01+00:00")
    rb = _seed_run(store, repo=repo_b, number=2, status=RunStatus.COMPLETED, branch="harness/win/issue-2", created_at="2026-01-01T00:00:02+00:00")
    ledger = SchedulerLedger(current_wave=WaveState(wave_id="w1", opened_at=1000.0, run_ids=[ra.run_id, rb.run_id]))

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.wave_pr is not None                        # report carries the first url
    assert len(gh.list_pulls(repo=repo_a)) == 1           # one PR per repo, not on completed[0] only
    assert len(gh.list_pulls(repo=repo_b)) == 1
    assert ledger.current_wave is None


# --------------------------------------------------------------------------- #
# B3: wave PR marked ready vs left draft, per the project's mark_pr_ready tier.
# --------------------------------------------------------------------------- #
def _autonomous_mark_ready_project(pid: str = "app", repo: str = REPO) -> ProjectConfig:
    """A self-managed project that opts mark_pr_ready into autonomous so its wave PR
    is promoted draft -> ready (the closed-loop, agent-reviewed path)."""
    return ProjectConfig(
        id=pid,
        owner_instance=INSTANCE,
        repo=repo,
        overrides=ProjectOverrides(autonomy={"mark_pr_ready": AutonomyTier.AUTONOMOUS}),
    )


def _drained_completed_wave(store: AtomicJsonRunStore, gh: InMemoryGitHub) -> SchedulerLedger:
    """One completed run whose issue is PR_OPEN (queue drained) -> the wave is ready
    to draft on the next supervise()."""
    gh.create_issue(repo=REPO, title="one", body="x", labels=[co.PR_OPEN])
    a = _seed_run(store, number=1, status=RunStatus.COMPLETED, branch="harness/win/issue-1", created_at="2026-01-01T00:00:01+00:00")
    return SchedulerLedger(current_wave=WaveState(wave_id="wave1234", opened_at=1000.0, run_ids=[a.run_id]))


def test_b3_wave_pr_is_marked_ready_when_project_admits_mark_pr_ready(tmp_path: Path) -> None:
    # The overseer still OPENS a draft (invariant preserved), then promotes it to ready
    # because this project's mark_pr_ready tier is autonomous.
    ov, gh, store = _overseer(tmp_path, [_autonomous_mark_ready_project()])
    ledger = _drained_completed_wave(store, gh)

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.wave_pr is not None
    pulls = gh.list_pulls(repo=REPO)
    assert len(pulls) == 1
    assert pulls[0].draft is False                        # promoted draft -> ready
    # The comment reflects the ready promotion.
    assert any("ready for review" in c for c in gh.comments[(REPO, 1)])


def test_b3_wave_pr_stays_draft_when_mark_pr_ready_is_gated(tmp_path: Path) -> None:
    # The safe default: mark_pr_ready not autonomous (empty taxonomy => gated) leaves
    # the aggregated wave PR a DRAFT for a human to promote/merge. Never merged.
    ov, gh, store = _overseer(tmp_path, [_project()])  # no autonomy override
    ledger = _drained_completed_wave(store, gh)

    rep = ov.supervise(ledger, active_run_ids=set())
    assert rep.wave_pr is not None
    pulls = gh.list_pulls(repo=REPO)
    assert len(pulls) == 1
    assert pulls[0].draft is True                         # left a draft (gated)
    assert any("drafted" in c for c in gh.comments[(REPO, 1)])
