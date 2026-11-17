"""Refresh the JavaScript libraries sketches may use, under static/vendor/.

    python scripts/vendor.py

Downloads each pinned npm tarball, checks it against the registry's sha512
integrity, and copies out only the files listed below - for three.js add-ons
also every module they import relatively, so a listed add-on always works. The
output is committed: the image never fetches anything at build or run time. Each .js also
gets a pre-compressed .js.gz twin.

To upgrade, change a version below, run this, and update LIBRARIES in
app/vendor.py if a file moved.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import posixpath
import re
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "vendor"

PACKAGES = {
    "three": {
        "version": "0.187.0",
        "files": {
            "build/three.module.js": "three/three.module.js",
            "build/three.core.js": "three/three.core.js",
        },
        # Followed recursively through relative imports.
        "addons_root": "examples/jsm/",
        "addons": [
            "controls/OrbitControls.js",
            "postprocessing/EffectComposer.js",
            "postprocessing/RenderPass.js",
            "postprocessing/ShaderPass.js",
            "postprocessing/OutputPass.js",
            "postprocessing/UnrealBloomPass.js",
            "postprocessing/AfterimagePass.js",
            "postprocessing/GlitchPass.js",
            "geometries/RoundedBoxGeometry.js",
            "environments/RoomEnvironment.js",
            "math/ImprovedNoise.js",
            "math/SimplexNoise.js",
        ],
        "addons_out": "three/addons/",
    },
    "tone": {
        "version": "15.1.22",
        "files": {"build/Tone.js": "tone/Tone.js"},
    },
    "matter-js": {
        "version": "0.20.0",
        "files": {"build/matter.min.js": "matter/matter.min.js"},
    },
}

IMPORT_RE = re.compile(r"""(?:import|export)[^'"]*?from\s*['"](\.[^'"]+)['"]|import\s*['"](\.[^'"]+)['"]""")


def fetch(name, version):
    meta = json.load(urllib.request.urlopen(
        "https://registry.npmjs.org/%s/%s" % (name, version), timeout=60))
    dist = meta["dist"]
    data = urllib.request.urlopen(dist["tarball"], timeout=120).read()
    algo, _, expected = dist["integrity"].partition("-")
    if algo != "sha512":
        sys.exit("%s: unexpected integrity algorithm %s" % (name, algo))
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != expected:
        sys.exit("%s@%s: integrity mismatch, refusing to vendor it" % (name, version))
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz"), meta.get("license", "?")


def read(tar, path):
    member = tar.getmember("package/" + path)
    return tar.extractfile(member).read()


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    manifest = {}
    for name, spec in PACKAGES.items():
        tar, licence = fetch(name, spec["version"])
        written = []
        for src, dst in spec.get("files", {}).items():
            target = OUT / dst
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read(tar, src))
            written.append(dst)

        root = spec.get("addons_root")
        queue = list(spec.get("addons", []))
        seen = set()
        while queue:
            rel = posixpath.normpath(queue.pop())
            if rel in seen:
                continue
            seen.add(rel)
            body = read(tar, root + rel)
            target = OUT / spec["addons_out"] / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            written.append(spec["addons_out"] + rel)
            for m in IMPORT_RE.finditer(body.decode("utf-8")):
                dep = m.group(1) or m.group(2)
                queue.append(posixpath.join(posixpath.dirname(rel), dep))

        for licence_file in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
            try:
                body = read(tar, licence_file)
            except KeyError:
                continue
            target = OUT / written[0].split("/")[0] / "LICENSE"
            target.write_bytes(body)
            break
        manifest[name] = {"version": spec["version"], "license": licence,
                          "files": sorted(written)}
        print("%s@%s (%s): %d files" % (name, spec["version"], licence, len(written)))

    # Pre-compressed twins, served when the browser accepts gzip. three.js
    # alone is ~2MB uncompressed, and compressing per request would be waste.
    for path in OUT.rglob("*.js"):
        path.with_name(path.name + ".gz").write_bytes(
            gzip.compress(path.read_bytes(), compresslevel=9, mtime=0))

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
