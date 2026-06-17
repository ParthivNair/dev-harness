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

from harness.application.action_guard import ActionGuard
from harness.application.loop_runner import LoopDefinition, LoopRunner
from harness.application.ownership import owns
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
from harness.loops.demo import build_demo_loop
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


def _build_runner(c: Container, loop_name: str, project: Optional[ProjectConfig]) -> LoopRunner:
    if loop_name == "demo":
        if project is None:
            raise typer.BadParameter("the demo loop needs a project")
        loop: LoopDefinition = build_demo_loop(
            executor=c.executor,
            artifacts_dir=c.store.root / "artifacts",
            project=project,
        )
    else:
        raise typer.BadParameter(f"unknown loop '{loop_name}' (Milestone 1 ships only 'demo')")
    return LoopRunner(loop, c.store, c.notifier)


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
    loop: str = typer.Argument("demo", help="Loop name (M1: 'demo')."),
    project: str = typer.Argument(..., help="Registered project id."),
    config: Optional[Path] = CONFIG_OPT,
    notifier: Optional[str] = typer.Option(None, help="Override notifier: 'file' or 'console'."),
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
    runner = _build_runner(c, loop, proj)
    record = runner.create_run(project_id=project, breakers=_breakers_for(c.cfg, proj))
    typer.echo(f"created run {record.run_id}")
    status = runner.run(record.run_id)
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
    _report(c, run_id, status)


@app.command()
def poll(config: Optional[Path] = CONFIG_OPT) -> None:
    """Scan the file-notifier inbox for answers and resume any matching runs."""
    c = build_container(config)
    if not isinstance(c.notifier, FileNotifier):
        typer.echo("poll requires the file notifier", err=True)
        raise typer.Exit(1)
    waiting = c.store.list(status=RunStatus.WAITING)
    if not waiting:
        typer.echo("no runs waiting")
        return
    for record in waiting:
        req = record.pending_request
        if req is None:
            continue
        response = c.notifier.collect(req)
        if response is None:
            typer.echo(f"{record.run_id}: still waiting (no response file)")
            continue
        proj = c.registry.get(record.project_id) if record.project_id else None
        runner = _build_runner(c, record.loop_name, proj)
        status = runner.resume(record.run_id, response)
        c.notifier.archive(req.request_id)
        typer.echo(f"{record.run_id}: resumed -> {status.value}")


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
    registry = FileProjectRegistry(cfg.projects, path.parent)
    for p in registry.list_projects():
        typer.echo(
            f"  project {p.id}: owner={p.owner_instance} owned={owns(p, cfg.instance)} repo={p.repo}"
        )
    typer.echo("OK")


if __name__ == "__main__":
    app()
