"""Vision layer: capture + OCR + fuzzy text matching + exact OCR cache + coordinate grids."""

from __future__ import annotations

import difflib
import hashlib
import io
import math
import os
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont, ImageOps

from iris.geometry import Rect
from iris.tesseract_bootstrap import configure_tesseract

try:
    import mss

    HAS_MSS = True
except ImportError:
    HAS_MSS = False

try:
    import pytesseract

    HAS_TESSERACT_LIB = True
except ImportError:
    HAS_TESSERACT_LIB = False

try:
    import ctypes

    import win32gui
    import win32ui

    HAS_PRINTWINDOW = True
except ImportError:
    HAS_PRINTWINDOW = False


_TESSERACT_OK = configure_tesseract() if HAS_TESSERACT_LIB else False


# ---------------------------------------------------------------------------
# Capture (mss)
# ---------------------------------------------------------------------------
OPTIMAL_MAX_LONG_EDGE = 1568
OPTIMAL_MAX_PIXELS = 1_150_000
HARD_LIMIT = 8000

_sct = None
_sct_desktop: tuple | None = None  # virtual desktop the mss instance was created for


def _get_sct():
    """mss instance, recreated when the display layout changes. mss caches its
    monitor list at creation, so a monitor plugged in while the server runs
    would otherwise be captured against a stale desktop rect."""
    global _sct, _sct_desktop
    if not HAS_MSS:
        raise RuntimeError("mss unavailable")
    from iris import spatial as _spatial

    vd = _spatial.virtual_desktop()
    sig = (vd.x, vd.y, vd.width, vd.height)
    if _sct is None or sig != _sct_desktop:
        if _sct is not None:
            try:
                _sct.close()
            except Exception:
                pass
        _sct = mss.mss()
        _sct_desktop = sig
    return _sct


def capture_bounds(bounds: Rect | None = None, monitor: int = 0) -> Rect:
    """The screen-absolute rect capture() grabs for these arguments.

    bounds wins. Otherwise monitor 0 = the whole virtual desktop, monitor k
    (1-based) = the k-th entry of screen_info()/spatial.list_monitor_info(),
    primary first. Monitor indices are DISCOVERED per machine, never assumed.
    """
    from iris import spatial as _spatial

    if bounds is not None:
        return bounds
    if int(monitor) == 0:
        return _spatial.virtual_desktop()
    return _spatial.monitor_for_capture_id(int(monitor)).bounds


def capture(bounds: Rect | None = None, monitor: int = 0) -> Image.Image:
    """Capture pixels of `bounds`, or of a monitor (see capture_bounds)."""
    rect = capture_bounds(bounds, monitor)
    sct = _get_sct()
    area = {"top": rect.y, "left": rect.x, "width": rect.width, "height": rect.height}
    raw = sct.grab(area)
    return Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)


def capture_origin(bounds: Rect | None = None, monitor: int = 0) -> tuple[int, int]:
    """Screen-absolute top-left of what capture() with the same args returns."""
    rect = capture_bounds(bounds, monitor)
    return rect.x, rect.y


# PrintWindow flag for DWM-composited windows (Chrome, Electron, modern apps).
# Falls back to plain PrintWindow if the flag fails.
PW_RENDERFULLCONTENT = 0x00000002


