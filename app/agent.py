"""The slopbox agent.

Runs Qwen (Hetzner Inference API, OpenAI-compatible) in a tool loop whose only
capability is "propose a stylesheet". There is no shell tool, no filesystem
tool, no fetch tool - the tool table below is the entire attack surface, and
every write goes through the sanitiser plus the browser validator.

Context is bounded: images are dropped from history except the most recent, and
the transcript is mechanically compacted once it exceeds a character budget.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

from openai import AsyncOpenAI

from . import bus, resources, store
from .config import (
    ENABLE_ASSETS,
    ENABLE_FONTS,
    ENABLE_HTML,
    ENABLE_SKETCH,
    MAX_DECOR_BYTES,
    MAX_SKETCH_BYTES,
    MAX_CONTEXT_CHARS,
    MAX_CSS_BYTES,
    MAX_STEPS,
    MAX_TOKENS,
    REASONING_EFFORT,
    QWEN_API_KEY,
    QWEN_BASE_URL,
    QWEN_MODEL,
    self_url,
)
from .css_guard import sanitize
from .html_guard import sanitize_decor
from .validator import POOL

_client = None


def get_client():
    """Built on first use, never at import.

    openai 3.x raises OpenAIError when no key is present, so constructing this at
    module level made importing this module require credentials - which broke the
    credential-free unit tests. Nothing here should need a secret just to be
    imported and inspected.
    """
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL, timeout=180.0)
    return _client

SYSTEM_PROMPT = """You are slopbox, the resident stylist of a website whose only
job is to restyle itself on request. Visitors type a wish into a text box; you
rewrite the site's stylesheet to grant it.

THE PAGE (fixed HTML you cannot change - style it, do not expect to alter it):
  #qb-root            outer wrapper
    #qb-header        with #qb-title ("slopbox") and #qb-tagline
    #qb-main          with #chat (message log), #prompt-form
                      containing #prompt-input and #prompt-submit
    #qb-footer        with #qb-links and its <a> elements
  Chat messages are  #chat .msg  with .msg.user / .msg.assistant / .msg.system,
                     each containing .msg-role and .msg-text.
  .msg.shot          holds img.msg-shot - a screenshot of your own candidate,
                     shown to the visitor. Keep it visible and sensibly sized.
  .msg.thinking      is a <details>: <summary> holds .msg-role and .msg-peek
                     (a one-line preview), and .msg-text holds the full text,
                     shown only when open. Keep the summary clickable.
  <body> also carries class "qb-body". Use standard element selectors freely.

YOUR ONLY TOOLS write a candidate stylesheet, look at it, and publish it.

HARD RULES, enforced by machine - violating them wastes your turns:
  * CSS only. No @import, no @font-face, no url() except inline base64 raster
    data URIs, no external requests of any kind. Web fonts are unavailable, so
    use font-family stacks of generic and common system fonts.
  * Allowed at-rules: @media, @supports, @keyframes, @layer, @container.
  * #prompt-input must stay visible, at least 80x20px, unobstructed at its
    centre point, and readable. A tall design is fine - the visitor can scroll
    down to the box - but it must never need sideways scrolling to be reached.
    Readability is measured from the pixels actually rendered behind the text,
    so gradients, patterns and images are all fair game; only genuinely
    invisible text fails. Checked at 1280x800 and 390x844, at 0s, 0.6s, 1.8s and
    3.2s after load, so an animated input must stay reachable throughout.
  * The chat log must stay readable: #chat visible, and .msg / .msg-text with
    real contrast and enough height to read. It is how the visitor watches you
    work, so style it, do not erase it.
  * Never target the safety overlay (anything named *slopbox-guard*).

The screenshots show the chat log filled with sample messages so you can judge
its readability; the live page shows real ones.

DON'T GET STUCK ON ONE DETAIL. You have plenty of steps, so take the time a
good design needs - iterate, look, and refine. The one thing to avoid is
sinking attempt after attempt into a single stubborn detail:
  * Two or three tries on the same detail is plenty. If it still will not come
    together, drop that detail and move on with the rest.
  * If the thing fighting you is something you invented rather than something
    the visitor asked for, just remove it. Nobody is waiting for it.
  * Look before you rewrite: a screenshot after each change tells you what
    actually happened, and rewriting blind tells you nothing.

