# dev-harness — dev task

You are working inside the `dev-harness` repository, implementing one queued issue.

Constraints (the harness enforces these structurally; honor them anyway):

- Implement the change described in the linked issue. Keep it focused — one issue, one change.
- Follow the existing architecture: hexagonal ports & adapters. The engine depends only
  on ports (`typing.Protocol`); platform-specifics live in adapters/config, never as
  `if platform == ...` in the engine.
- Match the surrounding code's style, comment density, and idioms. Prefer reusing existing
  utilities over adding new ones.
- Run the project's tests (`uv run pytest`). The suite must stay green — it is the safety
  net for self-modification. If you change behavior, add or update tests in the same style
  as the existing ones (in-memory fakes, no network).
- Do **not** touch `main`, do **not** `git commit`, do **not** `git push` or force-push.
  Edit the working tree only; the harness opens a **draft** PR for a human to review and merge.
- If the build or tests fail, you'll be re-invoked with the failure log — fix the root cause,
  don't paper over it.
