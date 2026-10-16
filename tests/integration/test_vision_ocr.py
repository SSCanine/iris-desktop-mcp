import pytest
from PIL import Image, ImageDraw, ImageFont

from iris.vision import _TESSERACT_OK, find_text_in_image, ocr_text

pytestmark = pytest.mark.skipif(not _TESSERACT_OK, reason="Tesseract not available")


def _render_text(text: str, size=(400, 100)) -> Image.Image:
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 32)
    except OSError:
        font = ImageFont.load_default()
    draw.text((20, 30), text, fill=(0, 0, 0), font=font)
    return img


def test_ocr_reads_simple_text():
    img = _render_text("HELLO WORLD")
    words = ocr_text(img)
    text_combined = " ".join(w.text for w in words).upper()
    assert "HELLO" in text_combined
    assert "WORLD" in text_combined


def test_ocr_returns_word_bboxes():
    img = _render_text("Save Cancel OK")
    words = ocr_text(img)
    assert len(words) >= 1
    for w in words:
        assert w.bbox.width > 0
        assert w.bbox.height > 0


def test_find_text_via_ocr_pipeline():
    img = _render_text("Start Recording")
    words = ocr_text(img)
    matches = find_text_in_image(words, "Start Recording", fuzzy=True, threshold=0.6)
    assert len(matches) >= 1
    # Top match should be "start recording" (case insensitive similarity is 1.0)
    top = matches[0]
    assert "Start" in top.text or "start" in top.text.lower()
