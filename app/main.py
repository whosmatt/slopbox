"""FastAPI entrypoint.

Routes are deliberately few. The only mutable artefact in the whole service is
the stylesheet at /style.css, and the only way to change it is a prompt that
goes through the agent, the sanitiser and the browser validator.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from html import escape
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from . import bus, resources, store
from .config import (
    ADMIN_TOKEN,
    ENABLE_ASSETS,
    ENABLE_FONTS,
    ENABLE_HTML,
    ENABLE_SKETCH,
    QWEN_API_KEY,
    MAX_PROMPT_CHARS,
    RATE_LIMIT_COUNT,
    RATE_LIMIT_WINDOW_SEC,
    STATIC_DIR,
    TRUSTED_PROXIES,
    note_server_port,
)
from .validator import POOL
from .worker import ORCHESTRATOR

_hits = defaultdict(deque)


LOCAL_PEERS = ("127.0.0.1", "::1", "localhost", "testclient")


def _peer_ip(request: Request):
    """The actual TCP peer. Cannot be forged by a header."""
    return request.client.host if request.client else "unknown"


def _client_ip(request: Request):
    """Caller identity for rate limiting.

    X-Forwarded-For is only believed when the connection genuinely comes from a
    configured proxy - otherwise anyone could mint a fresh identity per request
    and walk straight through the rate limiter.
    """
    peer = _peer_ip(request)
    if peer in TRUSTED_PROXIES:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer


def _require_local(request: Request):
    """Internal-only routes. Judged on the peer address, never on a header."""
    if _peer_ip(request) not in LOCAL_PEERS:
        raise HTTPException(status_code=403, detail="local only")


def _rate_limited(ip):
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > RATE_LIMIT_WINDOW_SEC:
        q.popleft()
    if len(q) >= RATE_LIMIT_COUNT:
        return int(RATE_LIMIT_WINDOW_SEC - (now - q[0])) + 1
    q.append(now)
    # Keep the table from growing unbounded across many IPs.
    if len(_hits) > 5000:
        for k in [k for k, v in list(_hits.items())[:1000] if not v]:
            _hits.pop(k, None)
    return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not QWEN_API_KEY:
        # The client is built lazily, so this would otherwise only surface as a
        # failed prompt. Say it plainly at boot instead.
        print("WARNING: QWEN_API_KEY is empty - every prompt will fail.", flush=True)
    store.init()
    await POOL.start()
    ORCHESTRATOR.start()
    try:
        yield
    finally:
        await ORCHESTRATOR.stop()
        await POOL.stop()


app = FastAPI(title="slopbox", lifespan=lifespan, docs_url=None, redoc_url=None)

NOSNIFF = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

# Third layer, after the sanitiser and the validator: the browser itself refuses
# any outbound request the stylesheet might attempt. 'unsafe-inline' for styles
# is intentional - the guard overlay styles its own shadow root, and inline style
# permission grants no network reach, which is what this header exists to deny.
CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "frame-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # scope["server"] is the socket uvicorn bound, not anything the client sent,
    # so it is safe to learn our own address from it.
    server = request.scope.get("server")
    if server:
        note_server_port(server[1])
    response = await call_next(request)
    for k, v in NOSNIFF.items():
        response.headers.setdefault(k, v)
    if request.url.path in ("/", "/preview", "/gallery"):
        response.headers["Content-Security-Policy"] = CSP
        # The HTML embeds the current stylesheet version, so a cached copy sends
        # a reloading visitor to a stale /style.css?v=N.
        response.headers["Cache-Control"] = "no-store"
    return response


# Where the sketch frame is mounted, and the class that styles it there. The
# agent picks one; anything else falls back to the top slot.
PLACEMENTS = {
    "top": "qb-sketch-top",
    "above-chat": "qb-sketch-main",
    "beside-chat": "qb-sketch-main qb-sketch-beside",
    "background": "qb-sketch-bg",
}
DEFAULT_PLACEMENT = "top"


def _placement_for(kind, version=None):
    if kind == "preview":
        raw = store.candidate_part("sketchplace")
    elif kind == "pinned":
        raw = store.history_part("sketchplace", version)
    else:
        raw = store.current_part("sketchplace")
    raw = (raw or "").strip()
    return raw if raw in PLACEMENTS else DEFAULT_PLACEMENT


def _parts_for(kind, version=None):
    """The decor markup and sketch frame that belong with a given stylesheet.

    Safe mode gets neither: it is the guaranteed-plain view, so it shows only
    the fixed page and base.css.
    """
    if kind == "safe" or not (ENABLE_HTML or ENABLE_SKETCH):
        return "", "", ""
    if kind == "preview":
        decor = store.candidate_part("decor")
        sketch = store.candidate_part("sketch")
        src = "/sketch.html?preview=1"
    elif kind == "pinned":
        decor = store.history_part("decor", version)
        sketch = store.history_part("sketch", version)
        src = "/sketch.html?v=%d" % version
    else:
        decor = store.current_part("decor")
        sketch = store.current_part("sketch")
        src = "/sketch.html?v=live"

    decor_html = decor if (ENABLE_HTML and decor.strip()) else ""
    frame = ""
    if ENABLE_SKETCH and sketch.strip():
        placement = _placement_for(kind, version)
        # sandbox WITHOUT allow-same-origin: the frame gets an opaque origin, so
        # its script cannot reach this document, its storage or its cookies.
        # Adding allow-same-origin here would undo the entire containment.
        frame = (
            '<iframe id="qb-sketch" class="%s" title="Decorative sketch" '
            'sandbox="allow-scripts" referrerpolicy="no-referrer" src="%s"></iframe>'
            % (PLACEMENTS[placement], src)
        )
    return decor_html, frame, placement if frame else ""


def _page(safe_mode=False, preview=False, pinned=None):
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if safe_mode:
        sheet = ""
        mode = "safe"
    elif preview:
        sheet = '<link rel="stylesheet" href="/candidate.css">'
        mode = "preview"
    elif pinned is not None:
        # A design chosen from the gallery. Deliberately not /style.css, and
        # app.js refuses to hot-swap in this mode, so the visitor keeps this
        # look no matter what anyone else publishes meanwhile.
        sheet = '<link rel="stylesheet" href="/history/%d.css">' % pinned
        mode = "pinned"
    else:
        v = store.read_meta().get("version", 0)
        sheet = '<link rel="stylesheet" href="/style.css?v=%s">' % v
        mode = "live"
    fonts = '<link rel="stylesheet" href="/fonts.css">' if ENABLE_FONTS else ""
    decor, frame, placement = _parts_for(mode, pinned)
    top = frame if placement in ("top", "background") else ""
    main = frame if placement in ("above-chat", "beside-chat") else ""
    return (
        html.replace("<!--FONTS_CSS-->", fonts)
        .replace("<!--CUSTOM_STYLE-->", sheet)
        .replace("<!--DECOR-->", decor)
        .replace("<!--SKETCH_TOP-->", top)
        .replace("<!--SKETCH_MAIN-->", main)
        .replace("{{MODE}}", mode)
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    safe = request.query_params.get("safe") in ("1", "true", "yes")
    if safe:
        return HTMLResponse(_page(safe_mode=True))

    style = request.query_params.get("style")
    if style is not None:
        # An unknown or aged-out version simply loses the parameter rather than
        # erroring - the link may be old, and the plain page is always correct.
        try:
            wanted = int(style)
        except ValueError:
            wanted = None
        if wanted is None or store.history_css(wanted) is None:
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(_page(pinned=wanted))

    return HTMLResponse(_page())


@app.get("/history/{version}.css")
async def history_css(version: int):
    css = store.history_css(version)
    if css is None:
        raise HTTPException(status_code=404, detail="that design has aged out")
    return Response(
        content=css, media_type="text/css", headers={"Cache-Control": "no-store"}
    )


@app.get("/gallery", response_class=HTMLResponse)
async def gallery():
    entries = store.history_entries()
    # Which card is live is decided by content, not by version number: reset and
    # rollback bump the counter without writing a history entry, so comparing
    # versions can badge the wrong design (or badge one when the live look is
    # the bland default and matches nothing at all).
    live_css = store.current_css()
    if not entries:
        items = (
            '<p id="gal-empty">No designs yet. Ask for one on the '
            '<a href="/">front page</a>.</p>'
        )
    else:
        cards = []
        for e in entries:
            v = e["version"]
            is_live = store.history_css(v) == live_css
            label = escape(e["prompt"] or "(no prompt recorded)")
            when = (
                datetime.fromtimestamp(e["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                if e["ts"]
                else ""
            )
            shot = (
                '<img class="gal-shot" loading="lazy" alt="Screenshot of design %d" '
                'src="/gallery/%d.jpg">' % (v, v)
                if e["has_shot"]
                else '<p class="gal-noshot">no screenshot kept for this one</p>'
            )
            cards.append(
                '<a class="gal-card%s" href="/?style=%d">%s'
                '<span class="gal-meta"><span class="gal-v">#%d%s%s</span>'
                '<span class="gal-prompt">%s</span></span></a>'
                % (
                    " gal-live" if is_live else "",
                    v,
                    shot,
                    v,
                    " &middot; live now" if is_live else "",
                    (" &middot; " + when) if when else "",
                    label,
                )
            )
        items = '<div id="gal-grid">' + "".join(cards) + "</div>"
    html = (STATIC_DIR / "gallery.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("<!--GALLERY_ITEMS-->", items))


@app.get("/gallery/{version}.jpg")
async def gallery_shot(version: int):
    data = store.shot_bytes(version)
    if data is None:
        raise HTTPException(status_code=404, detail="no screenshot for that design")
    return Response(
        content=data,
        media_type="image/jpeg",
        # Immutable per version, so it may be cached hard.
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/preview", response_class=HTMLResponse)
async def preview(request: Request):
    """Internal: the candidate stylesheet, for the validator and screenshots."""
    _require_local(request)
    return HTMLResponse(_page(safe_mode=False, preview=True))


@app.get("/style.css")
async def style_css():
    return Response(
        content=store.current_css(),
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/candidate.css")
async def candidate_css(request: Request):
    _require_local(request)
    return Response(
        content=store.candidate_css(),
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


SKETCH_SKELETON = (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<style>html,body{margin:0;padding:0;overflow:hidden;background:transparent}</style>"
    "</head><body>%s</body></html>"
)

# The sketch is contained by origin, not by sanitising its script: an opaque
# origin plus this policy means it can render and animate but cannot reach the
# page, storage, or the network. frame-ancestors keeps it from being embedded
# anywhere but here.
SKETCH_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)


@app.get("/sketch.html", response_class=HTMLResponse)
async def sketch_html(request: Request):
    if not ENABLE_SKETCH:
        raise HTTPException(status_code=404, detail="sketches are disabled")
    # The client appends a cache-busting v= on every publish; "live" and a bare
    # version number both mean "whatever is current".
    which = request.query_params.get("v", "live")
    if which.isdigit() and int(which) == store.read_meta().get("version", 0):
        which = "live"
    if request.query_params.get("preview"):
        _require_local(request)
        body = store.candidate_part("sketch")
    elif which == "live":
        body = store.current_part("sketch")
    else:
        try:
            body = store.history_part("sketch", int(which))
        except ValueError:
            body = ""
    if not body.strip():
        raise HTTPException(status_code=404, detail="no sketch")
    return HTMLResponse(
        SKETCH_SKELETON % body,
        headers={"Content-Security-Policy": SKETCH_CSP, "Cache-Control": "no-store"},
    )


@app.get("/fonts.css")
async def fonts_css():
    """@font-face declarations, written by us rather than the agent.

    Handing the families over ready-made means @font-face can stay banned in the
    sanitiser: the agent never gets to point a font `src` anywhere.
    """
    if not ENABLE_FONTS:
        return Response(content="/* fonts disabled */", media_type="text/css")
    return Response(
        content=resources.font_face_css(),
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/fonts/{filename}")
async def font_file(filename: str):
    if not ENABLE_FONTS:
        raise HTTPException(status_code=404, detail="fonts are disabled")
    path = resources.FONT_FILES.get(filename)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="unknown font")
    return FileResponse(
        path,
        media_type="font/ttf",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/assets/{name}")
async def asset_file(name: str):
    if not ENABLE_ASSETS:
        raise HTTPException(status_code=404, detail="assets are disabled")
    path = resources.asset_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="unknown asset")
    return FileResponse(
        path,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/base.css")
async def base_css():
    return FileResponse(STATIC_DIR / "base.css", media_type="text/css")


@app.get("/guard.js")
async def guard_js():
    return FileResponse(STATIC_DIR / "guard.js", media_type="application/javascript")


@app.get("/app.js")
async def app_js():
    return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")


@app.post("/api/prompt")
async def api_prompt(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected JSON")

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt required")
    prompt = prompt.strip()[:MAX_PROMPT_CHARS]

    wait = _rate_limited(_client_ip(request))
    if wait:
        return JSONResponse(
            {"ok": False, "error": "Slow down - try again in %ds." % wait}, status_code=429
        )

    ok, message = ORCHESTRATOR.submit(prompt)
    return JSONResponse({"ok": ok, "message": message, **ORCHESTRATOR.status()})


@app.get("/api/design")
async def api_design():
    """The non-stylesheet parts of the live design.

    Publishing used to swap only /style.css, so decoration and the sketch frame
    appeared, changed or vanished only on a manual reload.
    """
    decor, frame, placement = _parts_for("live")
    meta = store.read_meta()
    return {
        "version": meta.get("version", 0),
        "decor": decor,
        "sketch": bool(frame),
        "placement": placement,
        "sketchClass": PLACEMENTS.get(placement, ""),
        "sketchSrc": "/sketch.html?v=live",
    }


@app.get("/api/status")
async def api_status():
    meta = store.read_meta()
    return {
        **ORCHESTRATOR.status(),
        "last_prompt": meta.get("prompt", ""),
        "updated": meta.get("updated", 0),
        "viewers": bus.subscriber_count(),
    }


@app.get("/events")
async def events(request: Request):
    async def gen():
        with bus.Subscription() as queue:
            yield bus.sse({"type": "hello", "replay": bus.replay(), **ORCHESTRATOR.status()})
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield bus.sse(event)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"


def _require_admin(token):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")


@app.post("/admin/reset")
async def admin_reset(x_admin_token: str = Header(default="")):
    _require_admin(x_admin_token)
    version = store.reset()
    bus.clear_replay()
    bus.publish({"type": "reload", "version": version})
    bus.publish({"type": "message", "role": "system", "text": "Styles reset to default."})
    return {"ok": True, "version": version}


@app.post("/admin/rollback")
async def admin_rollback(x_admin_token: str = Header(default="")):
    _require_admin(x_admin_token)
    version = store.rollback()
    bus.publish({"type": "reload", "version": version})
    bus.publish({"type": "message", "role": "system", "text": "Rolled back to the previous look."})
    return {"ok": True, "version": version}
