Research this repository and file a prioritized backlog of small, well-scoped
GitHub issues — one focused change each — for an autonomous dev loop to claim and
implement. Your job is to fill the work queue with *good* issues; do not implement
anything in this pass.

## Goals & context (what the owner wants)

{{goals}}

## Who consumes these issues (so you scope them correctly)

A `dev_task` loop will claim each issue, feed its **title + body** to Claude as the
task spec, implement it, run the project's test command, gate a human, and open a
**draft PR**. Therefore every issue must be:

- a single, independently-shippable change (a handful of files),
- **testable** — with acceptance criteria the loop can satisfy and a human can verify,
- self-contained — no "go research X" tasks; the spec must say what to build.

## Research first (take notes before writing any issue)

1. **The README / docs** — any "out of scope", "planned", "TODO", or roadmap
   section is an explicit backlog; mine it first.
2. **The code** — look for `TODO`/`FIXME` markers, thin spots (missing tests,
   unhandled error paths, edge cases), and known loose ends.
3. **The tests** — gaps in coverage are concrete, testable issues.
4. **The existing open issues** — so you never file a duplicate.

## How to scope each issue

- **One focused change.** Split anything large into separate issues.
- **Title** — imperative, one line (e.g. "Add a `status` command",
  "Handle the empty-input case in the parser").
- **Severity** — `low`, `med`, or `high`, reflecting value × urgency.
- **Body** — three short sections:
  - **What & why** (1–2 sentences).
  - **Acceptance criteria** — a bullet list of concrete done-conditions, always
    including "tests added and the project's test command is green".
  - **Pointers** — the relevant files/modules and the existing pattern to follow.

## Guardrails

- **Dedup** against existing issues — never refile an equivalent.
- **No empty/placeholder issues** — every issue must have a real, implementable spec.
- **Do not implement anything** in this pass — research and propose the queue only.
- Aim for a handful of high-quality issues, not dozens. Prioritize `high` first.
