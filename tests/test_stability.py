"""Requiring a line to repeat at the same place.

This is the answer to "the camera moves, so world text drifts into my chat box".
Chat sits still for seconds; world text and damage numbers do not.
"""

from __future__ import annotations

from translator.core.types import Source, TextRegion
from translator.ocr.stability import StabilityGate


def _r(text: str, x: int = 10, y: int = 20) -> TextRegion:
    return TextRegion(text=text, source=Source.SCREEN, bbox=(x, y, x + 100, y + 18))


def test_disabled_gate_returns_everything():
    gate = StabilityGate(confirm_frames=1)
    regions = [_r("gg wp"), _r("+150")]
    accepted, rejected = gate.filter(regions)
    assert accepted == regions
    assert rejected == 0
    assert gate.enabled is False


def test_a_line_near_a_static_line_is_not_confirmed_by_it():
    """Matching is exact on text: different text never inherits confirmation."""
    gate = StabilityGate(confirm_frames=2)
    gate.filter([_r("first")])
    accepted, _ = gate.filter([_r("second", 10, 20)])
    assert accepted == []


def test_static_line_is_accepted_on_the_second_pass():
    gate = StabilityGate(confirm_frames=2)
    first, rejected = gate.filter([_r("PlayerOne: rush b")])
    assert first == [] and rejected == 1, "first sighting is held back"

    second, _ = gate.filter([_r("PlayerOne: rush b")])
    assert [r.text for r in second] == ["PlayerOne: rush b"]


def test_moving_world_text_never_passes():
    """A sign sliding across the box changes position every frame."""
    gate = StabilityGate(confirm_frames=2)
    for offset in range(0, 200, 20):
        accepted, _ = gate.filter([_r("PALACE BELL", 10 + offset, 300)])
        assert accepted == [], f"accepted a moving sign at offset {offset}"


def test_transient_text_never_passes():
    """Damage numbers appear for a single pass."""
    gate = StabilityGate(confirm_frames=2)
    assert gate.filter([_r("83", 400, 500)])[0] == []
    assert gate.filter([_r("98", 420, 460)])[0] == []
    assert gate.filter([_r("31", 380, 540)])[0] == []


def test_three_frames_needs_three_sightings():
    gate = StabilityGate(confirm_frames=3)
    assert gate.filter([_r("gg")])[0] == []
    assert gate.filter([_r("gg")])[0] == []
    assert [r.text for r in gate.filter([_r("gg")])[0]] == ["gg"]


def test_missing_a_pass_resets_the_count():
    """A line that disappears and comes back is treated as new, so a chat line
    scrolled away and a new message at the same spot are not conflated."""
    gate = StabilityGate(confirm_frames=2)
    gate.filter([_r("gg")])
    gate.filter([])  # nothing this pass
    assert gate.filter([_r("gg")])[0] == [], "count must have been reset"
    assert [r.text for r in gate.filter([_r("gg")])[0]] == ["gg"]


def test_small_position_jitter_still_confirms():
    """OCR boxes wobble by a pixel or two; that must not reset the count."""
    gate = StabilityGate(confirm_frames=2)
    gate.filter([_r("TeamMate: push", 10, 20)])
    accepted, _ = gate.filter([_r("TeamMate: push", 12, 21)])
    assert len(accepted) == 1


def test_text_case_and_spacing_jitter_still_confirms():
    gate = StabilityGate(confirm_frames=2)
    gate.filter([_r("GG  WP")])
    assert gate.filter([_r("gg wp")])[0]


def test_a_region_without_a_bbox_is_matched_on_text_alone():
    gate = StabilityGate(confirm_frames=2)
    bare = TextRegion(text="gg", source=Source.SCREEN)
    gate.filter([bare])
    assert gate.filter([bare])[0], "no geometry means no position drift"


def test_reset_clears_history():
    gate = StabilityGate(confirm_frames=2)
    gate.filter([_r("gg")])
    gate.reset()
    assert gate.filter([_r("gg")])[0] == []


def test_pipeline_reports_unstable_lines_separately():
    """The user must be able to tell "dropped as junk" from "dropped as moving"."""
    from translator.core.config import AppConfig

    cfg = AppConfig()
    cfg.filters.confirm_frames = 2
    from translator.ocr.stability import StabilityGate

    gate = StabilityGate(cfg.filters.confirm_frames)
    accepted, rejected = gate.filter([_r("83")])
    assert accepted == [] and rejected == 1
