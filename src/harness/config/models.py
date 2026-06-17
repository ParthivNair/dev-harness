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
from typing import Optional

from pydantic import BaseModel, Field


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
    token: Optional[str] = None      # injected from env HARNESS_GITHUB_TOKEN, never TOML


class StateStoreConfig(BaseModel):
    backend: str = "json"          # "json" (M1) | "sqlite" (later)
    root: str = ".harness"         # relative to the harness.toml directory unless absolute


class NotifierConfig(BaseModel):
    selection: str = "file"        # "file" (durable) | "console" (interactive)
    log_path: str = ".harness/notifications.log"
    inbox: str = ".harness/inbox"  # where file requests/responses live


class ProjectPointer(BaseModel):
    id: str
    path: str                      # local working-copy path, relative to harness.toml dir
    config_file: Optional[str] = None


class HarnessConfig(BaseModel):
    schema_version: int = 1
    instance: InstanceInfo
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    state_store: StateStoreConfig = Field(default_factory=StateStoreConfig)
    notifier: NotifierConfig = Field(default_factory=NotifierConfig)
    autonomy: dict[str, AutonomyTier] = Field(default_factory=dict)
    circuit_breakers: CircuitBreakers = Field(default_factory=CircuitBreakers)
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


class ClaudeConfig(BaseModel):
    model: str = "claude-opus-4-8"
    permission_mode: str = "acceptEdits"  # default|acceptEdits|plan|dontAsk|bypassPermissions
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    add_dirs: list[str] = Field(default_factory=list)


class ProjectOverrides(BaseModel):
    circuit_breakers: Optional[CircuitBreakers] = None
    autonomy: dict[str, AutonomyTier] = Field(default_factory=dict)


class ProjectConfig(BaseModel):
    schema_version: int = 1
    id: str
    display_name: str = ""
    repo: str = ""
    owner_instance: str
    description: str = ""
    commands: ProjectCommands = Field(default_factory=ProjectCommands)
    prompts: PromptSet = Field(default_factory=PromptSet)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    overrides: ProjectOverrides = Field(default_factory=ProjectOverrides)

    def effective_breakers(self, default: CircuitBreakers) -> CircuitBreakers:
        return self.overrides.circuit_breakers or default

    def effective_autonomy(self, default: dict[str, AutonomyTier]) -> dict[str, AutonomyTier]:
        return {**default, **self.overrides.autonomy}
