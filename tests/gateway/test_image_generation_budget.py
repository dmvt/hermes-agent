"""Tests for fi_b6ce3936 — image-generation budget, lifecycle, validation.

Covers the four contract pieces the triage calls out:

1. Per-profile iteration + wall-clock budget (image-producing profiles
   inherit a small cap without touching the disk-canonical profile tree).
2. Lifecycle state machine — legal transitions surface as milestone events,
   illegal ones raise, terminal states stay terminal.
3. Artifact validation — files missing / empty / non-image / too-small get
   rejected with an actionable, correlation-id-tagged error.
4. Clean termination — timeout / cancel / provider failure funnel through
   ``fail()`` and produce a single ``ImageGenerationError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from gateway import image_generation as ig


# ---------------------------------------------------------------------------
# Budget resolution
# ---------------------------------------------------------------------------


def test_default_image_generation_budget_is_small():
    """No config, no env → tight default budget (<=25 iters, bounded wall clock)."""
    budget = ig.image_generation_budget()
    assert budget.max_iterations <= 25
    assert budget.max_iterations == ig.DEFAULT_IMAGE_MAX_ITERATIONS
    assert 0 < budget.wall_clock_seconds <= 600


def test_config_overrides_image_budget():
    """agent.image_generation.* in config.yaml wins over env / defaults."""
    cfg = {
        "agent": {
            "image_generation": {
                "max_iterations": 12,
                "wall_clock_seconds": 90.0,
            }
        }
    }
    budget = ig.image_generation_budget(cfg)
    assert budget.max_iterations == 12
    assert budget.wall_clock_seconds == pytest.approx(90.0)


def test_env_overrides_image_budget_when_config_absent(monkeypatch):
    """HERMES_IMAGE_GENERATION_MAX_ITERATIONS / _WALL_CLOCK_SECONDS take effect."""
    monkeypatch.setenv("HERMES_IMAGE_GENERATION_MAX_ITERATIONS", "8")
    monkeypatch.setenv("HERMES_IMAGE_GENERATION_WALL_CLOCK_SECONDS", "45")
    budget = ig.image_generation_budget()
    assert budget.max_iterations == 8
    assert budget.wall_clock_seconds == pytest.approx(45.0)


def test_bad_config_falls_back_to_defaults():
    """Non-int / non-positive values fall through instead of raising."""
    cfg = {
        "agent": {
            "image_generation": {
                "max_iterations": "not-a-number",
                "wall_clock_seconds": -5,
            }
        }
    }
    budget = ig.image_generation_budget(cfg)
    assert budget.max_iterations == ig.DEFAULT_IMAGE_MAX_ITERATIONS
    assert budget.wall_clock_seconds == ig.DEFAULT_IMAGE_WALL_CLOCK_SECONDS


def test_clamp_iterations_intersects_with_global_cap():
    """The image cap always wins against a larger global HERMES_MAX_ITERATIONS."""
    budget = ig.ImageGenerationBudget(max_iterations=25, wall_clock_seconds=180)
    assert budget.clamp_iterations(500) == 25
    assert budget.clamp_iterations(10) == 10  # smaller global still wins
    assert budget.clamp_iterations(0) == 25   # zero → treat as no global cap


def test_runyard_image_director_is_image_producing_by_default():
    """Built-in list includes the canonical profile from the triage."""
    assert "runyard-image-director" in ig.image_generation_profile_names()
    assert ig.is_image_generation_profile("runyard-image-director")
    assert not ig.is_image_generation_profile("default")
    assert not ig.is_image_generation_profile(None)


def test_env_extends_but_does_not_remove_builtin_profiles(monkeypatch):
    """The env only adds — runyard-image-director always inherits the cap."""
    monkeypatch.setenv("HERMES_IMAGE_GENERATION_PROFILES", "artbot,another-director")
    names = ig.image_generation_profile_names()
    assert "runyard-image-director" in names
    assert "artbot" in names
    assert "another-director" in names


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _record_emissions() -> Tuple[List[Tuple[str, Dict[str, Any]]], ig.LifecycleEmitter]:
    """Return a list-plus-emitter pair that records every lifecycle emission."""
    recorded: List[Tuple[str, Dict[str, Any]]] = []

    def emitter(event_class: str, payload: Dict[str, Any]) -> None:
        recorded.append((event_class, dict(payload)))

    return recorded, emitter


def test_lifecycle_happy_path_emits_milestone_events():
    """queued → generating → validating → delivered emits four milestones."""
    events, emitter = _record_emissions()
    lifecycle = ig.ImageGenerationLifecycle(emit=emitter)
    lifecycle.transition(ig.LIFECYCLE_GENERATING)
    lifecycle.transition(ig.LIFECYCLE_VALIDATING)
    lifecycle.deliver(Path("/tmp/does-not-matter.png"))

    # queued was seeded via __post_init__ but not emitted (no transition).
    assert [e[1]["state"] for e in events] == [
        ig.LIFECYCLE_GENERATING,
        ig.LIFECYCLE_VALIDATING,
        ig.LIFECYCLE_DELIVERED,
    ]
    # Every event uses the milestone event class per fi_a18c64a3.
    assert all(cls == "milestone" for cls, _ in events)
    # Correlation id is stable across transitions.
    ids = {payload["correlation_id"] for _, payload in events}
    assert len(ids) == 1
    # Elapsed seconds are recorded, monotonic, and non-negative.
    assert all(payload["elapsed_seconds"] >= 0 for _, payload in events)


def test_lifecycle_illegal_transition_raises():
    """You cannot skip validation on the way to delivered."""
    lifecycle = ig.ImageGenerationLifecycle()
    lifecycle.transition(ig.LIFECYCLE_GENERATING)
    with pytest.raises(ig.InvalidLifecycleTransition):
        lifecycle.transition(ig.LIFECYCLE_DELIVERED)


def test_lifecycle_terminal_states_are_terminal():
    """Once delivered/failed, no further transitions are allowed."""
    lifecycle = ig.ImageGenerationLifecycle()
    lifecycle.transition(ig.LIFECYCLE_GENERATING)
    lifecycle.fail("provider", "downstream 500")
    with pytest.raises(ig.InvalidLifecycleTransition):
        lifecycle.transition(ig.LIFECYCLE_GENERATING)
    with pytest.raises(ig.InvalidLifecycleTransition):
        lifecycle.transition(ig.LIFECYCLE_FAILED)


def test_lifecycle_fail_returns_correlation_id_tagged_error():
    """fail() surfaces a single ImageGenerationError with a stable id."""
    lifecycle = ig.ImageGenerationLifecycle(correlation_id="deadbeef1234")
    lifecycle.transition(ig.LIFECYCLE_GENERATING)
    err = lifecycle.fail("timeout", "provider exceeded 180s wall clock")
    assert isinstance(err, ig.ImageGenerationError)
    assert err.correlation_id == "deadbeef1234"
    assert err.reason == "timeout"
    assert "180s" in err.message
    assert "deadbeef1234" in str(err)


def test_lifecycle_fail_is_valid_from_any_non_terminal_state():
    """Timeout / cancel / provider fault can strike at any live state."""
    for entry in (ig.LIFECYCLE_QUEUED, ig.LIFECYCLE_GENERATING, ig.LIFECYCLE_VALIDATING):
        lifecycle = ig.ImageGenerationLifecycle()
        # Walk forward to the entry state.
        cursor = ig.LIFECYCLE_QUEUED
        for target in (ig.LIFECYCLE_GENERATING, ig.LIFECYCLE_VALIDATING):
            if cursor == entry:
                break
            lifecycle.transition(target)
            cursor = target
        assert lifecycle.state == entry
        err = lifecycle.fail("cancelled", "operator cancelled request")
        assert err.reason == "cancelled"
        assert lifecycle.state == ig.LIFECYCLE_FAILED


def test_lifecycle_emitter_exceptions_do_not_break_state_machine():
    """A crashing telemetry sink must not corrupt the lifecycle transition."""

    def bad_emitter(event_class: str, payload: Dict[str, Any]) -> None:
        raise RuntimeError("boom")

    lifecycle = ig.ImageGenerationLifecycle(emit=bad_emitter)
    lifecycle.transition(ig.LIFECYCLE_GENERATING)  # must not raise
    assert lifecycle.state == ig.LIFECYCLE_GENERATING


# ---------------------------------------------------------------------------
# Artifact validation
# ---------------------------------------------------------------------------


def _write_png(path: Path, size: Tuple[int, int] = (64, 64)) -> None:
    """Write a real PNG at ``path``. Skips the test if Pillow is missing."""
    try:
        from PIL import Image
    except Exception:  # pragma: no cover — Pillow is a hard test dep here
        pytest.skip("Pillow not available in venv")
    img = Image.new("RGB", size, color=(200, 30, 60))
    img.save(path, format="PNG")


def test_validate_accepts_real_png(tmp_path):
    path = tmp_path / "artifact.png"
    _write_png(path)
    result = ig.validate_image_artifact(path, correlation_id="abc123")
    assert result.path == path
    assert result.format == "PNG"
    assert result.width == 64 and result.height == 64
    assert result.size_bytes > 0


def test_validate_rejects_missing_file(tmp_path):
    with pytest.raises(ig.ImageGenerationError) as excinfo:
        ig.validate_image_artifact(tmp_path / "nope.png", correlation_id="cid-miss")
    err = excinfo.value
    assert err.reason == "artifact"
    assert "cid-miss" in str(err)
    assert "no file exists" in err.message


def test_validate_rejects_empty_file(tmp_path):
    path = tmp_path / "empty.png"
    path.write_bytes(b"")
    with pytest.raises(ig.ImageGenerationError) as excinfo:
        ig.validate_image_artifact(path, correlation_id="cid-empty")
    assert excinfo.value.reason == "artifact"
    assert "empty" in excinfo.value.message


def test_validate_rejects_non_image_bytes(tmp_path):
    path = tmp_path / "text.png"
    path.write_bytes(b"this is definitely not an image")
    with pytest.raises(ig.ImageGenerationError) as excinfo:
        ig.validate_image_artifact(path, correlation_id="cid-bad")
    err = excinfo.value
    assert err.reason == "artifact"
    assert "cid-bad" in str(err)


def test_validate_rejects_image_below_min_dimension(tmp_path):
    path = tmp_path / "tiny.png"
    _write_png(path, size=(4, 4))
    with pytest.raises(ig.ImageGenerationError) as excinfo:
        ig.validate_image_artifact(path, min_dimension=16, correlation_id="cid-tiny")
    assert "below the minimum" in excinfo.value.message


def test_validate_rejects_image_above_max_dimension(tmp_path):
    path = tmp_path / "big.png"
    _write_png(path, size=(64, 64))
    with pytest.raises(ig.ImageGenerationError) as excinfo:
        ig.validate_image_artifact(
            path, max_dimension=32, correlation_id="cid-big"
        )
    assert "exceed the maximum" in excinfo.value.message


def test_validate_rejects_directory(tmp_path):
    with pytest.raises(ig.ImageGenerationError) as excinfo:
        ig.validate_image_artifact(tmp_path, correlation_id="cid-dir")
    assert excinfo.value.reason == "artifact"
    assert "not a regular file" in excinfo.value.message


def test_validate_generates_correlation_id_when_absent(tmp_path):
    """Ad-hoc callers without a correlation id still get a tag on the error."""
    with pytest.raises(ig.ImageGenerationError) as excinfo:
        ig.validate_image_artifact(tmp_path / "missing.png")
    assert len(excinfo.value.correlation_id) > 0


# ---------------------------------------------------------------------------
# Clean termination — timeout / cancel / provider failure produce a single
# actionable error, never an unbounded retry or a "success without artifact".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason,message",
    [
        ("timeout", "wall clock exceeded 180s"),
        ("cancelled", "operator cancelled"),
        ("provider", "provider returned 502"),
    ],
)
def test_clean_termination_paths_produce_single_error(reason, message):
    events, emitter = _record_emissions()
    lifecycle = ig.ImageGenerationLifecycle(emit=emitter)
    lifecycle.transition(ig.LIFECYCLE_GENERATING)
    err = lifecycle.fail(reason, message)
    # Exactly one failure milestone was emitted for this termination.
    fail_events = [e for e in events if e[1]["state"] == ig.LIFECYCLE_FAILED]
    assert len(fail_events) == 1
    assert fail_events[0][1]["reason"] == reason
    assert fail_events[0][1]["message"] == message
    # The returned error carries the same correlation id as the milestone.
    assert err.correlation_id == fail_events[0][1]["correlation_id"]
    # The lifecycle refuses to transition again — no retry loop possible.
    assert lifecycle.state == ig.LIFECYCLE_FAILED
    with pytest.raises(ig.InvalidLifecycleTransition):
        lifecycle.deliver(Path("/tmp/should-not-succeed.png"))
