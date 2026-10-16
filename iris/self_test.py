"""self_test: spawn the test harness, run a battery of checks, return a structured report."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HARNESS = Path(__file__).parent.parent / "tests" / "fixtures" / "iris_test_harness.py"


@dataclass
class CheckResult:
    name: str
    status: str  # "pass" | "fail" | "skip"
    ms: int = 0
    reason: str = ""

    def to_dict(self) -> dict:
        d = {"name": self.name, "status": self.status, "ms": self.ms}
        if self.reason:
            d["reason"] = self.reason
        return d


def _wait_harness_window(pid: int, timeout: float = 5.0):
    from iris.spatial import enumerate_windows

    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in enumerate_windows():
            if w.pid == pid and "IRIS_TEST_HARNESS" in w.title:
                return w
        time.sleep(0.1)
    return None


def _time_check(fn) -> CheckResult:
    name = fn.__name__
    t0 = time.perf_counter()
    try:
        fn()
        ms = int((time.perf_counter() - t0) * 1000)
        return CheckResult(name=name, status="pass", ms=ms)
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        return CheckResult(name=name, status="fail", ms=ms, reason=f"{type(e).__name__}: {e}")


def run_self_test() -> dict:
    """Run the full self-test battery. Returns dict with passed/failed/results."""
    from iris import panels as panels_mod
    from iris import resolver as resolver_mod
    from iris.fingerprint import compare
    from iris.semantic import HAS_UIA, query, walk_tree
    from iris.spatial import (
        _make_window_info,
        enumerate_windows,
        get_monitor_for_window,
        list_monitors,
        match_window,
    )
    from iris.tokens import FocusToken, inspect, revalidate
    from iris.vision import _TESSERACT_OK, capture, ocr_text

    results: list[CheckResult] = []
    t_overall = time.perf_counter()

    # Spawn harness
    if not HARNESS.exists():
        return {"ok": False, "error": "harness_not_found", "harness_path": str(HARNESS)}

    proc = subprocess.Popen(
        [sys.executable, str(HARNESS), "--geometry", "400x350+50+50"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        win = _wait_harness_window(proc.pid, timeout=5.0)
        if win is None:
            return {"ok": False, "error": "harness_window_did_not_appear"}

        # Raise harness to foreground so OCR sees the right pixels (not whatever's on top)
        from iris.spatial import bring_to_front

        try:
            bring_to_front(win.hwnd)
            time.sleep(0.3)
        except Exception:
            pass

        info = _make_window_info(win.hwnd)
        monitor_idx = max(get_monitor_for_window(info.bounds), 0)
        token = FocusToken.create(
            hwnd=info.hwnd,
            pid=info.pid,
            exe_name=info.exe_name,
            title=info.title,
            monitor_index=monitor_idx,
            bounds=info.bounds,
        )

        # ---- Spatial checks ----
        def spatial_enumerate():
            wins = enumerate_windows()
            assert len(wins) > 0

        results.append(_time_check(spatial_enumerate))

        def spatial_match():
            wins = match_window({"title_contains": "IRIS_TEST_HARNESS"})
            assert any(w.hwnd == token.hwnd for w in wins)

        results.append(_time_check(spatial_match))

        def spatial_monitors():
            mons = list_monitors()
            assert len(mons) >= 1

        results.append(_time_check(spatial_monitors))

        # ---- Vision checks ----
        def vision_capture():
            img = capture(bounds=token.bounds_at_creation)
            assert img.width > 0 and img.height > 0

        results.append(_time_check(vision_capture))

        if _TESSERACT_OK:

            def vision_ocr():
                img = capture(bounds=token.bounds_at_creation)
                words = ocr_text(img)
                assert len(words) > 0

            results.append(_time_check(vision_ocr))
        else:
            results.append(
                CheckResult(name="vision_ocr", status="skip", reason="tesseract_missing")
            )

        # ---- Semantic checks ----
        if HAS_UIA:

            def semantic_query_titlebar():
                ctrls = query(token.hwnd, name="Close")
                assert len(ctrls) >= 1

            results.append(_time_check(semantic_query_titlebar))

            def semantic_walk_tree():
                dump = walk_tree(token.hwnd, max_depth=4, max_nodes=50)
                assert len(dump) > 0

            results.append(_time_check(semantic_walk_tree))
        else:
            results.append(
                CheckResult(name="semantic_query_titlebar", status="skip", reason="uia_missing")
            )
            results.append(
                CheckResult(name="semantic_walk_tree", status="skip", reason="uia_missing")
            )

        # ---- Resolver checks ----
        def resolver_uia_path():
            r = resolver_mod.find(token, "Close")
            assert r.found
            assert r.backend in ("uia", "ocr")

        results.append(_time_check(resolver_uia_path))

        if _TESSERACT_OK:

            def resolver_ocr_fallback():
                r = resolver_mod.find(token, "Click Me", threshold=0.6)
                assert r.found, f"backend={r.backend} backends_tried={r.backends_tried}"

            results.append(_time_check(resolver_ocr_fallback))
        else:
            results.append(
                CheckResult(name="resolver_ocr_fallback", status="skip", reason="tesseract_missing")
            )

        def resolver_handoff_for_missing():
            r = resolver_mod.find(token, "DoesNotExistXyz123", threshold=0.7)
            assert not r.found
            assert r.backend == "vision_handoff"

        results.append(_time_check(resolver_handoff_for_missing))

        # ---- Token checks ----
        def tokens_revalidate_alive():
            token.last_revalidated_at = 0.0
            assert revalidate(token) is True

        results.append(_time_check(tokens_revalidate_alive))

        def tokens_inspect():
            r = inspect(token)
            assert r["alive"] is True
            assert "monitor" in r

        results.append(_time_check(tokens_inspect))

        # ---- Fingerprint check ----
        if HAS_UIA:

            def fingerprint_drift():
                d1 = walk_tree(token.hwnd, max_depth=4, max_nodes=50)
                d2 = walk_tree(token.hwnd, max_depth=4, max_nodes=50)
                cmp = compare(d1, d2)
                assert cmp["drift_detected"] is False

            results.append(_time_check(fingerprint_drift))
        else:
            results.append(
                CheckResult(name="fingerprint_drift", status="skip", reason="uia_missing")
            )

        # ---- Panels checks ----
        if HAS_UIA:

            def discover_panels_smoke():
                tree = walk_tree(token.hwnd, max_depth=6, max_nodes=200)
                win_bounds = token.bounds_at_creation.to_dict()
                ps = panels_mod.discover_panels(tree, [], win_bounds)
                # The harness is a single Tk window, so weak-pane fallback may
                # find at most one or none; either is fine. We just check the
                # discovery function runs end-to-end without raising.
                assert isinstance(ps, list)

            results.append(_time_check(discover_panels_smoke))
        else:
            results.append(
                CheckResult(name="discover_panels_smoke", status="skip", reason="uia_missing")
            )

        def concept_boost_ranker():
            # Synthetic check: 'Mic/Aux' and 'GoXLR Mic' must rank above 'Mixer'.
            from iris.resolver import _token_overlap

            assert _token_overlap("Mic/Aux", "GoXLR Mic") == 0.5
            assert _token_overlap("Mic/Aux", "Mixer") == 0.0

        results.append(_time_check(concept_boost_ranker))

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    skipped = sum(1 for r in results if r.status == "skip")
    return {
        "ok": failed == 0,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "duration_ms": int((time.perf_counter() - t_overall) * 1000),
        "results": [r.to_dict() for r in results],
    }
