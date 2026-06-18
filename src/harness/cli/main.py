"""The CLI surface AND the composition root.

This is the one place adapters are chosen and wired into the engine. Every
command builds a :class:`Container`, then hands ports (never concrete classes)
to the :class:`LoopRunner`. Swapping fake<->real GitHub or echo<->subprocess
executor is config-only; the engine code never changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer

from harness.application import coordination as co
from harness.application.action_guard import ActionGuard
from harness.application.loop_runner import LoopDefinition, LoopRunner
from harness.application.ownership import owns
from harness.application.scheduler import Scheduler, TickReport
from harness.adapters.executor.echo import EchoExecutor
from harness.adapters.executor.subprocess_executor import SubprocessExecutor
from harness.adapters.github.fake import InMemoryGitHub
from harness.adapters.github.pygithub_adapter import PyGithubAdapter
from harness.adapters.notifier.console import ConsoleNotifier
from harness.adapters.notifier.file import FileNotifier
from harness.adapters.registry.file_registry import FileProjectRegistry
from harness.adapters.state.json_store import AtomicJsonRunStore
from harness.config.loader import find_config, load_harness_config, resolve_under
from harness.config.models import HarnessConfig, ProjectConfig
from harness.domain.models import BreakerState, RunStatus, VerificationResponse
from harness.loops.arch_review import build_arch_review_loop
from harness.loops.demo import build_demo_loop
from harness.loops.dev_task import build_dev_loop
from harness.ports.executor import Executor
from harness.ports.github import GitHubAdapter
from harness.ports.notifier import Notifier

app = typer.Typer(no_args_is_help=True, help="dev-harness — orchestrate AI-assisted development.")

CONFIG_OPT = typer.Option(None, "--config", "-c", help="Path to harness.toml (else auto-discovered).")


@dataclass
class Container:
    cfg: HarnessConfig
    base_dir: Path
    registry: FileProjectRegistry
    store: AtomicJsonRunStore
    notifier: Notifier
    executor: Executor
    github: GitHubAdapter
    guard: ActionGuard


def build_container(
    config_path: Optional[Path] = None, *, notifier_override: Optional[str] = None
) -> Container:
    path = Path(config_path) if config_path else find_config()
    cfg = load_harness_config(path)
    base_dir = path.parent

    registry = FileProjectRegistry(cfg.projects, base_dir)
    store = AtomicJsonRunStore(resolve_under(base_dir, cfg.state_store.root))

    selection = notifier_override or cfg.notifier.selection
    if selection == "console":
        notifier: Notifier = ConsoleNotifier()
    elif selection == "discord":
        file_n = FileNotifier(
            resolve_under(base_dir, cfg.notifier.inbox),
            log_path=resolve_under(base_dir, cfg.notifier.log_path),
        )
        if cfg.discord.enabled and cfg.discord.token and cfg.discord.gates_channel_id:
            from harness.adapters.notifier.discord import DiscordNotifier, DiscordRestPoster

            notifier = DiscordNotifier(
                file_n,
                poster=DiscordRestPoster(token=cfg.discord.token),
                gates_channel_id=cfg.discord.gates_channel_id,
            )
        else:
            typer.echo(
                "notifier=discord but discord is disabled / missing token or channel; "
                "using the file notifier (gates still durable, answer via CLI/bot)",
                err=True,
            )
            notifier = file_n
    else:
        notifier = FileNotifier(
            resolve_under(base_dir, cfg.notifier.inbox),
            log_path=resolve_under(base_dir, cfg.notifier.log_path),
        )

    if cfg.github.use_in_memory_fake or not cfg.github.token:
        github: GitHubAdapter = InMemoryGitHub()
    else:
        github = PyGithubAdapter(token=cfg.github.token, api_base=cfg.github.api_base)

    if cfg.github.use_in_memory_fake:
        executor: Executor = EchoExecutor()
    else:
        roots = {p.id: resolve_under(base_dir, p.path) for p in cfg.projects}
        executor = SubprocessExecutor(project_root_resolver=lambda pid: roots[pid])

    return Container(
        cfg=cfg,
        base_dir=base_dir,
        registry=registry,
        store=store,
        notifier=notifier,
        executor=executor,
        github=github,
        guard=ActionGuard(cfg.autonomy),
    )


def _project_root(c: Container, project_id: str) -> Path:
    for p in c.cfg.projects:
        if p.id == project_id:
            return resolve_under(c.base_dir, p.path)
    raise KeyError(project_id)


def _build_runner(c: Container, loop_name: str, project: Optional[ProjectConfig]) -> LoopRunner:
    if loop_name == "demo":
        if project is None:
            raise typer.BadParameter("the demo loop needs a project")
        loop: LoopDefinition = build_demo_loop(
            executor=c.executor,
            artifacts_dir=c.store.root / "artifacts",
            project=project,
        )
    elif loop_name == "dev_task":
        if project is None:
            raise typer.BadParameter("the dev_task loop needs a project")
        loop = build_dev_loop(
            executor=c.executor,
            github=c.github,
            guard=c.guard,
            project=project,
            instance_id=c.cfg.instance.instance_id,
            project_root=_project_root(c, project.id),
            artifacts_dir=c.store.root / "artifacts",
        )
    elif loop_name == "arch_review":
        if project is None:
            raise typer.BadParameter("the arch_review loop needs a project")
        loop = build_arch_review_loop(
            executor=c.executor,
            github=c.github,
            guard=c.guard,
            project=project,
            project_root=_project_root(c, project.id),
        )
    else:
        raise typer.BadParameter(
            f"unknown loop '{loop_name}' (try 'demo', 'dev_task', or 'arch_review')"
        )
    return LoopRunner(loop, c.store, c.notifier)


def _mark_blocked_if_aborted(c: Container, run_id: str, status: RunStatus) -> None:
    if status is RunStatus.ABORTED:
        co.block_dev_issue_if_aborted(c.github, record=c.store.load(run_id), status=status)


def _build_scheduler(c: Container) -> Scheduler:
    return Scheduler(
        cfg=c.cfg,
        store=c.store,
        registry=c.registry,
        github=c.github,
        notifier=c.notifier,
        runner_factory=lambda loop_name, project: _build_runner(c, loop_name, project),
        breakers_factory=lambda project: _breakers_for(c.cfg, project),
        ledger_path=c.store.root / "scheduler.json",
    )


def _echo_tick(report: TickReport) -> None:
    for rid, status in report.resumed:
        typer.echo(f"resumed {rid} -> {status}")
    for pid, rid, status in report.started:
        typer.echo(f"started {pid} {rid} -> {status}")
    note = "  (HALTED: spend ceiling reached)" if report.halted_for_spend else ""
    typer.echo(f"window spend: ${report.window_spend_usd:.2f}{note}")
    if not report.resumed and not report.started:
        typer.echo("nothing to do")


def _breakers_for(cfg: HarnessConfig, project: ProjectConfig) -> BreakerState:
    cb = project.effective_breakers(cfg.circuit_breakers)
    return BreakerState(max_iterations=cb.max_iterations, budget_ceiling_usd=cb.spend_ceiling_usd)


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
    loop: str = typer.Argument("demo", help="Loop name: 'demo' or 'dev_task'."),
    project: str = typer.Argument(..., help="Registered project id."),
    config: Optional[Path] = CONFIG_OPT,
    notifier: Optional[str] = typer.Option(None, help="Override notifier: 'file' or 'console'."),
    issue: Optional[int] = typer.Option(
        None, "--issue", help="dev_task: issue number to work (else claim the next queued)."
    ),
) -> None:
    """Create and start a run of LOOP for PROJECT."""
    c = build_container(config, notifier_override=notifier)
    proj = c.registry.get(project)
    if not owns(proj, c.cfg.instance):
        typer.echo(
            f"refusing: instance '{c.cfg.instance.instance_id}' does not own project "
            f"'{project}' (owner: {proj.owner_instance}). Reads are fine; acting is not.",
            err=True,
        )
        raise typer.Exit(1)

    data: Optional[dict] = None
    if loop == "dev_task":
        number = issue if issue is not None else co.find_claimable(
            c.github, repo=proj.repo, instance_id=c.cfg.instance.instance_id
        )
        if number is None:
            typer.echo(f"no queued work for '{project}' ({proj.repo})")
            return
        data = {"issue_number": number, "repo": proj.repo}
        typer.echo(f"working issue #{number} on {proj.repo}")

    runner = _build_runner(c, loop, proj)
    record = runner.create_run(project_id=project, breakers=_breakers_for(c.cfg, proj), data=data)
    typer.echo(f"created run {record.run_id}")
    status = runner.run(record.run_id)
    _mark_blocked_if_aborted(c, record.run_id, status)
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
    record = c.store.load(run_id)
    if record.status is not RunStatus.WAITING or record.pending_request is None:
        typer.echo(f"run {run_id} is {record.status.value}; nothing to answer", err=True)
        raise typer.Exit(1)
    req = record.pending_request
    response = VerificationResponse(
        request_id=req.request_id,
        run_id=record.run_id,
        step_id=req.step_id,
        answer={"approved": approve, "notes": notes},
        approved=approve,
        via="cli",
    )
    proj = c.registry.get(record.project_id) if record.project_id else None
    runner = _build_runner(c, record.loop_name, proj)
    if isinstance(c.notifier, FileNotifier):
        c.notifier.write_response(response)
    status = runner.resume(run_id, response)
    if isinstance(c.notifier, FileNotifier):
        c.notifier.archive(req.request_id)
    _mark_blocked_if_aborted(c, run_id, status)
    _report(c, run_id, status)


@app.command()
def poll(config: Optional[Path] = CONFIG_OPT) -> None:
    """Resume any WAITING run whose answer has arrived (the scheduler's resume phase)."""
    c = build_container(config)
    if not isinstance(c.notifier, FileNotifier):
        typer.echo("poll requires the file notifier", err=True)
        raise typer.Exit(1)
    resumed = _build_scheduler(c).resume_waiting()
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
    _echo_tick(_build_scheduler(c).tick())


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
        _echo_tick(_build_scheduler(build_container(config)).tick())
        time.sleep(every)


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
        _build_scheduler(build_container(config)).resume_waiting()

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
