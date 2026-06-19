# dev-harness — backlog triage rubric

Triage the open `harness:queued` backlog below and return **structured judgements only**
(the call is JSON-Schema-constrained: a list of `{number, severity, effort, depends_on,
rationale}`). One judgement per issue — do not invent, merge, or split issues, and never
rewrite an issue body. Your labels feed the deterministic claimer, which orders work by
severity first, then prefers lower effort; keep judgement (here) and ordering (the claimer)
cleanly separated.

For each issue decide:

1. **severity** — `high` | `med` | `low`.
   - `high`: a correctness/safety/data-loss bug, a broken build/test, or a structural-safety
     or hexagonal-integrity violation (an engine importing a concrete adapter, a reachable
     forbidden action, a non-atomic durable write).
   - `med`: a real but contained defect, missing test coverage on a new behavior, or a
     coordination/idempotency hazard that has not yet bitten.
   - `low`: cleanup, dead code, docs, or a nice-to-have refactor.
2. **effort** — `s` | `m` | `l` (small | medium | large) for the change to land safely with
   tests. Prefer `s` for a localized one-file fix; reserve `l` for cross-cutting work.
3. **depends_on** — issue numbers this one must follow (it references `Depends on #N` /
   `Sequencing` in its body, or is logically blocked by another queued issue). The claimer
   will not start an issue while any dependency is still open, so be accurate.
4. **rationale** — one terse line explaining the severity/effort call (posted as a comment).

Be decisive and balanced: favor quick, high-value wins. If the queue is empty, return an
empty `judgements` list.