WORKFLOW: write_css -> screenshot -> fix what looks wrong -> publish -> finish.
Write a COMPLETE stylesheet every time; it replaces the previous one entirely.
Keep it compact - a few thousand characters is plenty for a striking look, and an
over-long one gets truncated mid-call and thrown away.
Be bold and commit to the visitor's aesthetic - this site is meant to be fun.
Keep your visible commentary to one or two short sentences per turn; the
visitor sees it live. When publish succeeds, call finish immediately."""

# Roughly 3.5 characters per token, with headroom for reasoning and the JSON
# envelope. Advertising the 100KB sanitiser cap instead invites a stylesheet
# that cannot fit in one response and gets cut off mid-string - which used to
# poison the whole run.
SAFE_CSS_CHARS = max(4000, int(MAX_TOKENS * 2.2))

_STRUCTURE = ""
if ENABLE_HTML:
    _STRUCTURE += (
        "DECORATIVE MARKUP: #qb-decor sits at the top of #qb-root and is yours to "
        "fill with write_decor. Inert structure only - plain tags and inline SVG "
        "(shapes, gradients, filters), with class attributes for your CSS to target. "
        "No script, links, forms, images, iframes or style attributes, and no ids or "
        "classes starting qb-, gal-, msg, slopbox, prompt- or chat. Inline SVG is the "
        "strongest thing you have here: real shapes, blobs, arcs and noise filters "
        "that CSS cannot draw. It defaults to pointer-events:none. "
    )
if ENABLE_SKETCH:
    _STRUCTURE += (
        "SKETCH FRAME: write_sketch fills #qb-sketch, a sandboxed iframe where "
        "<script> and <canvas> DO work. It is a separate little document with no "
        "access to this page and no network, so use it for motion and generative "
        "decoration, never for anything the page depends on. It defaults to a "
        "frame is 900x420 by default. Do NOT guess a canvas size: the frame gets a "
        "small helper, so call SLOP.fit(canvas) and it is sized to the frame for you "
        "and re-fitted whenever the frame changes - guessed sizes are what gets "
        "clipped. SLOP.width and SLOP.height give the current size. Choose where it "
        "sits with placement: top (default), above-chat, beside-chat (roomiest, "
        "sits next to the transcript on wide screens and stays put as it scrolls) or "
        "background (fixed behind everything). For anything a visitor plays with, "
        "pass interactive:true - do not try to arrange it in CSS. The frame then "
        "captures clicks and keys, shows a click-to-play badge, releases on Escape, "
        "and stops arrow keys scrolling the page. "
    )
if _STRUCTURE:
    SYSTEM_PROMPT = SYSTEM_PROMPT + chr(10) + chr(10) + _STRUCTURE

_RESOURCES = resources.prompt_section(ENABLE_FONTS, ENABLE_ASSETS)
if _RESOURCES:
    SYSTEM_PROMPT = SYSTEM_PROMPT + """

""" + _RESOURCES

SYSTEM_PROMPT = SYSTEM_PROMPT + """

