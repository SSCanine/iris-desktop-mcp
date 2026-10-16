"""App launcher: start apps from apps.yaml and resolve their windows."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import yaml

from iris.spatial import get_monitor_for_window, match_window


# Search order for the user's apps.yaml:
#   1. $IRIS_APPS env var (explicit override)
#   2. <cwd>/apps.yaml
#   3. User config dir, e.g. %APPDATA%/iris-mcp/apps.yaml on Windows
#   4. <repo>/apps.yaml (kept for develop-from-checkout convenience)
# The first one that exists wins. Missing file is not an error: DEFAULT_APPS
# always covers the common cases.
def _apps_yaml_search_paths() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("IRIS_APPS")
    if env:
        out.append(Path(env))
    out.append(Path.cwd() / "apps.yaml")
    try:
        from platformdirs import user_config_dir

        out.append(Path(user_config_dir("iris-mcp")) / "apps.yaml")
    except ImportError:
        pass
    out.append(Path(__file__).parent.parent / "apps.yaml")
    return out


DEFAULT_APPS = {
    "obs": {
        "launch": "C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe",
        "match": {"process": "obs64.exe", "title_contains": "OBS"},
    },
    "chrome": {
        "launch": "shell:start chrome",
        "match": {"process": "chrome.exe"},
    },
    "edge": {
        "launch": "shell:start msedge",
        "match": {"process": "msedge.exe"},
    },
    "explorer": {
        "launch": "explorer.exe",
        "match": {"process": "explorer.exe", "class": "CabinetWClass"},
    },
    "vscode": {
        "launch": "shell:start code",
        "match": {"process": "Code.exe"},
    },
    "notepad": {
        "launch": "notepad.exe",
        "match": {"process": "notepad.exe", "title_contains": "Notepad"},
    },
}


def load_apps() -> dict:
    """Load app registry from the first apps.yaml found in search paths.

    DEFAULT_APPS is always the base. Any user yaml entries are merged on top,
    so users can override individual apps (e.g. point `obs` at a different
    install path) without redefining every entry.
    """
    merged = dict(DEFAULT_APPS)
    for candidate in _apps_yaml_search_paths():
        try:
            if candidate.exists():
                with candidate.open("r", encoding="utf-8") as f:
                    user_cfg = yaml.safe_load(f) or {}
                merged.update(user_cfg)
                break
        except Exception:
            continue
    return merged


def write_default_apps_yaml(target: Path | None = None) -> Path:
    """Materialize a user apps.yaml seeded with DEFAULT_APPS at the user
    config dir (or `target` if given). Returns the path written. Existing
    files are NOT overwritten."""
    if target is None:
        try:
            from platformdirs import user_config_dir

            target = Path(user_config_dir("iris-mcp")) / "apps.yaml"
        except ImportError:
            target = Path.cwd() / "apps.yaml"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Iris app registry. Override or add entries here.\n"
        + yaml.safe_dump(DEFAULT_APPS, sort_keys=False),
        encoding="utf-8",
    )
    return target


def launch(app_name: str, *, wait_seconds: float = 5.0) -> dict:
    apps = load_apps()
    cfg = apps.get(app_name)
    if cfg is None:
        return {"ok": False, "error": f"unknown_app:{app_name}", "available": list(apps.keys())}
    launch_spec = cfg["launch"]
    match_spec = cfg.get("match", {})
    # Snapshot existing windows matching the spec so we know which is NEW
    pre = {w.hwnd for w in match_window(match_spec)}
    # Start the process
    try:
        if launch_spec.startswith("shell:"):
            cmd = launch_spec[len("shell:") :]
            subprocess.Popen(cmd, shell=True)
        else:
            subprocess.Popen(launch_spec)
    except Exception as e:
        return {"ok": False, "error": f"spawn_failed:{e}"}
    # Poll for new window
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        wins = match_window(match_spec)
        new = [w for w in wins if w.hwnd not in pre]
        if new:
            w = new[0]
            monitor = get_monitor_for_window(w.bounds)
            return {
                "ok": True,
                "app": app_name,
                "hwnd": w.hwnd,
                "pid": w.pid,
                "exe": w.exe_name,
                "title": w.title,
                "bounds": w.bounds.to_dict(),
                "monitor": monitor,
            }
        time.sleep(0.1)
    return {"ok": False, "error": "window_did_not_appear", "match_spec": match_spec}


def list_apps() -> dict:
    return {"apps": load_apps()}
