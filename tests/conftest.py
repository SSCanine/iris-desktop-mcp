"""Pytest fixtures for Iris integration tests."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

HARNESS_PATH = Path(__file__).parent / "fixtures" / "iris_test_harness.py"
HARNESS_TITLE = "IRIS_TEST_HARNESS"


@dataclass
class HarnessHandle:
    process: subprocess.Popen
    pid: int
    hwnd: int

    def terminate(self):
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
        except Exception:
            pass


def _wait_for_window(pid: int, timeout: float = 5.0) -> int | None:
    from iris.spatial import enumerate_windows

    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in enumerate_windows():
            if w.pid == pid and HARNESS_TITLE in w.title:
                return w.hwnd
        time.sleep(0.1)
    return None


@pytest.fixture
def iris_harness():
    """Spawn the Tkinter test harness, yield a handle with pid+hwnd, terminate on teardown."""
    if not HARNESS_PATH.exists():
        pytest.skip("Test harness not found")
    cmd = [sys.executable, str(HARNESS_PATH), "--geometry", "400x350+50+50"]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        # 20 s, not 5: a cold CI runner takes several seconds to import
        # tkinter and map the first window, and the 5 s limit flaked on
        # alternating Python versions (PR #2, 2026-09-06).
        hwnd = _wait_for_window(proc.pid, timeout=20.0)
        if hwnd is None:
            proc.kill()
            pytest.fail("Test harness window did not appear within 20s")
        # Raise harness to foreground so other apps don't occlude OCR pixels
        try:
            from iris.spatial import bring_to_front

            bring_to_front(hwnd)
            time.sleep(0.2)
        except Exception:
            pass
        handle = HarnessHandle(process=proc, pid=proc.pid, hwnd=hwnd)
        yield handle
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass
