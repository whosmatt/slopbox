"""Headless-browser validation and screenshotting.

Two jobs:
  1. screenshot() - renders the candidate stylesheet so the multimodal model can
     see what it just wrote and iterate (closed loop).
  2. validate()   - the gate. Custom CSS only goes live if the prompt input is
     still on screen, hit-testable and typeable, at every viewport and at
     several points in time (so an animation cannot park it off screen after
     the check).

Screenshots are returned as bytes and never touch the filesystem.
"""
from __future__ import annotations

import asyncio
import struct
import zlib
from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from .config import SAMPLE_TIMES, VIEWPORTS

# Injected into the page. Pure DOM inspection, no dependencies.
CHECK_JS = r"""
() => {
  const problems = [];
  const el = document.getElementById('prompt-input');
  if (!el) return { problems: ['#prompt-input is missing from the DOM'], info: {} };

  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight;

  const parseColor = (s) => {
    const m = String(s).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const contrast = (a, b) => {
    const la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  };

  // Effective opacity and any hiding ancestor.
  let opacity = 1, node = el, hiddenBy = null, blurred = null;
  while (node && node.nodeType === 1) {
    const s = getComputedStyle(node);
    opacity *= parseFloat(s.opacity === '' ? '1' : s.opacity);
    if (s.display === 'none') hiddenBy = hiddenBy || (node.tagName + (node.id ? '#' + node.id : '') + ' has display:none');
    if (s.visibility === 'hidden' || s.visibility === 'collapse') hiddenBy = hiddenBy || (node.tagName + (node.id ? '#' + node.id : '') + ' has visibility:hidden');
    if (s.contentVisibility === 'hidden') hiddenBy = hiddenBy || (node.tagName + ' has content-visibility:hidden');
    const fl = s.filter || '';
    let fm = fl.match(/blur\(([\d.]+)px\)/);
    if (fm && parseFloat(fm[1]) > 3) blurred = blurred || (node.tagName + ' blur(' + fm[1] + 'px)');
    fm = fl.match(/opacity\(([\d.%]+)\)/);
    if (fm) {
      const v = fm[1].endsWith('%') ? parseFloat(fm[1]) / 100 : parseFloat(fm[1]);
      opacity *= v;
    }
    node = node.parentElement;
  }

  if (hiddenBy) problems.push('the prompt input is not rendered: ' + hiddenBy);
  if (blurred) problems.push('the prompt input is blurred beyond use: ' + blurred);
  if (opacity < 0.35) problems.push('the prompt input is nearly transparent (effective opacity ' + opacity.toFixed(2) + ', need >= 0.35)');

  if (r.width < 80) problems.push('the prompt input is too narrow (' + Math.round(r.width) + 'px, need >= 80px)');
  if (r.height < 20) problems.push('the prompt input is too short (' + Math.round(r.height) + 'px, need >= 20px)');


  // Below the fold is not the same as unreachable: a tall design is fine as long
  // as the visitor can scroll to the box. So try scrolling to it and judge what
  // happens - which still fails anything genuinely out of reach, like a fixed
  // element parked off screen or one at left:-4000px, since neither can be
  // scrolled into view.
  const coverage = (box) => {
    const ix = Math.max(0, Math.min(box.right, vw) - Math.max(box.left, 0));
    const iy = Math.max(0, Math.min(box.bottom, vh) - Math.max(box.top, 0));
    return (ix * iy) / Math.max(1, box.width * box.height);
  };
  let rect = r, visible = coverage(r), scrolled = false;
  if (visible < 0.6) {
    // Vertically only, and deliberately not scrollIntoView: that also scrolls
    // sideways, which would bless an input flung 4000px to the right as
    // "reachable". Needing to scroll down is normal; needing to scroll across
    // to find the prompt box is not.
    const docY = r.top + window.scrollY;
    window.scrollTo(0, Math.max(0, docY - vh / 2 + r.height / 2));
    rect = el.getBoundingClientRect();
    visible = coverage(rect);
    scrolled = true;
  }
  if (visible < 0.6) {
    problems.push('the prompt input is ' + Math.round((1 - visible) * 100) +
      '% off screen and scrolling down does not bring it into view - the page ' +
      'must never need sideways scrolling to reach it');
  }

  const cx = Math.min(vw - 1, Math.max(1, rect.left + rect.width / 2));
  const cy = Math.min(vh - 1, Math.max(1, rect.top + rect.height / 2));

  if (cs.pointerEvents === 'none') problems.push('the prompt input has pointer-events:none so it cannot be clicked');

  // Decoration defaults to pointer-events:none, which would let markup painted
  // straight over the input stay hit-testable - invisible to a human, perfect to
  // a machine. Forcing it back on turns "visually covering" into "hit-testable
  // covering", which the check below already understands, and it still respects
  // paint order, so decoration sitting behind the input is not punished.
  const forced = [];
  document.querySelectorAll('#qb-decor, #qb-decor *, #qb-sketch').forEach((el) => {
    if (getComputedStyle(el).pointerEvents === 'none') {
      el.style.setProperty('pointer-events', 'auto', 'important');
      forced.push(el);
    }
  });

  const hit = document.elementFromPoint(cx, cy);
  forced.forEach((el) => el.style.removeProperty('pointer-events'));
  if (!hit) {
    problems.push('nothing is hit-testable at the centre of the prompt input');
  } else if (hit !== el && !el.contains(hit)) {
    const label = hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : '') +
      (hit.className && typeof hit.className === 'string' ? '.' + hit.className.trim().split(/\s+/).join('.') : '');
    problems.push('another element (' + label + ') covers the prompt input - clicks would not reach it');
  }

  const fontSize = parseFloat(cs.fontSize);
  if (fontSize < 9) problems.push('the prompt input font-size is ' + fontSize + 'px, too small to read (need >= 9px)');

  // Typed text must be distinguishable from the field behind it.
  const fg = parseColor(cs.color);
  let bg = null, bnode = el;
  while (bnode && bnode.nodeType === 1) {
    const c = parseColor(getComputedStyle(bnode).backgroundColor);
    if (c && c.a > 0.2) { bg = c; break; }
    bnode = bnode.parentElement;
  }
  if (!bg) bg = { r: 255, g: 255, b: 255, a: 1 };
  // Only the alpha case is decidable from computed styles. The ratio itself is
  // measured from rendered pixels in Python: reading background-color alone
  // cannot see a gradient, an image or a pattern, and would fall through to
  // some ancestor's colour and reject perfectly readable text.
  if (fg && fg.a < 0.35) {
    problems.push('the prompt input text colour is nearly transparent - typing would be invisible');
  }
  // The chat log is where the visitor watches the agent work, so it is a hard
  // requirement too, not decoration. /preview seeds it with sample messages so
  // there is always something real to measure here.
  const effectiveBg = (start) => {
    let n = start;
    while (n && n.nodeType === 1) {
      const c = parseColor(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.2) return c;
      n = n.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const renderedOpacity = (start) => {
    let o = 1, n = start;
    while (n && n.nodeType === 1) {
      const s = getComputedStyle(n);
      if (s.display === 'none' || s.visibility === 'hidden') return 0;
      o *= parseFloat(s.opacity === '' ? '1' : s.opacity);
      n = n.parentElement;
    }
    return o;
  };

  const chat = document.getElementById('chat');
  if (!chat) {
    problems.push('#chat is missing from the DOM');
  } else if (renderedOpacity(chat) < 0.35) {
    problems.push('the chat log is hidden - the visitor cannot see the agent working');
  } else {
    const cr = chat.getBoundingClientRect();
    const msgs = chat.querySelectorAll('.msg');
    if (msgs.length) {
      if (cr.width < 80 || cr.height < 24) {
        problems.push('the chat log is collapsed to ' + Math.round(cr.width) + 'x' +
          Math.round(cr.height) + 'px - agent progress would be unreadable');
      }
      let readable = 0;
      msgs.forEach((m) => {
        const t = m.querySelector('.msg-text');
        if (!t || !t.textContent.trim()) return;
        if (renderedOpacity(t) < 0.35) return;
        const mr = m.getBoundingClientRect();
        if (mr.height < 18 || mr.width < 60) return;
        const tc = parseColor(getComputedStyle(t).color);
        if (!tc || tc.a < 0.35) return;
        // A gradient or image behind the text makes the computed-style
        // comparison meaningless, so only judge contrast over a solid colour.
        let painted = false, pn = t;
        while (pn && pn.nodeType === 1) {
          if ((getComputedStyle(pn).backgroundImage || 'none') !== 'none') { painted = true; break; }
          pn = pn.parentElement;
        }
        if (!painted && contrast(tc, effectiveBg(t)) < 1.7) return;
        readable++;
      });
      if (readable === 0) {
        problems.push('no chat message is legible - check .msg / .msg-text colour, ' +
          'contrast and height so the visitor can read the agent transcript');
      }
    }
  }

  // Advisory only: Enter still submits without a visible button.
  const warnings = [];
  const form = document.getElementById('prompt-form');
  if (form && getComputedStyle(form).display === 'none') warnings.push('the form wrapper is display:none');
  const btn = document.getElementById('prompt-submit');
  if (btn) {
    const br = btn.getBoundingClientRect();
    if (br.width < 8 || br.height < 8) warnings.push('the send button is collapsed (Enter still submits)');
  }

  return {
    problems, warnings,
    info: {
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
      scrolledIntoView: scrolled,
      // page coordinates, for a one-pixel background sample
      probe: { x: Math.round(cx + window.scrollX), y: Math.round(cy + window.scrollY) },
      viewport: { w: vw, h: vh },
      opacity: Number(opacity.toFixed(2)),
      color: cs.color, background: cs.backgroundColor, fontSize: cs.fontSize
    }
  };
}
"""


