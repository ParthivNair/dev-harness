"""Install the already-idempotent ``harness tick`` onto an OS scheduler.

``harness tick`` is one short-lived, idempotent scheduling pass (see
:class:`~harness.application.scheduler.Scheduler.tick`); to run the harness
unattended a supervisor must invoke it on a cadence. The README documents the
Task Scheduler / launchd / cron recipes as manual prose — this module turns that
prose into a one-shot installer.

Two layers, kept apart on purpose:

* :func:`render_schedule` is pure text. Given a platform name it returns the
  ``schtasks`` command line (Windows), the launchd ``.plist`` XML (darwin) or the
  crontab line (linux) — no side effects, deterministically testable.
* :func:`install_schedule` is the side-effecting wrapper. By default it is a
  dry run: it renders the text and the exact command a human would run, and
  executes nothing. With ``dry_run=False`` it actually creates the Windows
  scheduled task via ``schtasks``; on darwin/linux it writes the ``.plist`` (or
  hands back the crontab line) and returns instructions rather than editing the
  user's crontab for them.

The engine never branches on the live OS — platform is always an explicit
argument. The one concession is :func:`current_platform`, whose default reads
``platform.system()``; that is a *packaging* concern local to this install
utility (which machine am I installing onto?), not engine logic, and callers may
always pass the platform explicitly.
"""

from __future__ import annotations

import platform as _platform
import subprocess
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

_DEFAULT_TASK_NAME = "dev-harness-tick"

# The platform names render_schedule understands, keyed off platform.system().lower().
_WINDOWS = "windows"
_DARWIN = "darwin"
_LINUX = "linux"


def current_platform() -> str:
    """The platform name this machine installs as, e.g. ``"windows"``/``"darwin"``/
    ``"linux"``. A packaging concern (which supervisor am I targeting?), so reading
    ``platform.system()`` here is fine — engine code passes platform explicitly."""
    return _platform.system().lower()


class InstallResult(BaseModel):
    """The outcome of an :func:`install_schedule` call — plain data for the CLI.

    Always carries the rendered ``schedule_text`` and the ``command`` a human can
    run by hand. ``applied`` is True only when a non-dry-run actually created the
    task; ``instructions`` carries the human follow-up (darwin/linux never auto
    -edit the crontab / load the agent)."""

    platform: str
    task_name: str
    schedule_text: str
    command: str
    applied: bool = False
    dry_run: bool = True
    plist_path: Optional[str] = None
    instructions: list[str] = Field(default_factory=list)


def _normalize_platform(platform: str) -> str:
    p = platform.strip().lower()
    if p in {_WINDOWS, "win32"}:
        return _WINDOWS
    if p in {_DARWIN, "macos", "mac", "osx"}:
        return _DARWIN
    if p == _LINUX:
        return _LINUX
    raise ValueError(
        f"unsupported platform {platform!r}: expected one of "
        f"'{_WINDOWS}', '{_DARWIN}', '{_LINUX}'"
    )


