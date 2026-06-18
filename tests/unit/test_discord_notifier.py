"""DiscordNotifier: pure schema/correlation helpers + the bridge-to-inbox contract.

No network and no discord.py — the poster is injected as a fake, and the durable
behaviour (write the request file; collect/archive delegate to FileNotifier) is
the part that must be exactly right.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.notifier.discord import (
    DiscordNotifier,
    GatePost,
    components_for_schema,
    parse_custom_id,
    response_payload,
    to_discord_payload,
)
from harness.adapters.notifier.file import FileNotifier
from harness.domain.models import VerificationRequest

BOOL_SCHEMA = {
    "type": "object",
    "properties": {"approved": {"type": "boolean"}, "notes": {"type": "string"}},
    "required": ["approved"],
}


def _request(**kw) -> VerificationRequest:
    base = dict(run_id="run1", step_id="verify#1", prompt="hear the tone?", answer_schema=BOOL_SCHEMA)
    base.update(kw)
    return VerificationRequest(**base)


def test_components_for_boolean_schema_are_approve_reject() -> None:
    comps = components_for_schema(BOOL_SCHEMA, "REQ")
    labels = [c["label"] for c in comps]
    assert labels == ["Approve", "Reject"]
    assert comps[0]["custom_id"] == "gate:approve:REQ"
    assert comps[1]["custom_id"] == "gate:reject:REQ"


def test_components_fallback_to_reply_for_non_boolean() -> None:
    comps = components_for_schema({"type": "object", "properties": {"rating": {"type": "number"}}}, "R")
    assert comps == [{"label": "Reply with JSON", "style": "secondary", "custom_id": "gate:reply:R"}]


def test_parse_custom_id_round_trips() -> None:
    assert parse_custom_id("gate:approve:abc123") == ("approve", "abc123")
    assert parse_custom_id("gate:reply:x:y") == ("reply", "x:y")  # only first two colons split


def test_response_payload_shapes() -> None:
    assert response_payload("approve") == {"answer": {"approved": True}, "via": "discord"}
    assert response_payload("reject", notes="crackle") == {
        "answer": {"approved": False, "notes": "crackle"}, "via": "discord"
    }


def test_to_discord_payload_wraps_buttons_in_an_action_row() -> None:
    post = GatePost(channel_id="C", content="hi", components=components_for_schema(BOOL_SCHEMA, "R"))
    body = to_discord_payload(post)
    assert body["content"] == "hi"
    row = body["components"][0]
    assert row["type"] == 1
    assert {b["style"] for b in row["components"]} == {3, 4}  # success, danger
    assert all(b["type"] == 2 for b in row["components"])


def test_notify_writes_durable_file_then_posts(tmp_path: Path) -> None:
    posted: list[GatePost] = []
    file_n = FileNotifier(tmp_path / "inbox")
    notifier = DiscordNotifier(file_n, poster=posted.append, gates_channel_id="GATES")
    req = _request()

    notifier.notify(req)

    # The durable request file exists (written BEFORE the post).
    assert (tmp_path / "inbox" / f"{req.request_id}.request.json").exists()
    # And the post went to the gates channel with correlating buttons.
    assert len(posted) == 1
    assert posted[0].channel_id == "GATES"
    assert posted[0].request_id == req.request_id
    assert posted[0].components[0]["custom_id"] == f"gate:approve:{req.request_id}"


def test_notify_survives_poster_failure(tmp_path: Path) -> None:
    def boom(_post: GatePost):
        raise OSError("discord down")

    file_n = FileNotifier(tmp_path / "inbox")
    notifier = DiscordNotifier(file_n, poster=boom, gates_channel_id="GATES")
    req = _request()
    notifier.notify(req)  # must not raise — the gate is still durable
    assert (tmp_path / "inbox" / f"{req.request_id}.request.json").exists()


def test_collect_reads_a_bridged_response(tmp_path: Path) -> None:
    file_n = FileNotifier(tmp_path / "inbox")
    notifier = DiscordNotifier(file_n, poster=lambda p: None, gates_channel_id="GATES")
    req = _request()
    notifier.notify(req)

    # The bot writes a minimal {answer, via} payload; collect() backfills + validates shape.
    file_n.write_response_payload(req.request_id, response_payload("approve"))
    resp = notifier.collect(req)
    assert resp is not None
    assert resp.answer["approved"] is True  # `approved` convenience is set later, by resume()
    assert resp.request_id == req.request_id and resp.run_id == req.run_id  # backfilled
    assert resp.via == "discord"
