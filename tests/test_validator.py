"""Validator tests. Run inside a running slopbox container:

    docker compose exec slopbox python -m tests.test_validator

Writes candidate stylesheets and asserts the browser gate accepts the usable
ones and rejects each way of breaking the prompt box.
"""
import asyncio
import sys

from app import store
from app.config import self_url
from app.css_guard import sanitize
from app.validator import POOL

SHOULD_FAIL = [
    ("display none", "#prompt-input{display:none}"),
    ("hidden ancestor", "#prompt-form{visibility:hidden}"),
    ("zero size", "#prompt-input{width:0;height:0;padding:0;border:0}"),
    ("off screen", "#prompt-input{position:fixed;left:-4000px;top:-4000px}"),
    ("transparent", "#prompt-input{opacity:0.02}"),
    ("no pointer events", "#prompt-input{pointer-events:none}"),
    (
        "covered by overlay",
        "body::after{content:'';position:fixed;inset:0;background:#f0f;z-index:9999}",
    ),
    ("invisible text", "#prompt-input{color:#fff;background:#fff}"),
    ("tiny font", "#prompt-input{font-size:2px}"),
    ("heavy blur", "#qb-main{filter:blur(24px)}"),
    (
        "animated off screen",
        "@keyframes fly{0%{transform:translateX(0)}100%{transform:translateX(4000px)}}"
        "#prompt-input{animation:fly 1s 0.5s forwards}",
    ),
    ("body hidden", "body{display:none}"),
    # The chat log is where the visitor watches the agent work, so erasing it
    # is a failure too, not a style choice.
    ("chat hidden", "#chat{display:none}"),
    # Needs an ID in the selector to outrank base.css's .msg.user etc; with a
    # bare .msg the per-role backgrounds survive and the text stays legible.
    ("chat invisible text", "#chat .msg{background:#000}#chat .msg-text{color:#050505}"),
    ("chat collapsed", "#chat{height:4px;overflow:hidden}"),
    ("chat transparent", "#chat{opacity:0.05}"),
]

SHOULD_PASS = [
    ("bold colors", "body{background:#111;color:#0f0}#prompt-input{background:#000;color:#0f0;"
                    "border:3px solid #0f0}"),
    (
        "wobbling but on screen",
        "@keyframes wob{0%{transform:rotate(-2deg)}100%{transform:rotate(2deg)}}"
        "#prompt-input{animation:wob .8s infinite alternate;background:#ffe;color:#300;"
        "border:4px dashed #c0f}",
    ),
    ("crooked whimsy", "body{background:repeating-linear-gradient(45deg,#ff0,#ff0 20px,#f0f 20px,"
                       "#f0f 40px)}#prompt-input{transform:rotate(-3deg);background:#fff;color:#000;"
                       "border:5px solid #000;font-size:22px}"),
    # A heavily restyled but still readable chat log must not trip the gate.
    (
        "chat restyled",
        "#chat{gap:2px;background:#101014;padding:10px;border-radius:14px}"
        ".msg{background:#1b1b22;border:1px solid #33334a;border-radius:10px;padding:10px}"
        ".msg-text{color:#d8d8ea}.msg-role{color:#7a7a9a}"
        ".msg.thinking>summary{color:#8888aa}",
    ),
]


async def main():
    store.init()
    await POOL.start()
    failures = []
    try:
        for name, css in SHOULD_FAIL:
            guarded = sanitize(css)
            if not guarded.ok:
                failures.append("sanitiser wrongly rejected fixture %r: %s" % (name, guarded.message))
                continue
            store.set_candidate(guarded.css)
            report = await POOL.inspect(self_url() + "/preview")
            if report.ok:
                failures.append("VALIDATOR MISSED: %s" % name)
                print("  MISSED     %-22s (published would have broken the site)" % name)
            else:
                print("  caught     %-22s %s" % (name, report.problems[0][:78]))

        for name, css in SHOULD_PASS:
            guarded = sanitize(css)
            if not guarded.ok:
                failures.append("sanitiser wrongly rejected %r: %s" % (name, guarded.message))
                continue
            store.set_candidate(guarded.css)
            report = await POOL.inspect(self_url() + "/preview")
            if not report.ok:
                failures.append("FALSE POSITIVE on %s: %s" % (name, report.problems[:3]))
                print("  FALSE POS  %-22s %s" % (name, report.problems[0][:70]))
            else:
                print("  passed     %-22s %s" % (name, list(report.info.keys())))
    finally:
        store.clear_candidate()
        await POOL.stop()

    print()
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("all %d validator checks behaved correctly" % (len(SHOULD_FAIL) + len(SHOULD_PASS)))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