def _interval_minutes(interval_seconds: int) -> int:
    """schtasks/cron think in whole minutes; round up so a sub-minute interval still
    fires (and never becomes 0, which schtasks rejects)."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    return max(1, (interval_seconds + 59) // 60)


def _windows_action(*, harness_cmd: str, working_dir: str) -> str:
    """The ``/tr`` action: ``cmd /c cd /d <dir> && <harness tick>`` so the task runs
    in the project directory (where .env / config live). Kept as ONE string with no
    inner quotes — schtasks treats the whole ``/tr`` value as the action, and a
    nested ``cmd /c "..."`` would collide with the outer quoting on both the display
    line and the argv. Works as long as the dir/command carry no spaces (true for a
    repo checkout path on Windows and the ``harness tick`` verb)."""
    return f"cmd /c cd /d {working_dir} && {harness_cmd}"


def _windows_argv(*, interval_seconds: int, harness_cmd: str, working_dir: str, task_name: str) -> list[str]:
    """The schtasks invocation as a ready-to-run argv (shell=False). Source of truth
    for both the executed command and the rendered display line, so the two never
    drift — and there is no quoted line to re-parse."""
    every = _interval_minutes(interval_seconds)
    return [
        "schtasks", "/create",
        "/tn", task_name,
        "/tr", _windows_action(harness_cmd=harness_cmd, working_dir=working_dir),
        "/sc", "minute", "/mo", str(every),
        "/rl", "LIMITED", "/f",
    ]


def _render_windows(
    *, interval_seconds: int, harness_cmd: str, working_dir: str, task_name: str
) -> str:
    # The human-runnable display line. Quote exactly the two fields that contain
    # spaces (the task name may; the action does) so a paste into a shell parses the
    # same argv we run programmatically. /sc minute /mo N is the cadence; /rl LIMITED
    # + /f keep it re-installable.
    argv = _windows_argv(
        interval_seconds=interval_seconds,
        harness_cmd=harness_cmd,
        working_dir=working_dir,
        task_name=task_name,
    )
    return " ".join(_quote_if_spaced(a) for a in argv)


def _quote_if_spaced(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or "\t" in arg) else arg


def _render_darwin(
    *, interval_seconds: int, harness_cmd: str, working_dir: str, task_name: str
) -> str:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    # launchd runs the program directly (no shell), so split the command into a
    # ProgramArguments array and set WorkingDirectory + StartInterval (seconds).
    args = harness_cmd.split()
    program_args = "\n".join(f"        <string>{a}</string>" for a in args)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{task_name}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{program_args}\n"
        "    </array>\n"
        "    <key>WorkingDirectory</key>\n"
        f"    <string>{working_dir}</string>\n"
        "    <key>StartInterval</key>\n"
        f"    <integer>{interval_seconds}</integer>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _render_linux(
    *, interval_seconds: int, harness_cmd: str, working_dir: str, task_name: str
) -> str:
    every = _interval_minutes(interval_seconds)
    # A crontab line: `*/N * * * * cd <dir> && <harness tick>`. The trailing comment
    # tags the line so a human can find (and remove) it. cron has no sub-minute
    # resolution, so the interval is rounded up to whole minutes.
    schedule = "* * * * *" if every == 1 else f"*/{every} * * * *"
    return f"{schedule} cd {working_dir} && {harness_cmd}  # {task_name}"


def render_schedule(
    *,
    platform: str,
    interval_seconds: int,
    harness_cmd: str,
    working_dir: str,
    task_name: str = _DEFAULT_TASK_NAME,
) -> str:
    """Render the scheduler recipe for ``platform`` as text — no side effects.

    * ``windows`` -> a ``schtasks /create ... /sc minute /mo N`` command line that
      runs ``harness_cmd`` in ``working_dir`` (re-installable via ``/f``).
    * ``darwin``  -> a launchd ``.plist`` XML with ``StartInterval`` (seconds) and
      ``WorkingDirectory``.
    * ``linux``   -> a single crontab line at the rounded-up minute cadence.

    ``platform`` is an explicit argument; this function never inspects the live OS.
    """
    p = _normalize_platform(platform)
    if p == _WINDOWS:
        render = _render_windows
    elif p == _DARWIN:
        render = _render_darwin
    else:
        render = _render_linux
    return render(
        interval_seconds=interval_seconds,
        harness_cmd=harness_cmd,
        working_dir=working_dir,
        task_name=task_name,
    )


def install_schedule(
    *,
    platform: Optional[str] = None,
    interval_seconds: int,
    harness_cmd: str,
    working_dir: str,
    task_name: str = _DEFAULT_TASK_NAME,
    plist_dir: Optional[str] = None,
    dry_run: bool = True,
) -> InstallResult:
    """Render — and, unless ``dry_run``, install — the OS schedule for ``harness tick``.

    ``dry_run`` (the default) executes nothing: it returns the rendered text plus
    the exact command a human would run. With ``dry_run=False``:

    * **windows** — actually runs the ``schtasks`` command via ``subprocess`` and
      reports whether it took (``applied``).
    * **darwin**  — writes the ``.plist`` to ``plist_dir`` (default
      ``~/Library/LaunchAgents``) and returns the ``launchctl load`` instruction;
      it never loads the agent for you.
    * **linux**   — never edits the crontab; it returns the line and the
      ``crontab -e`` instruction for the human to paste.

    ``platform`` defaults to :func:`current_platform` (a packaging concern) but may
    always be passed explicitly.
    """
    p = _normalize_platform(platform if platform is not None else current_platform())
    schedule_text = render_schedule(
        platform=p,
        interval_seconds=interval_seconds,
        harness_cmd=harness_cmd,
        working_dir=working_dir,
        task_name=task_name,
    )

    if p == _WINDOWS:
        return _install_windows(
            schedule_text=schedule_text,
            interval_seconds=interval_seconds,
            harness_cmd=harness_cmd,
            working_dir=working_dir,
            task_name=task_name,
            dry_run=dry_run,
        )
    if p == _DARWIN:
        return _install_darwin(
            schedule_text=schedule_text,
            task_name=task_name,
            plist_dir=plist_dir,
            dry_run=dry_run,
        )
    return _install_linux(
        schedule_text=schedule_text, task_name=task_name, dry_run=dry_run
    )


def _install_windows(
    *,
    schedule_text: str,
    interval_seconds: int,
    harness_cmd: str,
    working_dir: str,
    task_name: str,
    dry_run: bool,
) -> InstallResult:
    result = InstallResult(
        platform=_WINDOWS,
        task_name=task_name,
        schedule_text=schedule_text,
        command=schedule_text,  # the schtasks line IS the command
        dry_run=dry_run,
    )
    if dry_run:
        result.instructions = [
            "Dry run: nothing was installed. Re-run with apply to create the task,",
            "or run the command above yourself in an elevated shell.",
        ]
        return result
    # Real install: rebuild the SAME argv the display line came from (shell=False, so
    # the spaced /tr action stays one argument) rather than re-parsing the quoted
    # line. The /f flag makes this re-runnable.
    argv = _windows_argv(
        interval_seconds=interval_seconds,
        harness_cmd=harness_cmd,
        working_dir=working_dir,
        task_name=task_name,
    )
    proc = subprocess.run(argv, shell=False, capture_output=True, text=True)
    result.applied = proc.returncode == 0
    result.instructions = [
        (proc.stdout or proc.stderr).strip()
        or f"schtasks exited {proc.returncode}"
    ]
    return result


def _install_darwin(
    *, schedule_text: str, task_name: str, plist_dir: Optional[str], dry_run: bool
) -> InstallResult:
    target_dir = Path(plist_dir) if plist_dir else Path.home() / "Library" / "LaunchAgents"
    plist_path = target_dir / f"{task_name}.plist"
    load_cmd = f"launchctl load {plist_path}"
    result = InstallResult(
        platform=_DARWIN,
        task_name=task_name,
        schedule_text=schedule_text,
        command=load_cmd,
        plist_path=str(plist_path),
        dry_run=dry_run,
    )
    if dry_run:
        result.instructions = [
            f"Dry run: nothing was written. The plist would go to {plist_path};",
            f"after writing it, load it with: {load_cmd}",
        ]
        return result
    # Write the plist; we never load it for the user (loading touches their session).
    target_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(schedule_text, encoding="utf-8")
    result.applied = True
    result.instructions = [
        f"Wrote {plist_path}.",
        f"Activate it with: {load_cmd}",
    ]
    return result


def _install_linux(
    *, schedule_text: str, task_name: str, dry_run: bool
) -> InstallResult:
    # We deliberately never edit the user's crontab — hand back the line + how to add it.
    add_cmd = "crontab -e"
    result = InstallResult(
        platform=_LINUX,
        task_name=task_name,
        schedule_text=schedule_text,
        command=add_cmd,
        dry_run=dry_run,
        instructions=[
            "This installer never edits your crontab. Add the line above by running:",
            f"  {add_cmd}",
            "and pasting it in (the trailing comment tags it for later removal).",
        ],
    )
    return result
