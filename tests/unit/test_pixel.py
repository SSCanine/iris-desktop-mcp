"""Pixel backend: color blobs + template matching on synthetic images."""

import pytest
from PIL import Image, ImageDraw

from iris import pixel

pytestmark = pytest.mark.skipif(not pixel.HAS_NUMPY, reason="numpy required")


def _canvas(w=400, h=300, bg=(255, 255, 255)):
    img = Image.new("RGB", (w, h), bg)
    return img, ImageDraw.Draw(img)


# --- grammar -----------------------------------------------------------------
def test_parse_plain_text_is_not_pixel():
    assert pixel.parse_target("Save") is None
    assert pixel.parse_target("Color Picker") is None


def test_parse_color_hex_defaults():
    s = pixel.parse_target("color:#FF3B30")
    assert s.kind == "color"
    assert s.color == (0xFF, 0x3B, 0x30)
    assert s.tolerance == pixel.DEFAULT_COLOR_TOLERANCE
    assert s.min_area == 1 and s.max_area is None


def test_parse_color_with_tolerance_and_area():
    s = pixel.parse_target("color:ff0000~40@4-400")
    assert s.color == (255, 0, 0)
    assert s.tolerance == 40
    assert s.min_area == 4 and s.max_area == 400


def test_parse_color_rgb_form():
    s = pixel.parse_target("color:rgb(10, 20, 30)~5")
    assert s.color == (10, 20, 30) and s.tolerance == 5


def test_parse_image_with_threshold():
    s = pixel.parse_target("image:C:/tmp/icon.png~0.7")
    assert s.kind == "image"
    assert s.template_path == "C:/tmp/icon.png"
    assert s.threshold == 0.7


def test_parse_bad_color_raises():
    with pytest.raises(ValueError):
        pixel.parse_target("color:#zzz")


# --- color search ------------------------------------------------------------
def test_find_color_locates_tiny_squares_exactly():
    img, d = _canvas()
    d.rectangle([100, 50, 103, 53], fill=(255, 0, 0))  # 4x4 red
    d.rectangle([10, 10, 11, 11], fill=(0, 0, 255))  # 2x2 blue
    d.rectangle([300, 200, 300, 200], fill=(0, 200, 0))  # 1x1 green
    red = pixel.find_color(img, (255, 0, 0))
    assert len(red) == 1
    assert red[0].bbox.to_dict() == {"x": 100, "y": 50, "width": 4, "height": 4}
    assert red[0].area == 16 and red[0].score == pytest.approx(1.0)
    blue = pixel.find_color(img, (0, 0, 255))
    assert blue[0].bbox.to_dict() == {"x": 10, "y": 10, "width": 2, "height": 2}
    green = pixel.find_color(img, (0, 200, 0))
    assert green[0].bbox.to_dict() == {"x": 300, "y": 200, "width": 1, "height": 1}


def test_find_color_tolerance_rejects_decoys():
    img, d = _canvas()
    d.rectangle([50, 50, 57, 57], fill=(255, 0, 0))  # target
    d.rectangle([150, 50, 157, 57], fill=(255, 45, 45))  # decoy, delta 45
    hits = pixel.find_color(img, (255, 0, 0), tolerance=24)
    assert len(hits) == 1 and hits[0].bbox.x == 50
    loose = pixel.find_color(img, (255, 0, 0), tolerance=60)
    assert len(loose) == 2
    # Closest color ranks first.
    assert loose[0].bbox.x == 50 and loose[0].score > loose[1].score


def test_find_color_area_filter():
    img, d = _canvas()
    d.rectangle([0, 0, 199, 99], fill=(0, 0, 0))  # big black panel 20000px
    d.rectangle([300, 200, 303, 203], fill=(0, 0, 0))  # 4x4 black dot
    hits = pixel.find_color(img, (0, 0, 0), max_area=100)
    assert len(hits) == 1 and hits[0].area == 16
    hits = pixel.find_color(img, (0, 0, 0), min_area=1000)
    assert len(hits) == 1 and hits[0].area == 20000


def test_find_color_no_match_returns_empty():
    img, _ = _canvas()
    assert pixel.find_color(img, (255, 0, 0)) == []