def capture_window(hwnd: int) -> Image.Image:
    """Capture a window's contents via Win32 PrintWindow.

    Works on partially-occluded windows because the window itself renders into
    a memory DC, no screen pixels involved. Does NOT work on minimized or
    truly hidden windows (the window has no rendered surface to print).

    Falls back to bounds-based screen capture if PrintWindow is unavailable
    or the returned bitmap is all black (occasional driver issue).

    Args:
        hwnd: Win32 window handle.

    Raises:
        RuntimeError: if hwnd is invalid or has zero size.
    """
    if not HAS_PRINTWINDOW:
        # Fall back to bounds-based capture
        from iris import spatial as _spatial

        info = _spatial._make_window_info(hwnd)
        if info is None:
            raise RuntimeError(f"invalid hwnd {hwnd}")
        return capture(bounds=info.bounds)

    if not win32gui.IsWindow(hwnd):
        raise RuntimeError(f"invalid hwnd {hwnd}")

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"window has invalid dimensions: {width}x{height}")

    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    save_bitmap = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()

        save_bitmap = win32ui.CreateBitmap()
        save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(save_bitmap)

        # PrintWindow with PW_RENDERFULLCONTENT (modern composited content).
        # Returns 1 on success, 0 on failure.
        result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if not result:
            # Try plain PrintWindow as a fallback.
            result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)

        if not result:
            raise RuntimeError("PrintWindow returned 0")

        bmp_info = save_bitmap.GetInfo()
        bmp_str = save_bitmap.GetBitmapBits(True)

        img = Image.frombuffer(
            "RGB",
            (bmp_info["bmWidth"], bmp_info["bmHeight"]),
            bmp_str,
            "raw",
            "BGRX",
            0,
            1,
        )

        # Some hardware-accelerated apps (e.g. games, video players) return an
        # all-black bitmap. Detect this and fall back to screen capture.
        if _bitmap_is_black(img):
            from iris import spatial as _spatial

            info = _spatial._make_window_info(hwnd)
            if info is not None:
                return capture(bounds=info.bounds)

        return img
    finally:
        if save_bitmap is not None:
            try:
                win32gui.DeleteObject(save_bitmap.GetHandle())
            except Exception:
                pass
        if save_dc is not None:
            try:
                save_dc.DeleteDC()
            except Exception:
                pass
        if mfc_dc is not None:
            try:
                mfc_dc.DeleteDC()
            except Exception:
                pass
        if hwnd_dc is not None:
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass


