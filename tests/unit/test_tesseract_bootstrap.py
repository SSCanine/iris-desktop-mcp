from pathlib import Path

from iris.tesseract_bootstrap import configure_tesseract, locate_tesseract


def test_locate_tesseract_returns_path_or_none():
    result = locate_tesseract()
    assert result is None or isinstance(result, Path)
    if result is not None:
        assert result.exists()


def test_configure_tesseract_returns_bool():
    assert isinstance(configure_tesseract(), bool)
