# Dev task

You are working inside this repository, implementing one queued issue.

Constraints (the harness enforces these structurally; honor them anyway):

- Implement the change described in the linked issue. Keep it focused — one issue, one change.
- Follow the existing architecture and conventions. Read the neighboring code first and match
  its structure, layering, and idioms rather than introducing a new pattern. Prefer reusing
  existing utilities over adding new ones.
- Match the surrounding code's style, comment density, and naming.
- Run the project's tests (the harness uses the project's configured test command). The suite
  must stay green. If you change behavior, add or update tests in the same style as the existing
  ones (prefer the project's existing test patterns — in-memory fakes over network/integration
  where the project does so).
- Do **not** touch the default branch, do **not** `git commit`, do **not** `git push` or
  force-push. Edit the working tree only; the harness publishes the feature branch and a human
  reviews it before it merges.
- If the build or tests fail, you'll be re-invoked with the failure log — fix the root cause,
  don't paper over it.