You have %d steps in this turn, no more.""" % MAX_STEPS

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_design",
            "description": (
                "Read everything that is currently live: the stylesheet, the "
                "decorative markup and the sketch frame. Read this before changing "
                "direction - markup and sketch carry over from the previous design "
                "unless you explicitly clear them."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_css",
            "description": (
                "Replace the candidate stylesheet with a complete new one. Not live "
                "until you publish. Returns sanitiser errors if the CSS is rejected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "css": {
                        "type": "string",
                        "description": (
                            "The complete stylesheet. Aim for 3000-10000 characters; "
                            "past about %d it will be cut off mid-call and wasted. "
                            "Hard limit %d bytes." % (SAFE_CSS_CHARS, MAX_CSS_BYTES)
                        ),
                    }
                },
                "required": ["css"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": (
                "Render the candidate stylesheet in a real browser and look at it. "
                "Returns a desktop and a mobile screenshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "viewport": {
                        "type": "string",
                        "enum": ["desktop", "mobile", "both"],
                        "description": "Which viewport(s) to capture. Default both.",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish",
            "description": (
                "Validate the candidate in a real browser and, if the prompt input is "
                "still usable, make it the live stylesheet. Returns the failures if not."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "End your turn. Call this once the new look is live, or if you give up.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One short sentence for the visitor.",
                    }
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
]

DECOR_TOOL = {
    "type": "function",
    "function": {
        "name": "write_decor",
        "description": (
            "Replace the decorative markup inside #qb-decor. Inert HTML and inline "
            "SVG only - no script, links, forms, images or style attributes, and no "
            "ids or classes in the page's own namespace. Pass an empty string to "
            "remove it. Not live until you publish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": "The complete markup for #qb-decor. Max %d bytes."
                    % MAX_DECOR_BYTES,
                }
            },
            "required": ["html"],
            "additionalProperties": False,
        },
    },
}

SKETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "write_sketch",
        "description": (
            "Replace the contents of the sandboxed sketch frame (#qb-sketch): a tiny "
            "self-contained document where script IS allowed, for canvas or animated "
            "decoration. It runs with no access to this page and no network, so it "
            "cannot read or change anything outside its own box. Pass an empty string "
            "to remove it. Not live until you publish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": (
                        "A complete little document body - markup, <style> and "
                        "<script> are all fine here. Max %d bytes." % MAX_SKETCH_BYTES
                    ),
                },
                "interactive": {
                    "type": "boolean",
                    "description": (
                        "True if visitors should be able to click or type into it, as a "
                        "game needs. Frames are click-through by default so they cannot "
                        "steal clicks meant for the prompt box; set this rather than "
                        "trying to do it in CSS. Interactive frames get a click-to-play "
                        "badge and release on Escape."
                    ),
                },
                "placement": {
                    "type": "string",
                    "enum": ["top", "above-chat", "beside-chat", "background"],
                    "description": (
                        "Where the frame sits. top: above the header, the default. "
                        "above-chat: in the main column, directly above the transcript. "
                        "beside-chat: alongside the transcript on wide screens, stacking "
                        "on narrow ones - the roomiest option for a game. background: "
                        "fixed behind the whole page. Your CSS can size #qb-sketch "
                        "however you like in any of them."
                    ),
                },
            },
            "required": ["html"],
            "additionalProperties": False,
        },
    },
}

if ENABLE_HTML:
    TOOLS.append(DECOR_TOOL)
if ENABLE_SKETCH:
    TOOLS.append(SKETCH_TOOL)

IMAGE_PLACEHOLDER = "[earlier screenshot dropped to save context]"
STALE_IMAGE = (
    "[screenshot removed: it showed an EARLIER draft and no longer reflects the "
    "current candidate. Call screenshot again to see what you have now.]"
)


def step_nudge(step, max_steps, writes, published, last_nudge_writes):
    """A reminder only when the budget is genuinely about to run out.

    This used to nag after three revisions and again as the budget shrank,
    which the model read as "you are taking too long" and which pushed it to
    publish thin work. The concern was only ever about fixating on one detail,
    and that belongs in the system prompt as guidance, not as a running
    countdown. What is left here just stops finished work being thrown away.
    """
    remaining = max_steps - step
    if published:
        return None, last_nudge_writes
    if remaining <= 1:
        return (
            "LAST STEP. Publish what you have now - an unpublished design reaches "
            "nobody, and you can always refine it next time.",
            last_nudge_writes,
        )
    if remaining <= 3 and writes > 0:
        return (
            "%d steps left and nothing is live yet. Publish what you have so the "
            "work is not lost; there is still room to improve it afterwards."
            % remaining,
            last_nudge_writes,
        )
    return None, last_nudge_writes

    if remaining <= 1:
        return (
            "LAST STEP. Call publish right now with whatever you have. If you do "
            "not, nothing reaches the visitor at all.",
            last_nudge_writes,
        )
    if remaining <= 3:
        return (
            "Only %d steps left and nothing is live yet. Stop refining and call "
            "publish now - imperfect and live beats perfect and unpublished."
            % remaining,
            last_nudge_writes,
        )
    if writes >= 3 and writes != last_nudge_writes:
        return (
            "You have rewritten the stylesheet %d times without publishing. If some "
            "detail will not come together, delete that detail and publish what "
            "works - especially if it is something you added yourself rather than "
            "something the visitor asked for." % writes,
            writes,
        )
    return None, last_nudge_writes


def prepare_tool_calls(tool_calls, truncated=False):
    """Split streamed tool calls into what may enter the transcript, and what to
    dispatch.

    Arguments that do not parse must NEVER be stored: the transcript is resent
    on every subsequent request, and the server parses tool-call arguments when
    applying its chat template. One malformed string therefore 400s the whole
    rest of the run, not just the step that produced it. A cut-off `write_css`
    is the usual cause - the model runs out of output budget mid-CSS, leaving
    something like '{"css": "body{color:red' behind.
    """
    outgoing, prepared = [], []
    for tc in tool_calls:
        raw = tc.get("arguments") or "{}"
        args, error = None, None
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                args = loaded
            else:
                error = "Tool arguments must be a JSON object. Call the tool again."
        except json.JSONDecodeError as exc:
            if truncated:
                error = (
                    "Your tool call was CUT OFF before it finished - you exceeded the "
                    "output budget, almost certainly with an over-long stylesheet "
                    "(%d characters were sent). Nothing was applied. Write a much more "
                    "compact stylesheet and call write_css again." % len(raw)
                )
            else:
                error = (
                    "Your tool arguments were not valid JSON (%s). Nothing was applied. "
                    "Call the tool again with a complete, valid argument object."
                    % str(exc)[:80]
                )

        outgoing.append(
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    # Substituted, so the transcript always stays parseable.
                    "arguments": raw if args is not None else "{}",
                },
            }
        )
        prepared.append(
            {"id": tc["id"], "name": tc["name"], "args": args or {}, "error": error}
        )
    return outgoing, prepared


def _b64_image(data):
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def _publish_shot(report, label):
    """Send the desktop render to the chat log, so the visitor sees what the
    model sees. Desktop only - the mobile shot would double the payload for
    little gain, and these go out to every connected browser."""
    shot = report.shots.get("desktop")
    if shot:
        bus.publish({"type": "image", "label": label, "src": _b64_image(shot)})


def _approx_chars(messages):
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for part in c:
                if part.get("type") == "text":
                    total += len(part.get("text", ""))
                else:
                    # An image costs far more than its JSON length suggests.
                    total += 3000
        for tc in m.get("tool_calls") or ():
            total += len(json.dumps(tc))
    return total


class Run:
    """One agent run, servicing one or more visitor prompts."""

    def __init__(self, prompt, prior=None):
        self.prompt = prompt
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Each run starts with a clean context, so without this a follow-up like
        # "try again" would arrive with nothing to refer to.
        if prior:
            self.messages.append({"role": "user", "content": prior})
        self.messages.append({"role": "user", "content": "Visitor request: " + prompt})

        self.published = False
        self.version = None
        self.steering: list = []
        self.compactions = 0
        self.last_failure: list = []
        self.exhausted = False
        self.last_finish_reason = None
        self.writes = 0
        self._last_nudge_writes = None
        # Cleared once the loop can no longer consume steering, so late prompts
        # are queued as their own job instead of vanishing into a finished run.
        self.accepting_steering = True

    def outcome(self):
        """A short note for the next run, so follow-up prompts make sense."""
        asked = self.prompt[:200]
        if self.published:
            what = "you published it successfully as version %s" % self.version
        elif self.last_failure:
            what = ("your stylesheet was REJECTED by validation and never went live. "
                    "Reasons: " + "; ".join(self.last_failure[:3])[:300])
        elif self.exhausted:
            what = "you ran out of steps and published nothing, so the site is unchanged"
        else:
            what = "the attempt ended without publishing anything"
        return (
            "[Context from the attempt immediately before this one, in case the visitor "
            "refers to it: they asked for %r and %s. None of that stylesheet is in your "
            "context any more, so write a COMPLETE stylesheet from scratch.]" % (asked, what)
        )

    # -- context hygiene -------------------------------------------------
    def _strip_old_images(self):
        """Keep only the newest screenshot; older ones become a placeholder."""
        seen_latest = False
        for m in reversed(self.messages):
            if m.get("role") != "user" or not isinstance(m.get("content"), list):
                continue
            has_image = any(p.get("type") == "image_url" for p in m["content"])
            if not has_image:
                continue
            if not seen_latest:
                seen_latest = True
                continue
            m["content"] = [{"type": "text", "text": IMAGE_PLACEHOLDER}]

    def _invalidate_screenshots(self):
        """Drop screenshots once the stylesheet they showed has been replaced.

        Without this, a render taken early keeps sitting in context under its
        original present-tense caption while later rewrites pile up. The model
        then reasons about a stale picture, concludes its change "still is not
        showing up", rewrites again, and loops until the step budget is gone.
        """
        for m in self.messages:
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                if any(p.get("type") == "image_url" for p in m["content"]):
                    m["content"] = [{"type": "text", "text": STALE_IMAGE}]

    def _compact(self):
        """Collapse the middle of the transcript into a short note."""
        if len(self.messages) <= 4:
            return
        head = self.messages[:2]
        tail = self.messages[-3:]
        # A tool result must not be orphaned from its tool_call.
        while tail and tail[0].get("role") == "tool":
            tail = tail[1:]
        dropped = len(self.messages) - len(head) - len(tail)
        if dropped <= 0:
            return
        note = {
            "role": "user",
            "content": (
                "[context compacted: %d earlier steps removed. You have already been "
                "iterating on this stylesheet. Call get_current_design, or write_css with a "
                "complete stylesheet and publish it - do not assume earlier CSS survived "
                "in your memory.]" % dropped
            ),
        }
        self.messages = head + [note] + tail
        self.compactions += 1
        bus.publish({"type": "status", "text": "compacted context"})

    def _budget(self):
        self._strip_old_images()
        guard = 0
        while _approx_chars(self.messages) > MAX_CONTEXT_CHARS and guard < 5:
            self._compact()
            guard += 1

    # -- tools -----------------------------------------------------------
    async def _tool_get_current_design(self, args):
        def clip(text, limit):
            return text if len(text) <= limit else text[:limit] + " ...(truncated)"

        parts = ["Current live stylesheet:", "", clip(store.current_css(), 16000)]

        if ENABLE_HTML:
            decor = store.current_part("decor").strip()
            parts += ["", "Decorative markup in #qb-decor:"]
            parts += (
                ["", clip(decor, 4000), "",
                 "To remove it entirely, call write_decor with an empty string."]
                if decor else ["", "(none)"]
            )
        if ENABLE_SKETCH:
            sketch = store.current_part("sketch").strip()
            parts += ["", "Sketch frame #qb-sketch:"]
            parts += (
                ["", clip(sketch, 4000), "",
                 "To remove it entirely, call write_sketch with an empty string. "
                 "Rewriting only the stylesheet does NOT remove it."]
                if sketch else ["", "(none)"]
            )
        return chr(10).join(parts), None

    async def _tool_write_css(self, args):
        css = args.get("css")
        if not isinstance(css, str):
            return "Rejected: 'css' must be a string containing a complete stylesheet.", None
        result = sanitize(css)
        if not result.ok:
            return "REJECTED by the sanitiser: " + result.message + "\nFix and call write_css again.", None
        store.set_candidate(result.css)
        self.writes += 1
        self._invalidate_screenshots()
        return (
            "Candidate accepted as draft #%d (%d rules, %d declarations, %d bytes). Not "
            "live yet, and any screenshot you took before this is now out of date. "
            "Call screenshot to see THIS draft, then publish."
            % (
                self.writes,
                result.stats.get("rules", 0),
                result.stats.get("declarations", 0),
                len(result.css.encode("utf-8")),
            )
        ), None

    async def _tool_write_decor(self, args):
        result = sanitize_decor(args.get("html"))
        if not result.ok:
            return "REJECTED: " + result.message + ". Fix and call write_decor again.", None
        store.set_candidate_part("decor", result.html)
        self.writes += 1
        self._invalidate_screenshots()
        if not result.html.strip():
            return "Decorative markup removed. Screenshot to see the result.", None
        return (
            "Decor accepted (%d bytes). %sNot live yet, and any earlier screenshot is "
            "out of date - screenshot to see it."
            % (len(result.html), (result.note + ". ") if result.note else "")
        ), None

    async def _tool_write_sketch(self, args):
        html = args.get("html")
        if not isinstance(html, str):
            return "Rejected: 'html' must be a string.", None
        if len(html.encode("utf-8")) > MAX_SKETCH_BYTES:
            return (
                "Rejected: sketch is %d bytes, limit %d. Make it smaller."
                % (len(html.encode("utf-8")), MAX_SKETCH_BYTES)
            ), None
        placement = str(args.get("placement") or "").strip()
        if placement not in ("top", "above-chat", "beside-chat", "background"):
            placement = "top"
        interactive = bool(args.get("interactive"))
        store.set_candidate_part("sketch", html)
        store.set_candidate_part(
            "sketchplace",
            json.dumps({"placement": placement, "interactive": interactive}),
        )
        self.writes += 1
        self._invalidate_screenshots()
        if not html.strip():
            return "Sketch frame removed. Screenshot to see the result.", None
        return (
            "Sketch accepted (%d bytes), placed %s, %s. It runs sandboxed with no page "
            "access and no network. Not live yet - screenshot to see it, then publish."
            % (len(html), placement,
               "clickable" if interactive else "click-through (decorative)")
        ), None

    async def _tool_screenshot(self, args):
        if not store.has_candidate():
            return "No candidate stylesheet yet - call write_css first.", None
        which = (args.get("viewport") or "both").lower()
        from .config import VIEWPORTS

        vps = VIEWPORTS if which == "both" else [v for v in VIEWPORTS if v[0] == which] or VIEWPORTS
        report = await POOL.inspect(self_url() + "/preview", screenshot_only=True, viewports=vps)
        if not report.shots:
            return "Screenshot failed: " + ("; ".join(report.problems) or "unknown error"), None
        _publish_shot(report, "candidate")
        parts = [
            {
                "type": "text",
                "text": "Render of draft #%d (%s), as it looks right now. Judge it "
                "and fix anything broken." % (self.writes, ", ".join(report.shots.keys())),
            }
        ]
        for label, data in report.shots.items():
            parts.append({"type": "text", "text": label + " view:"})
            parts.append({"type": "image_url", "image_url": {"url": _b64_image(data)}})
        return "Screenshots attached below.", parts

    async def _tool_publish(self, args):
        if not store.has_candidate():
            return "No candidate stylesheet to publish - call write_css first.", None
        report = await POOL.inspect(self_url() + "/preview")
        if not report.ok:
            self.last_failure = list(report.problems)
            _publish_shot(report, "rejected")
            bus.publish({"type": "status", "text": "validation failed, not published"})
            parts = None
            if report.shots:
                parts = [
                    {
                        "type": "text",
                        "text": "The candidate FAILED validation:\n- "
                        + "\n- ".join(report.problems[:8])
                        + "\n\nHere is what it looks like. Fix the stylesheet and publish again.",
                    }
                ]
                for label, data in report.shots.items():
                    parts.append({"type": "text", "text": label + " view:"})
                    parts.append({"type": "image_url", "image_url": {"url": _b64_image(data)}})
            return (
                "NOT PUBLISHED - validation failed:\n- " + "\n- ".join(report.problems[:8]),
                parts,
            )

        version = store.publish_candidate(self.prompt)
        # The validation render IS the final look, so it doubles as the
        # gallery thumbnail - no extra browser work to produce one.
        shot = report.shots.get("desktop")
        if shot:
            store.save_shot(version, shot)
        self.published = True
        self.version = version
        bus.publish({"type": "reload", "version": version})
        bus.publish({"type": "status", "text": "published v%d" % version})
        return (
            "PUBLISHED as version %d. It is live for everyone now. %s Call finish."
            % (version, report.summary())
        ), None

    async def _tool_finish(self, args):
        return "__FINISH__" + (args.get("summary") or ""), None

    async def _dispatch(self, name, args):
        handler = {
            "get_current_design": self._tool_get_current_design,
            "write_css": self._tool_write_css,
            "screenshot": self._tool_screenshot,
            "publish": self._tool_publish,
            "finish": self._tool_finish,
            "write_decor": self._tool_write_decor,
            "write_sketch": self._tool_write_sketch,
        }.get(name)
        if handler is None:
            return "Unknown tool: " + str(name), None
        try:
            return await handler(args)
        except Exception as exc:
            return "Tool error (%s): %s" % (type(exc).__name__, str(exc)[:300]), None

    def add_steering(self, prompt):
        self.steering.append(prompt)

    # -- main loop -------------------------------------------------------
    async def execute(self):
        bus.publish({"type": "status", "text": "thinking", "busy": True})
        try:
            for step in range(MAX_STEPS):
                # Steering is consumed at the top of an iteration, so during the
                # last one there is no next iteration to consume it.
                self.accepting_steering = step < MAX_STEPS - 1
                if self.steering:
                    extra = self.steering[:]
                    self.steering.clear()
                    for s in extra:
                        self.messages.append(
                            {"role": "user", "content": "Additional visitor request: " + s}
                        )
                        self.prompt = (self.prompt + " + " + s)[:400]

                nudge, marker = step_nudge(
                    step, MAX_STEPS, self.writes, self.published, self._last_nudge_writes
                )
                if nudge:
                    self._last_nudge_writes = marker
                    self.messages.append({"role": "user", "content": nudge})

                self._budget()
                text, tool_calls = await self._stream_turn()

                if not tool_calls:
                    if not text.strip():
                        self.messages.append(
                            {
                                "role": "user",
                                "content": "You said nothing and called no tool. Use write_css "
                                "then publish, or call finish.",
                            }
                        )
                        continue
                    # Model chose to answer in prose; treat as end of turn.
                    return text.strip()

                outgoing, prepared = prepare_tool_calls(
                    tool_calls, truncated=self.last_finish_reason == "length"
                )
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": outgoing,
                    }
                )

                finished = None
                for call in prepared:
                    if call["error"]:
                        bus.publish({"type": "status", "text": "retrying a bad tool call"})
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": call["error"],
                            }
                        )
                        continue

                    bus.publish({"type": "tool", "name": call["name"]})
                    result, extra_parts = await self._dispatch(call["name"], call["args"])

                    if isinstance(result, str) and result.startswith("__FINISH__"):
                        finished = result[len("__FINISH__") :]
                        self.messages.append(
                            {"role": "tool", "tool_call_id": call["id"], "content": "ok"}
                        )
                        continue

                    self.messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": result}
                    )
                    if extra_parts:
                        self.messages.append({"role": "user", "content": extra_parts})

                if finished is not None:
                    return finished

            # Out of steps with an unpublished candidate: publishing it is strictly
            # better than discarding it. It already passed the sanitiser, and
            # publish runs the full browser validation anyway, so this cannot
            # ship a broken page.
            if not self.published and store.has_candidate():
                bus.publish({"type": "status", "text": "out of steps - publishing best draft"})
                await self._tool_publish({})
                if self.published:
                    self.exhausted = True
                    return (
                        "I ran out of steps fiddling, so I published the closest draft I had."
                    )

            self.exhausted = True
            return "I ran out of steps on that one." + (
                "" if self.published else " The site is unchanged - ask again to retry."
            )
        finally:
            self.accepting_steering = False
            store.clear_candidate()
            bus.publish({"type": "status", "text": "idle", "busy": False})

    async def _stream_turn(self):
        """One streamed completion. Returns (text, tool_calls)."""
        stream = await get_client().chat.completions.create(
            model=QWEN_MODEL,
            messages=self.messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.8,
            max_tokens=MAX_TOKENS,
            reasoning_effort=REASONING_EFFORT,
            stream=True,
        )

        text_parts = []
        calls = {}
        order = []
        msg_id = "m%d" % int(time.time() * 1000)
        opened = False
        think_opened = False
        finish_reason = None

        async for chunk in stream:
            if not chunk.choices:
                continue
            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            # This model streams its deliberation separately from its answer,
            # and often says nothing in `content` at all when calling a tool -
            # so the thinking is what the visitor actually watches happen.
            think = getattr(delta, "reasoning", None) or getattr(
                delta, "reasoning_content", None
            )
            if think:
                if not think_opened:
                    bus.publish(
                        {"type": "message", "role": "thinking", "mid": msg_id + ":r", "text": ""}
                    )
                    think_opened = True
                bus.publish({"type": "delta", "mid": msg_id + ":r", "text": think})

            piece = getattr(delta, "content", None)
            if piece:
                if not opened:
                    bus.publish({"type": "message", "role": "assistant", "mid": msg_id, "text": ""})
                    opened = True
                text_parts.append(piece)
                bus.publish({"type": "delta", "mid": msg_id, "text": piece})

            for tc in getattr(delta, "tool_calls", None) or ():
                idx = tc.index if tc.index is not None else 0
                slot = calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                if idx not in order:
                    order.append(idx)
                if tc.id:
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments

        tool_calls = []
        for i in order:
            slot = calls[i]
            if not slot["name"]:
                continue
            slot["id"] = slot["id"] or ("call_%s_%d" % (msg_id, i))
            tool_calls.append(slot)

        self.last_finish_reason = finish_reason

        if finish_reason == "length" and not tool_calls:
            # It thought itself out of budget. Nudge it towards acting.
            self.messages.append(
                {
                    "role": "user",
                    "content": "You ran out of output budget before calling a tool. Think "
                    "less, act now: call write_css with a complete stylesheet.",
                }
            )

        return "".join(text_parts), tool_calls
