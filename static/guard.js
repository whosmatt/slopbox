/* slopbox safety net - immutable, and unstylable by design.
 *
 * The server-side validator refuses to publish a stylesheet that hides the
 * prompt input. This is the second line of defence, for what a headless check
 * at two viewports cannot see: the visitor's own window size, fonts, zoom,
 * reduced-motion settings, or a rendering difference in their browser.
 *
 * It re-runs the core usability check in the live page every second or so. On
 * failure it renders a fallback overlay inside a shadow root, whose host is
 * styled through the CSSOM with `!important` - inline important declarations
 * outrank any author stylesheet rule, so the published CSS cannot suppress it.
 */
(function () {
  'use strict';

  var HOST_ID = 'slopbox-guard';
  var POLL_MS = 1200;
  var FAILS_TO_SHOW = 2;
  var PASSES_TO_HIDE = 2;
  var SNOOZE_MS = 60000;

  var fails = 0, passes = 0, visible = false, snoozedUntil = 0;
  var host = null, root = null, reasonEl = null, inputEl = null, statusEl = null;

  function parseColor(s) {
    var m = String(s).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    var p = m[1].split(',').map(function (x) { return parseFloat(x.trim()); });
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function lum(c) {
    function f(v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function contrast(a, b) {
    var la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }

  /* Deliberately more lenient than the server-side gate: a false overlay is
     worse here, because the page is probably fine. */
  function check() {
    var el = document.getElementById('prompt-input');
    if (!el) return 'the input field is gone from the page';

    var cs = getComputedStyle(el);
    var r = el.getBoundingClientRect();
    var vw = window.innerWidth, vh = window.innerHeight;

    var opacity = 1, node = el;
    while (node && node.nodeType === 1) {
      var s = getComputedStyle(node);
      if (s.display === 'none') return 'the input field is hidden (display:none)';
      if (s.visibility === 'hidden' || s.visibility === 'collapse') return 'the input field is hidden (visibility)';
      opacity *= parseFloat(s.opacity === '' ? '1' : s.opacity);
      node = node.parentElement;
    }
    if (opacity < 0.2) return 'the input field is transparent';
    if (r.width < 60 || r.height < 16) return 'the input field has collapsed to almost nothing';

    var ix = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    var iy = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    if ((ix * iy) / Math.max(1, r.width * r.height) < 0.5) {
      /* Below the fold is not broken - the visitor can simply scroll, and on a
         short window a tall design puts the box there legitimately. Work out
         whether it is reachable without actually scrolling: moving the page
         under the visitor every poll would be worse than the bug. */
      var fixed = false, n = el;
      while (n && n.nodeType === 1) {
        if (getComputedStyle(n).position === 'fixed') { fixed = true; break; }
        n = n.parentElement;
      }
      var docTop = r.top + window.scrollY;
      var docLeft = r.left + window.scrollX;
      var docH = Math.max(document.documentElement.scrollHeight,
                          document.body ? document.body.scrollHeight : 0);
      var docW = Math.max(document.documentElement.scrollWidth,
                          document.body ? document.body.scrollWidth : 0);
      /* A fixed element does not scroll into view, so for those the viewport is
         all there is. */
      var reachable = !fixed &&
        docTop + r.height > 0 && docTop < docH &&
        docLeft + r.width > 0 && docLeft < docW;
      if (!reachable) return 'the input field has moved off screen';
    }

    if (cs.pointerEvents === 'none') return 'the input field cannot be clicked';

    var cx = Math.min(vw - 1, Math.max(1, r.left + r.width / 2));
    var cy = Math.min(vh - 1, Math.max(1, r.top + r.height / 2));
    /* Skip our own overlay in the hit stack - otherwise, once shown, it would
       be the thing "covering" the input and could never decide to go away.
       An ancestor coming back is still a failure: its background or
       ::before/::after is painted over the field. */
    var stack = document.elementsFromPoint
      ? document.elementsFromPoint(cx, cy)
      : [document.elementFromPoint(cx, cy)];
    var hit = null;
    for (var i = 0; i < stack.length; i++) {
      if (stack[i] && stack[i].id !== HOST_ID) { hit = stack[i]; break; }
    }
    if (hit && hit !== el && !el.contains(hit)) {
      return 'something is covering the input field';
    }

    var fg = parseColor(cs.color);
    if (fg && fg.a < 0.25) return 'text typed into the field would be invisible';
    if (fg) {
      var bg = null, b = el;
      while (b && b.nodeType === 1) {
        var c = parseColor(getComputedStyle(b).backgroundColor);
        if (c && c.a > 0.2) { bg = c; break; }
        b = b.parentElement;
      }
      /* A gradient, image or pattern behind the text makes this comparison
         meaningless - background-color reads as transparent and the walk falls
         through to some ancestor's colour, which rejected plenty of readable
         designs. The server-side validator measures the real pixel instead. */
      var painted = false, pn = el;
      while (pn && pn.nodeType === 1) {
        if ((getComputedStyle(pn).backgroundImage || 'none') !== 'none') { painted = true; break; }
        pn = pn.parentElement;
      }
      if (!painted && bg && contrast(fg, bg) < 1.35) {
        return 'text typed into the field would be unreadable';
      }
    }
    return null;
  }

  var HOST_STYLE = {
    position: 'fixed', inset: '0px', top: '0px', left: '0px',
    width: '100%', height: '100%', margin: '0px', padding: '0px',
    display: 'block', visibility: 'visible', opacity: '1',
    'pointer-events': 'auto', 'z-index': '2147483647',
    transform: 'none', filter: 'none', 'clip-path': 'none', clip: 'auto',
    contain: 'none', overflow: 'auto', 'mix-blend-mode': 'normal',
    'backdrop-filter': 'none', animation: 'none', transition: 'none',
    'font-family': 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    'font-size': '15px', 'line-height': '1.5', 'text-align': 'left',
    'writing-mode': 'horizontal-tb', direction: 'ltr', 'color-scheme': 'light'
  };

  var SHADOW_CSS = [
    ':host { all: initial; }',
    '.wrap { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;',
    '  background: rgba(12,12,14,0.94); padding: 24px; box-sizing: border-box;',
    '  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }',
    '.card { width: 100%; max-width: 520px; background: #fff; color: #111; border-radius: 10px;',
    '  padding: 22px 22px 18px; box-shadow: 0 20px 60px rgba(0,0,0,0.5); box-sizing: border-box; }',
    'h2 { margin: 0 0 6px; font-size: 17px; }',
    'p { margin: 0 0 14px; font-size: 13px; line-height: 1.5; color: #444; }',
    'code { background: #f2f2f2; padding: 1px 5px; border-radius: 3px; font-size: 12px; }',
    'form { display: flex; gap: 8px; margin: 0 0 12px; }',
    'input { flex: 1 1 auto; min-width: 0; padding: 11px 12px; font: inherit; font-size: 14px;',
    '  color: #111; background: #fff; border: 1px solid #bbb; border-radius: 6px; }',
    'button { padding: 11px 14px; font: inherit; font-size: 13px; color: #fff; background: #111;',
    '  border: 1px solid #111; border-radius: 6px; cursor: pointer; }',
    'button.ghost { color: #111; background: #fff; border-color: #ccc; }',
    '.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }',
    '.row a { color: #555; font-size: 12px; }',
    '.status { margin: 10px 0 0; font-size: 12px; color: #777; min-height: 1.3em; }'
  ].join('\n');

  function build() {
    host = document.createElement('div');
    host.id = HOST_ID;
    host.setAttribute('data-slopbox-guard', '1');
    root = host.attachShadow({ mode: 'closed' });

    if (window.CSSStyleSheet && 'adoptedStyleSheets' in root) {
      try {
        var sheet = new CSSStyleSheet();
        sheet.replaceSync(SHADOW_CSS);
        root.adoptedStyleSheets = [sheet];
      } catch (e) { injectStyleTag(); }
    } else {
      injectStyleTag();
    }

    function injectStyleTag() {
      var st = document.createElement('style');
      st.textContent = SHADOW_CSS;
      root.appendChild(st);
    }

    var wrap = document.createElement('div');
    wrap.className = 'wrap';

    var card = document.createElement('div');
    card.className = 'card';

    var h = document.createElement('h2');
    h.textContent = 'This page styled itself into a corner.';

    reasonEl = document.createElement('p');

    var form = document.createElement('form');
    inputEl = document.createElement('input');
    inputEl.type = 'text';
    inputEl.maxLength = 600;
    inputEl.placeholder = 'ask for something usable…';
    var send = document.createElement('button');
    send.type = 'submit';
    send.textContent = 'restyle';
    form.appendChild(inputEl);
    form.appendChild(send);
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      submit(inputEl.value);
    });

    var row = document.createElement('div');
    row.className = 'row';
    var fix = document.createElement('button');
    fix.type = 'button';
    fix.className = 'ghost';
    fix.textContent = 'make it plain and readable';
    fix.addEventListener('click', function () {
      submit('Reset to a plain, calm, high-contrast layout: the input field large and centred, dark text on a light background, no animation.');
    });
    var safe = document.createElement('a');
    safe.href = '/?safe=1';
    safe.textContent = 'safe mode';
    var hide = document.createElement('a');
    hide.href = '#';
    hide.textContent = 'dismiss';
    hide.addEventListener('click', function (ev) {
      ev.preventDefault();
      snoozedUntil = Date.now() + SNOOZE_MS;
      teardown();
    });
    row.appendChild(fix);
    row.appendChild(safe);
    row.appendChild(hide);

    statusEl = document.createElement('p');
    statusEl.className = 'status';

    card.appendChild(h);
    card.appendChild(reasonEl);
    card.appendChild(form);
    card.appendChild(row);
    card.appendChild(statusEl);
    wrap.appendChild(card);
    root.appendChild(wrap);
  }

  function submit(text) {
    text = String(text || '').trim();
    if (!text) { statusEl.textContent = 'Type what you want first.'; return; }
    statusEl.textContent = 'Sending…';
    fetch('/api/prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: text })
    }).then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (d) {
        statusEl.textContent = d && d.ok
          ? 'Queued. The page will update when the agent is done.'
          : ((d && (d.error || d.message)) || 'That did not go through.');
        if (d && d.ok) inputEl.value = '';
      })
      .catch(function () { statusEl.textContent = 'Network error.'; });
  }

  /* If the page hid itself wholesale, force the document back into rendering.
     Inline !important beats author !important, so this always wins. */
  var forced = [];
  function forceDocumentVisible() {
    [document.documentElement, document.body].forEach(function (el) {
      if (!el) return;
      var s = getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') < 0.2) {
        el.style.setProperty('display', 'block', 'important');
        el.style.setProperty('visibility', 'visible', 'important');
        el.style.setProperty('opacity', '1', 'important');
        if (forced.indexOf(el) === -1) forced.push(el);
      }
    });
  }
  function releaseForced() {
    forced.forEach(function (el) {
      el.style.removeProperty('display');
      el.style.removeProperty('visibility');
      el.style.removeProperty('opacity');
    });
    forced.length = 0;
  }

  function setup(reason) {
    if (!host) build();
    forceDocumentVisible();
    reasonEl.textContent = 'The stylesheet on this page broke the prompt box: ' + reason +
      '. Use the box below instead, or switch to safe mode.';
    for (var prop in HOST_STYLE) {
      if (Object.prototype.hasOwnProperty.call(HOST_STYLE, prop)) {
        host.style.setProperty(prop, HOST_STYLE[prop], 'important');
      }
    }
    if (!host.isConnected) {
      (document.body || document.documentElement).appendChild(host);
    }
    visible = true;
  }

  function teardown() {
    visible = false;
    releaseForced();
    if (host && host.isConnected) host.remove();
    if (statusEl) statusEl.textContent = '';
  }

  function tick() {
    var reason = null;
    try { reason = check(); } catch (e) { reason = null; }

    if (reason) {
      passes = 0;
      fails++;
      if (fails >= FAILS_TO_SHOW && !visible && Date.now() >= snoozedUntil) setup(reason);
      else if (visible && reasonEl) {
        reasonEl.textContent = 'The stylesheet on this page broke the prompt box: ' + reason +
          '. Use the box below instead, or switch to safe mode.';
      }
    } else {
      fails = 0;
      passes++;
      if (visible && passes >= PASSES_TO_HIDE) teardown();
    }
  }

  function start() {
    setInterval(tick, POLL_MS);
    setTimeout(tick, 400);
    /* Re-check promptly after the stylesheet is swapped. */
    window.addEventListener('slopbox:restyled', function () {
      fails = 0; passes = 0;
      setTimeout(tick, 600);
      setTimeout(tick, 2500);
    });
  }

  /* The validator renders /preview with this script present; an overlay there
     would pollute the very checks it exists to back up. */
  if (document.documentElement.getAttribute('data-mode') === 'preview') return;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
