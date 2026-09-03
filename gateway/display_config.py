"""Per-platform display/verbosity configuration resolver.

Provides ``resolve_display_setting()`` — the single entry-point for reading
display settings with platform-specific overrides and sensible defaults.

Resolution order (first non-None wins):
    1. ``display.platforms.<platform>.<key>``  — explicit per-platform user override
    2. ``display.<key>``                       — global user setting
    3. ``_PLATFORM_DEFAULTS[<platform>][<key>]``  — built-in sensible default
    4. ``_GLOBAL_DEFAULTS[<key>]``              — built-in global default

Exception: ``display.streaming`` is CLI-only.  Gateway streaming follows the
top-level ``streaming`` config unless ``display.platforms.<platform>.streaming``
sets an explicit per-platform override.

Backward compatibility: ``display.tool_progress_overrides`` is still read as a
fallback for ``tool_progress`` when no ``display.platforms`` entry exists.  A
config migration (version bump) automatically moves the old format into the new
``display.platforms`` structure.

Event classes and per-platform delivery policy (fi_a18c64a3):
    ``resolve_event_delivery(cfg, platform, event_class)`` maps a gateway
    event (tool telemetry, milestone, terminal result, agent activity,
    lifecycle warning) to the surface it lands on for that platform:

        chat      — a durable in-thread chat message the user sees inline
        ephemeral — a transient "working" indicator (composer/typing surface)
        audit     — an audit/activity log surface (never user-facing chat)
        operator  — an operator/home-channel surface (not user chat)
        off       — suppressed entirely

    The policy defaults ensure that on Buzz — a permanent-message relay
    where every send is a durable event visible to a channel — tool
    telemetry is NEVER posted as a chat message. It is routed instead to
    an in-memory working-indicator publisher that the composer area can
    render locally. Milestones and exactly one terminal result stay
    threaded in-chat. Agent activity notices (e.g. background_review
    "Self-improvement review: ...") route to audit surfaces, never to a
    channel. Lifecycle warnings (e.g. Gateway-shutting-down interrupt
    reasons) route to the operator surface, never as unsolicited assistant
    messages inside a user conversation.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Overrideable display settings and their global defaults
# ---------------------------------------------------------------------------
# These are the settings that can be configured per-platform.
# Other display settings (compact, personality, skin, etc.) are CLI-only
# and don't participate in per-platform resolution.

_GLOBAL_DEFAULTS: dict[str, Any] = {
    "tool_progress": "all",
    "tool_progress_grouping": "accumulate",  # "accumulate" = edit one bubble; "separate" = one msg per tool
    "show_reasoning": False,
    # How a reasoning/thinking summary is rendered when show_reasoning is on.
    #   "code"      -> 💭 **Reasoning:** + fenced code block (legacy default)
    #   "blockquote"-> each line prefixed with "> "
    #   "subtext"   -> each line prefixed with "-# " (Discord small grey subtext)
    # Discord defaults to "subtext"; everywhere else defaults to "code".
    "reasoning_style": "code",
    "tool_preview_length": 0,
    "streaming": None,  # None = follow top-level streaming config
    # Gateway-only assistant/status chatter controls. These default on for
    # back-compat, but mobile platforms can opt down to final-answer-first.
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
    # Whether busy_input_mode=steer sends a visible "Steered into current run"
    # acknowledgment after successfully injecting the user's mid-turn message.
    # Disable when the platform should steer silently (the text still lands in
    # the active run; only the confirmation echo is suppressed).
    "busy_steer_ack_enabled": True,
    # When true, delete tool-progress / "⏳ Working — N min" / status bubbles
    # after the final response lands on platforms that support message
    # deletion (e.g. Telegram). Off by default — progress is still shown
    # live, just cleaned up after success so the chat doesn't fill up with
    # stale breadcrumbs. Failed runs leave bubbles in place as breadcrumbs.
    "cleanup_progress": False,
    # Live working-state status on platforms whose typing indicator renders
    # text (Slack's assistant status line). Values:
    #   "full" / true  -> verb + argument preview ("is running pytest…")
    #   "verb"         -> verb only ("is running…") — keeps file paths and
    #                     commands out of shared channels
    #   "off" / false  -> static text (typing_status_text or "is thinking...")
    # Independent of tool_progress: works even when progress bubbles are off
    # (Slack's default), and costs no extra API calls — the existing typing
    # refresh cadence just renders different text.
    "live_status": "full",
    # Chat notification for background memory/skill review results
    # ("💾 Memory updated" / "💾 Self-improvement review: …").
    #   off     — no chat notification (still logged to stdout)
    #   on      — generic summary (default)
    #   verbose — include compact content previews
    "memory_notifications": "on",
}

# ---------------------------------------------------------------------------
# Sensible per-platform defaults — tiered by platform capability
# ---------------------------------------------------------------------------
# Tier 1 (high): Supports message editing, typically personal/team use
# Tier 2 (medium): Supports editing but often workspace/customer-facing
# Tier 3 (low): No edit support — each progress msg is permanent
# Tier 4 (minimal): Batch/non-interactive delivery

_TIER_HIGH = {
    "tool_progress": "all",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": None,  # follow global
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
}

_TIER_MEDIUM = {
    "tool_progress": "new",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": None,
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
}

_TIER_LOW = {
    "tool_progress": "off",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": False,
    "interim_assistant_messages": False,
    "long_running_notifications": False,
    "busy_ack_detail": False,
}

_TIER_MINIMAL = {
    "tool_progress": "off",
    "show_reasoning": False,
    "tool_preview_length": 0,
    "streaming": False,
    "interim_assistant_messages": False,
    "long_running_notifications": False,
    "busy_ack_detail": False,
}

_PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    # Tier 1 — full edit support, personal/team use
    # Telegram is usually a mobile inbox: keep tool_progress quiet and skip
    # the verbose busy-ack iteration counter, but DO surface real mid-turn
    # assistant commentary (interim_assistant_messages) and DO send periodic
    # heartbeats (long_running_notifications) so the user has signal between
    # turn start and final answer. Otherwise it looks like "typing..." for
    # 30 minutes with nothing happening. Opt in to verbose iteration detail
    # via display.platforms.telegram.busy_ack_detail / tool_progress.
    "telegram":    {
        **_TIER_HIGH,
        "tool_progress": "off",
        "busy_ack_detail": False,
    },
    # Discord has a native "subtext" primitive (-# small grey text) that reads
    # as metadata rather than content, so reasoning summaries default to it
    # here instead of the fenced code block used elsewhere.
    "discord":     {**_TIER_HIGH, "reasoning_style": "subtext"},

    # Tier 2 — edit support, often customer/workspace channels
    # Slack: tool_progress off by default — Bolt posts cannot be edited like CLI;
    # "new"/"all" spam permanent lines in channels (hermes-agent#14663).
    "slack":           {
        **_TIER_MEDIUM,
        "tool_progress": "off",
        "long_running_notifications": False,
        "busy_ack_detail": False,
    },
    "mattermost":      _TIER_MEDIUM,
    "matrix":          _TIER_MEDIUM,
    "feishu":          _TIER_MEDIUM,

    # Tier 3 — no edit support, progress messages are permanent
    "signal":          _TIER_LOW,
    # Buzz (Nostr relay, CLI-driven): no message editing, so every progress or
    # status bubble is a permanent event in a shared channel. Keep the agent
    # calm by default: no tool progress, no interim/heartbeat chatter, no
    # steer/redirect confirmation bubbles, no self-improvement summaries.
    "buzz":            {
        **_TIER_LOW,
        "busy_steer_ack_enabled": False,
        "memory_notifications": "off",
    },
    "whatsapp":        _TIER_MEDIUM,  # Baileys bridge supports /edit
    # WhatsApp Cloud API: Meta added message editing in 2023 but the
    # Hermes Cloud adapter doesn't implement edit_message yet, so we
    # stay on TIER_LOW (tool_progress off) to avoid spamming each
    # status update as a separate message. Promote to TIER_MEDIUM once
    # Cloud's edit_message lands.
    "whatsapp_cloud":  _TIER_LOW,
    # Photon (managed iMessage over the gRPC sidecar) and BlueBubbles are both
    # permanent-message iMessage inboxes with no message-edit support, so both
    # stay TIER_LOW. This keeps tool progress, interim scratch commentary,
    # "still working" heartbeats, and busy-ack iteration detail out of the
    # user's iMessage thread. Without this entry Photon inherited the noisy
    # global ("all") defaults and compacted/narrated on nearly every turn.
    "photon":          _TIER_LOW,
    "bluebubbles":     _TIER_LOW,
    "weixin":          _TIER_LOW,
    "wecom":           _TIER_LOW,
    "wecom_callback":  _TIER_LOW,
    "dingtalk":        _TIER_LOW,

    # Tier 4 — batch or non-interactive delivery
    "email":           _TIER_MINIMAL,
    "sms":             _TIER_MINIMAL,
    "webhook":         _TIER_MINIMAL,
    "homeassistant":   _TIER_MINIMAL,
    "api_server":      {**_TIER_HIGH, "tool_preview_length": 0},
}

# Canonical set of per-platform overrideable keys (for validation).
OVERRIDEABLE_KEYS = frozenset(_GLOBAL_DEFAULTS.keys())


def resolve_display_setting(
    user_config: dict,
    platform_key: str,
    setting: str,
    fallback: Any = None,
) -> Any:
    """Resolve a display setting with per-platform override support.

    Parameters
    ----------
    user_config : dict
        The full parsed config.yaml dict.
    platform_key : str
        Platform config key (e.g. ``"telegram"``, ``"slack"``).  Use
        ``_platform_config_key(source.platform)`` from gateway/run.py.
    setting : str
        Display setting name (e.g. ``"tool_progress"``, ``"show_reasoning"``).
    fallback : Any
        Fallback value when the setting isn't found anywhere.

    Returns
    -------
    The resolved value, or *fallback* if nothing is configured.
    """
    display_cfg = user_config.get("display") or {}

    # 1. Explicit per-platform override (display.platforms.<platform>.<key>)
    platforms = display_cfg.get("platforms") or {}
    plat_overrides = platforms.get(platform_key)
    if isinstance(plat_overrides, dict):
        val = plat_overrides.get(setting)
        if val is not None:
            return _normalise(setting, val)

    # 1b. Backward compat: display.tool_progress_overrides.<platform>
    if setting == "tool_progress":
        legacy = display_cfg.get("tool_progress_overrides")
        if isinstance(legacy, dict):
            val = legacy.get(platform_key)
            if val is not None:
                return _normalise(setting, val)

    # 2. Global user setting (display.<key>).  Skip display.streaming because
    # that key controls only CLI terminal streaming; gateway token streaming is
    # governed by the top-level streaming config plus per-platform overrides.
    if setting != "streaming":
        val = display_cfg.get(setting)
        if val is not None:
            return _normalise(setting, val)

    # 3. Built-in platform default
    plat_defaults = _PLATFORM_DEFAULTS.get(platform_key)
    if plat_defaults:
        val = plat_defaults.get(setting)
        if val is not None:
            return val

    # 4. Built-in global default
    val = _GLOBAL_DEFAULTS.get(setting)
    if val is not None:
        return val

    return fallback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(setting: str, value: Any) -> Any:
    """Normalise YAML quirks (bare ``off`` → False in YAML 1.1)."""
    if setting == "tool_progress":
        if value is False:
            return "off"
        if value is True:
            return "all"
        val = str(value).strip().lower()
        if val in {"false", "0", "no"}:
            return "off"
        if val in {"true", "1", "yes", "on"}:
            return "all"
        return val if val in {"off", "new", "all", "verbose", "log"} else "all"
    if setting in {
        "show_reasoning",
        "streaming",
        "interim_assistant_messages",
        "long_running_notifications",
        "busy_ack_detail",
        "busy_steer_ack_enabled",
        "thinking_progress",
    }:
        if isinstance(value, str):
            val = value.strip().lower()
            if val == "generic" and setting == "long_running_notifications":
                return "generic"
            return val in {"true", "1", "yes", "on", "raw", "verbose"}
        return bool(value)
    if setting == "memory_notifications":
        if isinstance(value, bool):
            return "on" if value else "off"
        val = str(value).strip().lower()
        return val if val in ("off", "on", "verbose") else "on"
    if setting == "cleanup_progress":
        if isinstance(value, str):
            return value.lower() in {"true", "1", "yes", "on"}
        return bool(value)
    if setting == "live_status":
        # Tri-state: "full" (verb + preview), "verb" (verb only), "off".
        if value is True:
            return "full"
        if value is False:
            return "off"
        val = str(value).strip().lower()
        if val in {"true", "1", "yes", "on", "all"}:
            return "full"
        if val in {"false", "0", "no"}:
            return "off"
        return val if val in {"full", "verb", "off"} else "full"
    if setting == "tool_progress_grouping":
        val = str(value).lower()
        return val if val in ("accumulate", "separate") else "accumulate"
    if setting == "reasoning_style":
        val = str(value).lower()
        return val if val in ("code", "blockquote", "subtext") else "code"
    if setting == "tool_preview_length":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return value


# ---------------------------------------------------------------------------
# Event classes and per-platform delivery policy (fi_a18c64a3)
# ---------------------------------------------------------------------------
# Explicit taxonomy for the gateway-emitted signal types so that platforms
# whose transports are permanent-write (no message edit, no ephemeral typing
# text) can route each class to the surface that fits it — instead of
# defaulting to "post another durable chat message". Each class carries a
# short intent contract:
#
#   tool_telemetry  — running-tool progress, "still working" heartbeats,
#                     compact status bubbles. High-frequency, low-value once
#                     the turn ends. On permanent-write platforms it MUST NOT
#                     durably post to chat.
#   milestone       — meaningful mid-turn events (interim assistant
#                     commentary, streamed chunks). Should reach chat.
#   terminal_result — the single final response for a turn. Always chat;
#                     exactly one per turn regardless of platform.
#   agent_activity  — background/adjacent-agent notices unrelated to the
#                     user's current question (background_review
#                     Self-improvement summaries, memory-updated notices,
#                     compaction receipts). Never unsolicited channel spam.
#   lifecycle       — gateway lifecycle warnings the user didn't ask for
#                     (Gateway shutting down / restarting interrupt hints,
#                     drain notices). Operator surface only.

EVENT_TOOL_TELEMETRY = "tool_telemetry"
EVENT_MILESTONE = "milestone"
EVENT_TERMINAL_RESULT = "terminal_result"
EVENT_AGENT_ACTIVITY = "agent_activity"
EVENT_LIFECYCLE = "lifecycle"

EVENT_CLASSES = frozenset({
    EVENT_TOOL_TELEMETRY,
    EVENT_MILESTONE,
    EVENT_TERMINAL_RESULT,
    EVENT_AGENT_ACTIVITY,
    EVENT_LIFECYCLE,
})

# Delivery targets. A resolver returns exactly one of these strings — the
# caller decides how to render it (send() to chat, publish a working
# indicator, log to audit, notify the operator surface, or suppress).
DELIVERY_CHAT = "chat"          # durable in-conversation message
DELIVERY_EPHEMERAL = "ephemeral"  # transient/composer working indicator
DELIVERY_AUDIT = "audit"        # audit log / activity feed, no chat
DELIVERY_OPERATOR = "operator"  # operator / home-channel surface, no chat
DELIVERY_OFF = "off"            # suppressed entirely

DELIVERY_TARGETS = frozenset({
    DELIVERY_CHAT,
    DELIVERY_EPHEMERAL,
    DELIVERY_AUDIT,
    DELIVERY_OPERATOR,
    DELIVERY_OFF,
})

# Global default policy — pre-fi_a18c64a3 behaviour for legacy platforms
# that already accept durable telemetry / activity / lifecycle posts.
_EVENT_GLOBAL_DEFAULTS: dict[str, str] = {
    EVENT_TOOL_TELEMETRY: DELIVERY_CHAT,
    EVENT_MILESTONE: DELIVERY_CHAT,
    EVENT_TERMINAL_RESULT: DELIVERY_CHAT,
    EVENT_AGENT_ACTIVITY: DELIVERY_CHAT,
    EVENT_LIFECYCLE: DELIVERY_CHAT,
}

# Per-platform policy overrides. Keys omitted here fall back to the global
# default. Buzz is the flagship case — a permanent-write Nostr relay where
# every send is a durable channel event; each event class must be routed
# to its natural surface instead of dumping into the conversation.
_PLATFORM_EVENT_DELIVERY: dict[str, dict[str, str]] = {
    "buzz": {
        EVENT_TOOL_TELEMETRY: DELIVERY_EPHEMERAL,
        EVENT_MILESTONE: DELIVERY_CHAT,
        EVENT_TERMINAL_RESULT: DELIVERY_CHAT,
        EVENT_AGENT_ACTIVITY: DELIVERY_AUDIT,
        EVENT_LIFECYCLE: DELIVERY_OPERATOR,
    },
}


def _normalise_delivery(value: Any) -> Any:
    """Coerce a user-supplied delivery value into a canonical target string."""
    if value is None:
        return None
    if isinstance(value, bool):
        return DELIVERY_CHAT if value else DELIVERY_OFF
    val = str(value).strip().lower()
    if val in {"true", "yes", "on", "1"}:
        return DELIVERY_CHAT
    if val in {"false", "no", "0", "none", "null"}:
        return DELIVERY_OFF
    return val if val in DELIVERY_TARGETS else None


def resolve_event_delivery(
    user_config: dict,
    platform_key: str,
    event_class: str,
) -> str:
    """Resolve the delivery target for one event class on one platform.

    Resolution order (first non-None wins):
        1. ``display.platforms.<platform>.event_delivery.<class>`` — explicit
           per-platform user override.
        2. ``display.event_delivery.<class>`` — global user override.
        3. ``_PLATFORM_EVENT_DELIVERY[<platform>][<class>]`` — built-in
           per-platform default.
        4. ``_EVENT_GLOBAL_DEFAULTS[<class>]`` — built-in global default
           (``DELIVERY_CHAT`` for every class).

    Unknown ``event_class`` values raise ``ValueError`` — callers must use
    one of the ``EVENT_*`` constants so typos are surfaced loudly instead
    of silently degrading to the legacy chat behaviour.
    """
    if event_class not in EVENT_CLASSES:
        raise ValueError(f"Unknown event_class: {event_class!r}")

    display_cfg = user_config.get("display") if isinstance(user_config, dict) else None
    display_cfg = display_cfg or {}

    # 1. Explicit per-platform override
    platforms = display_cfg.get("platforms") or {}
    plat_overrides = platforms.get(platform_key)
    if isinstance(plat_overrides, dict):
        plat_events = plat_overrides.get("event_delivery")
        if isinstance(plat_events, dict):
            val = _normalise_delivery(plat_events.get(event_class))
            if val is not None:
                return val

    # 2. Global user override
    global_events = display_cfg.get("event_delivery")
    if isinstance(global_events, dict):
        val = _normalise_delivery(global_events.get(event_class))
        if val is not None:
            return val

    # 3. Built-in per-platform default
    plat_defaults = _PLATFORM_EVENT_DELIVERY.get(platform_key)
    if plat_defaults:
        val = plat_defaults.get(event_class)
        if val is not None:
            return val

    # 4. Built-in global default
    return _EVENT_GLOBAL_DEFAULTS[event_class]
