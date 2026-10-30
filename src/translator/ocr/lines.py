"""Post-processing: stitch OCR fragments back into readable lines.

The detector happily splits one chat message into several boxes (it boxes by
connected text, not by sentence). We merge horizontally-adjacent fragments that
share a baseline, but deliberately do **not** merge vertically: in an FPS chat
each visual line is usually a different player's message, and joining them
produces nonsense ("rush b" + "nice shot" -> "rush b nice shot").
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

from ..core.types import TextRegion

__all__ = ["merge_horizontal", "drop_duplicates", "drop_oversized"]


def _center_y(bbox: tuple[int, int, int, int]) -> float:
    return (bbox[1] + bbox[3]) / 2


def _height(bbox: tuple[int, int, int, int]) -> int:
    return max(1, bbox[3] - bbox[1])


def _union(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"


_TRAILING_PUNCT = ",.!?:;)]}"  # space goes *after* these
_LEADING_PUNCT = ",.!?:;)]}"  # no space goes *before* these


def _join(left: str, right: str) -> str:
    """Join two fragments with the separator English demands.

    Default is a space: the detector splits on glyph runs, so two adjacent boxes
    are almost always two words. We only omit it when the characters themselves
    say otherwise (punctuation, CJK, existing whitespace).
    """
    if not left:
        return right
    if not right:
        return left
    if left[-1].isspace() or right[0].isspace():
        return left + right
    if _is_cjk(left[-1]) or _is_cjk(right[0]):
        return left + right
    if right[0] in _LEADING_PUNCT:
        return left + right
    return left + " " + right


def merge_horizontal(
    regions: Iterable[TextRegion],
    baseline_ratio: float = 0.55,
    gap_ratio: float = 1.1,
) -> list[TextRegion]:
    """Merge fragments sharing a baseline and separated by a small gap.

    ``baseline_ratio``: vertical centres may differ by this fraction of the line
    height and still count as the same line.
    ``gap_ratio``: horizontal gap allowed between fragments, as a fraction of
    line height (1.1 = about one character width).
    """
    items = [r for r in regions if r.text and r.bbox]
    if not items:
        return []
    items.sort(key=lambda r: (_center_y(r.bbox), r.bbox[0]))  # type: ignore[arg-type]

    merged: list[TextRegion] = []
    cur = items[0]
    cur_bbox = cur.bbox
    assert cur_bbox is not None

    for nxt in items[1:]:
        nb = nxt.bbox
        assert nb is not None
        line_h = max(_height(cur_bbox), _height(nb))
        same_line = abs(_center_y(nb) - _center_y(cur_bbox)) <= baseline_ratio * line_h
        gap = nb[0] - cur_bbox[2]
        adjacent = gap <= gap_ratio * line_h

        if same_line and adjacent:
            cur = dataclasses.replace(
                cur,
                text=_join(cur.text, nxt.text),
                bbox=_union(cur_bbox, nb),
                confidence=min(cur.confidence, nxt.confidence),
            )
            cur_bbox = cur.bbox
        else:
            merged.append(cur)
            cur, cur_bbox = nxt, nb

    merged.append(cur)
    return merged


def drop_duplicates(regions: Iterable[TextRegion]) -> list[TextRegion]:
    """Remove identical text that the detector reported twice (overlapping boxes)."""
    seen: set[str] = set()
    out: list[TextRegion] = []
    for region in regions:
        key = region.text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(region)
    return out


def drop_oversized(regions: Iterable[TextRegion], factor: float = 2.2) -> list[TextRegion]:
    """Discard boxes that are far taller than the rest of the lines.

    A frame captured mid-transition (chat scrolling, text fading in) can make the
    detector return one box spanning two lines. The recogniser then reads them as
    a single garbled string -- e.g. "Playero xX_Shadow_Xx: ..." -- which is worse
    than showing nothing.

    Only applied when there are enough boxes for a median to be meaningful, and
    the threshold is generous, so a legitimately large line survives.
    """
    items = [r for r in regions if r.bbox]
    if len(items) < 3:
        return list(regions)
    heights = sorted(_height(r.bbox) for r in items)  # type: ignore[arg-type]
    median = heights[len(heights) // 2]
    limit = max(median * factor, median + 12)
    kept = [r for r in items if _height(r.bbox) <= limit]  # type: ignore[arg-type]
    # Never let the filter empty the screen.
    return kept if kept else list(regions)
