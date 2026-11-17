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


# Where things sit on the page, and whether the chat is holding its content.
# Read at the top of the page so document coordinates are stable. Overflow is
# what goes wrong most once designs get ambitious: long words and screenshots
# spilling out of message bubbles, or the chat scrolling sideways.
LAYOUT_JS = r"""
() => {
  const vh = innerHeight, vw = innerWidth, sy = scrollY;
  const de = document.documentElement;
  const height = Math.max(de.scrollHeight, document.body ? document.body.scrollHeight : 0);
  const part = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    if (cs.display === 'none' || (r.width < 1 && r.height < 1)) return null;
    return { top: Math.round(r.top + sy), bottom: Math.round(r.bottom + sy),
             fixed: cs.position === 'fixed' || cs.position === 'sticky' };
  };
  const parts = {
    header: part('#qb-header'), sketch: part('#qb-sketch'), chat: part('#chat'),
    'prompt box': part('#prompt-input'), footer: part('#qb-footer'),
  };
  const issues = [];
  const add = (t) => { if (issues.length < 6 && issues.indexOf(t) === -1) issues.push(t); };
  if (de.scrollWidth > vw + 1) add('the page is ' + de.scrollWidth + 'px wide in a ' + vw +
    'px viewport, so it scrolls sideways');
  const chat = document.getElementById('chat');
  if (chat) {
    const cr = chat.getBoundingClientRect();
    if (chat.scrollWidth > chat.clientWidth + 1) add('#chat scrolls sideways (content ' +
      chat.scrollWidth + 'px in ' + chat.clientWidth + 'px)');
    chat.querySelectorAll('.msg').forEach((m) => {
      const kind = '.msg.' + (m.className.split(/\s+/).filter(c => c !== 'msg')[0] || '?');
      const mr = m.getBoundingClientRect();
      if (mr.right > cr.right + 1 || mr.left < cr.left - 1) add(kind + ' sticks out of #chat by ' +
        Math.round(Math.max(mr.right - cr.right, cr.left - mr.left)) + 'px');
      if (m.scrollWidth > m.clientWidth + 1) add(kind + ' content is wider than the bubble (' +
        m.scrollWidth + 'px in ' + m.clientWidth + 'px) and gets clipped or spills');
      const t = m.querySelector('.msg-text');
      if (t && t.scrollWidth > t.clientWidth + 1) add(kind + ' .msg-text does not wrap: a long ' +
        'word runs ' + (t.scrollWidth - t.clientWidth) + 'px past its box (try overflow-wrap:anywhere)');
      const img = m.querySelector('img.msg-shot');
      if (img) {
        const ir = img.getBoundingClientRect();
        if (ir.right > mr.right + 1 || ir.left < mr.left - 1) add('img.msg-shot is wider than its ' +
          '.msg.shot bubble (' + Math.round(ir.width) + 'px in ' + Math.round(mr.width) + 'px)');
        else if (ir.height > vh * 0.8) add('img.msg-shot is ' + Math.round(ir.height) +
          'px tall - most of a screen for one thumbnail');
      }
    });
  }
  return { height, vh, vw, parts, issues };
}
"""


def describe_layout(label, data):
    """One readable paragraph about where things are on a page."""
    if not isinstance(data, dict) or not data.get("vh"):
        return ""
    vh, height = data["vh"], data["height"]
    bits = []
    for name, p in (data.get("parts") or {}).items():
        if not p:
            continue
        where = "%s-%s" % (format(p["top"], ","), format(p["bottom"], ","))
        if p.get("fixed"):
            where += " (fixed)"
        elif p["bottom"] > vh:
            screens = p["top"] / vh
            where += " (%s)" % ("starts above the fold, ends below it" if p["top"] < vh
                               else "%.1f screens down" % screens)
        bits.append("%s %s" % (name, where))
    text = "%s: page %spx tall = %.1f screens of %dpx. %s." % (
        label, format(height, ","), height / vh, vh, "; ".join(bits))
    issues = data.get("issues") or []
    text += (" Overflow: " + "; ".join(issues) + ".") if issues else " No overflow found."
    return text


SCROLL_MODES = ("auto", "top", "prompt", "bottom", "all")
MAX_SLICES = 4