def test_py_labeling_matches_cv2_when_available():
    import numpy as np

    img, d = _canvas()
    d.rectangle([5, 5, 9, 9], fill=(1, 2, 3))  # 5x5 = 25
    d.rectangle([20, 5, 20, 9], fill=(1, 2, 3))  # 1x5 line, separate
    d.rectangle([10, 10, 12, 12], fill=(1, 2, 3))  # 3x3 = 9, diagonal-touching the square
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    mask = np.abs(arr - np.array([1, 2, 3], dtype=np.int16)).max(axis=2) <= 0
    py = sorted(pixel._components_py(mask))
    areas = sorted(c[4] for c in py)
    assert areas == [5, 34]
    if pixel.HAS_CV2:
        cv = sorted(pixel._components_cv2(mask))
        assert cv == py


# --- template search ---------------------------------------------------------
@pytest.mark.skipif(not pixel.HAS_CV2, reason="opencv required")
def test_find_template_locates_glyph():
    img, d = _canvas()
    # A small distinctive glyph at (120, 80): 12x12 with a diagonal.
    d.rectangle([120, 80, 131, 91], fill=(30, 30, 30))
    d.line([120, 80, 131, 91], fill=(250, 200, 0), width=2)
    d.ellipse([200, 150, 211, 161], fill=(30, 30, 30))  # different shape decoy
    tpl = img.crop((120, 80, 132, 92))
    hits = pixel.find_template(img, tpl, threshold=0.9)
    assert len(hits) == 1
    assert (hits[0].bbox.x, hits[0].bbox.y) == (120, 80)
    assert hits[0].score >= 0.9


@pytest.mark.skipif(not pixel.HAS_CV2, reason="opencv required")
def test_find_template_absent_returns_empty():
    img, d = _canvas()
    d.rectangle([120, 80, 131, 91], fill=(30, 30, 30))
    tpl_img, td = _canvas(12, 12)
    td.ellipse([0, 0, 11, 11], fill=(0, 120, 255))
    assert pixel.find_template(img, tpl_img, threshold=0.9) == []


@pytest.mark.skipif(not pixel.HAS_CV2, reason="opencv required")
def test_find_template_scaled_fallback():
    img, d = _canvas()
    # Draw the glyph at 1.5x (18x18) and search with a 12x12 template.
    d.rectangle([100, 100, 117, 117], fill=(30, 30, 30))
    d.line([100, 100, 117, 117], fill=(250, 200, 0), width=3)
    tpl_img, td = _canvas(12, 12)
    td.rectangle([0, 0, 11, 11], fill=(30, 30, 30))
    td.line([0, 0, 11, 11], fill=(250, 200, 0), width=2)
    hits = pixel.find_template(img, tpl_img, threshold=0.8)
    assert hits, "expected a fallback scale to match"
    assert hits[0].scale in (1.25, 1.5)  # 1.25 correlates well enough too
    assert abs(hits[0].bbox.x - 100) <= 2 and abs(hits[0].bbox.y - 100) <= 2
    # The glyph itself, not the 1.0 scale: make sure the scale was not 1.0.
    assert hits[0].scale != 1.0


def test_find_dispatch_color_and_image(tmp_path):
    img, d = _canvas()
    d.rectangle([40, 40, 45, 45], fill=(0, 255, 255))
    spec = pixel.parse_target("color:#00ffff")
    hits = pixel.find(img, spec)
    assert hits and hits[0].to_dict()["bbox"]["x"] == 40
    assert hits[0].to_dict()["text"] == "color:#00ffff"
    if pixel.HAS_CV2:
        p = tmp_path / "t.png"
        img.crop((40, 40, 46, 46)).save(p)
        hits = pixel.find(img, pixel.parse_target(f"image:{p}"))
        assert hits and (hits[0].bbox.x, hits[0].bbox.y) == (40, 40)


def test_find_image_missing_file_raises(tmp_path):
    spec = pixel.parse_target(f"image:{tmp_path / 'nope.png'}")
    img, _ = _canvas()
    with pytest.raises(FileNotFoundError):
        pixel.find(img, spec)
