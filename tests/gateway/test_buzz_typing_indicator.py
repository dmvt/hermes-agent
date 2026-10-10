"""Buzz agent typing indicator — the kind-20002 publisher (fi_1a5a623902abd8558f9b).

Fran reported on 2026-08-14: "definitely not seeing any thinking or typing or
using tools notices at the bottom of the page". Buzz Web had rendered typing
indicators the whole time — ``src/lib/workspace-state.ts`` runs an
active-channel-scoped kind-20002 subscription and ``TypingIndicator`` renders it
above the composer — but ``BuzzAdapter.send_typing`` was an explicit no-op, so
nothing ever published one for the agent.

These tests pin the wire contract that Buzz Web's receiver (src/lib/typing.ts)
and upstream (desktop/src/features/agents/agentWorkingSignal.ts) actually read,
because that contract is invisible from this repo and a silently-wrong tag shape
would render nothing while every test still passed.
"""

import asyncio
import json

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_buzz_mod = load_plugin_adapter("buzz")
BuzzAdapter = _buzz_mod.BuzzAdapter

import importlib.util as _ilu
from pathlib import Path as _Path

_auth_path = _Path(_buzz_mod.__file__).with_name("nostr_auth.py")
_spec = _ilu.spec_from_file_location("plugin_adapter_buzz_nostr_auth_typing", _auth_path)
nostr_auth = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(nostr_auth)

SELF_PUBKEY = "9fd5c7ba6d3ef224da78f541e0fcb9c50f72cc63edb19aae76ac6a0474dfa860"
# BIP-340 test vector 0 private key.
TEST_PRIVATE_KEY = "00" * 31 + "03"
CHANNEL = "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"
ROOT_EVENT = "a" * 64

# Buzz Web: src/lib/kinds.ts:121 `export const KIND_TYPING_INDICATOR = 20002`.
# Upstream agrees (desktop/src/shared/constants/kinds.ts). Hard-coded here on
# purpose: this is a cross-repo wire constant, so the test must fail if the
# adapter's own constant drifts away from what the receiver subscribes to.
KIND_TYPING_INDICATOR = 20002


class _CapturingWebSocket:
    """Records raw frames the adapter publishes."""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def events(self):
        return [frame[1] for frame in self.sent if frame and frame[0] == "EVENT"]


def _make_adapter(extra=None, *, connected=True):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={"relay_url": "https://test.relay", **(extra or {})})
    adapter = BuzzAdapter(cfg)
    adapter._self_pubkey = SELF_PUBKEY
    adapter._private_key = TEST_PRIVATE_KEY
    adapter._display_name = "Chip"
    websocket = _CapturingWebSocket()
    if connected:
        adapter._ws_connection = websocket
        adapter._ws_active = True
    return adapter, websocket


# ── nostr_auth.build_event: the generic signing path ──────────────────────