def scroll_targets(mode, data, label):
    """Scroll offsets to photograph for a screenshot mode."""
    vh = (data or {}).get("vh") or 800
    height = (data or {}).get("height") or vh
    last = max(0, height - vh)
    prompt = ((data or {}).get("parts") or {}).get("prompt box")
    # Prompt box near the bottom of the screen, so the chat above it shows.
    at_prompt = min(last, max(0, prompt["bottom"] + 60 - vh)) if prompt else last
    if isinstance(mode, int):
        return [min(last, max(0, mode))]
    if mode == "top":
        return [0]
    if mode == "prompt":
        return [at_prompt]
    if mode == "bottom":
        return [last]
    if mode == "all":
        ys = []
        for i in range(MAX_SLICES):
            y = min(last, i * vh)
            if y not in ys:
                ys.append(y)
            if y >= last:
                break
        return ys
    # auto: the first screen, plus the chat and prompt box when they are below
    # it - desktop only, to keep the image count down.
    ys = [0]
    if label == "desktop" and prompt and prompt["bottom"] > vh and at_prompt > 0:
        ys.append(at_prompt)
    return ys


def parse_scroll(value):
    if value is None or value == "":
        return "auto"
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().rstrip("px")
    if text.isdigit():
        return int(text)
    return text if text in SCROLL_MODES else "auto"


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
    # What the sketch frame logged - errors and console output - or None when
    # the page has no sketch.
    sketch_log: list = None
    # label -> one-paragraph description of the page layout (screenshots only)
    layout: dict = field(default_factory=dict)

    def summary(self):
        if self.ok:
            base = "PASS - the prompt input is visible, on screen, unobstructed and typeable."
            if self.warnings:
                base += " Warnings: " + "; ".join(self.warnings[:4])
            return base
        return "FAIL - " + " | ".join(self.problems[:6])


# Long enough for module imports, a first frame and any setup timers.
SKETCH_SETTLE_MS = 900


async def _sketch_frame(page):
    for frame in page.frames:
        if "/sketch.html" in frame.url:
            return frame
    return None


async def read_sketch_log(page):
    """The sketch harness's log, or None if there is no sketch frame."""
    frame = await _sketch_frame(page)
    if frame is None:
        return None
    try:
        log = await frame.evaluate("() => window.__slopLog || []")
    except Exception as exc:
        return [{"k": "error", "t": 0,
                 "m": "the sketch frame could not be read (%s)" % type(exc).__name__}]
    return log if isinstance(log, list) else []


def _merge_log(into, entries):
    seen = {(e.get("k"), e.get("m")) for e in into}
    for e in entries or ():
        key = (e.get("k"), e.get("m"))
        if key not in seen:
            seen.add(key)
            into.append(e)


@dataclass
class PlayResult:
    shots: list = field(default_factory=list)  # (label, jpeg bytes)
    log: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    error: str = None


# Limits for one test play: enough for "start, steer, shoot, watch", short
# enough that a run cannot spend a minute per step pressing keys.
PLAY_MAX_ACTIONS = 40
PLAY_MAX_MS = 12000
PLAY_MAX_SHOTS = 6

# Hard ceilings on one browser job, end to end. Individual Playwright calls
# have their own timeouts, but evaluate() has none: a sketch stuck in a
# synchronous loop can freeze the renderer and leave a check waiting forever,
# holding the pool lock - the run then shows "working" until a restart.
# Normal jobs take a fraction of this even with software WebGL.
INSPECT_LIMIT_S = 90
PLAY_LIMIT_S = 60

