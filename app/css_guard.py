"""Structural CSS sanitiser.

The agent is untrusted: its instructions come from a public text box, so prompt
injection is assumed, not prevented. This module is the capability boundary for
the one write the agent can perform. It parses CSS properly (tinycss2, not
regex), rejects anything outside an allowlist, and re-serialises only from the
token tree that passed - so the bytes reaching disk are bytes we understood.

Rejected on purpose:
  @import / @font-face / @namespace  - outbound requests
  url(...) other than inline raster  - outbound requests, exfil via selectors
  expression() / -moz-binding        - legacy script execution
  behavior / -o-link                 - legacy script execution
  selectors touching the guard UI    - tampering with the safety net
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import tinycss2

from .config import ALLOWED_URL_PREFIXES, MAX_CSS_BYTES

ALLOWED_AT_RULES = {"media", "supports", "keyframes", "layer", "container", "scope"}
# Prefixed keyframes are common and harmless.
ALLOWED_AT_RULES |= {p + "keyframes" for p in ("-webkit-", "-moz-", "-o-", "-ms-")}

BANNED_PROPERTIES = {
    "behavior",
    "-moz-binding",
    "-o-link",
    "-o-link-source",
    "-ms-behavior",
}

BANNED_FUNCTIONS = {
    "expression",
    "url-prefix",
    "domain",
    "regexp",
    "-moz-binding",
    # element()/-moz-element() paints live document nodes into a background.
    "element",
    "-moz-element",
    "-webkit-element",
    "-ms-element",
}

# Substrings that must not appear anywhere, in any casing, even inside strings.
BANNED_SUBSTRINGS = ("javascript:", "vbscript:", "<script", "</style", "expression(")

# The safety overlay lives in a shadow root, but its host element is still
# addressable from the page stylesheet. Deny it explicitly.
GUARD_SELECTOR_TOKENS = ("slopbox-guard", "qb-guard", "data-slopbox")

MAX_DECLARATIONS = 4000
MAX_NESTING = 6

# tinycss2 renamed this helper; support both spellings.
_parse_decls = getattr(tinycss2, "parse_blocks_contents", None) or getattr(
    tinycss2, "parse_declaration_list"
)


@dataclass
class GuardResult:
    ok: bool
    css: str = ""
    errors: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def message(self) -> str:
        return "; ".join(self.errors[:8])


class _Rejected(Exception):
    pass


def _reject(msg):
    raise _Rejected(msg)


def _check_url_value(value):
    v = value.strip().strip("'\"").replace("\n", "").replace("\t", "")
    collapsed = re.sub(r"\s+", "", v).lower()
    if not collapsed:
        return
    if not any(collapsed.startswith(p) for p in ALLOWED_URL_PREFIXES):
        _reject(
            "url() target not allowed: " + repr(v[:40]) + ". Only inline base64 raster "
            "data URIs are permitted - no external requests, no web fonts, no @import."
        )


def _walk_tokens(tokens, depth=0):
    """Recursively validate a token stream (declaration values, preludes, blocks)."""
    if depth > MAX_NESTING:
        _reject("CSS value nesting too deep")
    for tok in tokens or ():
        ttype = tok.type
        if ttype == "url":
            _check_url_value(tok.value)
        elif ttype == "function":
            name = tok.lower_name
            if name in BANNED_FUNCTIONS:
                _reject("function " + name + "() is not allowed")
            if name == "url":
                joined = "".join(
                    t.value
                    for t in tok.arguments
                    if t.type in ("string", "ident", "literal")
                )
                _check_url_value(joined)
            else:
                _walk_tokens(tok.arguments, depth + 1)
        elif ttype in ("() block", "[] block", "{} block"):
            _walk_tokens(tok.content, depth + 1)
        elif ttype == "at-keyword":
            if tok.lower_value not in ALLOWED_AT_RULES:
                _reject("@" + tok.lower_value + " is not allowed here")


def _validate_declarations(content, counter, depth):
    for node in _parse_decls(content, skip_comments=True, skip_whitespace=True):
        if node.type == "error":
            _reject("parse error: " + str(node.message))
        if node.type == "declaration":
            counter["declarations"] += 1
            if counter["declarations"] > MAX_DECLARATIONS:
                _reject("too many declarations (limit %d)" % MAX_DECLARATIONS)
            if node.lower_name in BANNED_PROPERTIES:
                _reject("property " + repr(node.name) + " is not allowed")
            _walk_tokens(node.value, depth)
        elif node.type == "qualified-rule":
            # CSS nesting inside a rule body.
            _validate_rule(node, counter, depth + 1)
        elif node.type == "at-rule":
            _validate_at_rule(node, counter, depth + 1)


def _validate_rule(rule, counter, depth):
    if depth > MAX_NESTING:
        _reject("rule nesting too deep")
    sel = tinycss2.serialize(rule.prelude).lower()
    for banned in GUARD_SELECTOR_TOKENS:
        if banned in sel:
            _reject("selector may not target the safety overlay (" + banned + ")")
    _walk_tokens(rule.prelude, depth)
    counter["rules"] += 1
    _validate_declarations(rule.content, counter, depth)


def _validate_at_rule(rule, counter, depth):
    if depth > MAX_NESTING:
        _reject("at-rule nesting too deep")
    name = rule.lower_at_keyword
    if name not in ALLOWED_AT_RULES:
        _reject(
            "@" + name + " is not allowed. Allowed at-rules: @media, @supports, "
            "@keyframes, @layer, @container"
        )
    _walk_tokens(rule.prelude, depth)
    if rule.content is None:
        _reject("@" + name + " must have a block")

    nodes = tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True)
    if name.endswith("keyframes"):
        # Body is a list of percentage/from/to blocks of plain declarations.
        for node in nodes:
            if node.type == "error":
                _reject("parse error in @" + name + ": " + str(node.message))
            elif node.type == "qualified-rule":
                _walk_tokens(node.prelude, depth)
                _validate_declarations(node.content, counter, depth + 1)
            elif node.type == "at-rule":
                _validate_at_rule(node, counter, depth + 1)
        return

    for node in nodes:
        if node.type == "error":
            _reject("parse error in @" + name + ": " + str(node.message))
        elif node.type == "qualified-rule":
            _validate_rule(node, counter, depth + 1)
        elif node.type == "at-rule":
            _validate_at_rule(node, counter, depth + 1)
        elif node.type == "declaration":
            counter["declarations"] += 1
            if node.lower_name in BANNED_PROPERTIES:
                _reject("property " + repr(node.name) + " is not allowed")
            _walk_tokens(node.value, depth)


def sanitize(css):
    """Validate untrusted CSS. On success returns re-serialised, safe CSS."""
    if css is None:
        return GuardResult(False, errors=["no CSS provided"])

    raw = css.replace("\r\n", "\n").replace("\x00", "")
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_CSS_BYTES:
        return GuardResult(
            False,
            errors=["CSS too large: %d bytes (limit %d)" % (len(encoded), MAX_CSS_BYTES)],
        )

    lowered = raw.lower()
    for bad in BANNED_SUBSTRINGS:
        if bad in lowered:
            return GuardResult(False, errors=["forbidden text in CSS: " + repr(bad)])

    rules = tinycss2.parse_stylesheet(raw, skip_comments=True, skip_whitespace=True)
    counter = {"rules": 0, "declarations": 0}
    try:
        for rule in rules:
            if rule.type == "error":
                _reject("parse error: " + str(rule.message))
            elif rule.type == "qualified-rule":
                _validate_rule(rule, counter, 0)
            elif rule.type == "at-rule":
                _validate_at_rule(rule, counter, 0)
            elif rule.type == "declaration":
                _reject("top-level declaration outside a rule")
    except _Rejected as exc:
        return GuardResult(False, errors=[str(exc)])

    if counter["rules"] == 0 and counter["declarations"] == 0:
        return GuardResult(False, errors=["CSS contains no usable rules"])

    clean = tinycss2.serialize(rules).strip()
    if not clean:
        return GuardResult(False, errors=["CSS serialised to nothing"])

    clean_lower = clean.lower()
    for bad in BANNED_SUBSTRINGS:
        if bad in clean_lower:
            return GuardResult(
                False, errors=["sanitised output still contained forbidden text"]
            )

    return GuardResult(True, css=clean + "\n", stats=counter)
