# dev-harness

A **headless, portable engine that orchestrates AI-assisted development across many repos.** The *same* engine runs as independent installs per machine; each install acts only on the projects whose build + human-verification surface lives on that platform. There is **no central dispatcher and no cross-machine RPC** — installs coordinate the way two teammates would: **through GitHub** (Issues = task queue, labels + PR state = status, one shared account).

> Status: **Milestone 2** adds the orchestration layer on top of the M1 core — the real
> **dev/test loop** (claim issue → generate → build → test → human gate → draft PR), the
> bounded **architecture-review loop**, the **GitHub-native coordination** (Issues = queue,
> labels = state, an owner label = a lease), the weighted **multi-project scheduler**
> (`harness tick`/`watch`) that diverts attention across repos, and a **Discord** notifier +
> bridge bot. The harness is registered as a managed project of itself (dogfooding). A local
> observer **dashboard** (`harness ui`) watches it all work. Remaining follow-ups (SQLite store,
> GitHub Actions wiring, the parallel agent sweep) are listed at the end.

## Why this exists

An LLM can read code, logs, and test output, but it **cannot perceive runtime reality** — it can't hear whether the audio crackles, feel latency on a knob, or see whether the UI rendered. The human is therefore a **perceptual oracle**: a sensor for exactly the dimensions outside the model's reach. The harness automates everything up to that irreducible perceive-and-report moment and makes the moment cheap: load the project, arm the action, hand the human a precise script and a structured way to answer.

That moment is the **VerificationGate**, and it is the heart of the design.

## Architecture

Hexagonal (ports & adapters). The engine depends only on **ports** (`typing.Protocol` interfaces); concrete **adapters** implement them; one **composition root** (`src/harness/container.py`) wires them together. The CLI (`src/harness/cli/main.py`) and the web dashboard (`src/harness/web/`) are both *driving* surfaces over that root, and both delegate to the shared use-cases in `src/harness/operations.py` so engine logic lives in exactly one place. Platform-specifics live only in per-project config and the local `Executor` — never as `if platform == ...` in the engine.

```
src/harness/
├── domain/         models.py    — durable, JSON-serializable types (RunRecord, VerificationRequest/Response, ...)
├── ports/          run_store / notifier / github / executor / project_registry  (Protocols)
├── application/    loop_runner (the engine) · action_guard (autonomy) · ownership ·
│                   coordination (issue lease + label state machine) · scheduler (attention/spend)
├── adapters/       state/json_store · notifier/{console,file,discord} · github/{fake,pygithub_adapter} ·
│                   executor/{echo,subprocess_executor} · registry/file_registry
├── loops/          demo · dev_task (generate→build→test→gate→draft PR) · arch_review (rubric→issues)
├── bots/           discord_bot (the always-on gateway bridge)
├── config/         models.py (pydantic) · loader.py (tomllib + env + .env)
└── cli/            main.py (Typer commands + composition root)
```

### The VerificationGate (the central primitive)

A loop step can **suspend**, emit a **structured verification request** (a prompt, a JSON-Schema for the answer, and an optional artifact to perceive), have its **full state persisted to durable external storage**, and **resume** when the human answers — possibly after a reboot, or so the other machine can see the pending gate.

It is modeled as **exit-and-resume, not block-and-wait**: hitting a gate persists `WAITING` and *returns*; the process is free to exit. A later `resume()` (driven by the human, a poller, or the other machine) reloads the record, validates the answer against its schema, checks the `request_id`/`run_id` correlation so a stale answer can't resume the wrong run, and re-enters the saved step. The resume position is plain data (`current_step` + a `data` dict) — never a serialized coroutine or stack frame.

### Durability

One atomic JSON file per run (`RunStore` port → `AtomicJsonRunStore`). Writes use temp-file + `os.replace` (atomic on Windows and POSIX). Completing a step and advancing the position happen in a single write, so a crash simply re-runs the not-yet-recorded step (steps tolerate at-least-once execution). The persisted-state schema is pinned by `schema_version`.

### Guardrails (baked in from day one)