FROZE = (
    "the page did not finish %s within %ds and the browser was reset. The sketch "
    "most likely froze the page: an endless loop, or very heavy synchronous work "
    "at load or in an event handler. Nothing was published."
)


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

    async def _kill(self):
        """Tear the browser down without waiting on anything inside it.

        Closing Chromium is also what unblocks a call stuck in a frozen page:
        it fails with a closed-target error instead of waiting forever.
        """
        browser, pw = self._browser, self._pw
        self._browser, self._pw = None, None
        for closer in (browser and browser.close, pw and pw.stop):
            if closer is None:
                continue
            try:
                await asyncio.wait_for(closer(), timeout=10)
            except Exception:
                pass

    async def _limited(self, job, limit):
        """Run a browser job with a hard total time limit. Returns the job's
        result, or None if it had to be killed."""
        task = asyncio.ensure_future(job)
        done, _ = await asyncio.wait({task}, timeout=limit)
        if task in done:
            return task.result()
        # Deliberately not asyncio.wait_for: it waits for the cancelled task to
        # finish unwinding, and a task stuck in a frozen page never does.
        task.cancel()
        await self._kill()
        await asyncio.wait({task}, timeout=15)
        if task.done() and not task.cancelled():
            task.exception()  # retrieved, so asyncio does not warn about it
        return None

    async def inspect(self, url, screenshot_only=False, viewports=None, samples=None,
                      scroll=None):
        report = await self._limited(
            self._inspect(url, screenshot_only, viewports, samples, scroll), INSPECT_LIMIT_S
        )
        if report is None:
            what = "rendering" if screenshot_only else "validation"
            report = Report(ok=False, problems=["[timeout] " + FROZE % (what, INSPECT_LIMIT_S)])
        return report

    async def play(self, url, actions, viewport=None):
        result = await self._limited(self._play(url, actions, viewport), PLAY_LIMIT_S)
        if result is None:
            result = PlayResult(error=FROZE % ("the test play", PLAY_LIMIT_S))
        return result

    async def _inspect(self, url, screenshot_only=False, viewports=None, samples=None,
                       scroll=None):
        """Load `url` at each viewport, run the checks, grab screenshots.

        For screenshots, `scroll` picks which parts of a long page to show -
        always as real viewport-sized shots taken after scrolling, so fixed
        elements, vh units and the fold look exactly as a visitor sees them.
        """
        mode = parse_scroll(scroll)
        layout = {}
        if self._browser is None:
            await self.start()
        viewports = viewports or VIEWPORTS
        samples = samples if samples is not None else SAMPLE_TIMES

        problems, warnings, info, shots = [], [], {}, {}
        sketch_log = None

        async with self._lock:
            for label, w, h in viewports:
                context, page = await self._page(w, h)
                try:
                    # Belt to the sanitiser's braces: even if some url() slipped
                    # through, the browser may only talk to our own origin.
                    await page.route("**/*", _same_origin_only)
                    await page.goto(url, wait_until="load", timeout=20000)
                    has_sketch = await _sketch_frame(page) is not None
                    await page.wait_for_timeout(SKETCH_SETTLE_MS if has_sketch else 250)
                    if has_sketch:
                        if sketch_log is None:
                            sketch_log = []
                        _merge_log(sketch_log, await read_sketch_log(page))

                    if screenshot_only:
                        try:
                            data = await page.evaluate(LAYOUT_JS)
                        except Exception:
                            data = None
                        layout[label] = describe_layout(label, data)
                        for y in scroll_targets(mode, data, label):
                            await page.evaluate(
                                "y => window.scrollTo({top: y, left: 0, behavior: 'instant'})", y)
                            await page.wait_for_timeout(200)
                            actual = int(await page.evaluate("() => Math.round(scrollY)"))
                            name = label if actual == 0 else "%s scrolled %spx down" % (
                                label, format(actual, ","))
                            shots[name] = await page.screenshot(type="jpeg", quality=72)
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

        # A sketch that throws is broken however the page around it looks, and
        # one that fails to load a library is blank. Console output alone is
        # not a failure - libraries warn about all sorts of harmless things.
        if not screenshot_only:
            errors = [e["m"] for e in (sketch_log or ()) if e.get("k") == "error"]
            for err in errors[:4]:
                problems.append("[sketch] " + err)

        return Report(
            ok=not problems, problems=problems, warnings=warnings, info=info,
            shots=shots, sketch_log=sketch_log, layout=layout,
        )

    async def _play(self, url, actions, viewport=None):
        """Load the page and drive the sketch frame with real input.

        Input goes through the page exactly as a visitor's would, so a
        click-through (non-interactive) frame does not receive it - which is
        itself the useful finding.
        """
        if self._browser is None:
            await self.start()
        label, w, h = viewport or VIEWPORTS[0]
        result = PlayResult()

        async with self._lock:
            context, page = await self._page(w, h)
            try:
                await page.route("**/*", _same_origin_only)
                await page.goto(url, wait_until="load", timeout=20000)
                frame_el = await page.query_selector("#qb-sketch")
                if frame_el is None:
                    result.error = "there is no sketch frame on the page"
                    return result
                await page.wait_for_timeout(SKETCH_SETTLE_MS)
                await frame_el.scroll_into_view_if_needed(timeout=3000)
                box = await frame_el.bounding_box()
                if not box or box["width"] < 4 or box["height"] < 4:
                    result.error = "the sketch frame is not visible (it has no size on screen)"
                    return result
                classes = await frame_el.get_attribute("class") or ""
                if "qb-sketch-live" not in classes:
                    result.notes.append(
                        "the frame is click-through (interactive is false), so clicks "
                        "and keys went to the page behind it, not the sketch"
                    )
                result.notes.append(
                    "frame is %dx%d px on a %dx%d %s viewport"
                    % (box["width"], box["height"], w, h, label)
                )

                def point(a, kx="x", ky="y"):
                    def clamp(v, size):
                        if not isinstance(v, (int, float)):
                            return size / 2
                        return max(0, min(size - 1, v))
                    return (box["x"] + clamp(a.get(kx), box["width"]),
                            box["y"] + clamp(a.get(ky), box["height"]))

                async def shot(name):
                    data = await frame_el.screenshot(type="jpeg", quality=72, timeout=5000)
                    result.shots.append((name, data))

                budget = PLAY_MAX_MS
                clock = 0
                for i, a in enumerate(actions[:PLAY_MAX_ACTIONS]):
                    if not isinstance(a, dict):
                        continue
                    kind = a.get("do")
                    try:
                        if kind == "click":
                            await page.mouse.click(*point(a))
                        elif kind == "move":
                            await page.mouse.move(*point(a), steps=8)
                        elif kind == "drag":
                            await page.mouse.move(*point(a))
                            await page.mouse.down()
                            await page.mouse.move(*point(a, "to_x", "to_y"), steps=12)
                            await page.mouse.up()
                        elif kind == "key":
                            key = str(a.get("key") or "")
                            hold = int(min(max(a.get("hold_ms") or 60, 0), budget))
                            await page.keyboard.down(key)
                            await page.wait_for_timeout(hold)
                            await page.keyboard.up(key)
                            budget -= hold
                            clock += hold
                        elif kind == "type":
                            await page.keyboard.type(str(a.get("text") or "")[:80], delay=30)
                        elif kind == "wait":
                            ms = int(min(max(a.get("ms") or 0, 0), budget))
                            await page.wait_for_timeout(ms)
                            budget -= ms
                            clock += ms
                        elif kind == "shot":
                            if len(result.shots) < PLAY_MAX_SHOTS:
                                await shot(str(a.get("label") or "t+%.1fs" % (clock / 1000)))
                            else:
                                result.notes.append("step %d: shot limit reached" % (i + 1))
                        else:
                            result.notes.append("step %d: unknown action %r, skipped" % (i + 1, kind))
                    except Exception as exc:
                        result.notes.append("step %d (%s) failed: %s" % (i + 1, kind, str(exc)[:120]))
                    if budget <= 0:
                        result.notes.append("stopped after step %d: the %ds time budget ran out"
                                            % (i + 1, PLAY_MAX_MS // 1000))
                        break
                if len(actions) > PLAY_MAX_ACTIONS:
                    result.notes.append("only the first %d actions ran" % PLAY_MAX_ACTIONS)
                if not result.shots:
                    await shot("end")
                result.log = await read_sketch_log(page) or []
            except Exception as exc:
                result.error = "%s: %s" % (type(exc).__name__, str(exc).split(chr(10))[0][:200])
                if "Timeout" in type(exc).__name__:
                    result.error += (
                        " - the page stopped responding, so the sketch has most likely "
                        "frozen it (an endless loop, or very heavy synchronous work)"
                    )
            finally:
                await context.close()
        return result


POOL = BrowserPool()
