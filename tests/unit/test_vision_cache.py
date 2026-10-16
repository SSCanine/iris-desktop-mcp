from PIL import Image

from iris.vision import cached_ocr, clear_ocr_cache, ocr_cache_stats, phash


def test_phash_identical_images_same_hash():
    a = Image.new("RGB", (100, 100), color=(255, 0, 0))
    b = Image.new("RGB", (100, 100), color=(255, 0, 0))
    assert phash(a) == phash(b)


def test_phash_different_images_different_hash():
    # phash is structural, not color-based. Solid-color images all hash the same.
    # Use images with structural difference.
    from PIL import ImageDraw

    a = Image.new("RGB", (100, 100), color=(255, 255, 255))
    b = Image.new("RGB", (100, 100), color=(255, 255, 255))
    ImageDraw.Draw(b).rectangle([10, 10, 50, 50], fill=(0, 0, 0))
    assert phash(a) != phash(b)


def test_cached_ocr_uses_cache_for_identical_image():
    clear_ocr_cache()
    img = Image.new("RGB", (200, 100), color=(255, 255, 255))
    cached_ocr("token-1", img)
    stats_after_first = ocr_cache_stats()
    cached_ocr("token-1", img)
    stats_after_second = ocr_cache_stats()
    assert stats_after_second["hits"] == stats_after_first["hits"] + 1
