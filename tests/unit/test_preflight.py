"""Preflight doctor: is the install actually ready to spend money on a real run?

Hermetic — no network, no real ``claude`` binary. The container is a tiny
``SimpleNamespace`` stand-in (the integrator wires the real one), and the
``claude --version`` probe is monkeypatched, so the readiness *classification*
is what's under test, not the host's toolchain.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from harness.adapters.executor.echo import EchoExecutor
from harness.adapters.executor.subprocess_executor import SubprocessExecutor
from harness.adapters.github.fake import InMemoryGitHub
from harness.application import preflight


def _container(*, executor, github, token):
    """A minimal duck-typed stand-in for the Container the doctor reads from."""
    return SimpleNamespace(
        executor=executor,
        github=github,
        cfg=SimpleNamespace(github=SimpleNamespace(token=token)),
    )


def _real_github():
    """A real-typed GitHub adapter whose whoami succeeds, with no network.

    PyGithubAdapter.__init__ would build a client, so fabricate one without calling
    it — the doctor only checks the class NAME and calls whoami()."""
    from harness.adapters.github.pygithub_adapter import PyGithubAdapter

    gh = PyGithubAdapter.__new__(PyGithubAdapter)
    gh.whoami = lambda: "octocat"  # type: ignore[method-assign]
    return gh


def _patch_claude(monkeypatch, *, ok: bool, detail: str = "claude 1.2.3") -> None:
    monkeypatch.setattr(preflight, "check_claude_cli", lambda **_: (ok, detail))


# --------------------------------------------------------------------------- #
# run_doctor: end-to-end classification
# --------------------------------------------------------------------------- #
def test_doctor_reports_not_ready_for_the_demo_defaults(monkeypatch) -> None:
    _patch_claude(monkeypatch, ok=False, detail="run `claude login`")
    report = preflight.run_doctor(
        _container(executor=EchoExecutor(), github=InMemoryGitHub(), token=None)
    )
    assert not report.ok
    assert not report.executor_real
    assert not report.github_real
    assert not report.github_token_present
    assert report.github_whoami is None
    assert not report.claude_ok
    # every failing dimension contributes an actionable issue
    assert len(report.issues) == 4
    assert any("claude login" in i for i in report.issues)


def test_doctor_reports_ready_when_all_real_types_present(monkeypatch) -> None:
    _patch_claude(monkeypatch, ok=True, detail="claude 1.2.3")
    container = _container(
        executor=SubprocessExecutor(project_root_resolver=lambda pid: None),
        github=_real_github(),
        token="ghp_fake",
    )
    report = preflight.run_doctor(container)
    assert report.ok
    assert report.executor_real
    assert report.github_real
    assert report.github_token_present
    assert report.github_whoami == "octocat"
    assert report.claude_ok
    assert report.issues == []


def test_doctor_flags_token_present_but_unauthenticated(monkeypatch) -> None:
    _patch_claude(monkeypatch, ok=True)
    gh = _real_github()
    gh.whoami = lambda: None  # token present but does not authenticate
    container = _container(
        executor=SubprocessExecutor(project_root_resolver=lambda pid: None),
        github=gh,
        token="ghp_bad",
    )
    report = preflight.run_doctor(container)
    assert not report.ok
    assert report.github_token_present  # the token IS set...
    assert report.github_whoami is None  # ...but it didn't authenticate
    assert any("did not authenticate" in i for i in report.issues)


def test_doctor_never_includes_the_token_value(monkeypatch) -> None:
    _patch_claude(monkeypatch, ok=True)
    secret = "ghp_SUPERSECRET_must_not_leak"
    container = _container(
        executor=EchoExecutor(), github=InMemoryGitHub(), token=secret
    )
    report = preflight.run_doctor(container)
    blob = report.model_dump_json() + " ".join(report.issues)
    assert secret not in blob


def test_doctor_whoami_exception_is_swallowed(monkeypatch) -> None:
    _patch_claude(monkeypatch, ok=True)
    gh = _real_github()

    def _boom():
        raise RuntimeError("network down")

    gh.whoami = _boom  # type: ignore[method-assign]
    container = _container(
        executor=SubprocessExecutor(project_root_resolver=lambda pid: None),
        github=gh,
        token="ghp_fake",
    )
    report = preflight.run_doctor(container)  # must not raise
    assert report.github_whoami is None
    assert not report.ok


# --------------------------------------------------------------------------- #
# check_claude_cli: the subprocess probe in isolation
# --------------------------------------------------------------------------- #
def test_check_claude_cli_missing_binary(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
    ok, detail = preflight.check_claude_cli()
    assert not ok
    assert "not on PATH" in detail


def test_check_claude_cli_responsive(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/bin/claude")

    def _run(*_a, **_k):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="claude 9.9.9\n", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", _run)
    ok, detail = preflight.check_claude_cli()
    assert ok
    assert detail == "claude 9.9.9"


def test_check_claude_cli_signed_out_points_at_login(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/bin/claude")

    def _run(*_a, **_k):
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="not logged in")

    monkeypatch.setattr(preflight.subprocess, "run", _run)
    ok, detail = preflight.check_claude_cli()
    assert not ok
    assert "claude login" in detail
    assert "not logged in" in detail


def test_check_claude_cli_timeout(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/bin/claude")

    def _run(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="claude --version", timeout=10)

    monkeypatch.setattr(preflight.subprocess, "run", _run)
    ok, detail = preflight.check_claude_cli()
    assert not ok
    assert "did not respond" in detail
