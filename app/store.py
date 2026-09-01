"""Persistent state for slopbox.

Disk footprint is bounded by construction: three fixed-name files plus a
history ring of at most HISTORY_KEEP entries, pruned on every write. Nothing
here grows without limit, so the container never accumulates garbage.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import DATA_DIR, HISTORY_KEEP

CURRENT = DATA_DIR / "current.css"
CANDIDATE = DATA_DIR / "candidate.css"
LAST_GOOD = DATA_DIR / "last_good.css"
HISTORY_DIR = DATA_DIR / "history"
META = DATA_DIR / "meta.json"

_lock = threading.RLock()

SEED_CSS = """/* slopbox starts bland. Type a prompt to change that. */
"""


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        if not CURRENT.exists():
            CURRENT.write_text(SEED_CSS, encoding="utf-8")
        if not LAST_GOOD.exists():
            LAST_GOOD.write_text(CURRENT.read_text(encoding="utf-8"), encoding="utf-8")
        if not META.exists():
            _write_meta({"version": 0, "prompt": "", "updated": 0.0, "publishes": 0})
        # A candidate left over from a crashed run is meaningless; drop it.
        CANDIDATE.unlink(missing_ok=True)
    _prune_history()


def _write_meta(meta):
    META.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def read_meta():
    try:
        return json.loads(META.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 0, "prompt": "", "updated": 0.0, "publishes": 0}


def current_css():
    with _lock:
        try:
            return CURRENT.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SEED_CSS


def candidate_css():
    with _lock:
        try:
            return CANDIDATE.read_text(encoding="utf-8")
        except FileNotFoundError:
            # No candidate yet: preview should show what is live.
            return current_css()


def has_candidate():
    return CANDIDATE.exists()


def set_candidate(css):
    with _lock:
        CANDIDATE.write_text(css, encoding="utf-8")


def clear_candidate():
    with _lock:
        CANDIDATE.unlink(missing_ok=True)


def publish_candidate(prompt):
    """Promote the candidate to live. Caller must have validated it."""
    with _lock:
        css = candidate_css()
        prev = current_css()
        LAST_GOOD.write_text(prev, encoding="utf-8")
        CURRENT.write_text(css, encoding="utf-8")
        meta = read_meta()
        meta["version"] = int(meta.get("version", 0)) + 1
        meta["publishes"] = int(meta.get("publishes", 0)) + 1
        meta["prompt"] = prompt[:400]
        meta["updated"] = time.time()
        _write_meta(meta)
        _snapshot(meta["version"], css)
        CANDIDATE.unlink(missing_ok=True)
        return meta["version"]


def rollback():
    """Restore the last known-good stylesheet."""
    with _lock:
        try:
            good = LAST_GOOD.read_text(encoding="utf-8")
        except FileNotFoundError:
            good = SEED_CSS
        CURRENT.write_text(good, encoding="utf-8")
        meta = read_meta()
        meta["version"] = int(meta.get("version", 0)) + 1
        meta["prompt"] = "(rolled back)"
        meta["updated"] = time.time()
        _write_meta(meta)
        CANDIDATE.unlink(missing_ok=True)
        return meta["version"]


def reset():
    """Back to bland."""
    with _lock:
        LAST_GOOD.write_text(SEED_CSS, encoding="utf-8")
        CURRENT.write_text(SEED_CSS, encoding="utf-8")
        meta = read_meta()
        meta["version"] = int(meta.get("version", 0)) + 1
        meta["prompt"] = "(reset)"
        meta["updated"] = time.time()
        _write_meta(meta)
        CANDIDATE.unlink(missing_ok=True)
        for f in HISTORY_DIR.glob("*.css"):
            f.unlink(missing_ok=True)
        return meta["version"]


def _snapshot(version, css):
    path = HISTORY_DIR / ("%06d.css" % version)
    path.write_text(css, encoding="utf-8")
    _prune_history()


def _prune_history():
    try:
        files = sorted(HISTORY_DIR.glob("*.css"))
    except FileNotFoundError:
        return
    for stale in files[:-HISTORY_KEEP] if len(files) > HISTORY_KEEP else []:
        stale.unlink(missing_ok=True)
