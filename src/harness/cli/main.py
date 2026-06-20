"""The CLI surface — a *driving* adapter over the composition root.

Every command builds a :class:`~harness.container.Container` (the one place
adapters are chosen) and then delegates to the shared use-cases in
:mod:`harness.operations`, so the CLI and the web dashboard share one engine path.
The CLI's job here is argument parsing and human-readable echo, nothing more.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import Optional

import typer

from harness import operations
from harness.application import coordination as co
from harness.application import onboarding, preflight, schedule_install
from harness.application.ownership import owns
from harness.application.scheduler import TickReport
from harness.adapters.notifier.file import FileNotifier
from harness.adapters.registry.file_registry import FileProjectRegistry
from harness.config.loader import find_config, load_harness_config, resolve_under
from harness.config.models import ProjectPointer
from harness.container import (
    Container,
    build_container,
    build_scheduler,
)
from harness.domain.models import RunStatus
from harness.ports.project_registry import ProjectNotFound

# The coordination state machine + severity labels a fresh target repo needs so the
# harness:* / sev:* labels exist before the first run. Mirrors the set the loops emit.
DEFAULT_LABELS = [
    co.QUEUED,
    co.IN_PROGRESS,
    co.NEEDS_VERIFICATION,
    co.PR_OPEN,
    co.BLOCKED,
    co.DONE,
    co.CHANGES_REQUESTED,
    co.HANDOFF,
    "sev:high",
    "sev:med",
    "sev:low",
]

app = typer.Typer(no_args_is_help=True, help="dev-harness — orchestrate AI-assisted development.")

CONFIG_OPT = typer.Option(None, "--config", "-c", help="Path to harness.toml (else auto-discovered).")


# --------------------------------------------------------------------------- #
# Echo helpers (CLI presentation only)
# --------------------------------------------------------------------------- #
def _echo_tick(report: TickReport) -> None:
    for rid, status in report.resumed:
        typer.echo(f"resumed {rid} -> {status}")
    for pid, rid, status in report.started:
        typer.echo(f"started {pid} {rid} -> {status}")
    for number in report.reconciled:
        typer.echo(f"reconciled #{number} (stranded lease requeued)")
    for number, reason in report.handed_off:
        typer.echo(f"handed off #{number} ({reason})")
    if report.wave_pr:
        typer.echo(f"wave PR: {report.wave_pr}")
    note = "  (HALTED: spend ceiling reached)" if report.halted_for_spend else ""
    typer.echo(f"window spend: ${report.window_spend_usd:.2f}{note}")
    if not report.resumed and not report.started and not report.reconciled \
            and not report.handed_off and not report.wave_pr:
        typer.echo("nothing to do")


def _report(c: Container, run_id: str, status: RunStatus) -> None:
    record = c.store.load(run_id)
    typer.echo(f"status: {status.value}")
    if status is RunStatus.WAITING and record.pending_request is not None:
        req = record.pending_request
        typer.echo("")
        typer.echo("  GATE - perceive and report:")
        typer.echo(f"    {req.prompt}")
        if req.artifact_path:
            typer.echo(f"    artifact: {req.artifact_path}")
        typer.echo("")
        typer.echo(f"    answer with:  harness answer {run_id} --approve")
        typer.echo(f"             or:  harness answer {run_id} --reject --notes \"...\"")
    elif status is RunStatus.ABORTED:
        typer.echo(f"  aborted: {record.terminal_reason}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@app.command()
def projects(config: Optional[Path] = CONFIG_OPT) -> None:
    """List registered projects and whether THIS install owns each."""
    c = build_container(config)
    items = c.registry.list_projects()
    if not items:
        typer.echo("no registered projects")
        return
    typer.echo(f"instance: {c.cfg.instance.instance_id}")
    for p in items:
        mark = "OWNED" if owns(p, c.cfg.instance) else "read-only"
        typer.echo(f"  {p.id:14} owner={p.owner_instance:14} [{mark}]  {p.repo}")


@app.command()
def run(
    loop: str = typer.Argument("demo", help="Loop: 'demo' | 'dev_task' | 'arch_review' | 'pr_review'."),
    project: str = typer.Argument(..., help="Registered project id."),
    config: Optional[Path] = CONFIG_OPT,
    notifier: Optional[str] = typer.Option(None, help="Override notifier: 'file' or 'console'."),
    issue: Optional[int] = typer.Option(
        None, "--issue", help="dev_task: issue number to work (else claim the next queued)."
    ),
    pr: Optional[int] = typer.Option(
        None, "--pr", help="pr_review: PR number to review/merge (else auto-select the next harness PR)."
    ),
) -> None:
    """Create and start a run of LOOP for PROJECT."""
    c = build_container(config, notifier_override=notifier)
    try:
        record = operations.create_run_for(c, loop=loop, project_id=project, issue=issue, pr=pr)
    except operations.NotOwned as exc:
        typer.echo(f"refusing: {exc}. Reads are fine; acting is not.", err=True)
        raise typer.Exit(1)
    except operations.NoQueuedWork as exc:
        typer.echo(str(exc))
        return

    if record.data.get("issue_number") is not None:
        typer.echo(f"working issue #{record.data['issue_number']} on {record.data.get('repo')}")
    if record.data.get("pr_number") is not None:
        typer.echo(f"reviewing PR #{record.data['pr_number']} on {record.loop_name}")
    typer.echo(f"created run {record.run_id}")
    status = operations.execute_run(c, loop_name=loop, project_id=project, run_id=record.run_id)
    _report(c, record.run_id, status)


@app.command()
def answer(
    run_id: str = typer.Argument(..., help="The waiting run's id."),
    approve: bool = typer.Option(..., "--approve/--reject", help="Approve or reject the gate."),
    notes: str = typer.Option("", "--notes", help="Optional free-text notes."),
    config: Optional[Path] = CONFIG_OPT,
) -> None:
    """Deliver a verification answer and resume the run."""
    c = build_container(config)
    try:
        status = operations.answer_run(c, run_id=run_id, approved=approve, notes=notes)
    except operations.InvalidAnswer as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    _report(c, run_id, status)


@app.command()
def poll(config: Optional[Path] = CONFIG_OPT) -> None:
    """Resume any WAITING run whose answer has arrived (the scheduler's resume phase)."""
    c = build_container(config)
    if not isinstance(c.notifier, FileNotifier):
        typer.echo("poll requires the file notifier", err=True)
        raise typer.Exit(1)
    resumed = build_scheduler(c).resume_waiting()
    if not resumed:
        typer.echo("no runs resumed")
        return
    for rid, status in resumed:
        typer.echo(f"{rid}: resumed -> {status}")


@app.command()
def tick(config: Optional[Path] = CONFIG_OPT) -> None:
    """Run ONE scheduling pass: resume answered gates, then start eligible work.

    This is the unit an OS scheduler (Task Scheduler / launchd) invokes on a cadence.
    Idempotent and short-lived — runs that hit a gate persist WAITING and the pass returns.
    """
    c = build_container(config)
    _echo_tick(operations.tick_once(c))


@app.command()
def watch(
    config: Optional[Path] = CONFIG_OPT,
    interval: Optional[int] = typer.Option(
        None, "--interval", help="Seconds between ticks (default: scheduling.tick_interval_seconds)."
    ),
) -> None:
    """Run tick() forever on an interval (Ctrl-C to stop) — for a machine without an OS scheduler."""
    import time

    c = build_container(config)
    try:
        operations.ensure_scheduling_enabled(c)
    except operations.SchedulingDisabled as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    every = interval or c.cfg.scheduling.tick_interval_seconds
    typer.echo(f"watching every {every}s (Ctrl-C to stop)")
    while True:
        _echo_tick(operations.tick_once(build_container(config)))
        time.sleep(every)


@app.command()
def ui(
    config: Optional[Path] = CONFIG_OPT,
    host: Optional[str] = typer.Option(None, help="Bind host (default: [ui].host or 127.0.0.1)."),
    port: Optional[int] = typer.Option(None, help="Bind port (default: [ui].port or 8765)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not auto-open a browser."),
) -> None:
    """Serve the local observer dashboard: a minimized overview that expands to run detail.

    Reads the durable run state and (unless [ui].allow_actions is false) can answer
    gates and start/abort runs. Binds 127.0.0.1 only. Needs the 'web' extra
    (`uv sync --extra web`).
    """
    c = build_container(config)
    try:
        import uvicorn

        from harness.web.server import create_app
    except ImportError:
        typer.echo(
            "the dashboard needs the 'web' extra: uv sync --extra web", err=True
        )
        raise typer.Exit(1)

    ui_cfg = c.cfg.ui
    bind_host = host or ui_cfg.host
    bind_port = port or ui_cfg.port
    web_app = create_app(
        c,
        allow_actions=ui_cfg.allow_actions,
        poll_interval_ms=ui_cfg.poll_interval_ms,
        board_ttl_seconds=ui_cfg.board_ttl_seconds,
    )
    url = f"http://{bind_host}:{bind_port}"
    typer.echo(f"dashboard: {url}   (Ctrl-C to stop)")
    if ui_cfg.open_browser and not no_browser:
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()
    uvicorn.run(web_app, host=bind_host, port=bind_port, log_level="warning")


@app.command(name="discord-bot")
def discord_bot(config: Optional[Path] = CONFIG_OPT) -> None:
    """Run the always-on Discord bridge bot (a separate, long-lived process).

    Listens for Approve/Reject clicks on gate messages and writes the answer back
    into the inbox, then resumes the run. Requires DISCORD_BOT_TOKEN and the
    `discord` extra (`uv sync --extra discord`).
    """
    c = build_container(config)
    if not c.cfg.discord.token:
        typer.echo("DISCORD_BOT_TOKEN is not set; cannot start the bot", err=True)
        raise typer.Exit(1)
    from harness.bots.discord_bot import run_bot

    inbox = resolve_under(c.base_dir, c.cfg.notifier.inbox)

    def on_answer() -> None:
        build_scheduler(build_container(config)).resume_waiting()

    run_bot(token=c.cfg.discord.token, inbox=inbox, on_answer=on_answer)


@app.command(name="list-runs")
def list_runs(config: Optional[Path] = CONFIG_OPT) -> None:
    """List persisted runs with status, iteration, and accumulated spend."""
    c = build_container(config)
    runs = c.store.list()
    if not runs:
        typer.echo("no runs")
        return
    for r in runs:
        typer.echo(
            f"{r.run_id}  {r.loop_name:8} {r.status.value:10} "
            f"iter={r.breakers.loop_count} cost=${r.breakers.cumulative_cost_usd:.2f} "
            f"step={r.current_step}"
        )


@app.command()
def show(run_id: str = typer.Argument(...), config: Optional[Path] = CONFIG_OPT) -> None:
    """Dump a run's full persisted state (including the pending gate)."""
    c = build_container(config)
    typer.echo(c.store.load(run_id).model_dump_json(indent=2))


@app.command(name="config-check")
def config_check(config: Optional[Path] = CONFIG_OPT) -> None:
    """Validate the instance config and every registered project config."""
    path = Path(config) if config else find_config()
    cfg = load_harness_config(path)
    typer.echo(f"config: {path}")
    typer.echo(f"instance: {cfg.instance.instance_id} ({cfg.instance.platform})")
    typer.echo(
        f"github: repo={cfg.github.repo} fake={cfg.github.use_in_memory_fake} "
        f"token={'set' if cfg.github.token else 'unset'}"
    )
    typer.echo(
        f"scheduling: enabled={cfg.scheduling.enabled} "
        f"max_concurrent={cfg.scheduling.max_concurrent_runs} "
        f"global_ceiling=${cfg.scheduling.global_spend_ceiling_usd:.2f}/{cfg.scheduling.spend_window}"
    )
    typer.echo(
        f"discord: enabled={cfg.discord.enabled} "
        f"gates_channel={'set' if cfg.discord.gates_channel_id else 'unset'} "
        f"token={'set' if cfg.discord.token else 'unset'}"
    )
    typer.echo(
        f"ui: enabled={cfg.ui.enabled} bind={cfg.ui.host}:{cfg.ui.port} "
        f"allow_actions={cfg.ui.allow_actions}"
    )
    registry = FileProjectRegistry(cfg.projects, path.parent)
    for p in registry.list_projects():
        sc = p.scheduling
        typer.echo(
            f"  project {p.id}: owner={p.owner_instance} owned={owns(p, cfg.instance)} "
            f"repo={p.repo} priority={sc.priority} weight={sc.effective_weight():.2f} "
            f"min_interval={sc.min_poll_interval_seconds}s"
        )
    typer.echo("OK")


@app.command()
def research(
    project: str = typer.Argument(..., help="Registered project id to research."),
    goals: Optional[Path] = typer.Option(
        None, "--goals", help="Path to a goals/context file (else [project].goals)."
    ),
    config: Optional[Path] = CONFIG_OPT,
) -> None:
    """Research PROJECT against owner GOALS and file a prioritized backlog of
    harness:queued issues (one focused, testable change each). Files issues only —
    never opens a PR or touches code."""
    c = build_container(config)
    goals_text = goals.read_text("utf-8") if goals is not None else None
    try:
        record, status = operations.start_research(c, project_id=project, goals=goals_text)
    except ProjectNotFound:
        typer.echo(f"no such project '{project}' — run `harness add-project` first", err=True)
        raise typer.Exit(1)
    except operations.NotOwned as exc:
        typer.echo(f"refusing: {exc}. Reads are fine; acting is not.", err=True)
        raise typer.Exit(1)
    typer.echo(f"created research run {record.run_id}")
    rec = c.store.load(record.run_id)
    filed = rec.data.get("filed") or []
    skipped = rec.data.get("skipped") or []
    if filed:
        typer.echo(f"filed {len(filed)} issue(s): {', '.join('#' + str(n) for n in filed)}")
    if skipped:
        typer.echo(f"skipped {len(skipped)} duplicate(s) already in the queue")
    if not filed and not skipped:
        typer.echo("no findings — nothing to file")
    _report(c, record.run_id, status)


@app.command(name="add-project")
def add_project(
    repo_dir: Path = typer.Argument(..., help="Local path to the repo's working copy."),
    repo: str = typer.Option(..., "--repo", help="GitHub 'owner/name' coordination repo."),
    id: Optional[str] = typer.Option(
        None, "--id", help="Project id (default: the repo directory name)."
    ),
    autonomous: bool = typer.Option(
        False, "--autonomous", help="Opt this repo's publishing actions into autonomous (self-managed)."
    ),
    config: Optional[Path] = CONFIG_OPT,
) -> None:
    """Onboard an arbitrary repo: detect its build/test commands, scaffold its
    harness.project.toml, and register a pointer in harness.toml — no hand-editing."""
    cfg_path = Path(config) if config else find_config()
    c = build_container(config)
    repo_abs = Path(repo_dir).resolve()
    project_id = id or repo_abs.name or repo.split("/")[-1]
    # The pointer path is stored relative to harness.toml's dir when possible.
    try:
        rel = repo_abs.relative_to(cfg_path.parent.resolve())
        ptr_path = rel.as_posix()
    except ValueError:
        ptr_path = repo_abs.as_posix()
    config_obj, toml_text = onboarding.scaffold_project(
        project_id=project_id,
        repo=repo,
        owner_instance=c.cfg.instance.instance_id,
        path=ptr_path,
        repo_dir=repo_abs,
        autonomous=autonomous,
    )
    c.registry.add_project(
        pointer=ProjectPointer(id=project_id, path=ptr_path),
        project_config_toml=toml_text,
    )
    typer.echo(f"registered project '{project_id}' -> {repo} (path: {ptr_path})")
    typer.echo(
        f"  build={config_obj.commands.build or '[]'} test={config_obj.commands.test or '[]'}"
    )
    if autonomous:
        typer.echo("  self-managed: verify_gate/mark_pr_ready/merge_to_main -> autonomous")
    typer.echo("  next: `harness labels-init " + project_id + "` then `harness research " + project_id + "`")


@app.command(name="labels-init")
def labels_init(
    project: str = typer.Argument(..., help="Registered project id."),
    config: Optional[Path] = CONFIG_OPT,
) -> None:
    """Provision the harness:* state/owner + sev:* labels on PROJECT's repo so the
    coordination state machine is well-formed before the first run."""
    c = build_container(config)
    try:
        proj = c.registry.get(project)
    except ProjectNotFound:
        typer.echo(f"no such project '{project}' — run `harness add-project` first", err=True)
        raise typer.Exit(1)
    created = c.github.ensure_labels(repo=proj.repo, labels=DEFAULT_LABELS)
    if created:
        typer.echo(f"created {len(created)} label(s) on {proj.repo}: {', '.join(created)}")
    else:
        typer.echo(f"all {len(DEFAULT_LABELS)} labels already present on {proj.repo}")


@app.command()
def doctor(
    config: Optional[Path] = CONFIG_OPT,
    probe_auth: bool = typer.Option(
        False, "--probe-auth",
        help="Also verify the claude CLI is signed in (makes one tiny token-spending call).",
    ),
) -> None:
    """Preflight: is this install actually ready to spend money? Checks the real
    executor/GitHub adapter, a present+authenticating token, and the claude CLI.

    Note: by default the claude check confirms the CLI is INSTALLED, not signed in
    (`claude --version` exits 0 when signed out). Pass --probe-auth to verify login.
    """
    c = build_container(config)
    report = preflight.run_doctor(c, probe_auth=probe_auth)
    typer.echo(f"executor real:      {report.executor_real}")
    typer.echo(f"github real:        {report.github_real}")
    typer.echo(f"github token:       {'present' if report.github_token_present else 'missing'}")
    typer.echo(f"github whoami:      {report.github_whoami or '-'}")
    typer.echo(f"claude installed:   {'yes' if report.claude_ok else 'NO'} ({report.claude_detail})")
    if report.claude_authenticated is None:
        typer.echo("claude login:       not checked (pass --probe-auth; the first real run also confirms it)")
    else:
        typer.echo(
            f"claude login:       {'signed in' if report.claude_authenticated else 'NOT signed in'}"
            f" ({report.claude_auth_detail})"
        )
    if report.issues:
        typer.echo("")
        typer.echo("issues:")
        for issue in report.issues:
            typer.echo(f"  - {issue}")
    typer.echo("")
    typer.echo("READY" if report.ok else "NOT READY")
    if not report.ok:
        raise typer.Exit(1)


@app.command(name="install-schedule")
def install_schedule_cmd(
    interval: Optional[int] = typer.Option(
        None, "--interval", help="Seconds between ticks (default: scheduling.tick_interval_seconds)."
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Actually install the OS schedule (default: dry run)."
    ),
    config: Optional[Path] = CONFIG_OPT,
) -> None:
    """Register `harness tick` with the OS scheduler (Task Scheduler / launchd /
    cron) so the harness runs unattended. Dry run by default — pass --apply to install."""
    cfg_path = Path(config) if config else find_config()
    c = build_container(config)
    result = schedule_install.install_schedule(
        interval_seconds=interval or c.cfg.scheduling.tick_interval_seconds,
        harness_cmd="harness tick",
        working_dir=str(cfg_path.parent.resolve()),
        dry_run=not apply,
    )
    typer.echo(f"platform: {result.platform}  task: {result.task_name}")
    typer.echo("")
    typer.echo("schedule:")
    typer.echo(result.schedule_text)
    typer.echo("")
    typer.echo(f"command: {result.command}")
    for line in result.instructions:
        typer.echo(f"  {line}")
    typer.echo("")
    typer.echo(f"applied: {result.applied}")


if __name__ == "__main__":
    app()
