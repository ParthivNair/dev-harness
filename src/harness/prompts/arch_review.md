# Architecture review rubric

Review this codebase against the rubric below and return **structured findings only**
(the call is JSON-Schema-constrained: a list of `{title, severity, rationale}`). No praise,
no speculation — each finding must be concrete and actionable, and become a good queued
ticket for the dev loop. If nothing needs action, return an empty list.

Rubric:

1. **Architectural integrity.** Does the code honor the project's intended layering and
   boundaries — or does a leak (a low-level dependency reaching into core logic, a concern
   bleeding across a boundary the project means to keep clean) erode it? Wiring/composition
   should stay where the project puts it, not scatter through the codebase.
2. **Durability & idempotency.** Are writes to persistent state safe against partial failure
   (e.g. write-temp-then-replace rather than truncate-in-place)? Do steps that may run more
   than once tolerate it without corruption or double effects? Is recoverable progress stored
   as plain data rather than in-flight control state?
3. **Structural safety.** Are irreversible or dangerous operations gated, opt-in, or simply
   unreachable by default, rather than a step away by accident? Does the code fail safe when a
   signal is missing or ambiguous?
4. **Resource limits.** Are loops, retries, and any spend or rate budgets bounded and enforced,
   and do those limits survive a restart? Is there any path that resets or escapes a cap?
5. **Coordination.** Where multiple actors or runs share state, is access race-safe? Any
   double-claim, lost-update, or stuck-lock hazard?
6. **Test coverage.** Are new behaviors covered by tests in the project's existing style? Any
   untested edge in error handling, retries, or concurrent paths?
7. **Simplicity.** Dead code, needless abstraction, or duplication worth removing?
