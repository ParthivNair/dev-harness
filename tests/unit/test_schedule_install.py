"""schedule_install: render_schedule produces the right per-platform recipe, and
install_schedule(dry_run=True) is inert — it renders text but executes nothing.

Pure-text + dry-run by construction, so these need no git, no network, and no OS
scheduler. A guard test monkeypatches subprocess.run to a tripwire to prove the
default dry run never shells out.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from harness.application import schedule_install as si
from harness.application.schedule_install import (
    InstallResult,
    install_schedule,
    render_schedule,
)

HARNESS_CMD = "harness tick"
WORKING_DIR = "/srv/dev-harness"
INTERVAL = 300  # 5 minutes


def _render(platform: str, **overrides) -> str:
    kwargs = dict(
        platform=platform,
        interval_seconds=INTERVAL,
        harness_cmd=HARNESS_CMD,
        working_dir=WORKING_DIR,
    )
    kwargs.update(overrides)
    return render_schedule(**kwargs)


# --------------------------------------------------------------------------- #
# render_schedule — shape per platform
# --------------------------------------------------------------------------- #
def test_windows_renders_schtasks_with_minute_interval() -> None:
    text = _render("windows")
    assert "schtasks" in text
    assert "/sc minute" in text
    assert "/mo 5" in text          # 300s -> 5 minutes
    assert "/f" in text             # re-installable
    assert HARNESS_CMD in text
    assert WORKING_DIR in text
    assert "dev-harness-tick" in text  # default task name


def test_windows_sub_minute_interval_rounds_up_to_one() -> None:
    text = _render("windows", interval_seconds=30)
    assert "/mo 1" in text          # never 0 — schtasks rejects that


def test_darwin_renders_valid_plist_with_start_interval() -> None:
    text = _render("darwin")
    # Parses as XML and is a recognisable plist.
    root = ET.fromstring(text)
    assert root.tag == "plist"
    keys = [el.text for el in root.iter("key")]
    assert "StartInterval" in keys
    assert "ProgramArguments" in keys
    assert "WorkingDirectory" in keys
    # StartInterval is the raw seconds (launchd thinks in seconds, not minutes).
    integers = [el.text for el in root.iter("integer")]
    assert str(INTERVAL) in integers
    # The command was split into ProgramArguments strings, cwd preserved.
    strings = [el.text for el in root.iter("string")]
    assert "harness" in strings and "tick" in strings
    assert WORKING_DIR in strings


def test_linux_renders_a_cron_line() -> None:
    text = _render("linux")
    assert text.startswith("*/5 * * * *")   # every 5 minutes
    assert HARNESS_CMD in text
    assert WORKING_DIR in text
    assert "dev-harness-tick" in text        # tag comment


def test_linux_one_minute_interval_uses_star() -> None:
    text = _render("linux", interval_seconds=60)
    assert text.startswith("* * * * *")


def test_custom_task_name_flows_through() -> None:
    assert "my-task" in _render("windows", task_name="my-task")
    assert "my-task" in _render("darwin", task_name="my-task")
    assert "my-task" in _render("linux", task_name="my-task")


@pytest.mark.parametrize("alias,expected", [("win32", "windows"), ("macos", "darwin")])
def test_platform_aliases_are_accepted(alias: str, expected: str) -> None:
    # win32 / macos resolve to the canonical recipe rather than erroring.
    via_alias = _render(alias)
    via_canonical = _render(expected)
    assert via_alias == via_canonical


def test_unsupported_platform_raises() -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        _render("solaris")


# --------------------------------------------------------------------------- #
# install_schedule(dry_run=True) — renders, never executes
# --------------------------------------------------------------------------- #
def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_k):  # pragma: no cover - only fires on a bug
        raise AssertionError("install_schedule(dry_run=True) must not shell out")

    monkeypatch.setattr(si.subprocess, "run", _boom)


@pytest.mark.parametrize("platform", ["windows", "darwin", "linux"])
def test_dry_run_renders_and_executes_nothing(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_subprocess(monkeypatch)
    result = install_schedule(
        platform=platform,
        interval_seconds=INTERVAL,
        harness_cmd=HARNESS_CMD,
        working_dir=WORKING_DIR,
    )
    assert isinstance(result, InstallResult)
    assert result.dry_run is True
    assert result.applied is False           # nothing was installed
    assert result.platform == platform
    assert result.schedule_text == _render(platform)
    assert result.command                    # the human-runnable command is present
    assert result.instructions               # told what to do next


def test_dry_run_darwin_does_not_write_plist(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_subprocess(monkeypatch)
    result = install_schedule(
        platform="darwin",
        interval_seconds=INTERVAL,
        harness_cmd=HARNESS_CMD,
        working_dir=WORKING_DIR,
        plist_dir=str(tmp_path),
    )
    assert result.plist_path is not None
    # Dry run: the path is reported but nothing is actually written.
    assert not (tmp_path / "dev-harness-tick.plist").exists()
    assert list(tmp_path.iterdir()) == []


def test_dry_run_default_platform_reads_current_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When platform is omitted it falls back to current_platform() (a packaging
    # concern). Pin it deterministically rather than depending on the test host.
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(si, "current_platform", lambda: "linux")
    result = install_schedule(
        interval_seconds=INTERVAL,
        harness_cmd=HARNESS_CMD,
        working_dir=WORKING_DIR,
    )
    assert result.platform == "linux"
    assert result.applied is False
