/* slopbox client - immutable.
 * Streams the agent's progress into #chat, submits prompts, and hot-swaps the
 * stylesheet when a new version goes live (no reload, so the chat log survives).
 */
(function () {
  'use strict';

  var MAX_MESSAGES = 40;

  var chat = document.getElementById('chat');
  var form = document.getElementById('prompt-form');
  var input = document.getElementById('prompt-input');
  var button = document.getElementById('prompt-submit');
  var status = document.getElementById('qb-status');

  var bubbles = {};   // mid -> .msg-text node
  var peeks = {};     // mid -> .msg-peek node, for collapsed thinking messages
  var seen = {};      // event id -> true, so replayed events are not duplicated

  var PEEK_CHARS = 80;

  function peekText(full) {
    var flat = full.replace(/\s+/g, ' ').trim();
    return flat.length > PEEK_CHARS ? '…' + flat.slice(-PEEK_CHARS) : flat;
  }

  function trim() {
    while (chat.children.length > MAX_MESSAGES) {
      var gone = chat.firstElementChild;
      for (var k in bubbles) {
        if (bubbles[k] && !bubbles[k].isConnected) { delete bubbles[k]; delete peeks[k]; }
      }
      chat.removeChild(gone);
    }
  }

  function atBottom() {
    return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
  }

  function addMessage(role, text, mid, steering) {
    var stick = atBottom();
    var body = document.createElement('span');
    body.className = 'msg-text';
    body.textContent = text || '';

    var wrap;
    if (role === 'thinking') {
      /* The model's deliberation runs to thousands of characters, so it stays
         folded away. The summary carries a live tail of the stream, which is
         enough to see it working; click for the whole thing. */
      wrap = document.createElement('details');
      wrap.className = 'msg thinking';
      var summary = document.createElement('summary');
      var slabel = document.createElement('span');
      slabel.className = 'msg-role';
      slabel.textContent = 'thinking';
      var peek = document.createElement('span');
      peek.className = 'msg-peek';
      peek.textContent = peekText(text || '');
      summary.appendChild(slabel);
      summary.appendChild(peek);
      wrap.appendChild(summary);
      wrap.appendChild(body);
      if (mid) peeks[mid] = peek;
    } else {
      wrap = document.createElement('div');
      wrap.className = 'msg ' + role + (steering ? ' steering' : '');
      var label = document.createElement('span');
      label.className = 'msg-role';
      /* Marked in the role line rather than appended to the text, so the
         message body stays exactly what the visitor typed. */
      label.textContent = (role === 'assistant' ? 'slopbox' : role) +
        (steering ? ' (steering)' : '');
      wrap.appendChild(label);
      wrap.appendChild(body);
    }

    chat.appendChild(wrap);
    trim();
    if (stick) chat.scrollTop = chat.scrollHeight;
    if (mid) bubbles[mid] = body;
    return body;
  }

  /* The model is multimodal and looks at a render of its own candidate before
     publishing; showing the same frame here is the visitor's view of that. */
  function addImage(src, label) {
    if (!src) return;
    var stick = atBottom();
    var wrap = document.createElement('div');
    wrap.className = 'msg shot';
    var role = document.createElement('span');
    role.className = 'msg-role';
    role.textContent = label === 'rejected' ? 'rejected render' : 'what it sees';
    var img = document.createElement('img');
    img.className = 'msg-shot';
    img.alt = 'Screenshot of the candidate stylesheet';
    img.loading = 'lazy';
    /* Height is unknown until the image decodes, so keep the log pinned to the
       bottom once it does - otherwise it jumps away from the newest message. */
    img.addEventListener('load', function () {
      if (stick) chat.scrollTop = chat.scrollHeight;
    });
    img.src = src;
    wrap.appendChild(role);
    wrap.appendChild(img);
    chat.appendChild(wrap);
    trim();
    if (stick) chat.scrollTop = chat.scrollHeight;
  }

  function appendDelta(mid, text) {
    var node = bubbles[mid];
    if (!node || !node.isConnected) node = addMessage('assistant', '', mid);
    var stick = atBottom();
    node.textContent += text;
    var peek = peeks[mid];
    if (peek && peek.isConnected) peek.textContent = peekText(node.textContent);
    if (stick) chat.scrollTop = chat.scrollHeight;
  }

  function setStatus(text) {
    if (status) status.textContent = text;
  }

  function setBusy(busy) {
    document.documentElement.setAttribute('data-busy', busy ? '1' : '0');
    if (button) button.disabled = false; /* queuing while busy is allowed */
  }

  var MODE = document.documentElement.getAttribute('data-mode');
  /* safe   - served deliberately bare, nothing may style it back
     pinned - showing a design chosen from the gallery, which must survive
              whatever anyone else publishes while this visitor reads it */
  var NO_RESTYLE = MODE === 'safe' || MODE === 'pinned';

  function restyle(version) {
    if (NO_RESTYLE) return;

    var fresh = document.createElement('link');
    fresh.rel = 'stylesheet';
    fresh.setAttribute('data-qb-style', '1');
    fresh.href = '/style.css?v=' + (version || Date.now());

    function sweep() {
      /* Remove every other slopbox sheet rather than one node captured up
         front. Two restyles in quick succession used to each remove the node
         the other had captured, leaving an orphan behind - and an orphan whose
         rules are !important outranks the new sheet, so the page kept the old
         background until a reload. */
      var old = document.querySelectorAll('link[data-qb-style]');
      for (var i = 0; i < old.length; i++) {
        if (old[i] !== fresh && old[i].parentNode) old[i].parentNode.removeChild(old[i]);
      }
      window.dispatchEvent(new Event('slopbox:restyled'));
    }

    fresh.addEventListener('load', sweep);
    fresh.addEventListener('error', sweep);
    document.head.appendChild(fresh);
  }

  /* Publishing changes more than the stylesheet: decoration and the sketch frame
     are rendered into the HTML server-side, so before this they only appeared,
     changed or vanished when the visitor reloaded. Pinned and safe pages keep
     what they were served, exactly as they keep their stylesheet. */
  function refreshParts(version) {
    if (NO_RESTYLE) return;
    fetch('/api/design', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var decor = document.getElementById('qb-decor');
        if (decor && decor.innerHTML !== d.decor) {
          /* Already through the server-side allowlist sanitiser, and the page
             CSP forbids inline script regardless. */
          decor.innerHTML = d.decor || '';
        }

        var frame = document.getElementById('qb-sketch');
        if (!d.sketch) {
          if (frame && frame.parentNode) frame.parentNode.removeChild(frame);
          return;
        }
        var wanted = d.placement === 'above-chat' || d.placement === 'beside-chat'
          ? document.getElementById('qb-main')
          : document.getElementById('qb-root');
        var before = d.placement === 'above-chat' || d.placement === 'beside-chat'
          ? document.getElementById('chat')
          : document.getElementById('qb-header');
        if (!frame) {
          frame = document.createElement('iframe');
          frame.id = 'qb-sketch';
          frame.title = 'Decorative sketch';
          /* No allow-same-origin, ever: that would hand the frame this page. */
          frame.setAttribute('sandbox', 'allow-scripts');
          frame.setAttribute('referrerpolicy', 'no-referrer');
        }
        frame.className = d.sketchClass || '';
        var src = d.sketchSrc + '&v=' + (version || d.version || Date.now());
        if (frame.getAttribute('src') !== src) frame.setAttribute('src', src);
        if (wanted && (frame.parentNode !== wanted || frame.nextSibling !== before)) {
          wanted.insertBefore(frame, before || null);
        }
      })
      .catch(function () { /* the stylesheet still swapped; leave the rest */ });
  }

  function handle(ev) {
    if (ev.id) {
      if (seen[ev.id]) return;
      seen[ev.id] = true;
    }
    switch (ev.type) {
      case 'hello':
        (ev.replay || []).forEach(handle);
        setBusy(!!ev.busy);
        setStatus(ev.busy ? 'working…' : 'ready');
        break;
      case 'prompt':
        addMessage('user', ev.text, null, ev.steering);
        break;
      case 'message':
        addMessage(ev.role || 'system', ev.text || '', ev.mid);
        break;
      case 'delta':
        appendDelta(ev.mid, ev.text || '');
        break;
      case 'image':
        addImage(ev.src, ev.label);
        break;
      case 'tool':
        setStatus({
          get_current_design: 'reading the current design…',
          write_decor: 'drawing markup…',
          write_sketch: 'writing a sketch…',
          write_css: 'writing CSS…',
          screenshot: 'looking at the result…',
          publish: 'validating in a real browser…',
          finish: 'done'
        }[ev.name] || (ev.name + '…'));
        break;
      case 'status':
        if (typeof ev.busy === 'boolean') setBusy(ev.busy);
        setStatus(ev.text || '');
        break;
      case 'reload':
        restyle(ev.version);
        refreshParts(ev.version);
        setStatus('new look is live');
        break;
      case 'error':
        addMessage('error', ev.text || 'Something went wrong.');
        setBusy(false);
        break;
    }
  }

  function connect() {
    var es = new EventSource('/events');
    es.onmessage = function (e) {
      try { handle(JSON.parse(e.data)); } catch (err) { /* ignore */ }
    };
    es.onerror = function () {
      setStatus('reconnecting…');
      /* EventSource retries on its own; nothing to do. */
    };
  }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var text = (input.value || '').trim();
    if (!text) return;
    setStatus('sending…');
    fetch('/api/prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: text })
    }).then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (d) {
        if (d && d.ok) {
          input.value = '';
          setStatus(d.message || 'queued');
        } else {
          setStatus((d && (d.error || d.message)) || 'that did not go through');
        }
      })
      .catch(function () { setStatus('network error'); });
  });

  /* Mark the server-rendered stylesheet link so hot-swaps can replace it. */
  var initial = document.querySelector('link[href^="/style.css"]');
  if (initial) initial.setAttribute('data-qb-style', '1');

  /* The validator renders /preview to judge a candidate stylesheet. Populate the
     chat log with representative messages there instead of connecting to the
     live stream: it makes the readability checks deterministic, and it means the
     model's own screenshots show what the chat will actually look like. */
  if (MODE === 'preview') {
    addMessage('user', 'make it look like a haunted library at midnight');
    addMessage('thinking', 'The visitor wants something gothic and dim. Deep ' +
      'browns, candlelight amber, a serif face, and a slow flicker on the ' +
      'headings. The prompt box needs to stay legible against all of it.');
    addMessage('assistant', 'Dimming the lights and lighting the candles.');
    addMessage('system', 'Published. The new look is live.');
    return;
  }

  connect();
})();
