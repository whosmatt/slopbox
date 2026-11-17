"""JavaScript libraries that sketches may load, served from our own origin.

The files under static/vendor/ are written by scripts/vendor.py from pinned,
integrity-checked npm packages, and committed. Nothing is fetched at runtime,
and the sketch frame's policy still forbids every other network request, so a
library is just more code inside the same sandbox.

Only files listed in the manifest are servable - a fixed set, so /vendor/<path>
cannot be walked anywhere else however the path is spelled.
"""
from __future__ import annotations

import json

from .config import STATIC_DIR

VENDOR_DIR = STATIC_DIR / "vendor"


def _load_manifest():
    try:
        return json.loads((VENDOR_DIR / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


MANIFEST = _load_manifest()

# published path -> file on disk
FILES = {
    rel: VENDOR_DIR / rel
    for pkg in MANIFEST.values()
    for rel in pkg.get("files", ())
}


def version(name):
    return (MANIFEST.get(name) or {}).get("version", "?")


# Sketches get this in <head> ahead of anything they write, so
# `import * as THREE from "three"` and the add-ons just work. Module scripts
# from an opaque-origin frame are CORS requests, hence the header on /vendor.
IMPORT_MAP = (
    '<script type="importmap">'
    + json.dumps(
        {
            "imports": {
                "three": "/vendor/three/three.module.js",
                "three/addons/": "/vendor/three/addons/",
            }
        }
    )
    + "</script>"
)


def addon_names():
    return sorted(
        rel[len("three/addons/"):]
        for rel in FILES
        if rel.startswith("three/addons/") and not rel.startswith("three/addons/shaders/")
    )


def prompt_section():
    """What the model is told about the libraries. Exact versions matter: three.js
    renames things between releases, and code for the wrong one fails."""
    if not FILES:
        return ""
    return (
        "LIBRARIES (sketch frame only, served locally - nothing else can be "
        "fetched):\n"
        "  three.js r%s as an ES module. An import map is already in place:\n"
        '    <script type="module">\n'
        '      import * as THREE from "three";\n'
        '      import { OrbitControls } from "three/addons/controls/OrbitControls.js";\n'
        "    </script>\n"
        "    Add-ons available under three/addons/: %s. No model, texture or font\n"
        "    files exist, so build geometry and textures in code. Size it with\n"
        "    SLOP.fit(canvas, (w, h) => { renderer.setSize(w, h, false);\n"
        "    camera.aspect = w / h; camera.updateProjectionMatrix(); }) once the\n"
        "    renderer and camera exist.\n"
        "  The test browser behind screenshot and play_sketch renders WebGL in\n"
        "  software on a small server, so it runs at a fraction of a visitor's\n"
        "  frame rate. Do not judge or tune performance from it, and do not cut\n"
        "  quality to chase frame rate there. Animate by elapsed time (a clock or\n"
        "  the rAF timestamp), not per frame, so motion looks right at any frame\n"
        "  rate - including in your test plays.\n"
        "  Tone.js %s for sound: <script src=\"/vendor/tone/Tone.js\"></script>, global\n"
        "    Tone. Browsers only allow audio after a click inside the frame, so\n"
        "    call `await Tone.start()` from a click handler - and that only reaches\n"
        "    the frame when it is interactive.\n"
        "  matter.js %s for 2D physics: <script src=\"/vendor/matter/matter.min.js\">"
        "</script>,\n    global Matter."
        % (
            version("three").split(".")[1] if "." in version("three") else version("three"),
            ", ".join(addon_names()),
            version("tone"),
            version("matter-js"),
        )
    )
