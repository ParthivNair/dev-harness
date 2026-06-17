"""FileNotifier: the canonical exit-and-resume notifier.

``notify`` writes ``<inbox>/<request_id>.request.json`` and returns; the runner
persists WAITING and the process exits. The answer arrives out of band as
``<inbox>/<request_id>.response.json`` (dropped by a human, the ``answer`` CLI
command, a poller, or — later — a Discord bridge). ``collect`` is a non-blocking
check for that file. A Discord notifier is the same shape: publish elsewhere,
bridge replies back into a response.

``interactive = False`` so the runner never blocks on it; resumption is always a
separate :meth:`LoopRunner.resume` call.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from harness.domain.models import VerificationRequest, VerificationResponse, utcnow_iso
from harness.util.atomic import atomic_write_text


class FileNotifier:
    interactive = False

    def __init__(self, inbox: Path | str, *, log_path: Optional[Path | str] = None) -> None:
        self.inbox = Path(inbox).expanduser()
        self.done_dir = self.inbox / "done"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.log_path = Path(log_path).expanduser() if log_path else None

    def _request_path(self, request_id: str) -> Path:
        return self.inbox / f"{request_id}.request.json"

    def _response_path(self, request_id: str) -> Path:
        return self.inbox / f"{request_id}.response.json"

    def notify(self, request: VerificationRequest) -> None:
        atomic_write_text(self._request_path(request.request_id), request.model_dump_json(indent=2))
        if self.log_path is not None:
            line = (
                f"{utcnow_iso()} WAITING run={request.run_id} request={request.request_id} "
                f":: {request.prompt}\n"
            )
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line)

    def collect(self, request: VerificationRequest) -> Optional[VerificationResponse]:
        path = self._response_path(request.request_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text("utf-8"))
        raw.setdefault("request_id", request.request_id)
        raw.setdefault("run_id", request.run_id)
        raw.setdefault("step_id", request.step_id)
        raw.setdefault("via", "file")
        return VerificationResponse.model_validate(raw)

    def write_response(self, response: VerificationResponse) -> Path:
        """Persist an answer as a response file (used by the ``answer`` command)."""
        path = self._response_path(response.request_id)
        atomic_write_text(path, response.model_dump_json(indent=2))
        return path

    def archive(self, request_id: str) -> None:
        """Move a consumed request/response pair into ``inbox/done/``."""
        self.done_dir.mkdir(parents=True, exist_ok=True)
        for path in (self._request_path(request_id), self._response_path(request_id)):
            if path.exists():
                shutil.move(str(path), str(self.done_dir / path.name))
