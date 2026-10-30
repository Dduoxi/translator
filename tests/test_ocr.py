from __future__ import annotations

import numpy as np
import pytest

from fpst.core.types import Frame, Lang, Source, TextRegion
from fpst.ocr import postprocess
from fpst.ocr.lines import drop_duplicates, merge_horizontal


def region(text: str, bbox: tuple[int, int, int, int]) -> TextRegion:
    return TextRegion(text=text, source=Source.SCREEN, lang=Lang.EN, bbox=bbox)


# --------------------------------------------------------------- merge_horizontal


def test_fragments_on_same_baseline_merge():
    a = region("PlayerOne:", (10, 10, 110, 36))
    b = region("rush b site", (118, 11, 220, 35))
    out = merge_horizontal([a, b])
    assert len(out) == 1
    assert out[0].text == "PlayerOne: rush b site"
    assert out[0].bbox == (10, 10, 220, 36)


def test_wide_gap_keeps_lines_apart():
    a = region("left column", (10, 10, 100, 34))
    b = region("right column", (400, 12, 500, 34))
    assert len(merge_horizontal([a, b])) == 2


def test_different_rows_never_merge():
    a = region("gg wp", (10, 10, 80, 34))
    b = region("nice shot", (12, 44, 100, 68))
    out = merge_horizontal([a, b])
    assert [r.text for r in out] == ["gg wp", "nice shot"]


def test_adjacent_words_get_a_space():
    """Two touching boxes are two words: 'rushb' would be wrong."""
    a = region("rush", (10, 10, 60, 34))
    b = region("b", (60, 10, 76, 34))
    assert merge_horizontal([a, b])[0].text == "rush b"


def test_no_space_before_punctuation():
    a = region("good", (10, 10, 60, 34))
    b = region(",", (60, 10, 68, 34))
    assert merge_horizontal([a, b])[0].text == "good,"


def test_cjk_fragments_join_without_space():
    a = TextRegion("快", Source.SCREEN, Lang.ZH, bbox=(10, 10, 30, 34))
    b = TextRegion("攻", Source.SCREEN, Lang.ZH, bbox=(31, 10, 51, 34))
    assert merge_horizontal([a, b])[0].text == "快攻"


def test_output_is_top_to_bottom():
    regions = [
        region("third", (10, 80, 90, 104)),
        region("first", (10, 10, 90, 34)),
        region("second", (10, 45, 90, 69)),
    ]
    assert [r.text for r in merge_horizontal(regions)] == ["first", "second", "third"]


def test_regions_without_bbox_are_ignored():
    assert merge_horizontal([TextRegion("no box", Source.SCREEN)]) == []


def test_empty_input():
    assert merge_horizontal([]) == []


def test_merge_lowers_confidence_to_weakest_fragment():
    a = TextRegion("a", Source.SCREEN, confidence=0.99, bbox=(0, 0, 20, 20))
    b = TextRegion("b", Source.SCREEN, confidence=0.61, bbox=(21, 0, 40, 20))
    assert merge_horizontal([a, b])[0].confidence == pytest.approx(0.61)


# ------------------------------------------------------------- drop_duplicates


def test_drop_duplicates_is_case_insensitive():
    out = drop_duplicates([region("GG WP", (0, 0, 9, 9)), region("gg wp", (0, 0, 9, 9))])
    assert len(out) == 1


def test_postprocess_without_merge_filters_empty():
    out = postprocess([TextRegion("   ", Source.SCREEN, bbox=(0, 0, 5, 5))], merge=False)
    assert out == []


# ----------------------------------------------------------- mock engine wiring


def test_mock_engine_registered_and_scripted():
    import fpst.ocr.mock  # noqa: F401 - registers the engine
    from fpst.core.interfaces import OCREngine
    from fpst.core.registry import create

    engine = create(OCREngine, "mock", lines=["alpha", "beta"])
    first = engine.recognize(Frame(image=np.zeros((10, 10, 3), np.uint8)))
    assert [r.text for r in first] == ["alpha", "beta"]
    second = engine.recognize(Frame(image=np.zeros((10, 10, 3), np.uint8)))
    assert second == []


# ------------------------------------------------------------------ real engine

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _render_chat() -> np.ndarray:
    from PIL import Image, ImageDraw, ImageFont

    font_path = next((p for p in FONT_CANDIDATES if __import__("pathlib").Path(p).exists()), None)
    if font_path is None:
        pytest.skip("no TrueType font available to render a fixture")

    img = Image.new("RGB", (620, 130), (18, 20, 24))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, 22)
    for i, line in enumerate(["PlayerOne: rush b site now", "TeamMate: enemy behind the box"]):
        draw.text((12, 10 + i * 34), line, font=font, fill=(235, 235, 235))
    return np.array(img)[:, :, ::-1]  # RGB -> BGR


@pytest.mark.slow
def test_rapidocr_reads_rendered_chat():
    pytest.importorskip("rapidocr_onnxruntime")
    from fpst.ocr.rapidocr_engine import RapidOCREngine

    engine = RapidOCREngine(lang="en")
    engine.warmup()
    try:
        regions = postprocess(engine.recognize(Frame(image=_render_chat())))
    finally:
        engine.close()

    texts = [r.text.lower() for r in regions]
    assert len(texts) == 2, texts
    assert "rush" in texts[0] and "site" in texts[0]
    assert "enemy" in texts[1]
