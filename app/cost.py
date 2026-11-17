"""What a design cost to build, and the price tag that shows it.

Every Responses API reply reports its own token usage. A run adds those up, and
the total is priced with the per-million rates from config. The API does not
report prices, so they have to be configured; without them the tag shows the
token count instead of a dollar figure.

The cost is computed when it is recorded and stored with the design, so a later
price change does not rewrite what earlier builds cost.
"""
from __future__ import annotations

from html import escape

from .config import PRICE_CACHED_INPUT_PER_M, PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M

FIELDS = ("steps", "input", "cached", "output", "reasoning")


def empty(model):
    return {"model": model, "steps": 0, "input": 0, "cached": 0, "output": 0,
            "reasoning": 0, "cost": None}


def _num(obj, *path):
    for name in path:
        obj = getattr(obj, name, None)
        if obj is None:
            return 0
    try:
        return int(obj)
    except (TypeError, ValueError):
        return 0


def add(total, usage):
    """Fold one response's usage object into a running total, in place."""
    if usage is None:
        return total
    total["steps"] += 1
    total["input"] += _num(usage, "input_tokens")
    total["cached"] += _num(usage, "input_tokens_details", "cached_tokens")
    total["output"] += _num(usage, "output_tokens")
    total["reasoning"] += _num(usage, "output_tokens_details", "reasoning_tokens")
    return total


def priced(total):
    """A copy of the total with its cost filled in, or None if unpriced."""
    out = dict(total)
    if not (PRICE_INPUT_PER_M or PRICE_OUTPUT_PER_M):
        out["cost"] = None
        return out
    cached = min(out["cached"], out["input"])
    out["cost"] = (
        (out["input"] - cached) * PRICE_INPUT_PER_M
        + cached * PRICE_CACHED_INPUT_PER_M
        + out["output"] * PRICE_OUTPUT_PER_M
    ) / 1_000_000
    return out


def _money(value):
    if value >= 0.01:
        return "$%.2f" % value
    if value <= 0:
        return "$0"
    return "$%.3f" % value


def _short_tokens(n):
    if n >= 1_000_000:
        return "%.1fM tok" % (n / 1_000_000)
    if n >= 1000:
        return "%dk tok" % round(n / 1000)
    return "%d tok" % n


def label(usage):
    """The big text inside the tag."""
    if usage.get("cost") is not None:
        return _money(usage["cost"])
    return _short_tokens(usage.get("input", 0) + usage.get("output", 0))


def rows(usage):
    """The breakdown shown on hover, as (label, value, is_sub) tuples."""
    out = [
        ("steps", "{:,}".format(usage.get("steps", 0)), False),
        ("input", "{:,}".format(usage.get("input", 0)), False),
        ("cached", "{:,}".format(usage.get("cached", 0)), True),
        ("output", "{:,}".format(usage.get("output", 0)), False),
        ("reasoning", "{:,}".format(usage.get("reasoning", 0)), True),
        ("total", "{:,} tokens".format(usage.get("input", 0) + usage.get("output", 0)), False),
    ]
    cost = usage.get("cost")
    out.append(("cost", _money(cost) if cost is not None else "prices not configured", False))
    return out


def _valid(usage):
    return isinstance(usage, dict) and all(isinstance(usage.get(f, 0), int) for f in FIELDS)


def page_tag(usage):
    """The tag on the site itself. Fixed markup, styled by base.css and then by
    whatever the agent publishes."""
    if not _valid(usage):
        return ""
    detail = "".join(
        '<span class="qb-price-row%s"><span class="qb-price-k">%s</span>'
        '<span class="qb-price-v">%s</span></span>'
        % (" qb-price-sub" if sub else "", escape(k), escape(v))
        for k, v, sub in rows(usage)
    )
    text = escape(label(usage))
    return (
        '<div id="qb-price" tabindex="0" aria-label="This design cost %s to build">'
        '<span id="qb-price-amount">%s</span>'
        '<span id="qb-price-detail" role="tooltip">%s</span></div>'
        % (text, text, detail)
    )


def gallery_tag(usage):
    """The fixed-design tag on a gallery card. Spans only: it sits inside a link."""
    if not _valid(usage):
        return ""
    detail = "".join(
        '<span class="gal-price-row%s"><span>%s</span><span>%s</span></span>'
        % (" gal-price-sub" if sub else "", escape(k), escape(v))
        for k, v, sub in rows(usage)
    )
    return (
        '<span class="gal-price"><span class="gal-price-amount">%s</span>'
        '<span class="gal-price-detail">%s</span></span>'
        % (escape(label(usage)), detail)
    )
