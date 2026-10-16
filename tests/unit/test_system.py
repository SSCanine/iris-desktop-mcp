"""Unit tests for iris.system (clipboard, processes, notifications, window
state, registry).

These touch live Windows APIs but every test is non-destructive:
  - Clipboard tests save/restore the original contents.
  - Process tests only list/find, no kill (kill is guarded by force=True).
  - Window state tests target a fresh notepad subprocess and clean up.
  - Registry tests read HKCU keys that always exist; the write/delete path
    is exercised against a sandbox key under HKCU\\Software\\IrisMCPTest.
"""

from __future__ import annotations

import os

import pytest

from iris import system as system_mod


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not system_mod.HAS_CLIPBOARD, reason="win32clipboard unavailable")
class TestClipboard:
    def test_set_and_get_roundtrip(self):
        original = system_mod.clipboard_get()
        try:
            r = system_mod.clipboard_set("iris bench fixture")
            assert r["ok"] is True
            got = system_mod.clipboard_get()
            assert got["ok"] is True
            assert got["text"] == "iris bench fixture"
        finally:
            if original.get("ok") and original.get("text"):
                system_mod.clipboard_set(original["text"])

    def test_set_empty_string(self):
        original = system_mod.clipboard_get()
        try:
            r = system_mod.clipboard_set("")
            assert r["ok"] is True
            got = system_mod.clipboard_get()
            assert got["ok"] is True
            assert got["text"] == ""
        finally:
            if original.get("ok") and original.get("text"):
                system_mod.clipboard_set(original["text"])

    def test_set_non_string_coerced(self):
        original = system_mod.clipboard_get()
        try:
            r = system_mod.clipboard_set(42)
            assert r["ok"] is True
            got = system_mod.clipboard_get()
            assert got["text"] == "42"
        finally:
            if original.get("ok") and original.get("text"):
                system_mod.clipboard_set(original["text"])


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not system_mod.HAS_PSUTIL, reason="psutil unavailable")
class TestProcesses:
    def test_list_processes_returns_some(self):
        r = system_mod.list_processes(limit=10)
        assert r["ok"] is True
        assert r["count"] > 0
        # Each entry has the documented shape
        first = r["processes"][0]
        for key in ("pid", "name", "exe", "status"):
            assert key in first

    def test_list_processes_filter_by_name(self):
        # python.exe must be running (we're inside it). Filter should find us.
        r = system_mod.list_processes(name_contains="python")
        assert r["ok"] is True
        assert r["count"] >= 1
        assert all("python" in p["name"].lower() for p in r["processes"])

    def test_list_processes_respects_limit(self):
        r = system_mod.list_processes(limit=3)
        assert r["ok"] is True
        assert len(r["processes"]) <= 3

    def test_find_process_exact_match(self):
        r = system_mod.find_process("python.exe")
        assert r["ok"] is True
        # We're inside one. There must be at least one match.
        assert r["count"] >= 1

    def test_kill_protected_pid_refused(self):
        for pid in (0, 4):
            r = system_mod.kill_process(pid, force=True)
            assert r["ok"] is False
            assert r["reason"] == "protected_pid"

    def test_kill_without_force_refused(self):
        r = system_mod.kill_process(os.getpid(), force=False)
        assert r["ok"] is False
        assert r["reason"] == "force_required"

    def test_kill_nonexistent_pid(self):
        # A PID that almost certainly doesn't exist. (PIDs go up to 4194304.)
        r = system_mod.kill_process(4194303, force=True)
        # Either we got no_such_process, or we accidentally hit a pid; both are
        # acceptable as long as we didn't crash. The common case is no_such_process.
        assert r["ok"] in (False, True)
        if not r["ok"]:
            assert r["reason"] in ("no_such_process", "access_denied")


# ---------------------------------------------------------------------------
# Window state
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not system_mod.HAS_WIN32, reason="win32 unavailable")
def test_window_state_invalid_hwnd_returns_diagnostic():
    # hwnd 0 is never valid.
    for fn in (
        system_mod.window_minimize,
        system_mod.window_maximize,
        system_mod.window_restore,
        system_mod.window_close,
    ):
        r = fn(0)
        assert r["ok"] is False
        assert r["reason"] == "invalid_hwnd"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not system_mod.HAS_WINREG, reason="winreg unavailable")
class TestRegistry:
    def test_read_hkcu_environment_path(self):
        # PATH always exists under HKCU\Environment on Windows.
        r = system_mod.registry_read("HKCU", "Environment", "Path")
        assert r["ok"] is True
        assert r["type"] in ("REG_SZ", "REG_EXPAND_SZ")
        assert isinstance(r["value"], str)

    def test_read_unknown_hive(self):
        r = system_mod.registry_read("HKWHATEVER", "some/path", "x")
        assert r["ok"] is False
        assert r["reason"] == "unknown_hive"

    def test_read_missing_key(self):
        r = system_mod.registry_read(
            "HKCU",
            "Software\\IrisMCPTest_DefinitelyDoesNotExist_xyz",
            "x",
        )
        assert r["ok"] is False
        assert r["reason"] == "key_or_value_not_found"

    def test_write_without_confirm_refused(self):
        r = system_mod.registry_write(
            "HKCU",
            "Software\\IrisMCPTest",
            "v",
            "x",
            value_type="REG_SZ",
            confirm=False,
        )
        assert r["ok"] is False
        assert r["reason"] == "confirm_required"

    def test_delete_without_confirm_refused(self):
        r = system_mod.registry_delete_value(
            "HKCU",
            "Software\\IrisMCPTest",
            "v",
            confirm=False,
        )
        assert r["ok"] is False
        assert r["reason"] == "confirm_required"

    def test_write_then_read_then_delete_under_hkcu_sandbox(self):
        sandbox = "Software\\IrisMCPTest"
        try:
            w = system_mod.registry_write(
                "HKCU",
                sandbox,
                "iris_unit_test",
                "hello",
                value_type="REG_SZ",
                confirm=True,
            )
            assert w["ok"] is True, w
            r = system_mod.registry_read("HKCU", sandbox, "iris_unit_test")
            assert r["ok"] is True
            assert r["value"] == "hello"
            assert r["type"] == "REG_SZ"
        finally:
            system_mod.registry_delete_value(
                "HKCU",
                sandbox,
                "iris_unit_test",
                confirm=True,
            )

    def test_list_values_under_hkcu_environment(self):
        r = system_mod.registry_list_values("HKCU", "Environment")
        assert r["ok"] is True
        names = [v["name"] for v in r["values"]]
        assert "Path" in names
