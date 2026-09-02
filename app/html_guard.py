"""Allowlist sanitiser for the agent's decorative markup.

Same discipline as the CSS guard: parse with a real parser (nh3, the Rust
ammonia bindings), keep only what an allowlist recognises, and emit the parsed
tree rather than the original text. Structure is what the agent gains here -
extra elements and inline SVG, which CSS alone cannot conjure - not behaviour.

What makes this containable, in order of importance:

1. The markup is inert. No script, no event handlers, no links, no forms, no
   remote references. nh3 drops all of it, and the page CSP (`script-src 'self'`,
   `form-action 'none'`) independently denies the same things, so a sanitiser
   bypass still lands on a closed door.
2. It cannot impersonate the app's own elements. `id` is stripped everywhere it
   is not structurally required, and where it is (SVG gradients), the value must
   pass a shape check and avoid the app's namespace. An element answering to
   `prompt-input` would otherwise capture `getComputedStyle`/`getElementById`
   from the guard and the validator, since the first match in document order
   wins - which would quietly disarm the safety net.
3. It lands in one fixed mount point, so #prompt-input, #chat and the guard host
   are still guaranteed to exist and the validator can still assert usability.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import nh3

from .config import MAX_DECOR_BYTES

# Structural elements plus a useful SVG subset. Deliberately absent: script,
# style, link, meta, iframe, object, embed, form and its inputs, a, img, video,
# audio, template, math, and the SVG escape hatches (foreignObject, use,
# animate, set, script).
TAGS = {
    # structure
    "div", "span", "p", "section", "article", "aside", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "blockquote", "figure", "figcaption", "pre", "code",
    "strong", "em", "b", "i", "u", "s", "small", "mark", "sub", "sup",
    "br", "hr", "table", "thead", "tbody", "tr", "th", "td",
    # inline svg
    "svg", "g", "defs", "path", "circle", "ellipse", "rect", "line",
    "polygon", "polyline", "text", "tspan", "title",
    "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern",
    "filter", "feTurbulence", "feGaussianBlur", "feColorMatrix", "feBlend",
    "feOffset", "feMerge", "feMergeNode", "feDisplacementMap",
}

_SHAPE = {"fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
          "stroke-dasharray", "stroke-opacity", "fill-opacity", "opacity",
          "transform", "clip-path", "mask", "filter"}

ATTRS = {
    # class is how the agent's CSS targets its own markup; id is not offered.
    "*": {"class"},
    "svg": {"viewBox", "width", "height", "preserveAspectRatio", "xmlns"} | _SHAPE,
    "g": _SHAPE,
    "path": {"d"} | _SHAPE,
    "circle": {"cx", "cy", "r"} | _SHAPE,
    "ellipse": {"cx", "cy", "rx", "ry"} | _SHAPE,
    "rect": {"x", "y", "width", "height", "rx", "ry"} | _SHAPE,
    "line": {"x1", "y1", "x2", "y2"} | _SHAPE,
    "polygon": {"points"} | _SHAPE,
    "polyline": {"points"} | _SHAPE,
    "text": {"x", "y", "dx", "dy", "font-size", "font-family", "font-weight",
             "text-anchor", "letter-spacing"} | _SHAPE,
    "tspan": {"x", "y", "dx", "dy", "font-size", "text-anchor"} | _SHAPE,
    # These genuinely need an id so paint can reference them by fragment.
    "linearGradient": {"id", "x1", "y1", "x2", "y2", "gradientUnits",
                       "gradientTransform", "spreadMethod"},
    "radialGradient": {"id", "cx", "cy", "r", "fx", "fy", "gradientUnits",
                       "gradientTransform"},
    "stop": {"offset", "stop-color", "stop-opacity"},
    "clipPath": {"id", "clipPathUnits"},
    "mask": {"id", "maskUnits", "x", "y", "width", "height"},
    "pattern": {"id", "x", "y", "width", "height", "patternUnits",
                "patternTransform", "viewBox"},
    "filter": {"id", "x", "y", "width", "height", "filterUnits"},
    "feTurbulence": {"type", "baseFrequency", "numOctaves", "seed", "result"},
    "feGaussianBlur": {"in", "stdDeviation", "result"},
    "feColorMatrix": {"in", "type", "values", "result"},
    "feBlend": {"in", "in2", "mode", "result"},
    "feOffset": {"in", "dx", "dy", "result"},
    "feMergeNode": {"in"},
    "feDisplacementMap": {"in", "in2", "scale", "xChannelSelector",
                          "yChannelSelector", "result"},
}

# Names the app itself answers to. An agent element carrying one of these could
# shadow the real thing for getElementById and for the guard's DOM lookups.
RESERVED_PREFIXES = ("qb-", "gal-", "msg", "slopbox", "prompt-", "chat")
RESERVED_EXACT = {"chat", "prompt-input", "prompt-form", "prompt-submit"}

ID_SHAPE = re.compile(r"^[a-zA-Z][A-Za-z0-9_-]{0,31}$")
ID_ATTR = re.compile(r'\bid\s*=\s*"([^"]*)"', re.I)
URL_REF = re.compile(r"url\(\s*['\"]?([^'\")]*)", re.I)
BANNED_TEXT = ("javascript:", "vbscript:", "<script", "onerror=", "onload=")


@dataclass
class DecorResult:
    ok: bool
    html: str = ""
    errors: list = field(default_factory=list)
    note: str = ""

    @property
    def message(self):
        return "; ".join(self.errors[:6])


def _reserved(value):
    low = value.strip().lower()
    return low in RESERVED_EXACT or any(low.startswith(p) for p in RESERVED_PREFIXES)


def sanitize_decor(html):
    """Validate untrusted markup. On success returns the re-serialised tree."""
    if not isinstance(html, str):
        return DecorResult(False, errors=["decor must be a string of HTML"])

    raw = html.replace("\r\n", "\n").replace("\x00", "")
    if not raw.strip():
        return DecorResult(True, html="", note="decor cleared")

    if len(raw.encode("utf-8")) > MAX_DECOR_BYTES:
        return DecorResult(
            False,
            errors=["decor too large: %d bytes (limit %d)"
                    % (len(raw.encode("utf-8")), MAX_DECOR_BYTES)],
        )

    low = raw.lower()
    for bad in BANNED_TEXT:
        if bad in low:
            return DecorResult(False, errors=["forbidden text in decor: " + repr(bad)])

    cleaned = nh3.clean(
        raw,
        tags=TAGS,
        attributes=ATTRS,
        url_schemes=set(),
        strip_comments=True,
        link_rel=None,
    )

    # Value-level checks nh3 cannot express: ids must be safe names outside the
    # app's namespace, and paint references must stay inside this document.
    for value in ID_ATTR.findall(cleaned):
        if not ID_SHAPE.match(value):
            return DecorResult(
                False,
                errors=["id %r is not a simple name (letters, digits, - and _)" % value[:40]],
            )
        if _reserved(value):
            return DecorResult(
                False,
                errors=["id %r collides with the page's own elements - pick another"
                        % value[:40]],
            )
    for ref in URL_REF.findall(cleaned):
        target = ref.strip()
        if not target.startswith("#"):
            return DecorResult(
                False,
                errors=["url(%r) in decor may only reference a fragment in the same "
                        "markup, like url(#my-gradient)" % target[:40]],
            )
        if _reserved(target[1:]):
            return DecorResult(False, errors=["url(%r) points at a reserved name" % target[:40]])
    for cls in re.findall(r'\bclass\s*=\s*"([^"]*)"', cleaned, re.I):
        for one in cls.split():
            if _reserved(one):
                return DecorResult(
                    False,
                    errors=["class %r is reserved by the page - prefix your own instead"
                            % one[:40]],
                )

    if not cleaned.strip():
        return DecorResult(
            False,
            errors=["every element was rejected. Allowed: plain structural tags and "
                    "inline SVG shapes, with class attributes. No script, links, "
                    "forms, images, iframes or style attributes."],
        )

    note = ""
    if len(cleaned) < len(raw) * 0.6:
        note = ("a good deal was stripped (%d -> %d bytes) - check the screenshot"
                % (len(raw), len(cleaned)))
    return DecorResult(True, html=cleaned, note=note)
