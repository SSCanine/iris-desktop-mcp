"""Locate Tesseract binary: $IRIS_TESSERACT, bundled vendor/, PATH, then well-known install dirs.

Importable without pytesseract installed (the setup UI uses `locate_tesseract`
before dependencies exist); only `configure_tesseract` needs it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

VENDOR_DIR = Path(__file__).parent.parent / "vendor" / "tesseract"
WELL_KNOWN = [
    Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
    Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
]


def locate_tesseract() -> Path | None:
    override = os.environ.get("IRIS_TESSERACT", "").strip().strip('"')
    if override and Path(override).is_file():
        return Path(override)
    bundled = VENDOR_DIR / "tesseract.exe"
    if bundled.exists():
        return bundled
    on_path = shutil.which("tesseract")
    if on_path:
        return Path(on_path)
    for p in WELL_KNOWN:
        if p.exists():
            return p
    return None


def configure_tesseract() -> bool:
    import pytesseract

    binary = locate_tesseract()
    if binary is None:
        return False
    pytesseract.pytesseract.tesseract_cmd = str(binary)
    return True
