"""History-ring tests. No browser, no model, no secrets.

The gallery made screenshots the first thing this service persists, so the
"disk footprint is bounded by construction" promise now has to hold for three
artefacts per design - stylesheet, thumbnail and index row - which must be
pruned together or the ring leaks.

Run with: python -m tests.test_store
"""
import os
import shutil
import tempfile

# Must be set before app.config is imported, since it reads these once.
_TMP = tempfile.mkdtemp(prefix="slopbox-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["HISTORY_KEEP"] = "5"

import sys  # noqa: E402

from app import store  # noqa: E402
from app.config import HISTORY_KEEP  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0not-a-real-jpeg\xff\xd9"


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (("  " + detail) if detail else ""))
    return bool(condition)


def publish(prompt, css, with_shot=True):
    store.set_candidate(css)
    version = store.publish_candidate(prompt)
    if with_shot:
        store.save_shot(version, JPEG)
    return version


def main():
    results = []
    store.init()

    print("publishing records all three artefacts:")
    v1 = publish("first design", "body{color:#111}")
    results.append(check("stylesheet retained", store.history_css(v1) == "body{color:#111}"))
    results.append(check("thumbnail retained", store.shot_bytes(v1) == JPEG))
    entry = store.history_entries()[0]
    results.append(check("indexed with its prompt", entry["prompt"] == "first design"))
    results.append(check("entry reports its thumbnail", entry["has_shot"] is True))

    print()
    print("a design without a thumbnail is still listed:")
    v2 = publish("no shot", "body{color:#222}", with_shot=False)
    e2 = [e for e in store.history_entries() if e["version"] == v2][0]
    results.append(check("listed anyway", e2["has_shot"] is False))
    results.append(check("no thumbnail bytes", store.shot_bytes(v2) is None))

    print()
    print("newest first, so the gallery reads top-left as most recent:")
    versions = [e["version"] for e in store.history_entries()]
    results.append(check("descending order", versions == sorted(versions, reverse=True),
                         str(versions)))

    print()
    print("the ring prunes stylesheet, thumbnail and index row together:")
    for i in range(HISTORY_KEEP + 4):
        publish("design %d" % i, "body{color:#%03d}" % i)
    entries = store.history_entries()
    results.append(check("retains exactly HISTORY_KEEP designs",
                         len(entries) == HISTORY_KEEP,
                         "kept %d, limit %d" % (len(entries), HISTORY_KEEP)))
    kept = {e["version"] for e in entries}
    css_files = set(int(f.stem) for f in store.HISTORY_DIR.glob("*.css"))
    jpg_files = set(int(f.stem) for f in store.HISTORY_DIR.glob("*.jpg"))
    results.append(check("no stylesheet outlives the ring", css_files == kept))
    results.append(check("no thumbnail outlives its stylesheet", jpg_files <= kept,
                         "orphans: %s" % sorted(jpg_files - kept)))
    index_rows = set(int(k) for k in store._read_index())
    results.append(check("no index row outlives its stylesheet", index_rows <= kept,
                         "orphans: %s" % sorted(index_rows - kept)))
    results.append(check("an aged-out design reads back as absent",
                         store.history_css(v1) is None and store.shot_bytes(v1) is None))

    print()
    print("an orphaned thumbnail is swept up:")
    (store.HISTORY_DIR / "099999.jpg").write_bytes(JPEG)
    publish("triggers a prune", "body{color:#333}")
    results.append(check("orphan removed",
                         not (store.HISTORY_DIR / "099999.jpg").exists()))

    print()
    print("reset returns the live look to bland but keeps the archive:")
    before = len(store.history_entries())
    store.reset()
    after = store.history_entries()
    results.append(check("archive survives", len(after) == before,
                         "%d -> %d" % (before, len(after))))
    results.append(check("live look is bland", store.current_css() == store.SEED_CSS))
    results.append(check("thumbnails survive too",
                         store.shot_bytes(after[0]["version"]) == JPEG))

    print()
    print("bad input does not raise:")
    results.append(check("unknown version", store.history_css(999999) is None))
    results.append(check("non-numeric version", store.history_css("nonsense") is None))
    results.append(check("thumbnail for unknown version", store.shot_bytes(999999) is None))
    results.append(check("thumbnail refused without a stylesheet",
                         store.save_shot(424242, JPEG) is False))

    print()
    failed = results.count(False)
    if failed:
        print("%d/%d checks failed" % (failed, len(results)))
        return 1
    print("all %d history-ring checks passed" % len(results))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
