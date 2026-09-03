"""Tests for the Buzz platform adapter plugin."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

# Load plugins/platforms/buzz/adapter.py under a unique module name
# (plugin_adapter_buzz) so it cannot collide with other plugin adapters
# loaded by sibling tests in the same xdist worker.
_buzz_mod = load_plugin_adapter("buzz")

BuzzAdapter = _buzz_mod.BuzzAdapter
hex_to_npub = _buzz_mod.hex_to_npub
npub_to_hex = _buzz_mod.npub_to_hex
_normalize_user_ref = _buzz_mod._normalize_user_ref
_cli_error_message = _buzz_mod._cli_error_message
_resolve_private_key = _buzz_mod._resolve_private_key
check_requirements = _buzz_mod.check_requirements
validate_config = _buzz_mod.validate_config
register = _buzz_mod.register
_env_enablement = _buzz_mod._env_enablement
_standalone_send = _buzz_mod._standalone_send

# Real key pair (Chip's public identity — public information, not a secret)
SELF_PUBKEY = "9fd5c7ba6d3ef224da78f541e0fcb9c50f72cc63edb19aae76ac6a0474dfa860"
SELF_NPUB = "npub1nl2u0wnd8mezfknc74q7pl9ec58h9nrrakce4tnk434qgaxl4psqe5twr6"
OTHER_PUBKEY = "a" * 64
CHANNEL = "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"
# Real DM conversation as materialized by a hosted relay: `dms list` returns
# [] for it (#68871) while `channels list` shows it as name "DM", empty
# description, indistinguishable from a channel except via message p-tags.
DM_CHANNEL = "6468cc16-a114-4f23-8b8c-02c1655cbf6b"

_ENV_VARS = (
    "BUZZ_RELAY_URL",
    "BUZZ_PRIVATE_KEY",
    "BUZZ_CHANNELS",
    "BUZZ_HOME_CHANNEL",
    "BUZZ_ALLOWED_USERS",
    "BUZZ_ALLOW_ALL_USERS",
    "BUZZ_POLL_INTERVAL",
    "BUZZ_CLI_PATH",
    "BUZZ_CREDENTIALS_FILE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Keep tests hermetic: no ambient Buzz env vars or real credentials."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_buzz_mod, "_DEFAULT_CREDENTIALS_DIR", tmp_path / "no-creds")
    yield


def _event(event_id, pubkey=OTHER_PUBKEY, content="hello", created_at=1000, kind=9, p=None):
    tags = [["h", CHANNEL]]
    if p:
        tags.append(["p", p])
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
    }


def _make_adapter(extra=None):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={"relay_url": "https://test.relay", **(extra or {})})
    adapter = BuzzAdapter(cfg)
    adapter._self_pubkey = SELF_PUBKEY
    adapter._self_npub = SELF_NPUB
    adapter._display_name = "Chip"
    adapter._private_key = "nsec1test"
    return adapter


class _ScriptedCli:
    """Fake ``_run_cli`` that routes on the buzz subcommand and records calls."""

    def __init__(self):
        self.responses = {}  # (group, cmd) -> list of (code, stdout, stderr)
        self.calls = []

    def script(self, group, cmd, payload, code=0, stderr=""):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        self.responses.setdefault((group, cmd), []).append((code, stdout, stderr))

    async def __call__(self, args, *, input_text=None):
        self.calls.append((list(args), input_text))
        queue = self.responses.get((args[0], args[1]), [])
        if len(queue) > 1:
            return queue.pop(0)
        if queue:
            return queue[0]
        return 0, "[]", ""


# ── bech32 / identity helpers ─────────────────────────────────────────────


class TestBech32Helpers:

    def test_hex_to_npub_known_pair(self):
        assert hex_to_npub(SELF_PUBKEY) == SELF_NPUB

    def test_npub_to_hex_known_pair(self):
        assert npub_to_hex(SELF_NPUB) == SELF_PUBKEY


# ── Adapter init / config precedence ──────────────────────────────────────


class TestBuzzAdapterInit:


    def test_init_from_config_extra(self):
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "relay_url": "https://cfg.relay",
                "channels": ["ccc"],
                "poll_interval": 2,
                "home_channel": "ccc",
            },
        )
        adapter = BuzzAdapter(cfg)
        assert adapter.relay_url == "https://cfg.relay"
        assert adapter.channels == ["ccc"]
        assert adapter.poll_interval == 2.0
        assert adapter.home_channel == "ccc"

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://env.relay")
        from gateway.config import PlatformConfig
        adapter = BuzzAdapter(PlatformConfig(enabled=True, extra={"relay_url": "https://cfg.relay"}))
        assert adapter.relay_url == "https://env.relay"


# ── CLI error contract ────────────────────────────────────────────────────


class TestCliErrorContract:

    def test_parses_json_error(self):
        msg = _cli_error_message('{"error":"relay_error","message":"boom","retryable":false}', 2)
        assert "relay_error" in msg and "boom" in msg and "exit 2" in msg


# ── Seeding / high-water mark / de-dupe ───────────────────────────────────


class TestPollingDedupe:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        return a

    @pytest.mark.asyncio
    async def test_seed_sets_high_water_mark_without_dispatch(self, adapter):
        cli = _ScriptedCli()
        cli.script("messages", "get", [
            _event("e1", content="@Chip old history", created_at=100),
            _event("e2", content="@Chip newer history", created_at=200),
        ])
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")

        state = adapter._channel_state[CHANNEL]
        assert state["last_ts"] == 200
        assert set(state["seen"]) == {"e1", "e2"}
        # Seeding must never replay history into the agent
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_new_event_dispatched_once(self, adapter):
        cli = _ScriptedCli()
        cli.script("messages", "get", [_event("e1", content="@Chip hi", created_at=100, p=SELF_PUBKEY)])
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")

        # Poll 1: seeded event + a genuinely new mention
        cli.responses.clear()
        cli.script("messages", "get", [
            _event("e1", content="@Chip hi", created_at=100, p=SELF_PUBKEY),
            _event("e2", content="hey @Chip, ping", created_at=150, p=SELF_PUBKEY),
        ])
        await adapter._poll_channel(CHANNEL)
        assert [d["message_id"] for d in adapter._dispatched] == ["e2"]
        assert adapter._dispatched[0]["text"] == "hey @Chip, ping"
        assert adapter._channel_state[CHANNEL]["last_ts"] == 150

        # Poll 2: identical response — the seen-id set must de-dupe
        await adapter._poll_channel(CHANNEL)
        assert len(adapter._dispatched) == 1


# ── Mention gating / DMs / authorization ──────────────────────────────────


class TestMentionGating:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(CHANNEL)

    @pytest.mark.asyncio
    async def test_unaddressed_channel_message_ignored(self, adapter):
        await self._poll_with(adapter, _event("e1", content="just chatting", created_at=10))
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_name_mention_requires_structural_p_tag(self, adapter):
        # Structural gating (fix/buzz require structural p-tag mentions): a
        # display-name @mention with no ["p", self] tag must NOT dispatch...
        await self._poll_with(adapter, _event("e1", content="hey @Chip can you help?", created_at=10))
        assert adapter._dispatched == []
        # ...while the same message carrying the structural tag dispatches.
        await self._poll_with(adapter, _event("e2", content="hey @Chip can you help?", created_at=20, p=SELF_PUBKEY))
        assert len(adapter._dispatched) == 1


    @pytest.mark.asyncio
    async def test_allowlist_blocks_unauthorized(self, adapter):
        adapter._allowed_pubkeys = {"b" * 64}
        await self._poll_with(adapter, _event("e1", content="@Chip hello", created_at=10))
        assert adapter._dispatched == []


# ── DM classification via p-tags (issue #68871) ──────────────────────────
#
# `buzz dms list` returns [] on some hosted relays, so DM conversations leak
# in via `channels list` and get seeded chat_type="group".  The adapter must
# reclassify them from the Nostr tags of real traffic: DM messages are
# p-tagged to our own pubkey WITHOUT the text mentioning us, while channel
# messages only ever p-tag us when the text visibly @mentions us.


def _tagged_event(event_id, channel, *, content, pubkey=OTHER_PUBKEY,
                  created_at=1000, kind=9, p=None, reply_to=None):
    """Event with the tag shapes observed on a live relay (h/p/e tags)."""
    tags = [["h", channel]]
    if reply_to:
        tags.append(["e", reply_to, "", "reply"])
    if p:
        tags.append(["p", p])
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
    }


class TestDmClassification:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        # Metadata exactly as `channels list` returns it on the hosted relay.
        a._channel_meta = {
            DM_CHANNEL: {"channel_id": DM_CHANNEL, "name": "DM", "description": ""},
            CHANNEL: {
                "channel_id": CHANNEL,
                "name": "general",
                "description": "General conversation and community updates.",
            },
        }
        a._channel_names = {DM_CHANNEL: "DM", CHANNEL: "general"}
        # Both leaked in as group — the bug under test.
        a._channel_state[DM_CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, channel, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(channel)

    @pytest.mark.asyncio
    async def test_unmentioned_ptagged_dm_latches_and_dispatches(self, adapter):
        """The reported bug: a DM without an @mention must dispatch."""
        await self._poll_with(
            adapter, DM_CHANNEL,
            _tagged_event("e1", DM_CHANNEL, content="here's a test message", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[DM_CHANNEL]["chat_type"] == "dm"
        assert [d["message_id"] for d in adapter._dispatched] == ["e1"]
        assert adapter._dispatched[0]["chat_type"] == "dm"


    @pytest.mark.asyncio
    async def test_general_reply_ptagging_self_stays_channel(self, adapter):
        """A #general reply to us p-tags our pubkey (observed live) — that
        must NOT reclassify the channel; mention gating still applies."""
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="@chip what's up?",
                          p=SELF_PUBKEY, reply_to="root-event"),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        # It carried a mention, so it dispatches — but as a group message.
        assert [d["chat_type"] for d in adapter._dispatched] == ["group"]

        # And once the mention is absent, the channel gate drops the message
        # even though the earlier reply p-tagged us.
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e2", CHANNEL, content="thanks everyone", created_at=1001),
        )
        assert len(adapter._dispatched) == 1


    @pytest.mark.asyncio
    async def test_channel_like_metadata_blocks_latch_even_without_mention(self, adapter):
        """Second guard on its own: even a p-tagged, un-mentioned message
        cannot reclassify a conversation whose metadata says real channel."""
        adapter._channel_meta[CHANNEL]["description"] = ""
        adapter._channel_meta[CHANNEL]["name"] = "announcements"
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="fyi everyone", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        # Under structural gating a ["p", self] tag IS a mention, so the
        # message dispatches — but as a group message: the guard's point is
        # that channel-like metadata still blocks the DM latch.
        assert all(d["chat_type"] == "group" for d in adapter._dispatched)


    @pytest.mark.asyncio
    async def test_dm_shaped_channel_discovered_when_dms_list_empty(self):
        """Fallback discovery: with `dms list` broken (returns []), a
        DM-shaped `channels list` entry gets watched; real channels not
        already watched are left alone."""
        a = _make_adapter()
        cli = _ScriptedCli()
        cli.script("dms", "list", [])
        cli.script("channels", "list", [
            {"channel_id": DM_CHANNEL, "name": "DM", "description": "", "created_at": 1},
            {"channel_id": CHANNEL, "name": "general",
             "description": "General conversation and community updates.", "created_at": 2},
        ])
        a._run_cli = cli
        await a._discover_dms(seed=False)
        # Watched as group; the p-tag latch flips it on the first real DM.
        assert a._channel_state[DM_CHANNEL]["chat_type"] == "group"
        assert a._may_reclassify_as_dm(DM_CHANNEL) is True
        assert CHANNEL not in a._channel_state
        assert a._may_reclassify_as_dm(CHANNEL) is False


# ── Reply-target resolution (threading rule) ──────────────────────────────
#
# Rule: tagged in a TOP-LEVEL channel message -> respond as a CHILD of that
# message (starts a thread). Tagged in a message that is itself a reply ->
# respond as a SIBLING at the same level (reply to that message's parent),
# so agent responses never nest one level deeper per exchange. Sends with no
# reply context at all (status notices, media, background summaries) follow
# the most recent dispatched message's resolved target.


class TestReplyTargetResolution:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        a._channel_meta = {
            CHANNEL: {
                "channel_id": CHANNEL,
                "name": "general",
                "description": "General conversation and community updates.",
            },
        }
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(CHANNEL)

    async def _send(self, adapter, content="answer", reply_to=None, metadata=None):
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-out", "message": ""})
        adapter._run_cli = cli
        result = await adapter.send(CHANNEL, content, reply_to=reply_to, metadata=metadata)
        assert result.success is True
        return cli.calls[0][0]

    @staticmethod
    def _reply_to_arg(args):
        return args[args.index("--reply-to") + 1] if "--reply-to" in args else None

    def test_event_thread_root_shapes(self):
        root = BuzzAdapter._event_thread_root
        # Top-level: no e tags at all.
        assert root({"tags": [["h", CHANNEL]]}) is None
        # Marked root wins over marked reply (deep nested reply).
        assert root({"tags": [["e", "root1", "", "root"], ["e", "mid1", "", "reply"]]}) == "root1"
        # Root-only marking (first-level thread reply).
        assert root({"tags": [["e", "root1", "", "root"]]}) == "root1"
        # Unmarked legacy tags: positional NIP-10 — FIRST one is the root.
        assert root({"tags": [["e", "root1"], ["e", "mid1"]]}) == "root1"
        # Lone reply marker (first-level reply): the parent IS the root.
        assert root({"tags": [["e", "root1", "", "reply"]]}) == "root1"
        # Invalid tag shapes are ignored, not fatal (safe fallback).
        assert root({"tags": "garbage"}) is None
        assert root({"tags": [["e"], ["e", ""]]}) is None

    @pytest.mark.asyncio
    async def test_top_level_mention_replies_as_child(self, adapter):
        """Tagged in a top-level message: the response starts a thread there."""
        await self._poll_with(
            adapter,
            _tagged_event("top1", CHANNEL, content="@Chip can you help?", p=SELF_PUBKEY),
        )
        assert [d["message_id"] for d in adapter._dispatched] == ["top1"]
        # The gateway's reply anchor is the triggering message id.
        args = await self._send(adapter, reply_to="top1")
        assert self._reply_to_arg(args) == "top1"

    @pytest.mark.asyncio
    async def test_nested_mention_replies_to_thread_root(self, adapter):
        """Tagged in a first-level thread reply: the response anchors to the
        thread root, keeping the thread flat."""
        await self._poll_with(
            adapter,
            _tagged_event(
                "nested1", CHANNEL, content="@Chip what about this?",
                p=SELF_PUBKEY, reply_to="thread-root",
            ),
        )
        assert [d["message_id"] for d in adapter._dispatched] == ["nested1"]
        args = await self._send(adapter, reply_to="nested1")
        assert self._reply_to_arg(args) == "thread-root"

    @pytest.mark.asyncio
    async def test_deep_nested_mention_anchors_to_marked_root(self, adapter):
        """Tagged deep in a thread (root + reply markers): the response
        anchors to the STABLE ROOT, not the immediate parent — never a
        reply to a reply."""
        deep = _tagged_event(
            "deep1", CHANNEL, content="@Chip deeper?", p=SELF_PUBKEY,
        )
        deep["tags"].insert(1, ["e", "thread-root", "", "root"])
        deep["tags"].insert(2, ["e", "mid-reply", "", "reply"])
        await self._poll_with(adapter, deep)
        args = await self._send(adapter, reply_to="deep1")
        assert self._reply_to_arg(args) == "thread-root"

    @pytest.mark.asyncio
    async def test_conversation_keeps_anchoring_to_same_root(self, adapter):
        """Subsequent exchanges in a rooted conversation keep anchoring to
        the original root rather than the previous reply."""
        await self._poll_with(
            adapter,
            _tagged_event("top-a", CHANNEL, content="@Chip start", p=SELF_PUBKEY),
        )
        args = await self._send(adapter, reply_to="top-a")
        assert self._reply_to_arg(args) == "top-a"
        follow = _tagged_event(
            "follow1", CHANNEL, content="@Chip more", p=SELF_PUBKEY, created_at=1001,
        )
        follow["tags"].insert(1, ["e", "top-a", "", "root"])
        follow["tags"].insert(2, ["e", "evt-out", "", "reply"])
        await self._poll_with(adapter, follow)
        args = await self._send(adapter, reply_to="follow1")
        assert self._reply_to_arg(args) == "top-a"

    @pytest.mark.asyncio
    async def test_websocket_and_poll_paths_share_root_anchoring(self, adapter):
        """The WebSocket loop routes events through the same _handle_event()
        as the poll loop; anchoring recorded there applies to both."""
        async def stub_cli(args, input_text=None, **kwargs):
            return 0, "{}", ""

        adapter._run_cli = stub_cli
        state = adapter._channel_state[CHANNEL]
        nested = _tagged_event(
            "ws-nested", CHANNEL, content="@Chip via ws", p=SELF_PUBKEY,
        )
        nested["tags"].insert(1, ["e", "ws-root", "", "root"])
        nested["tags"].insert(2, ["e", "ws-mid", "", "reply"])
        await adapter._handle_event(CHANNEL, state, nested)
        assert adapter._active_reply_target[CHANNEL] == "ws-root"
        assert adapter._reply_targets["ws-nested"] == "ws-root"

    @pytest.mark.asyncio
    async def test_contextless_send_follows_triggering_message(self, adapter):
        """Status/media/background sends carry no reply context — they must
        land in the same reply context as the triggering message, not at the
        channel root."""
        await self._poll_with(
            adapter,
            _tagged_event(
                "nested2", CHANNEL, content="@Chip and this?",
                p=SELF_PUBKEY, reply_to="thread-root",
            ),
        )
        args = await self._send(adapter, content="status notice")
        assert self._reply_to_arg(args) == "thread-root"

    @pytest.mark.asyncio
    async def test_thread_metadata_is_remapped_too(self, adapter):
        """metadata.thread_id carrying the triggering id is remapped as well."""
        await self._poll_with(
            adapter,
            _tagged_event(
                "nested3", CHANNEL, content="@Chip ping",
                p=SELF_PUBKEY, reply_to="thread-root",
            ),
        )
        args = await self._send(adapter, metadata={"thread_id": "nested3"})
        assert self._reply_to_arg(args) == "thread-root"

    @pytest.mark.asyncio
    async def test_explicit_undispatched_target_passes_through(self, adapter):
        """An explicit reply target that never triggered a dispatch (e.g. the
        send-message tool replying to an arbitrary event) is used verbatim."""
        args = await self._send(adapter, reply_to="some-other-event")
        assert self._reply_to_arg(args) == "some-other-event"

    @pytest.mark.asyncio
    async def test_no_context_no_history_sends_to_channel_root(self, adapter):
        args = await self._send(adapter)
        assert self._reply_to_arg(args) is None

    @pytest.mark.asyncio
    async def test_image_send_follows_resolved_target(self, adapter, tmp_path):
        img = tmp_path / "chart.png"
        img.write_bytes(b"\x89PNG fake")
        await self._poll_with(
            adapter,
            _tagged_event(
                "nested4", CHANNEL, content="@Chip chart please",
                p=SELF_PUBKEY, reply_to="thread-root",
            ),
        )
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-img", "message": ""})
        adapter._run_cli = cli
        result = await adapter.send_image(CHANNEL, str(img), caption="here", reply_to="nested4")
        assert result.success is True
        args = cli.calls[0][0]
        assert args[args.index("--reply-to") + 1] == "thread-root"


# ── Sending ───────────────────────────────────────────────────────────────


class TestBuzzAdapterSend:

    @pytest.mark.asyncio
    async def test_send_success_via_stdin(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt123", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send(CHANNEL, "hello **markdown**")
        assert result.success is True
        assert result.message_id == "evt123"

        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "send"]
        assert args[args.index("--channel") + 1] == CHANNEL
        # Content travels via stdin (--content -), never argv
        assert args[args.index("--content") + 1] == "-"
        assert stdin_text == "hello **markdown**"
        # Our own event id is marked seen for echo suppression
        assert "evt123" in adapter._channel_state[CHANNEL]["seen"]


    @pytest.mark.asyncio
    async def test_send_image_local_file_uses_file_flag(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt126", "message": ""})
        adapter._run_cli = cli
        result = await adapter.send_image(CHANNEL, str(img), caption="screenshot")
        assert result.success is True
        args, _stdin = cli.calls[0]
        assert args[args.index("--file") + 1] == str(img)

    @pytest.mark.asyncio
    async def test_local_media_dispatch_uses_file_attachment(self, tmp_path):
        """The shared MEDIA image path dispatches through send_image_file."""
        img = tmp_path / "generated.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt127", "message": ""})
        adapter._run_cli = cli

        await adapter.send_multiple_images(
            CHANNEL,
            [(img.as_uri(), "generated image")],
            metadata={"thread_id": "parent-event"},
        )

        args, stdin_text = cli.calls[0]
        assert args[args.index("--file") + 1] == str(img)
        assert args[args.index("--reply-to") + 1] == "parent-event"
        assert stdin_text == "generated image"


# ── Attachment upload: send_document + shared attachment path ─────────────
#
# Triage fi_6fa8864e (2026-09-03):
#   * ``send_document`` must upload via the same ``messages send --file``
#     path as ``send_image`` so document-routed artifacts stop falling back
#     to the generic file-attachment warning.
#   * A single artifact must produce a single delivery attempt: bounded
#     retry lives INSIDE the adapter (transient exits only), never as
#     duplicate CLI invocations after a message actually posted.
#   * Success requires the returned event to carry an attachment marker —
#     ``accepted=True`` alone is insufficient (was the previous behavior
#     that produced silent drops).
#   * Terminal errors expose only the basename plus a correlation id.


def _attachment_confirmed_payload(event_id="evt-attach"):
    """A ``buzz messages send`` response the receipt-verifier confirms."""
    return {
        "accepted": True,
        "event_id": event_id,
        "event": {
            "id": event_id,
            "tags": [
                ["h", CHANNEL],
                ["imeta", f"url https://relay.example/{event_id}.bin"],
            ],
        },
    }


def _attachment_missing_payload(event_id="evt-nofile"):
    """A ``messages send`` response with no attachment marker on the event."""
    return {
        "accepted": True,
        "event_id": event_id,
        "event": {"id": event_id, "tags": [["h", CHANNEL]]},
    }


class TestBuzzSendDocument:

    @pytest.mark.asyncio
    async def test_send_document_uploads_via_file_flag(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", _attachment_confirmed_payload("evt-doc"))
        adapter._run_cli = cli

        result = await adapter.send_document(CHANNEL, str(pdf), caption="see attached")
        assert result.success is True
        assert result.message_id == "evt-doc"
        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "send"]
        assert args[args.index("--channel") + 1] == CHANNEL
        assert args[args.index("--file") + 1] == str(pdf)
        assert args[args.index("--content") + 1] == "-"
        assert stdin_text == "see attached"

    @pytest.mark.asyncio
    async def test_send_document_preserves_thread_reply_target(self, tmp_path):
        """metadata.thread_id / reply_to must translate to --reply-to and
        obey the sibling/child threading remap."""
        pdf = tmp_path / "notes.pdf"
        pdf.write_bytes(b"%PDF")
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        # Simulate a prior nested mention whose parent is thread-root.
        adapter._reply_targets["evt-in"] = "thread-root"
        cli = _ScriptedCli()
        cli.script("messages", "send", _attachment_confirmed_payload("evt-doc-thread"))
        adapter._run_cli = cli

        result = await adapter.send_document(
            CHANNEL, str(pdf),
            reply_to="evt-in",
            metadata={"thread_id": "evt-in"},
        )
        assert result.success is True
        args, _ = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "thread-root"

    @pytest.mark.asyncio
    async def test_send_document_missing_file_terminal_hides_runner_path(self, tmp_path):
        missing = tmp_path / "no-such.pdf"
        adapter = _make_adapter()
        cli = _ScriptedCli()
        adapter._run_cli = cli

        result = await adapter.send_document(CHANNEL, str(missing))
        assert result.success is False
        assert result.retryable is False
        assert "no-such.pdf" in (result.error or "")
        assert "[id=" in (result.error or "")
        # Runner-local parent directory must not leak into the error text.
        assert str(missing.parent) not in (result.error or "")
        # No CLI invocation was attempted.
        assert cli.calls == []

    @pytest.mark.asyncio
    async def test_send_document_retries_transient_exit_then_succeeds(self, tmp_path, monkeypatch):
        pdf = tmp_path / "chart.pdf"
        pdf.write_bytes(b"%PDF")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        # First call: retryable relay error (exit 2). Second call: success
        # with a confirmed attachment marker.
        cli.script(
            "messages", "send", "",
            code=2, stderr='{"error":"relay_error","message":"boom"}',
        )
        cli.script("messages", "send", _attachment_confirmed_payload("evt-doc2"))
        adapter._run_cli = cli
        # Skip the retry backoff so the test doesn't sleep in CI.
        async def _no_sleep(_delay):
            return None
        monkeypatch.setattr(_buzz_mod.asyncio, "sleep", _no_sleep)

        result = await adapter.send_document(CHANNEL, str(pdf))
        assert result.success is True
        assert result.message_id == "evt-doc2"
        assert len(cli.calls) == 2

    @pytest.mark.asyncio
    async def test_send_document_hard_exit_produces_single_terminal_error(self, tmp_path):
        pdf = tmp_path / "big.pdf"
        pdf.write_bytes(b"%PDF")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script(
            "messages", "send", "",
            code=1, stderr='{"error":"invalid_arg","message":"file too large"}',
        )
        adapter._run_cli = cli

        result = await adapter.send_document(CHANNEL, str(pdf))
        assert result.success is False
        assert "big.pdf" in (result.error or "")
        assert "[id=" in (result.error or "")
        # No spinning on non-retryable exits.
        assert len(cli.calls) == 1

    @pytest.mark.asyncio
    async def test_send_document_receipt_missing_attachment_marker_fails(self, tmp_path):
        """The CLI accepted the send but the emitted event carries no
        attachment marker — report failure and do NOT retry (would
        duplicate the posted message)."""
        pdf = tmp_path / "spec.pdf"
        pdf.write_bytes(b"%PDF")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", _attachment_missing_payload("evt-doc-ghost"))
        adapter._run_cli = cli

        result = await adapter.send_document(CHANNEL, str(pdf))
        assert result.success is False
        assert "spec.pdf" in (result.error or "")
        assert "[id=" in (result.error or "")
        assert len(cli.calls) == 1


class TestBuzzSendAttachmentReceiptVerification:

    @pytest.mark.asyncio
    async def test_send_image_local_confirms_attachment_marker(self, tmp_path):
        img = tmp_path / "graph.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", _attachment_confirmed_payload("evt-img"))
        adapter._run_cli = cli

        result = await adapter.send_image(CHANNEL, str(img), caption="chart")
        assert result.success is True
        assert result.message_id == "evt-img"

    @pytest.mark.asyncio
    async def test_send_image_receipt_missing_attachment_marker_fails(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", _attachment_missing_payload("evt-img-ghost"))
        adapter._run_cli = cli

        result = await adapter.send_image(CHANNEL, str(img))
        assert result.success is False
        assert "shot.png" in (result.error or "")

    @pytest.mark.asyncio
    async def test_send_image_legacy_cli_shape_still_succeeds(self, tmp_path):
        """Older buzz CLIs return only ``accepted``/``event_id`` (no event
        object).  Trust the CLI to avoid false-negative user notices."""
        img = tmp_path / "legacy.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-legacy", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send_image(CHANNEL, str(img))
        assert result.success is True
        assert result.message_id == "evt-legacy"

    def test_receipt_confirms_recognizes_common_attachment_tags(self):
        conf = BuzzAdapter._receipt_confirms_attachment
        for tag_head in ("imeta", "file", "attachment", "attachments", "url", "media"):
            payload = {
                "accepted": True,
                "event_id": "e",
                "event": {"tags": [["h", CHANNEL], [tag_head, "value"]]},
            }
            assert conf(payload), f"expected {tag_head!r} tag to confirm attachment"

    def test_receipt_confirms_accepts_flat_attachments_list(self):
        payload = {
            "accepted": True,
            "event_id": "e",
            "event": {"tags": [["h", CHANNEL]]},
            "attachments": [{"url": "https://relay.example/x.pdf"}],
        }
        assert BuzzAdapter._receipt_confirms_attachment(payload) is True

    def test_receipt_confirms_rejects_explicit_not_accepted(self):
        payload = {"accepted": False, "event_id": "e"}
        assert BuzzAdapter._receipt_confirms_attachment(payload) is False


# ── Lifecycle ─────────────────────────────────────────────────────────────


class TestBuzzAdapterLifecycle:


    @pytest.mark.asyncio
    async def test_disconnect_releases_scoped_lock(self, monkeypatch):
        """The identity lock taken in connect() must be released on disconnect."""
        import gateway.status as gateway_status

        released = []
        monkeypatch.setattr(
            gateway_status,
            "release_scoped_lock",
            lambda platform, key: released.append((platform, key)),
        )
        adapter = _make_adapter()
        adapter._lock_key = "wss://relay.example:" + SELF_PUBKEY
        await adapter.disconnect()
        assert released == [("buzz", "wss://relay.example:" + SELF_PUBKEY)]
        assert adapter._lock_key is None

    @pytest.mark.asyncio
    async def test_connect_fails_when_identity_lock_held(self, monkeypatch):
        """A second profile using the same relay+pubkey must fail fast."""
        import gateway.status as gateway_status

        monkeypatch.setattr(
            gateway_status, "acquire_scoped_lock", lambda platform, key: False
        )
        adapter = _make_adapter()
        adapter.cli_path = "/fake/buzz"
        monkeypatch.setattr(_buzz_mod, "_resolve_private_key", lambda extra=None: "nsec1test")
        cli = _ScriptedCli()
        cli.script(
            "users", "get",
            [{"pubkey": SELF_PUBKEY, "display_name": "Chip"}],
        )
        adapter._run_cli = cli
        assert await adapter.connect() is False
        assert adapter._lock_key is None


# ── Credentials / requirements ────────────────────────────────────────────


class TestCredentialResolution:

    def test_env_key_wins(self, monkeypatch):
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1fromenv")
        assert _resolve_private_key() == "nsec1fromenv"

    def test_credentials_file_fallback(self, monkeypatch, tmp_path):
        creds = tmp_path / "agent_credentials.json"
        creds.write_text(json.dumps({"nsec": "nsec1fromfile", "npub": "npub1x"}), encoding="utf-8")
        monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(creds))
        assert _resolve_private_key() == "nsec1fromfile"


# ── Env enablement / registration / standalone send ──────────────────────


class TestEnvEnablement:

    def test_returns_none_when_unconfigured(self):
        assert _env_enablement() is None


class TestBuzzPluginRegistration:

    def test_register_platform_contract(self):
        from gateway.platform_registry import platform_registry

        platform_registry.unregister("buzz")
        ctx = MagicMock()
        register(ctx)
        ctx.register_platform.assert_called_once()
        kwargs = ctx.register_platform.call_args.kwargs
        assert kwargs["name"] == "buzz"
        assert kwargs["cron_deliver_env_var"] == "BUZZ_HOME_CHANNEL"
        assert kwargs["allowed_users_env"] == "BUZZ_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "BUZZ_ALLOW_ALL_USERS"
        assert callable(kwargs["standalone_sender_fn"])
        assert callable(kwargs["env_enablement_fn"])
        assert set(kwargs["required_env"]) == {"BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY"}


class TestStandaloneSend:

    @pytest.mark.asyncio
    async def test_standalone_send_success(self, monkeypatch, tmp_path):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))

        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, input_text=None, timeout=30.0):
            captured.update(cli_path=cli_path, args=args, relay_url=relay_url, input_text=input_text)
            return 0, json.dumps({"accepted": True, "event_id": "evt-cron", "message": ""}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = await _standalone_send(PlatformConfig(enabled=True, extra={}), CHANNEL, "cron says hi")
        assert result == {"success": True, "message_id": "evt-cron"}
        assert captured["args"][:2] == ["messages", "send"]
        assert captured["input_text"] == "cron says hi"
        # The private key must never be part of argv
        assert all("nsec1x" not in str(a) for a in captured["args"])