def _bitmap_is_black(img: Image.Image, sample_step: int = 32) -> bool:
    """Cheap test: sample pixels on a grid; if every sample is (0,0,0), assume black."""
    w, h = img.width, img.height
    if w == 0 or h == 0:
        return True
    pixels = img.load()
    for y in range(0, h, max(1, h // sample_step)):
        for x in range(0, w, max(1, w // sample_step)):
            p = pixels[x, y]
            if isinstance(p, tuple) and any(v != 0 for v in p[:3]):
                return False
            if isinstance(p, int) and p != 0:
                return False
    return True


def optimal_scale(w: int, h: int) -> float:
    long_scale = OPTIMAL_MAX_LONG_EDGE / max(w, h)
    pixel_scale = math.sqrt(OPTIMAL_MAX_PIXELS / (w * h))
    return min(1.0, long_scale, pixel_scale)


def encode_jpeg(
    img: Image.Image, quality: int = 60, optimize_tokens: bool = True
) -> tuple[bytes, int, int]:
    w, h = img.width, img.height
    if optimize_tokens:
        s = optimal_scale(w, h)
        if s < 1.0:
            w, h = int(w * s), int(h * s)
            img = img.resize((w, h), Image.Resampling.LANCZOS)
    elif max(w, h) > HARD_LIMIT:
        s = HARD_LIMIT / max(w, h)
        w, h = int(w * s), int(h * s)
        img = img.resize((w, h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue(), w, h


# ---------------------------------------------------------------------------
# Screenshot truth: metadata + coordinate grids + zoom
#
# Every screenshot the server hands back is shrunk to save tokens. Without the
# numbers below the model has no way to turn "the button is at (410, 220) in
# this picture" back into a screen coordinate. The grid draws SCREEN
# coordinates onto the picture so the model can read them off directly.
# ---------------------------------------------------------------------------
def capture_meta(
    origin: tuple[int, int], captured_size: tuple[int, int], image_size: tuple[int, int]
) -> dict:
    cw, ch = captured_size
    iw, ih = image_size
    scale = (iw / cw) if cw else 1.0
    return {
        "origin": [int(origin[0]), int(origin[1])],
        "captured_size": [int(cw), int(ch)],
        "image_size": [int(iw), int(ih)],
        "scale": round(scale, 4),
        "how_to_click": (
            f"screen_x = {int(origin[0])} + image_x / {scale:.4f}; "
            f"screen_y = {int(origin[1])} + image_y / {scale:.4f}. "
            "Use zoom(x, y) for anything smaller than ~20 image px."
        ),
    }


def _pick_grid_step(scale: float, min_image_px: int = 70) -> int:
    """Smallest 'nice' screen-pixel step that is at least min_image_px apart in the image."""
    for step in (10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if step * scale >= min_image_px:
            return step
    return 1000


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def draw_grid(
    img: Image.Image,
    origin: tuple[int, int],
    scale: float,
    *,
    step: int | None = None,
    color: tuple[int, int, int, int] = (255, 0, 200, 150),
    crosshair: tuple[int, int] | None = None,
) -> Image.Image:
    """Overlay a labeled grid in SCREEN coordinates onto an image.

    `origin` is the screen coordinate of the image's top-left; `scale` is
    image_px / screen_px. Lines fall on multiples of `step` screen pixels and
    are labeled with the screen coordinate they represent.
    """
    ox, oy = origin
    step = step or _pick_grid_step(scale)
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    w, h = base.size
    font_size = 11 if min(w, h) >= 400 else 9
    font = _font(font_size)
    label_bg = (0, 0, 0, 170)
    label_fg = (255, 255, 255, 255)

    def _label(x: int, y: int, text: str):
        try:
            l, t, r, b = d.textbbox((x, y), text, font=font)
        except Exception:
            r, b = x + 6 * len(text), y + font_size + 2
            l, t = x, y
        d.rectangle([l - 1, t - 1, r + 1, b + 1], fill=label_bg)
        d.text((x, y), text, fill=label_fg, font=font)

    # Vertical lines: screen x values that are multiples of step within view.
    first_sx = int(math.ceil(ox / step) * step)
    sx = first_sx
    while (sx - ox) * scale < w:
        ix = int(round((sx - ox) * scale))
        d.line([(ix, 0), (ix, h)], fill=color, width=1)
        _label(min(ix + 2, w - 40), 1, str(sx))
        sx += step
    first_sy = int(math.ceil(oy / step) * step)
    sy = first_sy
    while (sy - oy) * scale < h:
        iy = int(round((sy - oy) * scale))
        d.line([(0, iy), (w, iy)], fill=color, width=1)
        _label(1, min(iy + 1, h - font_size - 3), str(sy))
        sy += step
    if crosshair is not None:
        cx = int(round((crosshair[0] - ox) * scale))
        cy = int(round((crosshair[1] - oy) * scale))
        d.line([(cx - 12, cy), (cx + 12, cy)], fill=(0, 255, 0, 230), width=1)
        d.line([(cx, cy - 12), (cx, cy + 12)], fill=(0, 255, 0, 230), width=1)
    return Image.alpha_composite(base, overlay).convert("RGB")


_MARK_COLORS = {
    # outline, tag background
    "uia": ((0, 190, 255, 230), (0, 90, 130, 235)),
    "ocr": ((255, 0, 200, 220), (120, 0, 95, 235)),
}


def draw_marks(
    img: Image.Image,
    marks: list,
    origin: tuple[int, int],
    scale: float,
) -> Image.Image:
    """Overlay numbered tags (Set-of-Mark) for `marks` (objects with .id,
    .bbox screen Rect, .source) onto an image whose top-left is `origin`
    and whose pixels are `scale` times screen pixels. Cyan = UIA control,
    magenta = OCR text run. Tags avoid covering each other where possible."""
    ox, oy = origin
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    w, h = base.size
    font_size = 11 if min(w, h) >= 600 else 10
    font = _font(font_size)
    line_w = 2 if scale >= 1.0 else 1
    placed: list[tuple[int, int, int, int]] = []

    def _overlaps(r):
        return any(
            not (r[2] <= p[0] or p[2] <= r[0] or r[3] <= p[1] or p[3] <= r[1]) for p in placed
        )

    for m in marks:
        b = m.bbox
        x1 = int(round((b.x - ox) * scale))
        y1 = int(round((b.y - oy) * scale))
        x2 = int(round((b.right - ox) * scale))
        y2 = int(round((b.bottom - oy) * scale))
        if x2 < 0 or y2 < 0 or x1 > w or y1 > h:
            continue
        outline, tag_bg = _MARK_COLORS.get(getattr(m, "source", "ocr"), _MARK_COLORS["ocr"])
        d.rectangle([x1, y1, max(x1 + 1, x2), max(y1 + 1, y2)], outline=outline, width=line_w)
        label = str(m.id)
        try:
            tl, tt, tr, tb = d.textbbox((0, 0), label, font=font)
            tw, th = tr - tl + 4, tb - tt + 2
        except Exception:
            tw, th = 6 * len(label) + 4, font_size + 2
        # Preferred: tag sitting on the top-left corner, above the box.
        candidates = [
            (x1, y1 - th),
            (x1, y1),
            (x2 - tw, y1 - th),
            (x1, y2),
            (x2 - tw, y2),
        ]
        tag = None
        for cx, cy in candidates:
            r = (max(0, cx), max(0, cy), max(0, cx) + tw, max(0, cy) + th)
            if r[2] <= w and r[3] <= h and not _overlaps(r):
                tag = r
                break
        if tag is None:
            cx, cy = max(0, min(x1, w - tw)), max(0, min(y1, h - th))
            tag = (cx, cy, cx + tw, cy + th)
        placed.append(tag)
        d.rectangle([tag[0], tag[1], tag[2], tag[3]], fill=tag_bg)
        d.text((tag[0] + 2, tag[1]), label, fill=(255, 255, 255, 255), font=font)
    return Image.alpha_composite(base, overlay).convert("RGB")


def zoom_capture(
    x: int, y: int, *, size: int = 200, magnify: int = 3, grid: bool = True
) -> tuple[Image.Image, dict]:
    """Full-resolution crop centered on screen (x, y), magnified with NEAREST so
    single pixels stay visible, with a fine grid. Returns (image, meta)."""
    size = max(20, int(size))
    magnify = max(1, min(int(magnify), 8))
    half = size // 2
    bounds = Rect(int(x) - half, int(y) - half, size, size)
    raw = capture(bounds=bounds)
    big = raw.resize((raw.width * magnify, raw.height * magnify), Image.Resampling.NEAREST)
    meta = capture_meta((bounds.x, bounds.y), (raw.width, raw.height), (big.width, big.height))
    meta["center"] = [int(x), int(y)]
    if grid:
        big = draw_grid(
            big,
            (bounds.x, bounds.y),
            float(magnify),
            step=_pick_grid_step(float(magnify), min_image_px=45),
            crosshair=(int(x), int(y)),
        )
    return big, meta


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
# Page segmentation mode. Tesseract's default (3) assumes a document layout and
# drops or merges isolated UI labels. 11 = "sparse text, find as much as
# possible in no particular order", which is the right prior for GUI chrome.
OCR_PSM_DEFAULT = int(os.environ.get("IRIS_OCR_PSM", "11"))
# Auto-upscale rule for the first pass: small captures get 2x (legacy rule).
OCR_AUTO_UPSCALE_BELOW_HEIGHT = 400
# Second pass (resolver): upscale 2x when the first pass finds nothing, but
# only for captures up to this many pixels so a 4K window cannot take seconds.
OCR_UPSCALE_MAX_PIXELS = 2_600_000
OCR_UPSCALE_FACTOR = 2.0
# Second pass uses a DIFFERENT segmentation mode on purpose. Measured on
# Segoe UI labels rendered at 7-11 pt (2026-09-06): psm 11 at native scale
# reads 8/8 down to ~11 px glyphs, but at 9 px it drops to 5/8 and upscaling
# it does not help (4/8). psm 6 (uniform block) at 2x reads 8/8 at 9 px.
OCR_PASS2_PSM = int(os.environ.get("IRIS_OCR_PASS2_PSM", "6"))


@dataclass(frozen=True)
class OCRWord:
    text: str
    bbox: Rect  # window-local coords (caller translates to screen-absolute if needed)
    confidence: float
    line: int = -1  # Tesseract (block, paragraph, line) identity; -1 = unknown

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "bbox": self.bbox.to_dict(),
            "confidence": round(self.confidence, 2),
        }


def auto_ocr_scale(img: Image.Image) -> float:
    return OCR_UPSCALE_FACTOR if img.height < OCR_AUTO_UPSCALE_BELOW_HEIGHT else 1.0


def _preprocess_for_ocr(img: Image.Image, scale: float | None = None) -> Image.Image:
    """Grayscale + autocontrast, optionally upscaled (LANCZOS) by `scale`."""
    gray = img.convert("L")
    enhanced = ImageOps.autocontrast(gray, cutoff=2)
    s = auto_ocr_scale(img) if scale is None else float(scale)
    if s > 1.0:
        enhanced = enhanced.resize(
            (max(1, int(round(enhanced.width * s))), max(1, int(round(enhanced.height * s)))),
            Image.Resampling.LANCZOS,
        )
    return enhanced


def ocr_text(
    img: Image.Image,
    preprocess: bool = True,
    *,
    scale: float | None = None,
    psm: int | None = None,
) -> list[OCRWord]:
    """Run OCR over the image. Returns word-level results with bboxes in image-local coords.

    `scale` > 1 upscales before OCR (tiny UI text needs ~20 px glyphs to read
    reliably); bboxes are mapped back to original coords. `psm` overrides the
    Tesseract page segmentation mode (default OCR_PSM_DEFAULT).
    """
    if not (HAS_TESSERACT_LIB and _TESSERACT_OK):
        return []
    if preprocess:
        proc = _preprocess_for_ocr(img, scale)
        inv = img.height / proc.height if proc.height else 1.0
    else:
        proc = img
        inv = 1.0
    psm_val = OCR_PSM_DEFAULT if psm is None else int(psm)
    bands = ocr_bands(proc.height, proc.width * proc.height)
    if len(bands) == 1:
        raw = _tesseract_words(proc, psm_val, 0, band_index=0)
        if raw is None:
            return []
    else:
        # Big capture: Tesseract is single-threaded per process, so OCR
        # horizontal bands in parallel (one tesseract process each). Bands
        # overlap by more than a text line so every line is whole in at least
        # one band; words touching an interior cut edge are dropped, and
        # duplicates from the overlap are merged.
        from concurrent.futures import ThreadPoolExecutor

        def job(args):
            idx, (top, bottom) = args
            part = proc.crop((0, top, proc.width, bottom))
            words = _tesseract_words(part, psm_val, top, band_index=idx) or []
            keep = []
            for wd in words:
                x, y, w, h = wd[1]
                if idx > 0 and y - top <= 2:
                    continue  # cut by the band's top edge
                if bottom < proc.height and (y + h) >= bottom - 2:
                    continue  # cut by the band's bottom edge
                keep.append(wd)
            return keep

        with ThreadPoolExecutor(max_workers=len(bands)) as ex:
            parts = list(ex.map(job, enumerate(bands)))
        raw = _dedupe_band_words([w for p in parts for w in p])
    out = []
    for text, (x, y, w, h), conf, line_id in raw:
        out.append(
            OCRWord(
                text=text,
                bbox=Rect(
                    int(round(x * inv)),
                    int(round(y * inv)),
                    max(1, int(round(w * inv))),
                    max(1, int(round(h * inv))),
                ),
                confidence=conf,
                line=line_id,
            )
        )
    if len(bands) > 1:
        # Bands finish in any order; restore reading order. Single-band output
        # keeps Tesseract's own order, exactly as before banding existed.
        out.sort(key=lambda o: (o.line if o.line >= 0 else 0, o.bbox.x))
    return out


# Parallel banding for large captures. Measured 2026-09-29: a 2571x1403
# VS Code window took 3.6 s in one Tesseract process.
OCR_BAND_MIN_PIXELS = int(os.environ.get("IRIS_OCR_BAND_MIN_PIXELS", "1200000"))
OCR_BAND_TARGET_PIXELS = 450_000
OCR_BAND_MAX = max(1, min(8, (os.cpu_count() or 4) // 2))
OCR_BAND_OVERLAP = 96  # px in the (possibly upscaled) OCR image; > one line of UI text


def ocr_bands(height: int, area: int) -> list[tuple[int, int]]:
    """(top, bottom) rows of the horizontal bands to OCR in parallel. One band
    for small captures. Consecutive bands overlap by OCR_BAND_OVERLAP."""
    if area < OCR_BAND_MIN_PIXELS or OCR_BAND_MAX <= 1:
        return [(0, height)]
    n = max(2, min(OCR_BAND_MAX, round(area / OCR_BAND_TARGET_PIXELS)))
    step = height / n
    out = []
    for i in range(n):
        top = 0 if i == 0 else max(0, int(i * step) - OCR_BAND_OVERLAP // 2)
        bottom = height if i == n - 1 else min(height, int((i + 1) * step) + OCR_BAND_OVERLAP // 2)
        out.append((top, bottom))
    return out


def _dedupe_band_words(words: list) -> list:
    """Merge the same word reported by two overlapping bands."""
    kept: list = []
    for wd in sorted(words, key=lambda w: -w[2]):
        text, (x, y, w, h), _conf, _line = wd
        dup = False
        for k in kept:
            kx, ky, kw, kh = k[1]
            ix = max(0, min(x + w, kx + kw) - max(x, kx))
            iy = max(0, min(y + h, ky + kh) - max(y, ky))
            if ix * iy >= 0.5 * min(w * h, kw * kh) and k[0] == text:
                dup = True
                break
        if not dup:
            kept.append(wd)
    return kept


def _tesseract_words(img, psm_val: int, y_offset: int, band_index: int):
    """[(text, (x, y, w, h) in the full OCR image, confidence 0..1, line_id)]
    or None when Tesseract failed. Line ids are unique across bands."""
    try:
        data = pytesseract.image_to_data(
            img, output_type=pytesseract.Output.DICT, config=f"--psm {psm_val}"
        )
    except Exception:
        return None
    out = []
    n = len(data.get("text", []))
    blocks = data.get("block_num") or [0] * n
    pars = data.get("par_num") or [0] * n
    lines = data.get("line_num") or [0] * n
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        if conf < 0:
            continue
        try:
            line_id = (
                band_index * 1_000_000_000
                + int(blocks[i]) * 1_000_000
                + int(pars[i]) * 1_000
                + int(lines[i])
            )
        except (ValueError, TypeError):
            line_id = -1
        box = (
            int(data["left"][i]),
            int(data["top"][i]) + int(y_offset),
            max(1, int(data["width"][i])),
            max(1, int(data["height"][i])),
        )
        out.append((text, box, conf / 100.0, line_id))
    return out


# Pre-warm Tesseract on import to avoid first-call cold start
def _prewarm_tesseract():
    if not (HAS_TESSERACT_LIB and _TESSERACT_OK):
        return
    try:
        warm_img = Image.new("L", (50, 30), color=255)
        pytesseract.image_to_string(warm_img)
    except Exception:
        pass


_prewarm_tesseract()


# ---------------------------------------------------------------------------
# Fuzzy text find
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TextMatch:
    text: str
    bbox: Rect
    similarity: float
    confidence: float

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "bbox": self.bbox.to_dict(),
            "similarity": round(self.similarity, 3),
            "confidence": round(self.confidence, 2),
        }


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_text_in_image(
    words: list[OCRWord],
    query: str,
    *,
    fuzzy: bool = True,
    threshold: float = 0.6,
    max_ngram: int = 4,
) -> list[TextMatch]:
    """Search OCR words for query. Tries single words plus n-gram joins.

    N-grams only join consecutive words that Tesseract placed on the SAME line,
    so a two-word label never gets a bbox spanning two rows (whose center
    would land in the gap between them). Words with unknown line identity
    (line == -1, e.g. hand-built test data) join freely.

    Returns matches sorted by similarity, then exactness, then OCR confidence.
    """
    if not query or not words:
        return []
    q = query.strip()
    ql = q.lower()
    out: list[TextMatch] = []
    # Single words
    for w in words:
        sim = _similarity(w.text, q)
        if not fuzzy and sim < 1.0:
            continue
        if sim >= threshold:
            out.append(TextMatch(text=w.text, bbox=w.bbox, similarity=sim, confidence=w.confidence))
    # N-grams (joined text)
    if " " in q or len(q.split()) > 1:
        max_n = min(max_ngram, len(q.split()) + 1, len(words))
        for n in range(2, max_n + 1):
            for i in range(len(words) - n + 1):
                window = words[i : i + n]
                first_line = window[0].line
                if first_line != -1 and any(w.line != first_line for w in window):
                    continue
                joined = " ".join(w.text for w in window)
                sim = _similarity(joined, q)
                if not fuzzy and sim < 1.0:
                    continue
                if sim >= threshold:
                    # Bbox = bounding rect over all words in window
                    x = min(w.bbox.left for w in window)
                    y = min(w.bbox.top for w in window)
                    r = max(w.bbox.right for w in window)
                    b = max(w.bbox.bottom for w in window)
                    avg_conf = sum(w.confidence for w in window) / n
                    out.append(
                        TextMatch(
                            text=joined,
                            bbox=Rect.from_ltrb(x, y, r, b),
                            similarity=sim,
                            confidence=avg_conf,
                        )
                    )
    out.sort(
        key=lambda m: (round(m.similarity, 3), m.text.lower() == ql, m.confidence),
        reverse=True,
    )
    return out


# ---------------------------------------------------------------------------
# Perceptual hash (kept for callers) + exact OCR cache
# ---------------------------------------------------------------------------
def phash(img: Image.Image) -> str:
    """64-bit perceptual hash via 8x8 grayscale + threshold against mean.

    Too coarse to key an OCR cache on (a dot moving or a label flipping does
    not change it), so cached_ocr() uses exact_digest() instead. Kept because
    it is a useful cheap 'did the window change a lot' signal.
    """
    small = img.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p > avg else "0" for p in pixels)
    return hashlib.sha1(bits.encode("ascii")).hexdigest()[:16]


def exact_digest(img: Image.Image) -> str:
    """sha1 of the raw pixel bytes plus size. ~3 ms for a 2 MP RGB image.
    Any single pixel change produces a different digest."""
    h = hashlib.sha1()
    h.update(f"{img.mode}:{img.width}x{img.height}:".encode())
    h.update(img.tobytes())
    return h.hexdigest()[:20]


# Per-token OCR cache: { token_id -> { (scale, psm) -> (digest, [OCRWord]) } }
_OCR_CACHE: dict[str, dict[tuple[float | None, int | None], tuple[str, list[OCRWord]]]] = {}
_OCR_CACHE_HITS = 0
_OCR_CACHE_MISSES = 0


def cached_ocr(
    token_id: str,
    img: Image.Image,
    *,
    scale: float | None = None,
    psm: int | None = None,
) -> list[OCRWord]:
    """OCR with caching keyed on (token_id, scale, psm, exact pixel digest)."""
    global _OCR_CACHE_HITS, _OCR_CACHE_MISSES
    digest = exact_digest(img)
    key = (scale, psm)
    per_token = _OCR_CACHE.setdefault(token_id, {})
    cached = per_token.get(key)
    if cached and cached[0] == digest:
        _OCR_CACHE_HITS += 1
        return cached[1]
    _OCR_CACHE_MISSES += 1
    words = ocr_text(img, scale=scale, psm=psm)
    per_token[key] = (digest, words)
    return words


def ocr_cache_stats() -> dict:
    total = _OCR_CACHE_HITS + _OCR_CACHE_MISSES
    return {
        "hits": _OCR_CACHE_HITS,
        "misses": _OCR_CACHE_MISSES,
        "hit_rate": (_OCR_CACHE_HITS / total) if total else 0.0,
        "tokens_cached": len(_OCR_CACHE),
        "key": "exact_digest",
    }


def clear_ocr_cache(token_id: str | None = None) -> None:
    if token_id is None:
        _OCR_CACHE.clear()
    else:
        _OCR_CACHE.pop(token_id, None)
