"""Structured telemetry: one JSON line per find / click outcome.

The main log only says "CallToolRequest" with no tool name, target, backend
or result, so accuracy problems in the field could never be mined. This module
appends compact JSONL records to `<log_dir>/events.jsonl`. Fail-silent by
design: telemetry must never break a click.

Disable with IRIS_TELEMETRY=0. Redirect with IRIS_LOG_DIR=<dir>.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_DIR: Path | None = None
_DISABLED = os.environ.get("IRIS_TELEMETRY", "1").strip().lower() in {"0", "false", "no", "off"}
EVENTS_FILENAME = "events.jsonl"


def default_log_dir() -> Path:
    """$IRIS_LOG_DIR -> <repo>/logs when running from a source checkout ->
    platformdirs user log dir. Mirrors the server's resolution so both files
    land in the same directory."""
    env = os.environ.get("IRIS_LOG_DIR")
    if env:
        return Path(env)
    repo_root = Path(__file__).resolve().parent.parent
    if (repo_root / "pyproject.toml").exists():
        return repo_root / "logs"
    try:
        from platformdirs import user_log_dir

        return Path(user_log_dir("iris-mcp"))
    except ImportError:
        return repo_root / "logs"


def configure(log_dir: Path | str) -> None:
    global _DIR
    _DIR = Path(log_dir)


def events_path() -> Path:
    return (_DIR or default_log_dir()) / EVENTS_FILENAME


def _compact(value: Any, depth: int = 0) -> Any:
    """Trim payloads so a record stays one short line."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {str(k): _compact(v, depth + 1) for k, v in list(value.items())[:24]}
    if isinstance(value, (list, tuple)):
        return [_compact(v, depth + 1) for v in list(value)[:8]]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "..."
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    return repr(value)[:120]


def record(kind: str, **fields: Any) -> None:
    """Append one event. Never raises."""
    if _DISABLED:
        return
    rec = {"ts": round(time.time(), 3), "kind": kind}
    rec.update({k: _compact(v) for k, v in fields.items()})
    try:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _LOCK, path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except Exception:
        pass


def record_find(token, target: str, result, **extra: Any) -> None:
    """Summarize a resolver.FindResult without the screenshot bytes."""
    try:
        top = result.hits[0] if result.hits else None
        record(
            "find",
            token=getattr(token, "id", None),
            hwnd=getattr(token, "hwnd", None),
            exe=getattr(token, "exe_name", None),
            title=getattr(token, "title_at_creation", None),
            target=target,
            found=result.found,
            backend=result.backend,
            backends_tried=result.backends_tried,
            hits=len(result.hits),
            top=top,
            nearest=[m.get("text") for m in (result.nearest_matches or [])[:3]],
            elapsed_ms=result.elapsed_ms,
            notes=result.notes,
            window_state=result.window_state,
            **extra,
        )
    except Exception:
        pass


def tail(n: int = 50) -> list[dict]:
    """Last n events, oldest first. For diagnostics tools."""
    path = events_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out
    except Exception:
        return []
