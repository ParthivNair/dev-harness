"""The composition root — the ONE place adapters are chosen and wired together.

Every driving surface (the :mod:`harness.cli` Typer app, the :mod:`harness.web`
dashboard) builds a :class:`Container` here, then hands *ports* (never concrete
classes) to the engine. Swapping fake<->real GitHub or echo<->subprocess executor
is config-only; the engine code never changes.

This used to live inside ``cli/main.py``; it moved here so the web dashboard can
share the identical wiring without importing the CLI. The CLI and the web server
are both *driving* adapters over this one root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from harness.application import coordination as co
from harness.application.action_guard import ActionGuard
from harness.application.loop_runner import LoopDefinition, LoopRunner
from harness.application.overseer import Overseer
from harness.application.scheduler import Scheduler
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
from harness.domain.models import BreakerState, RunStatus
from harness.loops.arch_review import build_arch_review_loop
from harness.loops.demo import build_demo_loop
from harness.loops.dev_task import build_dev_loop
from harness.loops.pr_review import build_pr_review_loop
from harness.loops.triage import build_triage_loop
from harness.ports.executor import Executor
from harness.ports.github import GitHubAdapter
from harness.ports.notifier import Notifier
from harness.ports.project_registry import ProjectRegistry


class UnknownLoop(ValueError):
    """A loop name with no builder (not 'demo', 'dev_task', 'arch_review', or 'triage')."""


@dataclass
class Container:
    cfg: HarnessConfig
    base_dir: Path
    registry: ProjectRegistry          # the port — FileProjectRegistry in prod, a fake in tests
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
            # notifier=discord but discord is disabled / missing token or channel;
            # fall back to the file notifier (gates stay durable, answerable via CLI/bot).
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


def project_root(c: Container, project_id: str) -> Path:
    for p in c.cfg.projects:
        if p.id == project_id:
            return resolve_under(c.base_dir, p.path)
    raise KeyError(project_id)


def _guard_for(c: Container, project: ProjectConfig) -> ActionGuard:
    """A project-scoped ActionGuard: the instance autonomy taxonomy with this
    project's ``[overrides.autonomy]`` layered on top. This is how a self-managed
    repo opts a normally-gated action (``verify_gate``, ``mark_pr_ready``) into
    autonomous WITHOUT loosening the safe instance defaults for every other repo."""
    return ActionGuard(project.effective_autonomy(c.cfg.autonomy))


def breakers_for(cfg: HarnessConfig, project: ProjectConfig) -> BreakerState:
    cb = project.effective_breakers(cfg.circuit_breakers)
    return BreakerState(max_iterations=cb.max_iterations, budget_ceiling_usd=cb.spend_ceiling_usd)


def build_runner(c: Container, loop_name: str, project: Optional[ProjectConfig]) -> LoopRunner:
    if loop_name == "demo":
        if project is None:
            raise UnknownLoop("the demo loop needs a project")
        loop: LoopDefinition = build_demo_loop(
            executor=c.executor,
            artifacts_dir=c.store.root / "artifacts",
            project=project,
        )
    elif loop_name == "dev_task":
        if project is None:
            raise UnknownLoop("the dev_task loop needs a project")
        loop = build_dev_loop(
            executor=c.executor,
            github=c.github,
            guard=_guard_for(c, project),
            project=project,
            instance_id=c.cfg.instance.instance_id,
            project_root=project_root(c, project.id),
            artifacts_dir=c.store.root / "artifacts",
            store=c.store,
        )
    elif loop_name == "arch_review":
        if project is None:
            raise UnknownLoop("the arch_review loop needs a project")
        loop = build_arch_review_loop(
            executor=c.executor,
            github=c.github,
            guard=_guard_for(c, project),
            project=project,
            project_root=project_root(c, project.id),
        )
    elif loop_name == "triage":
        if project is None:
            raise UnknownLoop("the triage loop needs a project")
        loop = build_triage_loop(
            executor=c.executor,
            github=c.github,
            guard=_guard_for(c, project),
            project=project,
            project_root=project_root(c, project.id),
        )
    elif loop_name == "pr_review":
        if project is None:
            raise UnknownLoop("the pr_review loop needs a project")
        loop = build_pr_review_loop(
            executor=c.executor,
            github=c.github,
            guard=_guard_for(c, project),  # per-repo guard: honors merge_to_main opt-in
            project=project,
            instance_id=c.cfg.instance.instance_id,
            project_root=project_root(c, project.id),
            artifacts_dir=c.store.root / "artifacts",
        )
    else:
        raise UnknownLoop(
            f"unknown loop '{loop_name}' "
            "(try 'demo', 'dev_task', 'arch_review', 'triage', or 'pr_review')"
        )
    return LoopRunner(loop, c.store, c.notifier)


def build_overseer(c: Container) -> Overseer:
    return Overseer(
        cfg=c.cfg,
        store=c.store,
        github=c.github,
        registry=c.registry,
        executor=c.executor,
        notifier=c.notifier,
    )


def build_scheduler(c: Container) -> Scheduler:
    return Scheduler(
        cfg=c.cfg,
        store=c.store,
        registry=c.registry,
        github=c.github,
        notifier=c.notifier,
        runner_factory=lambda loop_name, project: build_runner(c, loop_name, project),
        breakers_factory=lambda project: breakers_for(c.cfg, project),
        overseer=build_overseer(c),
        ledger_path=c.store.root / "scheduler.json",
    )


def mark_blocked_if_aborted(c: Container, run_id: str, status: RunStatus) -> None:
    if status is RunStatus.ABORTED:
        co.block_dev_issue_if_aborted(c.github, record=c.store.load(run_id), status=status)