- **Autonomy taxonomy as config** (`[autonomy]`): every action is `autonomous`, `gated`, or `forbidden`. The `ActionGuard` classifies by name and `ALLOW` / `GATE` / `REFUSE`s at the boundary — the LLM proposes, code decides. Unknown actions default to `gated` (fail safe).
- **Structural safety**: the `GitHubAdapter` port has **no** merge method, and `open_draft_pr` always sets `draft=True`. The one push path is `Executor.publish_branch`, which is deliberately narrow — it refuses trunks (`main`/`master`/…) and never force-pushes, so it can publish a `harness/*` feature branch for a PR but *cannot* touch `main`. The merge stays human.
- **Circuit breakers on every loop**: a max-iterations cap and a spend ceiling (fed by `total_cost_usd` from `claude -p --output-format json`). Counters are persisted and read back before acting on resume, so a crash can't reset a budget or escape the cap.

### Coordination (cross-machine)

The local file store is the hot path / source of truth. GitHub is the cross-machine substrate: the design point is that a project stays legible from the machine that *isn't* running it (Issues/labels/draft-PR state). The `GitHubAdapter` is defined now; a thin PyGithub implementation is present, with an in-memory fake as the credential-free default.

## Quickstart

Requires Python 3.12+. With [uv](https://docs.astral.sh/uv/):

```bash
uv sync                 # create the venv and install
uv run harness --help
uv run harness config-check
uv run harness projects
```

### The demo: suspend → persist → resume, for real

The demo loop is `build → verify_gate → finish (or loop back on reject)`. With the default **file** notifier, the run goes durably `WAITING` at the gate and the process exits — there is no live process holding state between these commands.

```bash
# 1. Start a run. It builds, hits the gate, persists WAITING, and EXITS.
uv run harness run demo sample
#   -> created run <RUN_ID>
#   -> status: WAITING   (a <request_id>.request.json now sits in .harness/inbox/)

# 2. Reject once: the loop persists the answer, resumes, rebuilds, and waits again.
uv run harness answer <RUN_ID> --reject --notes "crackle present"
#   -> status: WAITING   (a new gate, iteration 2)

# 3. Approve: the loop resumes and finishes.
uv run harness answer <RUN_ID> --approve --notes "tone clean"
#   -> status: COMPLETED

uv run harness show <RUN_ID>     # full persisted state: step log, answers, breakers
uv run harness list-runs
```

`harness poll` is the hands-off variant: drop a `<request_id>.response.json` into the inbox (what a Discord bridge or the other machine would do) and `poll` resumes every matching run.

To run the whole gate interactively in one process instead, use `--notifier console`.

### The observer dashboard (`harness ui`)

A local web cockpit to watch agents work: a **minimized overview** (one compact line per active run — project, current step, iteration, spend, with verification gates highlighted) that **expands** to per-run detail (step timeline, Claude output, build/test logs, breakers). It can answer gates and start/abort runs from the page. Optional — needs the `web` extra.

```bash
uv sync --extra web
uv run harness ui                 # serves http://127.0.0.1:8765 and opens a browser
uv run harness ui --no-browser    # or set [ui] in harness.toml (enabled/host/port/allow_actions)
```

It reads the same durable run state the CLI does (no separate source of truth) and routes every action through the same use-cases. It binds **127.0.0.1 only** — the action surface mutates engine state, so it must never be network-exposed; set `[ui].allow_actions = false` for a read-only viewer.

## Configuration

Two levels, two files (see `harness.toml.example` and `examples/sample-project/`):

- **Instance** (`harness.toml`): this install's `instance_id`, GitHub coordinates, state-store location, autonomy taxonomy, circuit-breaker defaults, notifier choice, and pointers to registered projects. Machine-local. The GitHub token comes from `HARNESS_GITHUB_TOKEN`, never TOML.
- **Per-project** (`harness.project.toml`, lives in the managed repo): `id`, `owner_instance`, build/test commands, prompt-set paths, and per-project overrides. Because it lives in the repo, the other machine reads the identical config.

## Testing

```bash
uv run pytest            # unit + integration (in-memory fakes; no network)
```

The fakes are the production default wiring, so tests exercise the real default path. The integration test proves durability by resuming a run through **fresh store/runner instances** (simulating separate processes).

## Milestone 2 — the orchestration layer ("a team of employees")

GitHub is the office: **Issues = task queue, labels = status, draft PRs = work product,
assignee + an `harness:owner:<id>` label = a single-writer lease.** Each machine runs the
same engine and acts only on projects it owns; they coordinate through GitHub, no central
dispatcher.

```bash
# Real dev loop: claim a queued issue, generate, build, test, gate, open a DRAFT PR.
uv run harness run dev_task dev-harness --issue 42     # or omit --issue to claim the next queued
uv run harness answer <RUN_ID> --approve               # the gate -> opens the draft PR

# Architecture review: rubric -> structured findings -> filed issues (the dev loop's queue).
uv run harness run arch_review dev-harness

# The scheduler: one pass = resume answered gates, then start eligible work, weighted by
# priority/cadence. This is the unit an OS scheduler invokes; `watch` loops it.
uv run harness tick
uv run harness watch --interval 300
```

- **Label state machine:** `queued → in-progress → needs-verification → pr-open → done` (+ `blocked` on a breaker trip). Claiming is an optimistic lease with a confirm-read tiebreak (`set_labels` is last-writer-wins), so two machines never double-claim.
- **Attention diversion:** per-project `[scheduling]` (`priority`/`weight`, `min_poll_interval_seconds`) makes the scheduler check a low-effort repo less often and give a high-priority repo more starts. A **global spend ceiling** (per window) halts new starts while in-flight gates still resume.
- **The autonomy ceiling:** the loop publishes its work to a `harness/*` feature branch (`Executor.publish_branch` — guarded: no trunks, no force-push) and opens a **draft** PR. It **cannot merge** — the GitHub port has no merge method, so the merge is always a human's. The harness is registered as a managed project of itself (`harness.project.toml`), so it proposes its own next milestone as a draft PR you review.

### Secrets & `.env`

Copy `.env.example` → `.env` (gitignored, per machine). Secrets live in the environment,
never in TOML; non-secret Discord channel IDs live in `harness.toml` under `[discord]`.

| Secret | For |
|---|---|
| `HARNESS_GITHUB_TOKEN` | Fine-grained PAT — Issues RW, Pull requests RW, Contents RW, Metadata R |
| `DISCORD_BOT_TOKEN` | The Discord notifier's posts + the bridge bot (only if `notifier.selection="discord"`) |
| Claude Code CLI auth | Separate — the `claude` CLI authenticates itself; `ANTHROPIC_API_KEY` only if it uses API-key mode |

### Discord (optional)

Set `notifier.selection = "discord"` and fill `[discord]`. `notify()` writes the durable
request file **then** posts Approve/Reject buttons to `#verification-gates` — Discord bridges
*to* the inbox, it doesn't replace it. The always-on bot is a **separate** process
(`uv run harness discord-bot`, needs `uv sync --extra discord`) that turns a click into a
`<request_id>.response.json` and resumes the run. Bot down ⇒ gates still answerable via CLI.

### Always-on runtime (two machines)

Each machine runs its own stack and coordinates via GitHub:

- **Windows** (owns `dev-harness`): `harness tick` on a **Task Scheduler** trigger (every few minutes, "run whether logged on or not", with `.env` supplying secrets); the Discord bot as an **NSSM** service (auto-restart, starts at boot).
- **macOS** (owns the next app): the same two roles via **launchd** — `StartInterval` for `tick`, a `KeepAlive` plist for the bot. Same commands, different supervisor; the engine never branches on platform.

## Still out of scope (planned follow-ups)

- GitHub **Actions / branch-protection** wiring and a stale-lease reconciler.
- A **SQLite** `RunStore`; cross-process locking / CAS on run records.
- Artifact upload to Discord (multipart) and per-project gate channels.
- The **agent sweep**: one planner reads all queued issues, produces a cross-cutting plan, and fans out parallel dev_task agents in isolated git worktrees (the observer dashboard above is the cockpit for watching it).
- Sketch → Figma.
