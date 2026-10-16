from PIL import Image

from iris.geometry import Rect
from iris.vision import capture, encode_jpeg, optimal_scale


def test_capture_primary_returns_image():
    img = capture(monitor=1)
    assert isinstance(img, Image.Image)
    assert img.width > 0
    assert img.height > 0


def test_capture_region():
    img = capture(bounds=Rect(0, 0, 200, 100))
    assert img.width == 200
    assert img.height == 100


def test_encode_jpeg_returns_bytes():
    img = Image.new("RGB", (400, 300), color=(120, 200, 50))
    data, w, h = encode_jpeg(img, quality=60, optimize_tokens=True)
    assert isinstance(data, bytes)
    assert len(data) > 0
    assert w == 400 and h == 300


def test_encode_jpeg_token_optimize_scales_down_huge_image():
    img = Image.new("RGB", (8000, 4000), color=(0, 0, 0))
    _, w, h = encode_jpeg(img, optimize_tokens=True)
    assert max(w, h) <= 1568


def test_optimal_scale_returns_at_most_one():
    assert optimal_scale(100, 100) == 1.0
    assert optimal_scale(8000, 4000) < 1.0
