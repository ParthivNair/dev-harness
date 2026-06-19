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
from harness.application.ownership import owns
from harness.application.scheduler import TickReport
from harness.adapters.notifier.file import FileNotifier
from harness.adapters.registry.file_registry import FileProjectRegistry
from harness.config.loader import find_config, load_harness_config, resolve_under
from harness.container import (
    Container,
    build_container,
    build_scheduler,
)
from harness.domain.models import RunStatus

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
    note = "  (HALTED: spend ceiling reached)" if report.halted_for_spend else ""
    typer.echo(f"window spend: ${report.window_spend_usd:.2f}{note}")
    if not report.resumed and not report.started:
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

    every = interval or build_container(config).cfg.scheduling.tick_interval_seconds
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


if __name__ == "__main__":
    app()
