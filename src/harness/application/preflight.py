"""Preflight "doctor": is this install actually ready to do real work?

A run spends money (Claude tokens) and writes to GitHub, so before the first
autonomous tick on a new machine or a fresh repo it is worth one cheap pass that
confirms the wiring is *real*, not the credential-free demo defaults:

* the executor is the real :class:`SubprocessExecutor` (not the echo stub),
* the GitHub adapter is the real PyGithub one (not the in-memory fake),
* a token is present AND it actually authenticates (``whoami`` succeeds),
* the ``claude`` CLI is installed and responsive.

This is *diagnostic* application-layer code, so it is allowed to look at the
concrete adapter TYPE NAMES on the container — unlike the engine, which only ever
sees ports. It classifies by class name (a string), never imports an adapter to
branch on, and NEVER prints or returns the token value.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from pydantic import BaseModel

# Type names of the "real" adapters (vs the credential-free demo doubles). Checked
# by name so this stays a pure diagnostic — no adapter import, no engine branching.
_REAL_EXECUTOR = "SubprocessExecutor"
_FAKE_EXECUTOR = "EchoExecutor"
_FAKE_GITHUB = "InMemoryGitHub"


class DoctorReport(BaseModel):
    """The result of :func:`run_doctor` — a flat, printable readiness report.

    ``ok`` is the single go/no-go: True only when every check that gates real work
    passed. ``issues`` collects human-readable, *actionable* remedies (e.g. "run
    ``claude login``") for whatever did not. No secret ever appears here."""

    executor_real: bool
    github_real: bool
    github_token_present: bool
    github_whoami: str | None
    claude_ok: bool             # the `claude` CLI is INSTALLED and responsive
    claude_detail: str
    claude_authenticated: bool | None = None  # None = not probed (opt-in, spends tokens)
    claude_auth_detail: str = ""
    issues: list[str]
    ok: bool


def _type_name(obj: Any) -> str:
    return type(obj).__name__


def check_claude_cli(*, timeout_s: float = 10.0) -> tuple[bool, str]:
    """Is the ``claude`` CLI installed and responsive? Runs ``claude --version``.

    Returns ``(ok, detail)``. Any non-zero exit, missing binary, or timeout is
    treated as not-ready with a message pointing at the fix (``claude login`` /
    install) rather than raising — the doctor must always return a report.
    """
    if shutil.which("claude") is None:
        return False, "the `claude` CLI is not on PATH — install Claude Code"
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"`claude --version` did not respond within {timeout_s:.0f}s"
    except OSError as exc:  # binary vanished between which() and run(), perms, ...
        return False, f"could not run `claude`: {exc}"
    if proc.returncode != 0:
        # Most often: signed out. Point at the concrete remedy.
        detail = (proc.stderr or proc.stdout or "").strip()
        hint = f" ({detail})" if detail else ""
        return False, f"`claude --version` exited {proc.returncode}{hint} — try `claude login`"
    return True, (proc.stdout or "").strip()


# Stderr/stdout markers that mean "the CLI is installed but you are not signed in".
_SIGNED_OUT_MARKERS = ("login", "log in", "sign in", "signed out", "auth", "credential", "unauthor")


def check_claude_auth(*, timeout_s: float = 30.0) -> tuple[bool, str]:
    """Does the installed ``claude`` CLI have an AUTHENTICATED session?

    ``claude --version`` (see :func:`check_claude_cli`) exits 0 even when signed
    out, so presence is not readiness. This makes ONE minimal non-interactive call
    (``claude -p`` with a trivial prompt) and classifies a sign-in/credentials
    error as not-authenticated. It spends a tiny number of tokens, so it is opt-in:
    :func:`run_doctor` only calls it when ``probe_auth=True``. Returns
    ``(authenticated, detail)``.
    """
    if shutil.which("claude") is None:
        return False, "the `claude` CLI is not on PATH"
    try:
        proc = subprocess.run(
            ["claude", "-p", "ok", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"`claude -p` did not respond within {timeout_s:.0f}s"
    except OSError as exc:  # binary vanished / perms
        return False, f"could not run `claude`: {exc}"
    if proc.returncode == 0:
        return True, "authenticated"
    detail = (proc.stderr or proc.stdout or "").strip()
    if any(marker in detail.lower() for marker in _SIGNED_OUT_MARKERS):
        return False, "the `claude` CLI is not signed in — run `claude login`"
    short = f": {detail[:160]}" if detail else ""
    return False, f"`claude -p` exited {proc.returncode}{short}"


def _executor_remedy(container: Any) -> str:
    """The right fix for an echo executor, given the new ``[execution].mode`` knob.

    Since mode decouples the executor from ``github.use_in_memory_fake``, the old
    "flip use_in_memory_fake" advice is wrong when ``mode="echo"`` forces the stub.
    """
    mode = getattr(getattr(container.cfg, "execution", None), "mode", "auto")
    if mode == "echo":
        return (
            f'executor is {_FAKE_EXECUTOR}: [execution].mode is "echo" — set it to '
            f'"real" (or "auto" with github.use_in_memory_fake=false) to use {_REAL_EXECUTOR}'
        )
    return (
        f"executor is {_FAKE_EXECUTOR} (echo demo) — set [execution].mode = \"real\" "
        f"(or github.use_in_memory_fake = false with mode \"auto\") to use {_REAL_EXECUTOR}"
    )


def run_doctor(
    container: Any, *, claude_timeout_s: float = 10.0, probe_auth: bool = False
) -> DoctorReport:
    """Probe a wired :class:`~harness.container.Container` for run-readiness.

    Typed loosely (``Any``) to avoid importing the composition root from the
    application layer; it only reads ``container.executor``, ``container.github``,
    ``container.cfg.github.token``, and ``container.cfg.execution.mode``. Performs
    one live ``whoami`` and one ``claude --version`` — cheap, no money spent. Never
    includes the token value.

    ``claude --version`` proves the CLI is INSTALLED, not signed in (it exits 0 when
    signed out). Pass ``probe_auth=True`` to additionally verify an authenticated
    session via :func:`check_claude_auth` (spends a few tokens), surfacing a
    signed-out machine — the most common real-world failure — as not-ready.
    """
    executor_real = _type_name(container.executor) != _FAKE_EXECUTOR
    github_real = _type_name(container.github) != _FAKE_GITHUB
    token_present = bool(getattr(container.cfg.github, "token", None))

    whoami: str | None = None
    if github_real:
        try:
            whoami = container.github.whoami()
        except Exception:  # noqa: BLE001 — a probe never aborts the report
            whoami = None

    claude_ok, claude_detail = check_claude_cli(timeout_s=claude_timeout_s)

    claude_authed: bool | None = None
    auth_detail = ""
    if probe_auth and claude_ok:
        authed, auth_detail = check_claude_auth()
        claude_authed = authed

    issues: list[str] = []
    if not executor_real:
        issues.append(_executor_remedy(container))
    if not github_real:
        issues.append(
            f"GitHub adapter is {_FAKE_GITHUB} (in-memory fake) — set "
            "github.use_in_memory_fake = false and provide a token"
        )
    if not token_present:
        issues.append("no GitHub token — set HARNESS_GITHUB_TOKEN in the environment")
    elif github_real and not whoami:
        issues.append("GitHub token did not authenticate — check the token and its scopes")
    if not claude_ok:
        issues.append(claude_detail)
    elif claude_authed is False:
        issues.append(auth_detail or "the `claude` CLI is not signed in — run `claude login`")

    ok = not issues
    return DoctorReport(
        executor_real=executor_real,
        github_real=github_real,
        github_token_present=token_present,
        github_whoami=whoami,
        claude_ok=claude_ok,
        claude_detail=claude_detail,
        claude_authenticated=claude_authed,
        claude_auth_detail=auth_detail,
        issues=issues,
        ok=ok,
    )
