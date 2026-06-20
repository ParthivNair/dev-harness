"""Configuration models — two levels, two files.

* **Instance config** (``harness.toml``): THIS install's identity, GitHub
  coordinates, state-store location, autonomy taxonomy, circuit-breaker
  defaults, notifier choice, and POINTERS to registered projects. Machine-local;
  not shared. Secrets (the GitHub token) come from the environment, never TOML.

* **Per-project config** (``<repo>/harness.project.toml``): identity, owning
  instance, build/test commands, prompt-set paths, and per-project overrides.
  It lives *inside the managed repo*, so the other machine — which also checks
  that repo out — reads the identical config.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

# priority name -> attention/budget weight (used when a project sets no explicit weight)
PRIORITY_WEIGHTS: dict[str, float] = {"high": 4.0, "normal": 1.0, "low": 0.25}


class AutonomyTier(str, Enum):
    """How much latitude an action gets. Misclassification is a load-time error."""

    AUTONOMOUS = "autonomous"  # run freely
    GATED = "gated"            # must pass a human verification gate first
    FORBIDDEN = "forbidden"    # never executes on the autonomous path


class CircuitBreakers(BaseModel):
    max_iterations: int = 5
    spend_ceiling_usd: float = 5.0
    spend_is_hard_stop: bool = True


# --------------------------------------------------------------------------- #
# Instance-level config
# --------------------------------------------------------------------------- #
class InstanceInfo(BaseModel):
    instance_id: str               # the partition key; matches a project's owner_instance
    display_name: str = ""
    platform: str = "unknown"      # informational ONLY — the engine must not branch on it


class GitHubConfig(BaseModel):
    account: str = ""
    repo: str = ""                 # coordination repo (issues/PRs) "owner/name"
    api_base: str = "https://api.github.com"
    use_in_memory_fake: bool = True  # M1 default: demo runs with no token/network
    # injected from env HARNESS_GITHUB_TOKEN, never TOML. repr=False so a credential
    # can never leak into a traceback, log line, or pydantic repr.
    token: Optional[str] = Field(default=None, repr=False)


class ExecutionConfig(BaseModel):
    """Selects the code-generation executor INDEPENDENTLY of the GitHub adapter.

    Historically the executor was chosen off ``github.use_in_memory_fake`` (fake
    GitHub => echo executor, real GitHub => subprocess), so you could not mix a real
    Claude run with a fake GitHub (a safe "dry run": spend tokens, write nothing to
    GitHub) or vice-versa. This section breaks that coupling.

    ``mode`` defaults to ``"auto"``, which preserves the legacy behaviour exactly:
    the executor follows ``github.use_in_memory_fake`` (fake => echo, real =>
    subprocess). Set it to ``"real"`` to force :class:`SubprocessExecutor` (real
    Claude) or ``"echo"`` to force :class:`EchoExecutor` regardless of the GitHub
    adapter choice."""

    mode: Literal["auto", "real", "echo"] = "auto"


class StateStoreConfig(BaseModel):
    backend: str = "json"          # "json" (M1) | "sqlite" (later)
    root: str = ".harness"         # relative to the harness.toml directory unless absolute


class NotifierConfig(BaseModel):
    selection: str = "file"        # "file" (durable) | "console" (interactive) | "discord"
    log_path: str = ".harness/notifications.log"
    inbox: str = ".harness/inbox"  # where file requests/responses live


class SchedulingConfig(BaseModel):
    """Instance-level driver settings for the multi-project scheduler.

    The scheduler allocates *attention* (which project to start next) and the
    shared *spend budget* across all owned projects, layered on top of the
    existing per-run circuit breakers. Off by default so M1 behaviour (manual
    ``run``/``poll``) is unchanged.
    """

    enabled: bool = False
    tick_interval_seconds: int = 300       # base cadence of ``harness watch``
    max_concurrent_runs: int = 1           # how many runs may be active at once
    global_spend_ceiling_usd: float = 20.0  # shared cap across ALL projects per window
    spend_window: Literal["daily", "rolling_24h"] = "daily"
    default_weight: float = 1.0


class UIConfig(BaseModel):
    """The local observer dashboard (``harness ui``). Off by default; it reads the
    durable run state and — unless ``allow_actions`` is false — can answer gates and
    start/abort runs. It binds ``127.0.0.1`` ONLY: the action surface mutates engine
    state, so the dashboard must never be exposed on the network."""

    enabled: bool = False
    host: str = "127.0.0.1"          # localhost only — do not bind 0.0.0.0
    port: int = 8765
    poll_interval_ms: int = 1500     # how often the page refreshes the overview
    allow_actions: bool = True       # false => read-only viewer (no answer/start/abort)
    open_browser: bool = True        # auto-open the browser when `harness ui` starts
    board_ttl_seconds: int = 20      # cache for GitHub-derived board reads


class DiscordConfig(BaseModel):
    """Discord routing — channel/guild IDs are config (NOT secret); the bot token
    is injected from ``DISCORD_BOT_TOKEN`` in the environment, never from TOML."""

    enabled: bool = False
    guild_id: str = ""
    gates_channel_id: str = ""        # #verification-gates (the human's inbox)
    activity_channel_id: str = ""     # #activity (issues/PRs/labels)
    alerts_channel_id: str = ""       # #alerts (breaker trips, failures)
    runs_channel_id: str = ""         # #runs (lifecycle), optional
    poll_after_answer: bool = True    # bot triggers resume after writing a response
    project_channels: dict[str, str] = Field(default_factory=dict)  # project id -> channel id
    # injected from env DISCORD_BOT_TOKEN, never TOML. repr=False — see GitHubConfig.token.
    token: Optional[str] = Field(default=None, repr=False)


class ProjectPointer(BaseModel):
    id: str
    path: str                      # local working-copy path, relative to harness.toml dir
    config_file: Optional[str] = None


class HarnessConfig(BaseModel):
    schema_version: int = 1
    instance: InstanceInfo
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    state_store: StateStoreConfig = Field(default_factory=StateStoreConfig)
    notifier: NotifierConfig = Field(default_factory=NotifierConfig)
    autonomy: dict[str, AutonomyTier] = Field(default_factory=dict)
    circuit_breakers: CircuitBreakers = Field(default_factory=CircuitBreakers)
    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    projects: list[ProjectPointer] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Per-project config
# --------------------------------------------------------------------------- #
class ProjectCommands(BaseModel):
    build: list[str] | str = Field(default_factory=list)
    test: list[str] | str = Field(default_factory=list)
    cwd: str = "."


class PromptSet(BaseModel):
    root: str = ".harness/prompts"
    dev_task: Optional[str] = None
    arch_review: Optional[str] = None
    pr_review: Optional[str] = None
    triage: Optional[str] = None


class ClaudeConfig(BaseModel):
    model: str = "claude-opus-4-8"
    permission_mode: str = "acceptEdits"  # default|acceptEdits|plan|dontAsk|bypassPermissions
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    add_dirs: list[str] = Field(default_factory=list)


class ProjectOverrides(BaseModel):
    circuit_breakers: Optional[CircuitBreakers] = None
    autonomy: dict[str, AutonomyTier] = Field(default_factory=dict)


class ProjectScheduling(BaseModel):
    """Per-project attention/cadence knobs. Lives in the repo's
    ``harness.project.toml`` so the other machine reads the identical cadence.

    ``min_poll_interval_seconds`` is the "check the lower-effort repo less
    frequently" knob; ``priority``/``weight`` set its share of attention + budget.
    """

    priority: Literal["high", "normal", "low"] = "normal"
    weight: Optional[float] = None             # explicit override of priority's weight
    min_poll_interval_seconds: int = 900       # minimum gap between starts for this project
    loops: list[str] = Field(default_factory=lambda: ["dev_task"])
    arch_review_cadence_seconds: Optional[int] = None  # None => never auto-run arch_review
    triage_cadence_seconds: Optional[int] = None       # None => never auto-run triage
    pr_review_cadence_seconds: Optional[int] = None    # None => never auto-run pr_review
    # How a merged PR lands on the base branch. Only used when merge_to_main is opted in.
    pr_merge_method: Literal["squash", "merge", "rebase"] = "squash"

    def effective_weight(self, default: float = 1.0) -> float:
        if self.weight is not None:
            return self.weight
        return PRIORITY_WEIGHTS.get(self.priority, default)


class ProjectConfig(BaseModel):
    schema_version: int = 1
    id: str
    display_name: str = ""
    repo: str = ""
    owner_instance: str
    description: str = ""
    # Free-text owner goals/context for the `research` loop: what this repo is for
    # and what kinds of improvements matter. Fed to Claude (via {{goals}}) when the
    # research loop scopes a backlog. Empty => a generic "find the most valuable
    # improvements" brief. A `harness research <p> --goals <file>` overrides it.
    goals: str = ""
    commands: ProjectCommands = Field(default_factory=ProjectCommands)
    prompts: PromptSet = Field(default_factory=PromptSet)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    scheduling: ProjectScheduling = Field(default_factory=ProjectScheduling)
    overrides: ProjectOverrides = Field(default_factory=ProjectOverrides)

    def effective_breakers(self, default: CircuitBreakers) -> CircuitBreakers:
        return self.overrides.circuit_breakers or default

    def effective_autonomy(self, default: dict[str, AutonomyTier]) -> dict[str, AutonomyTier]:
        return {**default, **self.overrides.autonomy}
