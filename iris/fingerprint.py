"""Drift detection: fingerprint a window's UIA structure, compare for changes."""

from __future__ import annotations

import hashlib


def compute_fingerprint(uia_dump: list[dict]) -> str:
    """Hash the SHAPE of a UIA tree (roles + names), excluding mutable values."""
    parts = []
    for node in uia_dump:
        role = node.get("role", "")
        name = node.get("name", "")
        depth = node.get("depth", 0)
        parts.append(f"{depth}:{role}:{name}")
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def collect_button_names(uia_dump: list[dict]) -> set[str]:
    return {n["name"] for n in uia_dump if n.get("role") == "ButtonControl" and n.get("name")}


def compare(old_dump: list[dict], new_dump: list[dict], old_fingerprint: str | None = None) -> dict:
    """Compare two UIA dumps. Returns drift summary."""
    old_fp = old_fingerprint or compute_fingerprint(old_dump)
    new_fp = compute_fingerprint(new_dump)
    drift = old_fp != new_fp
    old_buttons = collect_button_names(old_dump)
    new_buttons = collect_button_names(new_dump)
    added = sorted(new_buttons - old_buttons)
    removed = sorted(old_buttons - new_buttons)
    # Drift severity: ratio of changed buttons
    total = max(len(old_buttons | new_buttons), 1)
    changed_ratio = (len(added) + len(removed)) / total
    return {
        "drift_detected": drift,
        "old_fingerprint": old_fp,
        "new_fingerprint": new_fp,
        "buttons_added": added,
        "buttons_removed": removed,
        "changed_ratio": round(changed_ratio, 3),
        "structural_change": changed_ratio > 0.3,
    }
