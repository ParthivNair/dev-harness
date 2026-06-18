"""The local observer dashboard — a *driving* adapter over the composition root.

A tiny FastAPI server that reads the durable run state and serves a no-build
single-page UI. It calls the same :mod:`harness.operations` use-cases the CLI
does, so it never re-implements engine logic. Optional: needs the ``web`` extra.
"""
