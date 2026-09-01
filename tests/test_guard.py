"""Sanitiser tests. Run with: python -m tests.test_guard (no pytest needed)."""
import sys

from app.css_guard import sanitize

MUST_REJECT = [
    ("@import", '@import url("https://evil.example/x.css"); body{color:red}'),
    ("@import bare", "@import 'x.css';"),
    ("@font-face", "@font-face{font-family:x;src:url(https://evil/x.woff2)}"),
    ("external url", "body{background:url(https://evil.example/pixel.png)}"),
    ("protocol-relative url", "body{background:url(//evil.example/p.png)}"),
    ("javascript url", "body{background:url(javascript:alert(1))}"),
    ("expression", "body{width:expression(alert(1))}"),
    ("behavior", "body{behavior:url(#default#time2)}"),
    ("moz-binding", "body{-moz-binding:url(http://evil/x.xml#e)}"),
    ("svg data uri", "body{background:url(data:image/svg+xml;base64,AAAA)}"),
    ("font data uri", "body{background:url(data:font/woff2;base64,AAAA)}"),
    ("html data uri", "body{background:url(data:text/html;base64,AAAA)}"),
    ("guard selector", "#slopbox-guard{display:none}"),
    ("guard attr selector", "[data-slopbox-guard]{opacity:0}"),
    ("guard descendant", "body > div#slopbox-guard .wrap{display:none}"),
    ("style tag escape", "body{content:'</style><script>alert(1)</script>'}"),
    ("moz-element", "body{background:-moz-element(#chat)}"),
    ("at-rule unknown", "@document url(x){body{color:red}}"),
    ("namespace", "@namespace svg url(http://www.w3.org/2000/svg);"),
    ("empty", ""),
    ("garbage", "this is not css at all"),
    ("too big", "body{color:red}" * 20000),
    ("nested import in media", "@media screen{@import url(https://evil/x.css);}"),
    ("url in image-set", "body{background:image-set(url(https://evil/a.png) 1x)}"),
    ("url spaced", "body{background:url( https://evil.example/p.png )}"),
]

MUST_ACCEPT = [
    ("plain", "body{background:#ff00ff;color:#000}"),
    ("input styling", "#prompt-input{border:4px dashed lime;font-size:20px}"),
    ("media query", "@media (max-width:480px){#qb-main{padding:4px}}"),
    ("keyframes", "@keyframes wob{0%{transform:rotate(-3deg)}100%{transform:rotate(3deg)}}"
                  "#qb-title{animation:wob 1s infinite alternate}"),
    ("prefixed keyframes", "@-webkit-keyframes k{from{opacity:.5}to{opacity:1}}"),
    ("supports", "@supports (display:grid){#qb-root{display:grid}}"),
    ("nesting", "#qb-main{color:red; & #chat{color:blue}}"),
    ("layer", "@layer base{body{margin:0}}"),
    ("container", "@container (min-width:300px){#chat{gap:2px}}"),
    ("gradients", "body{background:linear-gradient(45deg,#f0f,#0ff 50%,#ff0)}"),
    ("content escape", '#qb-title::after{content:"\\2728"}'),
    ("raster data uri", "body{background-image:url(data:image/png;base64,iVBORw0KGgo=)}"),
    ("calc and vars", ":root{--a:4px}#qb-main{gap:calc(var(--a)*2)}"),
    ("comments stripped", "/* hi */ body{color:red} /* bye */"),
]


def main():
    failures = []

    for name, css in MUST_REJECT:
        result = sanitize(css)
        if result.ok:
            failures.append("SHOULD REJECT but accepted: %s" % name)
        else:
            print("  reject ok  %-24s %s" % (name, result.message[:70]))

    for name, css in MUST_ACCEPT:
        result = sanitize(css)
        if not result.ok:
            failures.append("SHOULD ACCEPT but rejected: %s -> %s" % (name, result.message))
        else:
            print("  accept ok  %-24s %d rules" % (name, result.stats.get("rules", 0)))

    print()
    if failures:
        for f in failures:
            print("FAIL:", f)
        print("\n%d/%d checks failed" % (len(failures), len(MUST_REJECT) + len(MUST_ACCEPT)))
        return 1
    print("all %d sanitiser checks passed" % (len(MUST_REJECT) + len(MUST_ACCEPT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