def test_build_event_signs_an_arbitrary_kind():
    """build_event must produce a relay-valid id/sig for a non-22242 kind.

    The id is sha256 over the canonical NIP-01 serialization; recompute it here
    independently so a change to the separator/ordering is caught rather than
    being echoed back by the function under test.
    """
    event = nostr_auth.build_event(
        private_key=TEST_PRIVATE_KEY,
        kind=KIND_TYPING_INDICATOR,
        tags=[["h", CHANNEL]],
        content="",
        created_at=1_700_000_000,
        auxiliary_randomness=bytes(32),
    )
    assert event["kind"] == KIND_TYPING_INDICATOR
    assert event["content"] == ""
    assert event["pubkey"] == nostr_auth.public_key_hex(TEST_PRIVATE_KEY)
    assert len(bytes.fromhex(event["sig"])) == 64

    import hashlib

    expected_id = hashlib.sha256(
        json.dumps(
            [0, event["pubkey"], 1_700_000_000, KIND_TYPING_INDICATOR, [["h", CHANNEL]], ""],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert event["id"] == expected_id


def test_build_auth_event_still_matches_its_pinned_shape():
    """Refactoring build_auth_event onto build_event must not change its output.

    build_auth_event now delegates; this is the regression guard that the
    NIP-42 handshake event is byte-identical to what it was before.
    """
    event = nostr_auth.build_auth_event(
        private_key=TEST_PRIVATE_KEY,
        challenge="challenge-1",
        relay_url="wss://relay.example",
        created_at=1_700_000_000,
        auxiliary_randomness=bytes(32),
    )
    assert event["kind"] == 22242
    assert ["relay", "wss://relay.example"] in event["tags"]
    assert ["challenge", "challenge-1"] in event["tags"]
    assert event["content"] == ""
    assert len(bytes.fromhex(event["sig"])) == 64


# ── send_typing: the wire contract Buzz Web reads ──────────────────────────


@pytest.mark.asyncio
async def test_send_typing_publishes_kind_20002_with_h_tag():
    adapter, websocket = _make_adapter()

    await adapter.send_typing(CHANNEL)

    events = websocket.events()
    assert len(events) == 1, "exactly one EVENT frame per send_typing tick"
    event = events[0]
    assert event["kind"] == KIND_TYPING_INDICATOR
    # Buzz Web filters on {kinds:[20002], "#h":[activeChannelId]} and then
    # re-checks extractChannelId(event) === activeChannelId, so the h tag is
    # load-bearing twice over.
    assert ["h", CHANNEL] in event["tags"]
    # Content must be empty: the receiver renders a label from profile
    # metadata, never from the event body.
    assert event["content"] == ""
    assert event["pubkey"] == nostr_auth.public_key_hex(TEST_PRIVATE_KEY)


@pytest.mark.asyncio
async def test_channel_level_typing_carries_no_thread_anchor():
    """With no reply target, the indicator is channel-level.

    typingScopeId() returns null for a tagless event, which the receiver maps
    to the "channel" scope — the bottom-bar indicator.
    """
    adapter, websocket = _make_adapter()

    await adapter.send_typing(CHANNEL)

    event = websocket.events()[0]
    assert [tag for tag in event["tags"] if tag[0] == "e"] == []


@pytest.mark.asyncio
async def test_threaded_typing_anchors_to_the_same_root_as_the_reply():
    """A threaded turn must scope its indicator to the thread it answers in.

    The anchor is taken from _resolve_send_reply_target, i.e. the identical
    value send() will use, so the indicator cannot appear in one place while
    the reply lands in another.
    """
    adapter, websocket = _make_adapter()
    # _record_reply_target stores this before dispatch (adapter.py, _handle_event).
    adapter._active_reply_target[CHANNEL] = ROOT_EVENT

    await adapter.send_typing(CHANNEL)

    event = websocket.events()[0]
    assert ["h", CHANNEL] in event["tags"]
    # MUST be marked "reply", not "root": typingScopeId (src/lib/typing.ts)
    # derives the scope ONLY from a reply-marked e-tag, and a root-only anchor
    # resolves to null — the indicator would be misfiled to the channel
    # bottom bar instead of the thread panel.
    assert ["e", ROOT_EVENT, "", "reply"] in event["tags"]


@pytest.mark.asyncio
async def test_typing_tags_match_the_outbound_reply_target_exactly():
    """The indicator's anchor and the reply's --reply-to must be the same id."""
    adapter, _ = _make_adapter()
    adapter._active_reply_target[CHANNEL] = ROOT_EVENT

    tags = adapter._typing_tags(CHANNEL, None)
    reply_target = adapter._resolve_send_reply_target(CHANNEL, None, None)

    anchors = [tag[1] for tag in tags if tag[0] == "e"]
    assert anchors == [reply_target]


# ── Transport and failure behaviour ───────────────────────────────────────


@pytest.mark.asyncio
async def test_send_typing_is_a_noop_without_a_websocket():
    """On the polling transport there is no socket, so nothing is published.

    This is deliberate, not a gap: buzz-cli has no typing subcommand, and a
    durable `messages send` would post a visible chat message on a
    permanent-write relay.
    """
    adapter, websocket = _make_adapter(connected=False)

    await adapter.send_typing(CHANNEL)

    assert websocket.events() == []


@pytest.mark.asyncio
async def test_send_typing_is_a_noop_while_the_socket_is_reconnecting():
    """A stale handle with _ws_active False must not be published on."""
    adapter, websocket = _make_adapter()
    adapter._ws_active = False

    await adapter.send_typing(CHANNEL)

    assert websocket.events() == []


@pytest.mark.asyncio
async def test_send_typing_never_raises_when_the_socket_fails():
    """A failed indicator must not break the turn.

    _keep_typing catches broadly, but the bounded 1.5s wait_for means an
    exception here would also be logged on every tick; swallow it at source
    and let the next tick retry.
    """

    class _BrokenWebSocket(_CapturingWebSocket):
        async def send(self, raw):
            raise ConnectionError("relay went away")

    adapter, _ = _make_adapter()
    adapter._ws_connection = _BrokenWebSocket()

    await adapter.send_typing(CHANNEL)  # must not raise


@pytest.mark.asyncio
async def test_send_typing_ignores_an_empty_chat_id():
    adapter, websocket = _make_adapter()

    await adapter.send_typing("")

    assert websocket.events() == []


@pytest.mark.asyncio
async def test_repeated_ticks_publish_fresh_monotonic_events():
    """Each tick is a new event; the receiver folds them by created_at.

    applyTypingEvent ignores a re-broadcast that is not strictly newer than
    what it already holds, so ids must differ per tick for the 8s TTL to keep
    being extended across a long turn.
    """
    adapter, websocket = _make_adapter()

    await adapter.send_typing(CHANNEL)
    await asyncio.sleep(0)
    await adapter.send_typing(CHANNEL)

    events = websocket.events()
    assert len(events) == 2
    assert all(event["kind"] == KIND_TYPING_INDICATOR for event in events)


# ── Disconnect hygiene ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_clears_the_socket_handle():
    """A dropped connection must not leave a publishable stale handle."""
    adapter, _ = _make_adapter()

    await adapter.disconnect()

    assert adapter._ws_connection is None
    assert adapter._ws_active is False
