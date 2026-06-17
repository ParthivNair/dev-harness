# Sample dev-task prompt

This is a placeholder prompt set that ships with the sample project to show where
a project's prompts live (inside the managed repo, discovered via the project
config's `[prompts]` paths).

A real dev/test loop (a later milestone) would feed a prompt like this to Claude
Code via the Executor:

> Implement the change described in the linked issue. Run the project's tests.
> Open a **draft** PR. Do not touch `main`; do not force-push.
