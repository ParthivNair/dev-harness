"""DiscordNotifier: post a gate to a Discord channel, bridging TO the file inbox.

Discord is a *transport* over the durable substrate, not a replacement for it.
``notify`` writes the ``<request_id>.request.json`` file FIRST (so a gate can never
be lost to a network error), then posts a message with Approve/Reject buttons to
``#verification-gates``. The human's click is handled by the separate always-on
bot (:mod:`harness.bots.discord_bot`), which writes ``<request_id>.response.json``
back into the same inbox — so ``collect``/``archive`` are just the FileNotifier's.

``notify`` must not block (exit-and-resume), so the post is a single synchronous
REST call (stdlib ``urllib`` — no ``discord.py``, no gateway). Only the bot needs
``discord.py``. If the post fails, the gate is still answerable via the CLI or the
file inbox; Discord is strictly additive.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from harness.adapters.notifier.file import FileNotifier
from harness.domain.models import VerificationRequest, VerificationResponse

# Discord button styles (wire values).
_STYLES = {"primary": 1, "secondary": 2, "success": 3, "danger": 4}


@dataclass
class GatePost:
    """The transport-neutral shape of a gate message, handed to a Poster."""

    channel_id: str
    content: str
    components: list[dict] = field(default_factory=list)  # {label, style, custom_id}
    artifact_path: Optional[str] = None
    request_id: str = ""
    run_id: str = ""


Poster = Callable[[GatePost], Optional[str]]  # posts, returns a message id (or None)


# --------------------------------------------------------------------------- #
# Pure helpers (fully unit-tested; shared with the bot)
# --------------------------------------------------------------------------- #
def components_for_schema(answer_schema: Optional[dict], request_id: str) -> list[dict]:
    """Map a JSON Schema answer shape to button specs whose ``custom_id`` carries
    the ``request_id`` (so a click correlates to its request with zero extra state).

    The common ``{approved: boolean, notes?: string}`` shape -> Approve / Reject
    (the bot opens a modal on Reject to capture the reason, which becomes the next
    iteration's context). Anything else -> a reply-protocol button (the human
    replies a JSON object) — the universal escape hatch, mirroring a hand-edited
    response file.
    """
    props = (answer_schema or {}).get("properties", {})
    if props.get("approved", {}).get("type") == "boolean":
        return [
            {"label": "Approve", "style": "success", "custom_id": f"gate:approve:{request_id}"},
            {"label": "Reject", "style": "danger", "custom_id": f"gate:reject:{request_id}"},
        ]
    return [{"label": "Reply with JSON", "style": "secondary", "custom_id": f"gate:reply:{request_id}"}]


def parse_custom_id(custom_id: str) -> tuple[str, str]:
    """``"gate:approve:<id>"`` -> ``("approve", "<id>")``."""
    _, action, request_id = custom_id.split(":", 2)
    return action, request_id


def response_payload(action: str, *, notes: str = "") -> dict:
    """The minimal ``{answer, via}`` payload the bot writes as the response file.
    ``collect()`` backfills request_id/run_id/step_id; ``resume()`` validates it."""
    answer: dict[str, object] = {"approved": action == "approve"}
    if notes:
        answer["notes"] = notes
    return {"answer": answer, "via": "discord"}


def format_gate_message(request: VerificationRequest) -> str:
    lines = [
        "**Verification gate** — your perception is required",
        request.prompt,
        f"`run {request.run_id} · request {request.request_id}`",
    ]
    if request.artifact_path:
        lines.append(f"artifact: `{request.artifact_path}`")
    return "\n".join(lines)


def to_discord_payload(post: GatePost) -> dict:
    """Render a GatePost into a Discord create-message body (content + buttons)."""
    body: dict = {"content": post.content}
    if post.components:
        body["components"] = [{
            "type": 1,  # action row
            "components": [
                {
                    "type": 2,  # button
                    "style": _STYLES.get(c.get("style", "secondary"), 2),
                    "label": c["label"],
                    "custom_id": c["custom_id"],
                }
                for c in post.components
            ],
        }]
    return body


# --------------------------------------------------------------------------- #
# Production poster (best-effort synchronous REST; not exercised in tests)
# --------------------------------------------------------------------------- #
class DiscordRestPoster:
    """Posts a channel message via Discord's REST API using the bot token.

    Synchronous and best-effort: a failure raises, and DiscordNotifier swallows it
    (the gate is already durable in the file inbox). Artifact upload (multipart) is
    intentionally left to a later refinement — the artifact path is included as text.
    """

    API = "https://discord.com/api/v10"

    def __init__(self, *, token: str, timeout: float = 10.0) -> None:
        self._token = token
        self._timeout = timeout

    def __call__(self, post: GatePost) -> Optional[str]:
        data = json.dumps(to_discord_payload(post)).encode("utf-8")
        req = urllib.request.Request(
            f"{self.API}/channels/{post.channel_id}/messages",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "dev-harness (https://github.com/ParthivNair/dev-harness, 0.1)",
            },
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        return payload.get("id")


# --------------------------------------------------------------------------- #
# The notifier
# --------------------------------------------------------------------------- #
class DiscordNotifier:
    interactive = False

    def __init__(
        self,
        file_notifier: FileNotifier,
        *,
        poster: Poster,
        gates_channel_id: str,
    ) -> None:
        self._file = file_notifier
        self._poster = poster
        self._gates_channel = gates_channel_id

    def notify(self, request: VerificationRequest) -> None:
        # 1. Durable record FIRST — a REST failure can never lose a gate.
        self._file.notify(request)
        # 2. Best-effort post to #verification-gates.
        post = GatePost(
            channel_id=self._gates_channel,
            content=format_gate_message(request),
            components=components_for_schema(request.answer_schema, request.request_id),
            artifact_path=request.artifact_path,
            request_id=request.request_id,
            run_id=request.run_id,
        )
        try:
            self._poster(post)
        except (urllib.error.URLError, OSError, ValueError):
            pass  # gate stays answerable via CLI / file inbox

    def warn(self, message: str) -> None:
        # Durable capture via the inner file notifier; a chat surface is optional.
        self._file.warn(message)

    # collect / write_response / archive are the durable substrate, unchanged.
    def collect(self, request: VerificationRequest) -> Optional[VerificationResponse]:
        return self._file.collect(request)

    def write_response(self, response: VerificationResponse):
        return self._file.write_response(response)

    def archive(self, request_id: str) -> None:
        self._file.archive(request_id)
