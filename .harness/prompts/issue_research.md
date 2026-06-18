# Issue research & creation — new-session kickoff prompt

> Paste this into a **fresh Claude Code session opened in the dev-harness repo**. Its job is to
> fill the harness's work queue with *good* issues — not to implement anything.

## Goal

Research this project and file a prioritized backlog of **small, well-scoped GitHub issues** on
`ParthivNair/dev-harness`, each labeled `harness:queued` so the `dev_task` loop can claim them.
Quality over quantity: each issue must be something the autonomous dev loop can implement in one
sitting and a human can review as a focused draft PR. (The last run wasted a real Claude session
on an empty placeholder issue — that is exactly what this prevents.)

## Who consumes these issues (so you scope them correctly)

The `dev_task` loop claims a `harness:queued` issue, feeds its **title + body** to Claude as the
task spec, implements it, runs `uv run pytest`, gates a human, and opens a **draft PR**. Therefore
every issue must be:
- a single, independently-shippable change (a handful of files),
- **testable** — with acceptance criteria the loop can satisfy and a human can verify,
- self-contained — no "go research X" tasks; the spec must say what to build.

Labels: apply **`harness:queued`** (required — marks it claimable) **plus** a severity
`sev:low | sev:med | sev:high`. Do **not** apply other `harness:*` labels — the loop manages those.

## Research first (don't skip — take notes before writing any issue)

1. **README** — the "Still out of scope (planned follow-ups)" and "Milestone 2" sections are an
   explicit backlog (e.g. SQLite `RunStore`, GitHub Actions/branch-protection wiring, a stale-lease
   reconciler, Discord artifact upload + per-project gate channels, the `#activity`/`#alerts` feeds,
   a `harness status` view, a cockpit GUI).
2. **The code** — `grep -rn "TODO\|FIXME" src` and look for thin spots: missing tests, unhandled
   error paths, edge cases in `loop_runner` / `scheduler` / `coordination`; known loose ends like the
   vestigial `[github].repo` field, the assignee-isn't-a-real-GitHub-user nuance, a missing
   `harness labels-init` / `harness cancel` command.
3. **Memory** — the auto-loaded `dev-harness-milestone-2` note (what's pending to go live: app #2,
   the Mac/launchd install).
4. **Rubric** — `.harness/prompts/arch_review.md` enumerates quality dimensions worth turning into
   tickets (hexagonal integrity, durability, structural safety, coverage, simplicity).
5. **Existing issues** — `gh issue list --repo ParthivNair/dev-harness --state all --limit 100` — so
   you never file a duplicate.

## How to scope each issue

- **One focused change.** Split anything large into separate issues.
- **Title** — imperative, one line. e.g. "Add a `harness status` command", "Add a SQLite RunStore
  behind the RunStore port", "Add a stale-lease reconciler to coordination".
- **Body** — three short sections:
  - **What & why** (1–2 sentences).
  - **Acceptance criteria** — a bullet list of concrete done-conditions, always including
    "unit/integration tests added and `uv run pytest` green".
  - **Pointers** — the relevant files/ports and the existing pattern to follow (e.g. "mirror
    `AtomicJsonRunStore`", "new loop like `loops/arch_review.py`, wire into `_build_runner`").

## Process

1. Do the research above.
2. Draft a **prioritized list** (`sev:high` first): for each, the title, a 2–4 line body, and a
   severity. Aim for **~5–10 high-quality issues**, not dozens.
3. **Show me the list and wait for my go-ahead before filing.** I may cut or edit.
4. On approval, ensure the severity labels exist, then file each:
   ```bash
   gh label create sev:high --color d73a4a 2>/dev/null; gh label create sev:med --color fbca04 2>/dev/null; gh label create sev:low --color 0e8a16 2>/dev/null
   gh issue create --repo ParthivNair/dev-harness --title "<title>" --body "<body>" \
     --label harness:queued --label sev:<level>
   ```
5. Report back the issue numbers + titles you created.

## Guardrails

- **Dedup** against existing open *and* closed issues — never refile an equivalent.
- **No empty/placeholder issues** — every issue must have a real, implementable spec.
- **Do not implement anything** in this session — research and file the queue only.
- Prefer the items already named in the README backlog; only add new findings you can justify.
