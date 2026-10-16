"""Pixel backend: find targets by color or by image template.

For things that have no text and no UIA node: a colored dot, a small icon, a
canvas-drawn widget, a game HUD element. Works on any PIL image. The resolver
feeds it the PrintWindow capture of the focused window and adds the window
origin to the returned image-local coordinates.

Target grammar (used by resolver.find / click when the target string starts
with one of these prefixes):

    color:#RRGGBB            exact-ish color, default tolerance 24
    color:#RRGGBB~40         tolerance 40 (max per-channel abs diff)
    color:#RRGGBB~24@4-400   only components with 4 <= area <= 400 px
    color:rgb(255,0,0)       same, decimal form
    image:C:/path/icon.png   template match, default threshold 0.85
    image:C:/path/icon.png~0.7

Plain strings that do not match the grammar are not pixel targets; the caller
falls through to the UIA / OCR path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from iris.geometry import Rect

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:  # pragma: no cover - numpy is a hard dep in practice
    HAS_NUMPY = False

try:
    import cv2

    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


DEFAULT_COLOR_TOLERANCE = 24
DEFAULT_TEMPLATE_THRESHOLD = 0.85
# DPI-typical scales tried when a template does not match at 1.0.
TEMPLATE_FALLBACK_SCALES = (1.25, 1.5, 0.8, 2.0)
# Pure-Python component labeling is O(mask pixels); above this we refuse rather
# than hang. cv2 has no such limit.
_PY_LABEL_PIXEL_CAP = 250_000


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PixelSpec:
    kind: str  # 'color' | 'image'
    raw: str
    color: tuple[int, int, int] | None = None
    tolerance: int = DEFAULT_COLOR_TOLERANCE
    min_area: int = 1
    max_area: int | None = None
    template_path: str | None = None
    threshold: float = DEFAULT_TEMPLATE_THRESHOLD


_HEX_RE = r"#?([0-9a-fA-F]{6})"
_RGB_RE = r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)"
_COLOR_RE = re.compile(
    rf"^color:\s*(?:{_HEX_RE}|{_RGB_RE})(?:~(\d+))?(?:@(\d+)(?:-(\d+))?)?\s*$",
    re.IGNORECASE,
)
_IMAGE_RE = re.compile(r"^image:\s*(.+?)(?:~(0?\.\d+|1(?:\.0+)?))?\s*$", re.IGNORECASE)


def is_pixel_target(target: str) -> bool:
    t = (target or "").strip().lower()
    return t.startswith("color:") or t.startswith("image:")


def parse_target(target: str) -> PixelSpec | None:
    """Return a PixelSpec if `target` uses the pixel grammar, else None.

    Raises ValueError when the prefix is right but the body is malformed, so
    the caller can surface a useful error instead of silently OCR-ing the
    literal string 'color:#zzz'.
    """
    if not target or not is_pixel_target(target):
        return None
    t = target.strip()
    m = _COLOR_RE.match(t)
    if m:
        hex6, r, g, b, tol, amin, amax = m.groups()
        if hex6:
            color = (int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16))
        else:
            color = (int(r), int(g), int(b))
            if any(c > 255 for c in color):
                raise ValueError(f"rgb channel out of range in {target!r}")
        return PixelSpec(
            kind="color",
            raw=t,
            color=color,
            tolerance=int(tol) if tol is not None else DEFAULT_COLOR_TOLERANCE,
            min_area=int(amin) if amin is not None else 1,
            max_area=int(amax) if amax is not None else None,
        )
    m = _IMAGE_RE.match(t)
    if m:
        path, thr = m.groups()
        return PixelSpec(
            kind="image",
            raw=t,
            template_path=path.strip().strip('"').strip("'"),
            threshold=float(thr) if thr is not None else DEFAULT_TEMPLATE_THRESHOLD,
        )
    raise ValueError(
        f"bad pixel target {target!r}. Use color:#RRGGBB[~tol][@min-max] or image:<path>[~thr]"
    )


# ---------------------------------------------------------------------------
# Hits
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PixelHit:
    bbox: Rect  # image-local (caller adds window origin)
    area: int
    score: float  # 1.0 = perfect. color: closeness; template: correlation
    method: str  # 'color' | 'template'
    text: str = ""  # echo of the spec so hits read like OCR/UIA hits
    scale: float = 1.0  # template scale that matched

    @property
    def center(self) -> tuple[int, int]:
        return self.bbox.center

    def to_dict(self) -> dict:
        d = {
            "text": self.text,
            "bbox": self.bbox.to_dict(),
            "similarity": round(self.score, 3),
            "confidence": round(self.score, 3),
            "area": int(self.area),
            "method": self.method,
        }
        if self.scale != 1.0:
            d["template_scale"] = self.scale
        return d


# ---------------------------------------------------------------------------
# Color search
# ---------------------------------------------------------------------------
def _components_cv2(mask) -> list[tuple[int, int, int, int, int]]:
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(
        mask.astype("uint8"), connectivity=8
    )
    out = []
    for i in range(1, n):  # 0 is background
        x, y, w, h, area = (int(v) for v in stats[i])
        out.append((x, y, w, h, area))
    return out


def _components_py(mask) -> list[tuple[int, int, int, int, int]]:
    """8-connected component labeling without cv2. Fine for sparse masks."""
    ys, xs = np.nonzero(mask)
    if len(xs) > _PY_LABEL_PIXEL_CAP:
        raise RuntimeError(
            f"color mask has {len(xs)} pixels; install opencv-python-headless "
            f"for large masks or tighten the tolerance/area"
        )
    remaining = set(zip(xs.tolist(), ys.tolist(), strict=False))
    comps = []
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        minx = maxx = seed[0]
        miny = maxy = seed[1]
        area = 0
        while stack:
            x, y = stack.pop()
            area += 1
            minx, maxx = min(minx, x), max(maxx, x)
            miny, maxy = min(miny, y), max(maxy, y)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb = (x + dx, y + dy)
                    if nb in remaining:
                        remaining.remove(nb)
                        stack.append(nb)
        comps.append((minx, miny, maxx - minx + 1, maxy - miny + 1, area))
    return comps


def find_color(
    img: Image.Image,
    color: tuple[int, int, int],
    *,
    tolerance: int = DEFAULT_COLOR_TOLERANCE,
    min_area: int = 1,
    max_area: int | None = None,
    max_hits: int = 25,
    spec_text: str = "",
) -> list[PixelHit]:
    """Find connected blobs whose pixels are all within `tolerance` of `color`.

    Tolerance is the max absolute per-channel difference (0..255). Hits are
    sorted by score (closest color first) then area (largest first).
    """
    if not HAS_NUMPY:
        raise RuntimeError("numpy is required for color search")
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    target = np.array(color, dtype=np.int16).reshape(1, 1, 3)
    diff = np.abs(arr - target).max(axis=2)
    mask = diff <= int(tolerance)
    if not mask.any():
        return []
    comps = _components_cv2(mask) if HAS_CV2 else _components_py(mask)
    hits: list[PixelHit] = []
    for x, y, w, h, area in comps:
        if area < min_area:
            continue
        if max_area is not None and area > max_area:
            continue
        sub_diff = diff[y : y + h, x : x + w]
        sub_mask = mask[y : y + h, x : x + w]
        mean_diff = float(sub_diff[sub_mask].mean()) if sub_mask.any() else 0.0
        score = max(0.0, 1.0 - mean_diff / (tolerance + 1.0))
        hits.append(
            PixelHit(
                bbox=Rect(int(x), int(y), int(w), int(h)),
                area=int(area),
                score=score,
                method="color",
                text=spec_text or f"color:#{color[0]:02x}{color[1]:02x}{color[2]:02x}",
            )
        )
    hits.sort(key=lambda h: (round(h.score, 3), h.area), reverse=True)
    return hits[:max_hits]


# ---------------------------------------------------------------------------
# Template search
# ---------------------------------------------------------------------------
def load_template(path: str) -> Image.Image:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"template not found: {path}")
    return Image.open(p).convert("RGB")


def _nms(peaks: list[tuple[int, int, float]], w: int, h: int) -> list[tuple[int, int, float]]:
    """Greedy non-max suppression: drop peaks overlapping a stronger one."""
    peaks.sort(key=lambda p: p[2], reverse=True)
    kept: list[tuple[int, int, float]] = []
    for x, y, s in peaks:
        if all(abs(x - kx) >= w * 0.5 or abs(y - ky) >= h * 0.5 for kx, ky, _ in kept):
            kept.append((x, y, s))
    return kept


def find_template(
    img: Image.Image,
    template: Image.Image,
    *,
    threshold: float = DEFAULT_TEMPLATE_THRESHOLD,
    max_hits: int = 10,
    scales: tuple[float, ...] = (1.0,) + TEMPLATE_FALLBACK_SCALES,
    spec_text: str = "",
) -> list[PixelHit]:
    """Locate `template` inside `img` with normalized cross-correlation.

    Tries scale 1.0 first; if nothing clears `threshold`, tries the DPI-typical
    fallback scales. Returns hits sorted by score.
    """
    if not (HAS_NUMPY and HAS_CV2):
        raise RuntimeError("image: targets need opencv (pip install opencv-python-headless)")
    hay = np.asarray(img.convert("RGB"))
    base = template.convert("RGB")
    for scale in scales:
        if scale != 1.0:
            tw = max(1, int(round(base.width * scale)))
            th = max(1, int(round(base.height * scale)))
            tpl_img = base.resize((tw, th), Image.Resampling.LANCZOS)
        else:
            tpl_img = base
        tpl = np.asarray(tpl_img)
        th_, tw_ = tpl.shape[0], tpl.shape[1]
        if th_ > hay.shape[0] or tw_ > hay.shape[1] or th_ < 2 or tw_ < 2:
            continue
        # TM_CCOEFF_NORMED divides by the template's PER-CHANNEL variance, so a
        # flat (single-color) template yields inf/garbage everywhere. Use the
        # squared-difference form for those and flip it into a 0..1 score.
        per_channel_std = tpl.reshape(-1, tpl.shape[2]).astype("float32").std(axis=0)
        if float(per_channel_std.max()) < 1e-3:
            res = 1.0 - cv2.matchTemplate(hay, tpl, cv2.TM_SQDIFF_NORMED)
        else:
            res = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
        # Anything non-finite or above 1 is numerical garbage, not a match.
        res = np.where(np.isfinite(res) & (res <= 1.0001), res, -1.0)
        ys, xs = np.nonzero(res >= threshold)
        if len(xs) == 0:
            continue
        # Cap candidates before NMS (greedy NMS is O(n*k)); keep the strongest.
        if len(xs) > 2000:
            flat = res[ys, xs]
            top = np.argpartition(-flat, 2000)[:2000]
            xs, ys = xs[top], ys[top]
        peaks = [(int(x), int(y), float(res[y, x])) for x, y in zip(xs, ys, strict=False)]
        kept = _nms(peaks, tw_, th_)[:max_hits]
        return [
            PixelHit(
                bbox=Rect(x, y, tw_, th_),
                area=tw_ * th_,
                score=min(1.0, s),
                method="template",
                text=spec_text or "image:<template>",
                scale=scale,
            )
            for x, y, s in kept
        ]
    return []


# ---------------------------------------------------------------------------
# Entry point used by the resolver
# ---------------------------------------------------------------------------
def find(img: Image.Image, spec: PixelSpec) -> list[PixelHit]:
    if spec.kind == "color":
        assert spec.color is not None
        return find_color(
            img,
            spec.color,
            tolerance=spec.tolerance,
            min_area=spec.min_area,
            max_area=spec.max_area,
            spec_text=spec.raw,
        )
    if spec.kind == "image":
        assert spec.template_path is not None
        tpl = load_template(spec.template_path)
        return find_template(img, tpl, threshold=spec.threshold, spec_text=spec.raw)
    raise ValueError(f"unknown pixel spec kind {spec.kind!r}")
