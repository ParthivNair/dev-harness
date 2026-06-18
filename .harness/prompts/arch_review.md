# dev-harness — architecture review rubric

Review this codebase against the rubric below and return **structured findings only**
(the call is JSON-Schema-constrained: a list of `{title, severity, rationale}`). No praise,
no speculation — each finding must be concrete and actionable, and become a good queued
ticket for the dev loop. If nothing needs action, return an empty list.

Rubric:

1. **Hexagonal integrity.** Does any engine/application code import a concrete adapter or
   branch on the OS/backend? Ports must stay `typing.Protocol`; wiring lives only in the
   composition root (`cli/main.py`).
2. **Durability & idempotency.** Are run/ledger writes atomic (temp + `os.replace`)? Do
   steps tolerate at-least-once execution? Is any resume position a closure/stack frame
   instead of plain data (`current_step` + `data`)?
3. **Structural safety.** Is there still no merge/push/force-push method on the GitHub port?
   Does `open_draft_pr` stay hard-wired to `draft=True`? Are forbidden actions unreachable?
4. **Circuit breakers & spend.** Are per-run caps and the global spend window both enforced
   and persisted across crashes? Any path that could reset a budget or escape a cap?
5. **Coordination.** Is the issue lease (owner label + confirm-read) race-safe? Any
   double-claim or stuck-lease hazard?
6. **Test coverage.** Are new behaviors covered with in-memory fakes (the production default
   wiring)? Any untested edge in suspend/resume, the loops, the scheduler, or coordination?
7. **Simplicity.** Dead code, needless abstraction, or duplication worth removing?
