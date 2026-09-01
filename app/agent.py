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

from . import bus, store
from .config import (
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
from .validator import POOL

client = AsyncOpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL, timeout=180.0)

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
  * #prompt-input must stay visible, at least 80x20px, on screen without
    scrolling, unobstructed at its centre point, and readable (text contrast
    against its own background at least 1.7:1). This is checked at 1280x800 and
    390x844, at 0s, 0.6s, 1.8s and 3.2s after load - so if you animate the
    input, keep it on screen and hit-testable for the whole animation.
  * The chat log must stay readable: #chat visible, and .msg / .msg-text with
    real contrast and enough height to read. It is how the visitor watches you
    work, so style it, do not erase it.
  * Never target the safety overlay (anything named *slopbox-guard*).

The screenshots show the chat log filled with sample messages so you can judge
its readability; the live page shows real ones.

WORKFLOW: write_css -> screenshot -> fix what looks wrong -> publish -> finish.
Write a COMPLETE stylesheet every time; it replaces the previous one entirely.
Be bold and commit to the visitor's aesthetic - this site is meant to be fun.
Keep your visible commentary to one or two short sentences per turn; the
visitor sees it live. When publish succeeds, call finish immediately."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_css",
            "description": "Read the stylesheet that is currently live on the site.",
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
                        "description": "The complete stylesheet. Max %d bytes." % MAX_CSS_BYTES,
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

IMAGE_PLACEHOLDER = "[earlier screenshot dropped to save context]"


def _b64_image(data):
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


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

    def __init__(self, prompt):
        self.prompt = prompt
        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Visitor request: " + prompt},
        ]
        self.published = False
        self.steering: list = []
        self.compactions = 0

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
                "iterating on this stylesheet. Call get_current_css or write_css with a "
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
    async def _tool_get_current_css(self, args):
        css = store.current_css()
        if len(css) > 20000:
            css = css[:20000] + "\n/* ...truncated... */"
        return "Current live stylesheet:\n\n" + css, None

    async def _tool_write_css(self, args):
        css = args.get("css")
        if not isinstance(css, str):
            return "Rejected: 'css' must be a string containing a complete stylesheet.", None
        result = sanitize(css)
        if not result.ok:
            return "REJECTED by the sanitiser: " + result.message + "\nFix and call write_css again.", None
        store.set_candidate(result.css)
        return (
            "Candidate accepted (%d rules, %d declarations, %d bytes). Not live yet. "
            "Call screenshot to see it, then publish."
            % (
                result.stats.get("rules", 0),
                result.stats.get("declarations", 0),
                len(result.css.encode("utf-8")),
            )
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
        parts = [
            {
                "type": "text",
                "text": "Candidate stylesheet rendered ("
                + ", ".join(report.shots.keys())
                + "). Judge it and fix anything broken.",
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
        self.published = True
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
            "get_current_css": self._tool_get_current_css,
            "write_css": self._tool_write_css,
            "screenshot": self._tool_screenshot,
            "publish": self._tool_publish,
            "finish": self._tool_finish,
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
                if self.steering:
                    extra = self.steering[:]
                    self.steering.clear()
                    for s in extra:
                        self.messages.append(
                            {"role": "user", "content": "Additional visitor request: " + s}
                        )
                        self.prompt = (self.prompt + " + " + s)[:400]

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

                self.messages.append(
                    {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"] or "{}",
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )

                finished = None
                for tc in tool_calls:
                    try:
                        args = json.loads(tc["arguments"] or "{}")
                        if not isinstance(args, dict):
                            args = {}
                    except json.JSONDecodeError:
                        args = {}
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": "Your tool arguments were not valid JSON. Retry.",
                            }
                        )
                        continue

                    bus.publish({"type": "tool", "name": tc["name"]})
                    result, extra_parts = await self._dispatch(tc["name"], args)

                    if isinstance(result, str) and result.startswith("__FINISH__"):
                        finished = result[len("__FINISH__") :]
                        self.messages.append(
                            {"role": "tool", "tool_call_id": tc["id"], "content": "ok"}
                        )
                        continue

                    self.messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": result}
                    )
                    if extra_parts:
                        self.messages.append({"role": "user", "content": extra_parts})

                if finished is not None:
                    return finished

            return "I ran out of steps on that one." + (
                "" if self.published else " The site is unchanged."
            )
        finally:
            store.clear_candidate()
            bus.publish({"type": "status", "text": "idle", "busy": False})

    async def _stream_turn(self):
        """One streamed completion. Returns (text, tool_calls)."""
        stream = await client.chat.completions.create(
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
