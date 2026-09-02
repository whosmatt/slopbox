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
# Per-version notes for the gallery (the prompt that produced each look).
# Trimmed alongside the history ring, so it cannot grow either.
HISTORY_INDEX = DATA_DIR / "history.json"

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
        _record_history(meta["version"], prompt)
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
    """Return the live look to bland, keeping the gallery.

    Reset is the emergency lever for a bad style, so it deliberately does NOT
    delete the archive any more: destroying twenty browsable designs as a side
    effect of recovering the front page is the wrong trade, and leaving them
    means a visitor can still pin one afterwards.
    """
    with _lock:
        LAST_GOOD.write_text(SEED_CSS, encoding="utf-8")
        CURRENT.write_text(SEED_CSS, encoding="utf-8")
        meta = read_meta()
        meta["version"] = int(meta.get("version", 0)) + 1
        meta["prompt"] = "(reset)"
        meta["updated"] = time.time()
        _write_meta(meta)
        CANDIDATE.unlink(missing_ok=True)
        return meta["version"]


def _snapshot(version, css):
    path = HISTORY_DIR / ("%06d.css" % version)
    path.write_text(css, encoding="utf-8")
    _prune_history()


def save_shot(version, jpeg):
    """Keep the validated render of a published version, for the gallery.

    This is the one place screenshots touch disk. It is bounded exactly like the
    stylesheet ring - one image per retained version, pruned together with it -
    so the ceiling stays fixed rather than growing with use.
    """
    with _lock:
        if not (HISTORY_DIR / ("%06d.css" % version)).exists():
            return False
        (HISTORY_DIR / ("%06d.jpg" % version)).write_bytes(jpeg)
        _prune_history()
        return True


def shot_bytes(version):
    try:
        return (HISTORY_DIR / ("%06d.jpg" % int(version))).read_bytes()
    except (OSError, ValueError):
        return None


def history_css(version):
    """The stylesheet of a retained version, or None if it has aged out."""
    try:
        return (HISTORY_DIR / ("%06d.css" % int(version))).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _read_index():
    try:
        data = json.loads(HISTORY_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_history(version, prompt):
    index = _read_index()
    index[str(version)] = {"prompt": prompt[:400], "ts": time.time()}
    HISTORY_INDEX.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _versions_on_disk():
    try:
        out = []
        for f in HISTORY_DIR.glob("*.css"):
            try:
                out.append(int(f.stem))
            except ValueError:
                continue
        return sorted(out)
    except FileNotFoundError:
        return []


def history_entries():
    """Retained designs, newest first, for the gallery.

    Files on disk are the source of truth; the index only decorates them. That
    way a store written before the index existed still lists correctly, just
    without prompts.
    """
    index = _read_index()
    entries = []
    for v in reversed(_versions_on_disk()):
        note = index.get(str(v)) or {}
        entries.append(
            {
                "version": v,
                "prompt": note.get("prompt") or "",
                "ts": note.get("ts") or 0.0,
                "has_shot": (HISTORY_DIR / ("%06d.jpg" % v)).exists(),
            }
        )
    return entries


def _prune_history():
    versions = _versions_on_disk()
    keep = set(versions[-HISTORY_KEEP:]) if len(versions) > HISTORY_KEEP else set(versions)
    for v in versions:
        if v not in keep:
            (HISTORY_DIR / ("%06d.css" % v)).unlink(missing_ok=True)
            (HISTORY_DIR / ("%06d.jpg" % v)).unlink(missing_ok=True)
    # Images whose stylesheet has aged out, and index rows for both.
    try:
        for f in HISTORY_DIR.glob("*.jpg"):
            try:
                if int(f.stem) not in keep:
                    f.unlink(missing_ok=True)
            except ValueError:
                f.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    index = _read_index()
    trimmed = {k: v for k, v in index.items() if k.isdigit() and int(k) in keep}
    if trimmed != index:
        HISTORY_INDEX.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")
