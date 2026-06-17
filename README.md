# dev-harness

A **headless, portable engine that orchestrates AI-assisted development across many repos.** The *same* engine runs as independent installs per machine; each install acts only on the projects whose build + human-verification surface lives on that platform. There is **no central dispatcher and no cross-machine RPC** — installs coordinate the way two teammates would: **through GitHub** (Issues = task queue, labels + PR state = status, one shared account).

> Milestone 1 status: the engine core, the durable VerificationGate (pause/persist/resume), the ports, a sample registered project, and an end-to-end demo loop are implemented. The Discord notifier, the real architecture-review / dev-test loops, GitHub Actions / branch-protection wiring, and any GUI are explicit follow-ups (see below).

## Why this exists

An LLM can read code, logs, and test output, but it **cannot perceive runtime reality** — it can't hear whether the audio crackles, feel latency on a knob, or see whether the UI rendered. The human is therefore a **perceptual oracle**: a sensor for exactly the dimensions outside the model's reach. The harness automates everything up to that irreducible perceive-and-report moment and makes the moment cheap: load the project, arm the action, hand the human a precise script and a structured way to answer.

That moment is the **VerificationGate**, and it is the heart of the design.

## Architecture

Hexagonal (ports & adapters). The engine depends only on **ports** (`typing.Protocol` interfaces); concrete **adapters** implement them; one **composition root** (`src/harness/cli/main.py`) wires them together. Platform-specifics live only in per-project config and the local `Executor` — never as `if platform == ...` in the engine.

```
src/harness/
├── domain/         models.py    — durable, JSON-serializable types (RunRecord, VerificationRequest/Response, ...)
├── ports/          run_store / notifier / github / executor / project_registry  (Protocols)
├── application/    loop_runner (the engine) · action_guard (autonomy) · ownership
├── adapters/       state/json_store · notifier/{console,file} · github/{fake,pygithub_adapter} ·
│                   executor/{echo,subprocess_executor} · registry/file_registry
├── config/         models.py (pydantic) · loader.py (tomllib + env)
├── loops/          demo.py (the Milestone-1 demo loop)
└── cli/            main.py (Typer commands + composition root)
```

### The VerificationGate (the central primitive)

A loop step can **suspend**, emit a **structured verification request** (a prompt, a JSON-Schema for the answer, and an optional artifact to perceive), have its **full state persisted to durable external storage**, and **resume** when the human answers — possibly after a reboot, or so the other machine can see the pending gate.

It is modeled as **exit-and-resume, not block-and-wait**: hitting a gate persists `WAITING` and *returns*; the process is free to exit. A later `resume()` (driven by the human, a poller, or the other machine) reloads the record, validates the answer against its schema, checks the `request_id`/`run_id` correlation so a stale answer can't resume the wrong run, and re-enters the saved step. The resume position is plain data (`current_step` + a `data` dict) — never a serialized coroutine or stack frame.

### Durability

One atomic JSON file per run (`RunStore` port → `AtomicJsonRunStore`). Writes use temp-file + `os.replace` (atomic on Windows and POSIX). Completing a step and advancing the position happen in a single write, so a crash simply re-runs the not-yet-recorded step (steps tolerate at-least-once execution). The persisted-state schema is pinned by `schema_version`.

### Guardrails (baked in from day one)

- **Autonomy taxonomy as config** (`[autonomy]`): every action is `autonomous`, `gated`, or `forbidden`. The `ActionGuard` classifies by name and `ALLOW` / `GATE` / `REFUSE`s at the boundary — the LLM proposes, code decides. Unknown actions default to `gated` (fail safe).
- **Structural safety**: the `GitHubAdapter` port has **no** merge / push / force-push method, and `open_draft_pr` always sets `draft=True`. The autonomous path *cannot* touch `main` or force-push, because there is no code that can.
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

## Configuration

Two levels, two files (see `harness.toml.example` and `examples/sample-project/`):

- **Instance** (`harness.toml`): this install's `instance_id`, GitHub coordinates, state-store location, autonomy taxonomy, circuit-breaker defaults, notifier choice, and pointers to registered projects. Machine-local. The GitHub token comes from `HARNESS_GITHUB_TOKEN`, never TOML.
- **Per-project** (`harness.project.toml`, lives in the managed repo): `id`, `owner_instance`, build/test commands, prompt-set paths, and per-project overrides. Because it lives in the repo, the other machine reads the identical config.

## Testing

```bash
uv run pytest            # unit + integration (in-memory fakes; no network)
```

The fakes are the production default wiring, so tests exercise the real default path. The integration test proves durability by resuming a run through **fresh store/runner instances** (simulating separate processes).

## Out of scope for Milestone 1 (planned follow-ups)

- A real **Discord** notifier (another implementation of the `Notifier` port).
- The **architecture-review loop** (bounded, rubric-driven, with a "no action needed" exit) and the **dev/test loop** (generate → test → verify → draft PR).
- GitHub **Actions / branch-protection** wiring and live two-machine coordination.
- A **SQLite** `RunStore`; cross-process locking / CAS on run records.
- Sketch → Figma, and any **cockpit GUI**.
