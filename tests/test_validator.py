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


PAGE_WIRING_JS = r"""
() => ({
  mode: document.documentElement.getAttribute('data-mode'),
  styleSheets: [...document.querySelectorAll('link[rel=stylesheet]')]
    .map(l => l.getAttribute('href')),
  ownSheets: document.querySelectorAll('link[data-qb-style]').length,
  bodyBg: getComputedStyle(document.body).backgroundColor,
})
"""


async def check_page_wiring(base):
    """Two bugs that shipped, and that no CSS fixture would have caught.

    1. `reload` events used to sit in the SSE replay buffer, so every fresh page
       load re-applied them - which styled ?safe=1, the one page that must stay
       bare.
    2. restyle() removed a single link captured up front, so two swaps in quick
       succession left an orphan sheet behind. An orphan with !important rules
       outranks the new sheet, so the page kept its old background until F5.
    """
    problems = []
    if POOL._browser is None:
        await POOL.start()
    context = await POOL._browser.new_context(viewport={"width": 1000, "height": 700})
    try:
        page = await context.new_page()
        await page.goto(base + "/?safe=1", wait_until="load")
        await page.wait_for_timeout(2500)
        safe = await page.evaluate(PAGE_WIRING_JS)
        if any("style.css" in (h or "") for h in safe["styleSheets"]):
            problems.append("?safe=1 pulled in the live stylesheet: %s" % safe["styleSheets"])
        if safe["bodyBg"] != "rgb(255, 255, 255)":
            problems.append("?safe=1 is not the bland base look (bg %s)" % safe["bodyBg"])

        page2 = await context.new_page()
        await page2.goto(base + "/", wait_until="load")
        await page2.wait_for_timeout(2000)
        live = await page2.evaluate(PAGE_WIRING_JS)
        if live["ownSheets"] != 1:
            problems.append(
                "the live page carries %d slopbox stylesheets, expected exactly 1: %s"
                % (live["ownSheets"], live["styleSheets"])
            )
    finally:
        await context.close()
    return problems


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
    print("page wiring:")
    try:
        wiring = await check_page_wiring(self_url())
        for w in wiring:
            failures.append("PAGE WIRING: " + w)
            print("  FAIL       %s" % w[:74])
        if not wiring:
            print("  ok         ?safe=1 stays bare; live page carries exactly one stylesheet")
    except Exception as exc:
        print("  skipped    (%s: %s)" % (type(exc).__name__, str(exc)[:60]))

    print()
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("all %d validator checks behaved correctly" % (len(SHOULD_FAIL) + len(SHOULD_PASS)))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