def _first_pixel_rgb(png):
    """The top-left pixel of a PNG, without an image library.

    Only pixel (0,0) is ever needed, and that one is filter-independent: every
    PNG filter predicts from the pixel to the left and the row above, both of
    which are zero there, so the stored bytes are the raw values.
    """
    if len(png) < 26 or png[:8] != bytes((137, 80, 78, 71, 13, 10, 26, 10)):
        return None
    pos, width, height, depth, colour = 8, 0, 0, 0, 0
    idat = bytearray()
    while pos + 8 <= len(png):
        length, kind = struct.unpack(">I4s", png[pos:pos + 8])
        body = png[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length
    if depth != 8 or not idat:
        return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if channels is None:
        return None
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    if len(raw) < 1 + channels:
        return None
    px = raw[1:1 + channels]          # skip the scanline's filter byte
    if colour in (0, 4):
        return (px[0], px[0], px[0])
    return (px[0], px[1], px[2])


def _luminance(rgb):
    def channel(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(a, b):
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _parse_rgb(value):
    try:
        inner = value[value.index("(") + 1:value.index(")")]
        parts = [float(x.strip()) for x in inner.split(",")]
        return (parts[0], parts[1], parts[2])
    except (ValueError, IndexError):
        return None


HIDE_TEXT_JS = """
() => {
  const el = document.getElementById('prompt-input');
  if (!el) return null;
  const prev = { value: el.value, colour: el.style.getPropertyValue('color'),
                 priority: el.style.getPropertyPriority('color') };
  // A single space hides the placeholder (which is painted with its own colour,
  // so making `color` transparent would not), leaving only the background.
  el.value = ' ';
  el.style.setProperty('color', 'transparent', 'important');
  return prev;
}
"""

RESTORE_TEXT_JS = """
(prev) => {
  const el = document.getElementById('prompt-input');
  if (!el || !prev) return;
  el.value = prev.value;
  el.style.removeProperty('color');
  if (prev.colour) el.style.setProperty('color', prev.colour, prev.priority || '');
}
"""

MIN_CONTRAST = 1.7


async def measure_text_contrast(page, info):
    """Compare the input's text colour against the pixel actually rendered
    behind it. Computed styles cannot see a gradient, image or pattern, so
    reading background-color alone rejected plenty of readable designs."""
    probe = (info or {}).get("probe") or {}
    fg = _parse_rgb((info or {}).get("color") or "")
    if fg is None or "x" not in probe:
        return None
    prev = await page.evaluate(HIDE_TEXT_JS)
    try:
        shot = await page.screenshot(
            clip={"x": probe["x"], "y": probe["y"], "width": 1, "height": 1}
        )
    finally:
        await page.evaluate(RESTORE_TEXT_JS, prev)
    bg = _first_pixel_rgb(shot)
    if bg is None:
        return None
    ratio = contrast_ratio(fg, bg)
    if ratio < MIN_CONTRAST:
        return (
            "the prompt input text is almost invisible against what is actually "
            "rendered behind it (contrast %.2f, need %.2f)" % (ratio, MIN_CONTRAST)
        )
    return None


LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


async def _same_origin_only(route):
    """Abort any request that is not to our own service or an inline data URI."""
    req_url = route.request.url
    try:
        if req_url.startswith("data:") or req_url.startswith("blob:"):
            await route.continue_()
            return
        host = urlparse(req_url).hostname or ""
        if host in LOCAL_HOSTS:
            await route.continue_()
            return
        await route.abort()
    except Exception:
        try:
            await route.abort()
        except Exception:
            pass


@dataclass
class Report:
    ok: bool
    problems: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    info: dict = field(default_factory=dict)
    shots: dict = field(default_factory=dict)  # label -> jpeg bytes

    def summary(self):
        if self.ok:
            base = "PASS - the prompt input is visible, on screen, unobstructed and typeable."
            if self.warnings:
                base += " Warnings: " + "; ".join(self.warnings[:4])
            return base
        return "FAIL - " + " | ".join(self.problems[:6])


class BrowserPool:
    """One long-lived Chromium, reused for every check. No per-run processes."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def start(self):
        if self._browser:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--hide-scrollbars",
            ]
        )

    async def stop(self):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            self._browser = None
            if self._pw:
                await self._pw.stop()
                self._pw = None

    async def _page(self, width, height):
        context = await self._browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=1,
            java_script_enabled=True,
            bypass_csp=False,
            # No network beyond our own origin is needed; block everything else.
            offline=False,
        )
        page = await context.new_page()
        return context, page

    async def inspect(self, url, screenshot_only=False, viewports=None, samples=None):
        """Load `url` at each viewport, run the checks, grab screenshots."""
        if self._browser is None:
            await self.start()
        viewports = viewports or VIEWPORTS
        samples = samples if samples is not None else SAMPLE_TIMES

        problems, warnings, info, shots = [], [], {}, {}

        async with self._lock:
            for label, w, h in viewports:
                context, page = await self._page(w, h)
                try:
                    # Belt to the sanitiser's braces: even if some url() slipped
                    # through, the browser may only talk to our own origin.
                    await page.route("**/*", _same_origin_only)
                    await page.goto(url, wait_until="load", timeout=20000)
                    await page.wait_for_timeout(250)

                    if screenshot_only:
                        shots[label] = await page.screenshot(type="jpeg", quality=72)
                        continue

                    last = None
                    prev_t = 0.0
                    for t in samples:
                        delay = max(0.0, t - prev_t)
                        prev_t = t
                        if delay:
                            await page.wait_for_timeout(int(delay * 1000))
                        res = await page.evaluate(CHECK_JS)
                        last = res
                        for p in res.get("problems", []):
                            msg = "[%s @ %.1fs] %s" % (label, t, p)
                            if msg not in problems:
                                problems.append(msg)
                        for wn in res.get("warnings", []):
                            msg = "[%s] %s" % (label, wn)
                            if msg not in warnings:
                                warnings.append(msg)

                    # Can a human actually type into it?
                    if not any(p.startswith("[%s" % label) for p in problems):
                        try:
                            probe = "slopbox-check"
                            # Deliberately not page.click(): it waits for the
                            # element to stop moving, which would outlaw every
                            # animation even though a human could still click.
                            await page.focus("#prompt-input", timeout=3000)
                            focused = await page.evaluate(
                                "() => document.activeElement && document.activeElement.id"
                            )
                            if focused != "prompt-input":
                                problems.append(
                                    "[%s] the prompt input cannot take keyboard focus" % label
                                )
                            await page.fill("#prompt-input", probe, timeout=3000)
                            value = await page.input_value("#prompt-input", timeout=3000)
                            if value != probe:
                                problems.append(
                                    "[%s] typing into the prompt input did not register" % label
                                )
                            await page.fill("#prompt-input", "", timeout=3000)
                        except Exception as exc:
                            problems.append(
                                "[%s] the prompt input could not be clicked or typed into (%s)"
                                % (label, type(exc).__name__)
                            )

                    info[label] = (last or {}).get("info", {})
                    try:
                        issue = await measure_text_contrast(page, info[label])
                    except Exception:
                        issue = None
                    if issue:
                        problems.append("[%s] %s" % (label, issue))
                    shots[label] = await page.screenshot(type="jpeg", quality=72)
                except Exception as exc:
                    problems.append(
                        "[%s] the page failed to render: %s: %s"
                        % (label, type(exc).__name__, str(exc)[:180])
                    )
                finally:
                    await context.close()

        return Report(
            ok=not problems, problems=problems, warnings=warnings, info=info, shots=shots
        )


POOL = BrowserPool()
