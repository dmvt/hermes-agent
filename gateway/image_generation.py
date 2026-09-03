"""Per-profile image-generation budget, lifecycle, and artifact validation.

This module targets fi_b6ce3936: prior to this change, an image-producing
profile (canonical example: ``runyard-image-director``) inherited the global
``HERMES_MAX_ITERATIONS`` default of 500 turns via ``_current_max_iterations``
in :mod:`gateway.run`. That let an "Image Director" request report progress
like ``iteration 8/500`` and terminate as "success" without ever producing or
validating an image artifact.

The module supplies four things the gateway wires in:

1. A **per-profile iteration + wall-clock budget** — resolved from env / gateway
   config with a sane, small default. ``runyard-image-director`` (and any
   caller-declared image-producing profile) inherits the budget without having
   to hand-edit the disk-canonical ``~/.hermes/profiles/<name>`` tree.

2. An **explicit lifecycle** — ``queued → generating → validating → delivered``
   or ``queued → … → failed`` — emitted through the fi_a18c64a3 event-classed
   progress path as :data:`gateway.display_config.EVENT_MILESTONE`. Lifecycle
   changes are meaningful mid-turn events, not tool_telemetry heartbeats.

3. An **artifact validation step** — the file must exist, be non-empty, decode
   as a real image (Pillow when available; verified once at import time), and
   have sane dimensions. On rejection the request terminates ``failed`` with a
   single actionable error carrying a correlation id.

4. **Clean termination** on timeout / cancel / provider failure — no infinite
   retry, no "success without an artifact".

The gateway calls :func:`image_generation_budget` to size a turn's iteration
cap and :func:`validate_image_artifact` at delivery; adapters observe
lifecycle transitions through :class:`ImageGenerationLifecycle`.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Budget resolution
# ---------------------------------------------------------------------------

# Defaults chosen small on purpose — the triage in fi_b6ce3936 called out
# "e.g. <=25 iterations and a bounded wall clock" as an upper bound for a
# single image-producing turn. Anything larger is almost certainly a signal
# that the provider is looping without producing an artifact.
DEFAULT_IMAGE_MAX_ITERATIONS = 25
DEFAULT_IMAGE_WALL_CLOCK_SECONDS = 180

# Built-in list of profiles that are image-producing by convention. The
# ``runyard-image-director`` profile is the canonical case the triage names.
# Operators can extend or replace this via HERMES_IMAGE_GENERATION_PROFILES
# (comma-separated) without touching the disk-canonical profile tree.
BUILTIN_IMAGE_GENERATION_PROFILES: frozenset[str] = frozenset({
    "runyard-image-director",
})

_ENV_MAX_ITERATIONS = "HERMES_IMAGE_GENERATION_MAX_ITERATIONS"
_ENV_WALL_CLOCK = "HERMES_IMAGE_GENERATION_WALL_CLOCK_SECONDS"
_ENV_PROFILES = "HERMES_IMAGE_GENERATION_PROFILES"


@dataclass(frozen=True)
class ImageGenerationBudget:
    """Resolved iteration + wall-clock cap for an image-producing turn.

    Both fields are strictly positive. ``max_iterations`` caps the AI loop's
    api-call count; ``wall_clock_seconds`` caps end-to-end request latency
    (queued through delivered/failed).
    """

    max_iterations: int
    wall_clock_seconds: float

    def clamp_iterations(self, other: int) -> int:
        """Return the smaller of ``other`` and this budget's iteration cap.

        Callers use this to intersect the image-generation budget with a
        larger global ``HERMES_MAX_ITERATIONS`` — the image ceiling wins so
        no image-producing turn ever runs the 500-iteration global default.
        """
        if other <= 0:
            return self.max_iterations
        return min(other, self.max_iterations)


def _positive_int(value: Any, fallback: int) -> int:
    """Coerce ``value`` to a positive int, else return ``fallback``."""
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return fallback
    return candidate if candidate > 0 else fallback


def _positive_float(value: Any, fallback: float) -> float:
    """Coerce ``value`` to a positive float, else return ``fallback``."""
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return fallback
    return candidate if candidate > 0 else fallback


def image_generation_budget(user_config: Optional[dict] = None) -> ImageGenerationBudget:
    """Resolve the current image-generation budget.

    Resolution order (first present wins per field):
        1. ``agent.image_generation.max_iterations`` / ``wall_clock_seconds``
           in ``user_config`` — the documented, config.yaml-owned surface.
        2. :data:`_ENV_MAX_ITERATIONS` / :data:`_ENV_WALL_CLOCK` env override
           — for gateway operators that manage limits at deploy time.
        3. Built-in defaults (:data:`DEFAULT_IMAGE_MAX_ITERATIONS`,
           :data:`DEFAULT_IMAGE_WALL_CLOCK_SECONDS`).

    A malformed value at any layer falls through to the next — never raises.
    """
    max_iterations: Optional[int] = None
    wall_clock: Optional[float] = None

    if isinstance(user_config, dict):
        agent_cfg = user_config.get("agent") or {}
        if isinstance(agent_cfg, dict):
            img_cfg = agent_cfg.get("image_generation") or {}
            if isinstance(img_cfg, dict):
                if "max_iterations" in img_cfg:
                    max_iterations = _positive_int(
                        img_cfg.get("max_iterations"), 0
                    ) or None
                if "wall_clock_seconds" in img_cfg:
                    wall_clock = _positive_float(
                        img_cfg.get("wall_clock_seconds"), 0.0
                    ) or None

    if max_iterations is None:
        env_iter = os.getenv(_ENV_MAX_ITERATIONS)
        if env_iter is not None:
            max_iterations = _positive_int(env_iter, 0) or None
    if wall_clock is None:
        env_wall = os.getenv(_ENV_WALL_CLOCK)
        if env_wall is not None:
            wall_clock = _positive_float(env_wall, 0.0) or None

    if max_iterations is None:
        max_iterations = DEFAULT_IMAGE_MAX_ITERATIONS
    if wall_clock is None:
        wall_clock = DEFAULT_IMAGE_WALL_CLOCK_SECONDS

    return ImageGenerationBudget(
        max_iterations=max_iterations,
        wall_clock_seconds=wall_clock,
    )


def image_generation_profile_names() -> frozenset[str]:
    """Return the set of profile names treated as image-producing.

    Operators can extend the built-in set (which always contains
    ``runyard-image-director``) via :data:`_ENV_PROFILES` (comma-separated).
    An empty entry in the env value is ignored; unknown / whitespace-only
    values are trimmed. The env layer only *adds* to the built-in set — it
    cannot remove ``runyard-image-director`` since the triage requires it to
    inherit the tighter budget by default.
    """
    extras: set[str] = set()
    raw = os.getenv(_ENV_PROFILES, "")
    for chunk in raw.split(","):
        name = chunk.strip()
        if name:
            extras.add(name)
    return frozenset(BUILTIN_IMAGE_GENERATION_PROFILES | extras)


def is_image_generation_profile(profile_name: Optional[str]) -> bool:
    """Return True when ``profile_name`` is one of the image-producing set."""
    if not profile_name:
        return False
    return profile_name in image_generation_profile_names()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

# Explicit lifecycle states. Deliberately kept as a small, ordered string
# taxonomy (not an ``enum.Enum``) so downstream consumers — adapters, tests,
# operator dashboards — can compare against plain string constants without
# importing this module.
LIFECYCLE_QUEUED = "queued"
LIFECYCLE_GENERATING = "generating"
LIFECYCLE_VALIDATING = "validating"
LIFECYCLE_DELIVERED = "delivered"
LIFECYCLE_FAILED = "failed"

LIFECYCLE_STATES: Tuple[str, ...] = (
    LIFECYCLE_QUEUED,
    LIFECYCLE_GENERATING,
    LIFECYCLE_VALIDATING,
    LIFECYCLE_DELIVERED,
    LIFECYCLE_FAILED,
)

# Terminal states: once a request enters one of these, no further transitions
# are allowed. ``delivered`` marks a successful, validated artifact; ``failed``
# marks a clean termination with a single actionable error.
TERMINAL_LIFECYCLE_STATES: frozenset[str] = frozenset({
    LIFECYCLE_DELIVERED,
    LIFECYCLE_FAILED,
})

# Valid forward transitions. Failure is a legal transition from any
# non-terminal state (timeout / cancel / provider failure); success requires
# passing through ``validating`` first — you cannot go straight from
# ``generating`` to ``delivered`` without validating an artifact.
_ALLOWED_TRANSITIONS: Dict[str, frozenset[str]] = {
    LIFECYCLE_QUEUED: frozenset({LIFECYCLE_GENERATING, LIFECYCLE_FAILED}),
    LIFECYCLE_GENERATING: frozenset({LIFECYCLE_VALIDATING, LIFECYCLE_FAILED}),
    LIFECYCLE_VALIDATING: frozenset({LIFECYCLE_DELIVERED, LIFECYCLE_FAILED}),
    LIFECYCLE_DELIVERED: frozenset(),
    LIFECYCLE_FAILED: frozenset(),
}


class InvalidLifecycleTransition(RuntimeError):
    """Raised when a lifecycle transition would violate the state machine."""


@dataclass
class ImageGenerationError(Exception):
    """A single actionable error terminating an image-generation request.

    Carries a ``correlation_id`` so operators can join lifecycle events across
    the progress path, gateway log, and provider trace. ``reason`` is a
    stable short slug (``timeout``, ``cancelled``, ``provider``, ``artifact``);
    ``message`` is the human-readable, actionable description.
    """

    reason: str
    message: str
    correlation_id: str

    def __str__(self) -> str:  # pragma: no cover — trivial format
        return f"[{self.correlation_id}] {self.reason}: {self.message}"


LifecycleEmitter = Callable[[str, Dict[str, Any]], None]


@dataclass
class ImageGenerationLifecycle:
    """State machine + event emitter for a single image-generation request.

    Callers construct one per request, then invoke :meth:`transition` at each
    lifecycle boundary. Every transition is dispatched to ``emit`` as a
    ``(event_class, payload)`` pair where ``event_class`` is the fi_a18c64a3
    milestone class string (fetched lazily so tests don't require the full
    display-config import chain). The payload carries the correlation id,
    the previous/next state, and elapsed seconds since ``queued``.

    ``fail`` is a convenience terminator: it moves the state machine to
    ``failed`` and raises :class:`ImageGenerationError`. ``deliver`` requires
    the caller to have already validated the artifact (i.e. it enforces the
    ``validating → delivered`` transition).
    """

    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    emit: Optional[LifecycleEmitter] = None
    state: str = LIFECYCLE_QUEUED
    started_at: float = field(default_factory=time.monotonic)
    history: List[Tuple[str, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Seed history with the initial state so consumers see a clean
        # queued→… trail even when the first transition is failure.
        self.history.append((self.state, 0.0))

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def transition(self, new_state: str, **extra: Any) -> None:
        """Move to ``new_state``, emitting a milestone event.

        Raises :class:`InvalidLifecycleTransition` when the transition is not
        allowed by the state machine — this is a programming error, not a
        provider fault, so we surface it loudly instead of letting an image
        request quietly skip validation.
        """
        if new_state not in LIFECYCLE_STATES:
            raise InvalidLifecycleTransition(
                f"unknown lifecycle state: {new_state!r}"
            )
        allowed = _ALLOWED_TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise InvalidLifecycleTransition(
                f"illegal image-generation transition {self.state!r} → {new_state!r}"
            )
        previous = self.state
        self.state = new_state
        elapsed = time.monotonic() - self.started_at
        self.history.append((new_state, elapsed))
        self._emit(previous, new_state, elapsed, extra)

    def deliver(self, artifact_path: Path, **extra: Any) -> None:
        """Terminate as delivered, requiring the artifact path be recorded."""
        payload = dict(extra)
        payload.setdefault("artifact", str(artifact_path))
        self.transition(LIFECYCLE_DELIVERED, **payload)

    def fail(
        self,
        reason: str,
        message: str,
        **extra: Any,
    ) -> ImageGenerationError:
        """Move to ``failed`` and return an :class:`ImageGenerationError`.

        Returning (rather than raising) lets callers decide whether to raise
        or to funnel the error into an existing error-reporting surface.
        The lifecycle transition itself is unconditional: subsequent calls
        to ``transition`` will refuse to move the state machine further.
        """
        # Failure is a valid transition from every non-terminal state, so a
        # caller can invoke fail() at any point. We still route through the
        # state-machine check so a double-fail (or a fail after deliver)
        # surfaces as InvalidLifecycleTransition instead of silently
        # emitting a duplicate milestone.
        payload = dict(extra)
        payload["reason"] = reason
        payload["message"] = message
        self.transition(LIFECYCLE_FAILED, **payload)
        return ImageGenerationError(
            reason=reason,
            message=message,
            correlation_id=self.correlation_id,
        )

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    def _emit(
        self,
        previous: str,
        new_state: str,
        elapsed: float,
        extra: Dict[str, Any],
    ) -> None:
        if self.emit is None:
            return
        event_class = _milestone_event_class()
        payload: Dict[str, Any] = {
            "correlation_id": self.correlation_id,
            "previous_state": previous,
            "state": new_state,
            "elapsed_seconds": round(elapsed, 3),
        }
        payload.update(extra)
        try:
            self.emit(event_class, payload)
        except Exception as exc:  # noqa: BLE001 — telemetry MUST NOT crash the turn
            logger.debug(
                "image-generation lifecycle emit failed (correlation_id=%s state=%s): %s",
                self.correlation_id,
                new_state,
                exc,
            )


def _milestone_event_class() -> str:
    """Return the ``EVENT_MILESTONE`` constant, imported lazily.

    Kept lazy so unit tests can exercise the lifecycle without pulling in the
    full ``gateway.display_config`` import graph. Falls back to the literal
    string ``"milestone"`` if the display-config module ever fails to import
    — the string is the wire-level identifier the delivery resolver expects.
    """
    try:
        from gateway.display_config import EVENT_MILESTONE  # local import: see docstring
        return EVENT_MILESTONE
    except Exception:  # noqa: BLE001 — fallback keeps lifecycle emission alive
        return "milestone"


# ---------------------------------------------------------------------------
# Artifact validation
# ---------------------------------------------------------------------------

# Minimum / maximum image dimensions considered "sane". A 1x1 pixel image is
# almost always a placeholder / redirect and never a real artifact; a
# 16384x16384 image (256 megapixels) is well past any legitimate generation
# output and usually indicates a decode bug or maliciously-crafted file.
DEFAULT_MIN_DIMENSION = 16
DEFAULT_MAX_DIMENSION = 16384


def _pillow_available() -> bool:
    """Return True iff Pillow is importable in the current venv.

    Cached at first call — Pillow either is or is not installed for the life
    of the process; re-importing on every artifact would waste cycles.
    """
    global _PILLOW_CACHED
    try:
        return _PILLOW_CACHED  # type: ignore[name-defined]
    except NameError:
        pass
    try:
        import PIL.Image  # noqa: F401 — availability probe
        _PILLOW_CACHED = True  # type: ignore[assignment]
    except Exception:
        _PILLOW_CACHED = False  # type: ignore[assignment]
    return _PILLOW_CACHED  # type: ignore[name-defined]


@dataclass(frozen=True)
class ValidatedImage:
    """The result of a successful :func:`validate_image_artifact` call."""

    path: Path
    format: str
    width: int
    height: int
    size_bytes: int


def validate_image_artifact(
    path: Any,
    *,
    min_dimension: int = DEFAULT_MIN_DIMENSION,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    correlation_id: Optional[str] = None,
) -> ValidatedImage:
    """Validate an image artifact and return its resolved metadata.

    Rejection modes (each raises :class:`ImageGenerationError` with
    ``reason="artifact"`` and an actionable, correlation-id-tagged message):
        * missing path / non-file entry
        * zero-length file
        * Pillow unavailable in the venv (we refuse to declare an image
          "validated" without a real decode step)
        * Pillow ``UnidentifiedImageError`` or any decode exception
        * dimensions outside ``[min_dimension, max_dimension]``

    The ``correlation_id`` argument is optional; when omitted a fresh 12-hex
    id is minted so ad-hoc callers still get a tag they can grep in logs.
    """
    cid = correlation_id or uuid.uuid4().hex[:12]
    resolved = Path(path) if not isinstance(path, Path) else path

    if not resolved.exists():
        raise ImageGenerationError(
            reason="artifact",
            message=(
                f"expected image artifact at {resolved} but no file exists; "
                "check provider output path and gateway delivery permissions"
            ),
            correlation_id=cid,
        )
    if not resolved.is_file():
        raise ImageGenerationError(
            reason="artifact",
            message=(
                f"artifact path {resolved} is not a regular file "
                "(refusing to deliver a directory / symlink loop / device node)"
            ),
            correlation_id=cid,
        )

    try:
        size_bytes = resolved.stat().st_size
    except OSError as exc:
        raise ImageGenerationError(
            reason="artifact",
            message=f"cannot stat artifact {resolved}: {exc}",
            correlation_id=cid,
        ) from exc

    if size_bytes <= 0:
        raise ImageGenerationError(
            reason="artifact",
            message=(
                f"artifact {resolved} is empty ({size_bytes} bytes); "
                "provider likely wrote a placeholder before failing"
            ),
            correlation_id=cid,
        )

    if not _pillow_available():
        raise ImageGenerationError(
            reason="artifact",
            message=(
                "Pillow is not importable in this venv; cannot validate that "
                f"{resolved} decodes as a real image. Install pillow or route "
                "the image request to a gateway with pillow available."
            ),
            correlation_id=cid,
        )

    try:
        from PIL import Image, UnidentifiedImageError

        with Image.open(resolved) as img:
            # ``verify()`` walks the file to confirm structural integrity but
            # closes the image afterwards — re-open below to read dimensions.
            img.verify()
        with Image.open(resolved) as img:
            fmt = str(img.format or "").upper() or "UNKNOWN"
            width, height = img.size
    except UnidentifiedImageError as exc:
        raise ImageGenerationError(
            reason="artifact",
            message=(
                f"file {resolved} does not decode as an image "
                f"(size={size_bytes} bytes); provider returned non-image bytes"
            ),
            correlation_id=cid,
        ) from exc
    except Exception as exc:  # noqa: BLE001 — every PIL failure is actionable
        raise ImageGenerationError(
            reason="artifact",
            message=(
                f"image decode failed for {resolved}: {exc}. "
                "Provider output is corrupted or truncated"
            ),
            correlation_id=cid,
        ) from exc

    if width < min_dimension or height < min_dimension:
        raise ImageGenerationError(
            reason="artifact",
            message=(
                f"image {resolved} dimensions {width}x{height} are below the "
                f"minimum of {min_dimension}px; likely a placeholder / 1x1 pixel"
            ),
            correlation_id=cid,
        )
    if width > max_dimension or height > max_dimension:
        raise ImageGenerationError(
            reason="artifact",
            message=(
                f"image {resolved} dimensions {width}x{height} exceed the "
                f"maximum of {max_dimension}px; suspected decode bug or "
                "adversarial file — refusing to deliver"
            ),
            correlation_id=cid,
        )

    return ValidatedImage(
        path=resolved,
        format=fmt,
        width=width,
        height=height,
        size_bytes=size_bytes,
    )


__all__ = [
    "BUILTIN_IMAGE_GENERATION_PROFILES",
    "DEFAULT_IMAGE_MAX_ITERATIONS",
    "DEFAULT_IMAGE_WALL_CLOCK_SECONDS",
    "DEFAULT_MAX_DIMENSION",
    "DEFAULT_MIN_DIMENSION",
    "ImageGenerationBudget",
    "ImageGenerationError",
    "ImageGenerationLifecycle",
    "InvalidLifecycleTransition",
    "LIFECYCLE_DELIVERED",
    "LIFECYCLE_FAILED",
    "LIFECYCLE_GENERATING",
    "LIFECYCLE_QUEUED",
    "LIFECYCLE_STATES",
    "LIFECYCLE_VALIDATING",
    "TERMINAL_LIFECYCLE_STATES",
    "ValidatedImage",
    "image_generation_budget",
    "image_generation_profile_names",
    "is_image_generation_profile",
    "validate_image_artifact",
]
