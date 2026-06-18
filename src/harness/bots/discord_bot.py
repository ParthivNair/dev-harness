"""The always-on Discord bridge bot — a SEPARATE, long-lived process.

It is NOT a Notifier and does not implement the port. It holds the single gateway
websocket, listens for button clicks / modal submits on the gate messages the
:class:`~harness.adapters.notifier.discord.DiscordNotifier` posted, and writes
``<request_id>.response.json`` back into the file inbox — then triggers a resume.
Because the response is a durable file, a bot crash loses nothing: the next
``harness poll`` / ``harness tick`` still resumes the run.

``discord.py`` is imported lazily (the ``discord`` optional extra) so the core
engine and the fakes-only test path never import it. The correlation/payload logic
lives in :mod:`harness.adapters.notifier.discord` as pure, tested functions; this
module is only the gateway glue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from harness.adapters.notifier.discord import parse_custom_id, response_payload
from harness.adapters.notifier.file import FileNotifier


def run_bot(*, token: str, inbox: Path | str, on_answer: Optional[Callable[[], None]] = None) -> None:
    """Connect to Discord and bridge gate interactions to the file inbox forever.

    ``on_answer`` (optional) is invoked after a response file is written, to resume
    the run immediately (else a scheduled ``poll``/``tick`` picks it up later).
    """
    import discord  # lazy: requires `uv sync --extra discord`

    file_notifier = FileNotifier(inbox)

    def _write(request_id: str, payload: dict) -> None:
        file_notifier.write_response_payload(request_id, payload)
        if on_answer is not None:
            on_answer()

    client = discord.Client(intents=discord.Intents.default())

    class ReasonModal(discord.ui.Modal):  # reject-with-reason / reply-with-JSON
        def __init__(self, action: str, request_id: str) -> None:
            super().__init__(title="Reject — reason" if action == "reject" else "Reply (JSON)")
            self._action = action
            self._request_id = request_id
            self.field = discord.ui.TextInput(
                label="Notes" if action == "reject" else "JSON answer",
                required=False if action == "reject" else True,
                style=discord.TextStyle.paragraph,
            )
            self.add_item(self.field)

        async def on_submit(self, interaction: "discord.Interaction") -> None:
            text = str(self.field.value or "")
            if self._action == "reply":
                try:
                    payload = {"answer": json.loads(text), "via": "discord"}
                except json.JSONDecodeError:
                    await interaction.response.send_message("Invalid JSON — not recorded.", ephemeral=True)
                    return
            else:
                payload = response_payload("reject", notes=text)
            _write(self._request_id, payload)
            await interaction.response.send_message("Recorded ✅", ephemeral=True)

    @client.event
    async def on_ready() -> None:  # noqa: D401
        print(f"discord bridge online as {client.user} — watching {file_notifier.inbox}")

    @client.event
    async def on_interaction(interaction: "discord.Interaction") -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("gate:"):
            return
        action, request_id = parse_custom_id(custom_id)
        if action in ("reject", "reply"):
            await interaction.response.send_modal(ReasonModal(action, request_id))
        elif action == "approve":
            _write(request_id, response_payload("approve"))
            await interaction.response.send_message("Approved ✅", ephemeral=True)

    client.run(token)
