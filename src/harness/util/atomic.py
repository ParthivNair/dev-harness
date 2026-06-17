"""Cross-platform atomic file writes.

The durable-state guarantee rests on one trick: write a temp file in the *same
directory*, fsync it, then ``os.replace`` it over the target. ``os.replace`` is
atomic on POSIX and Windows alike and overwrites an existing destination — unlike
``os.rename``, which raises on Windows when the target exists. A reader therefore
always sees either the old file or the new one, never a torn write.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically (re)write ``path`` with ``text``. Creates parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on both POSIX and Windows
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    else:
        _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Best-effort durable flush of the rename. POSIX-only; a no-op on Windows,
    where fsync on a directory handle is unsupported."""
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, AttributeError):
        pass
