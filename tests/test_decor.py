"""Decor sanitiser tests. No browser, no model, no secrets.

The agent gained the ability to add markup, which is the classic stored-XSS
shape: whatever it writes is served to every visitor of a shared page. These
cases are the ones that would matter.

Run with: python -m tests.test_decor
"""
import sys

from app.html_guard import sanitize_decor

MUST_REJECT = [
    # Impersonating the page's own elements would shadow getElementById for the
    # guard and the validator, quietly disarming the safety net.
    ("id impersonates input", '<svg><linearGradient id="prompt-input"/></svg>'),
    ("id impersonates guard", '<svg><linearGradient id="slopbox-guard"/></svg>'),
    ("id in app namespace", '<svg><radialGradient id="qb-root"/></svg>'),
    ("reserved class", '<div class="qb-header">x</div>'),
    ("reserved class among others", '<div class="mine msg-text">x</div>'),
    ("id with odd characters", '<svg><linearGradient id="a b:c"/></svg>'),
    # Outbound references and script.
    ("javascript url", '<div class="a">x</div><svg><rect fill="url(javascript:alert(1))"/></svg>'),
    ("remote paint url", '<svg><rect fill="url(https://evil/x)"/></svg>'),
    ("script text", '<div>ok</div><script>alert(1)</script>'),
    ("onerror text", '<div onerror="alert(1)">x</div>'),
    # Nothing survives the allowlist.
    ("only forbidden tags", '<iframe src="https://evil"></iframe><form></form>'),
    ("bare image", '<img src="https://evil/x.png">'),
    ("too large", "<div>" + ("x" * 30000) + "</div>"),
    ("not a string", None),
]

MUST_ACCEPT = [
    ("plain structure", '<div class="blob"><p>hello</p></div>'),
    ("svg shapes", '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" fill="#f0f"/></svg>'),
    (
        "svg gradient with its own id",
        '<svg viewBox="0 0 8 8"><defs><linearGradient id="myFade">'
        '<stop offset="0" stop-color="red"/></linearGradient></defs>'
        '<rect width="8" height="8" fill="url(#myFade)"/></svg>',
    ),
    (
        "svg noise filter",
        '<svg viewBox="0 0 40 40"><filter id="grit">'
        '<feTurbulence type="fractalNoise" baseFrequency="0.7" numOctaves="2"/>'
        '</filter><rect width="40" height="40" filter="url(#grit)"/></svg>',
    ),
    ("headings and lists", '<h2 class="t">Title</h2><ul><li>one</li><li>two</li></ul>'),
    ("empty clears it", ""),
    ("whitespace clears it", "   \n  "),
]


def main():
    failures = []

    for name, html in MUST_REJECT:
        r = sanitize_decor(html)
        if r.ok:
            failures.append("SHOULD REJECT but accepted: %s -> %r" % (name, r.html[:60]))
            print("  ACCEPTED  %-26s %r" % (name, r.html[:48]))
        else:
            print("  reject ok %-26s %s" % (name, r.message[:60]))

    for name, html in MUST_ACCEPT:
        r = sanitize_decor(html)
        if not r.ok:
            failures.append("SHOULD ACCEPT but rejected: %s -> %s" % (name, r.message))
            print("  REJECTED  %-26s %s" % (name, r.message[:60]))
        else:
            print("  accept ok %-26s %d bytes" % (name, len(r.html)))

    # Stripping, rather than rejecting, is fine for individual bad parts as long
    # as nothing dangerous survives into the output.
    print()
    print("dangerous parts do not survive alongside good ones:")
    mixed = sanitize_decor(
        '<div class="keep">text</div>'
        '<a href="https://evil">link</a>'
        '<div onclick="alert(1)">handler</div>'
        '<svg><foreignObject><body>escape</body></foreignObject></svg>'
    )
    out = mixed.html.lower() if mixed.ok else ""
    for token in ("href", "onclick", "foreignobject", "<script", "javascript:"):
        ok = token not in out
        print(("  ok    " if ok else "  FAIL  ") + "no %r in output" % token)
        if not ok:
            failures.append("dangerous token survived: %s" % token)
    kept = mixed.ok and 'class="keep"' in mixed.html
    print(("  ok    " if kept else "  FAIL  ") + "the harmless part is kept")
    if not kept:
        failures.append("harmless markup was lost")

    print()
    if failures:
        for f in failures:
            print("FAIL:", f)
        print("\n%d/%d checks failed" % (len(failures), len(MUST_REJECT) + len(MUST_ACCEPT) + 6))
        return 1
    print("all %d decor checks passed" % (len(MUST_REJECT) + len(MUST_ACCEPT) + 6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
