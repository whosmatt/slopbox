# slopbox

A website with one text box in the middle. Type how the page should look, and an
incompetent AI agent rewrites the site's stylesheet to grant the wish. The new look is live for
everyone, until the next visitor asks for something else.

The whole point is that the agent is untrusted. Its instructions arrive from a
public input box, so prompt injection is assumed rather than prevented, and the
design question is not "how do we stop the model misbehaving" but "what is the
worst a misbehaving model can actually do here". The answer is: publish an ugly
stylesheet.

## Running it

```bash
cp docker-compose.yml.example docker-compose.yml   # then fill in the two secrets
docker compose up -d
```

Open <http://localhost:8000>. That pulls the published image; there is nothing to
build locally.

`docker-compose.yml` holds the API token inline and is gitignored for that
reason - `docker-compose.yml.example` is the tracked template. The flip side is
that the live file is not version-controlled, so mirror any setting you change
back into the example.

To roll out a newer build, `pull_policy: always` means a plain
`docker compose up -d` is enough.

## How a prompt becomes a stylesheet

```
visitor prompt
  -> queue (serial; a prompt arriving mid-run steers the running agent)
  -> agent loop: Qwen3.6 + 5 tools, progress streamed to every browser via SSE
       write_css   -> CSS SANITISER      structural allowlist; rejects or rewrites
       screenshot  -> headless Chromium  the model looks at its own work
       publish     -> BROWSER VALIDATOR  refuses to ship an unusable page
  -> /style.css, hot-swapped in every connected browser
  -> guard.js re-checks continuously in each real visitor's browser
```

### Layer 1: the sanitiser (`app/css_guard.py`)

CSS is parsed with tinycss2, walked token by token against an allowlist, and
then **re-serialised from the tree that passed** - so what lands on disk is only
what was understood, not the original text. Rejected: `@import`, `@font-face`,
`@namespace`, any `url()` that is not an inline base64 raster image,
`expression()`, `-moz-binding`, `behavior`, `element()`, and any selector
touching the safety overlay. Only `@media`, `@supports`, `@keyframes`, `@layer`
and `@container` are allowed through.

The effect is that a published stylesheet cannot make a network request. That
closes the exfiltration channel CSS otherwise offers (attribute-selector probes
that leak page contents one `url()` at a time), and it is why there are no web
fonts.

### Layer 2: the validator (`app/validator.py`)

Nothing goes live until a real Chromium says the page is still usable. The
candidate is rendered at 1280x800 and 390x844, and at 0s, 0.6s, 1.8s and 3.2s
after load - the time sampling is what stops an animation from parking the input
off-screen just after a naive one-shot check. At every sample the prompt input
must be rendered, at least 80x20px, at least 60% inside the viewport, not
blurred or transparent, hit-testable at its centre point, and readable
(>= 1.7:1 contrast against its own background). Then it must actually take
keyboard focus and accept typed text.

The chat log is gated the same way, because watching the agent work is half the
point: `#chat` must be visible and, when it holds messages, at least one must be
legible - real contrast, real height. `/preview` seeds the log with sample
messages so this is deterministic to check, and so the model's own screenshots
show what the transcript will actually look like.

Two subtleties worth keeping if you touch this code:

- `document.elementFromPoint` returning an **ancestor** of the input is a
  failure, not a pass. That is exactly what a full-screen `body::after` overlay
  looks like.
- Actionability is checked with `focus` + `fill`, deliberately **not**
  `page.click()`. Playwright's click waits for the element to stop moving, which
  would outlaw every animation even though a human can click a wobbling box
  perfectly well.

Failures are handed back to the model in prose, with screenshots, and it gets
another try.

### Layer 3: the guard (`static/guard.js`)

The validator checks two viewports on one browser. Real visitors have their own
window size, fonts, zoom and rendering quirks, so the same check runs client-side
about once a second. On failure it renders a fallback overlay - its own working
input, a one-click "make it plain and readable", and a link to safe mode.

The overlay lives in a **closed shadow root** whose host is styled through the
CSSOM with `!important`. Inline important declarations outrank any author
stylesheet rule, so published CSS cannot suppress it; selectors naming it are
refused by the sanitiser anyway. If the page hid itself wholesale, the guard also
forces `html`/`body` back into rendering the same way.

`/?safe=1` always serves the page with no custom CSS at all.

### Layer 4: CSP

`default-src 'none'` with `img-src 'self' data:`, `font-src 'self'` and
`connect-src 'self'`. Even if the sanitiser were bypassed, the browser refuses
the outbound request.

## The model

`Qwen/Qwen3.6-35B-A3B-FP8` on Hetzner's OpenAI-compatible endpoint. It is a
reasoning model, and two things about that shaped the code:

- It streams deliberation in a `reasoning` field and very often leaves `content`
  empty entirely when calling a tool. So the visible chat log is fed by the
  reasoning stream - that *is* the progress indicator. It runs to thousands of
  characters, so each thinking message is a collapsed `<details>` whose summary
  carries a live tail of the stream; click to read the whole thing.
- Left alone it will spend thousands of tokens deciding how to say hello.
  `REASONING_EFFORT=low` keeps it deliberating usefully without stalling the UI.

Context is bounded rather than trusted to stay small: every run starts fresh,
old screenshots are replaced by a placeholder so only the newest image is ever
in context, and the transcript is mechanically compacted once it exceeds
`MAX_CONTEXT_CHARS`, keeping the system prompt, a note about what was dropped,
and the tail. `MAX_STEPS` ends a run that will not converge.

## Not accumulating garbage

Disk footprint is bounded by construction, not by a cleanup job:

- `/data` holds exactly `current.css`, `last_good.css`, `meta.json` and a
  history ring of at most `HISTORY_KEEP` (20) stylesheets, pruned on every
  write. With the 100KB stylesheet cap that is a hard ceiling of ~2MB.
- Screenshots are passed around as bytes and never touch the filesystem.
- Chromium is one long-lived instance, not a process per request, with `/tmp` on
  a 256MB tmpfs that is wiped on restart.
- The SSE replay buffer and the rate-limit table are both size-capped in memory.

## Endpoints

| Route | Purpose |
| --- | --- |
| `GET /` | the site (`?safe=1` for no custom CSS) |
| `GET /style.css` | the live, agent-written stylesheet |
| `POST /api/prompt` | `{"prompt": "..."}` - rate limited per IP |
| `GET /events` | SSE stream of agent progress |
| `GET /api/status` | queue and version state |
| `POST /admin/reset` | back to bland; needs `X-Admin-Token` |
| `POST /admin/rollback` | restore the previous stylesheet |
| `GET /preview`, `/candidate.css` | internal, localhost peer only |

## Configuration

Everything is set in `docker-compose.yml` under `environment:`, and
`app/config.py` carries the same defaults - so any line there can be deleted
rather than tuned. Worth knowing:

- `TRUSTED_PROXIES` - empty by default, meaning `X-Forwarded-For` is **not**
  believed. Set it to your reverse proxy's address if you put one in front,
  otherwise every request looks like it comes from the proxy and shares one rate
  limit bucket. Never trust the header unconditionally: it would let anyone mint
  a fresh identity per request and walk through the limiter.
- `RATE_LIMIT_COUNT` / `RATE_LIMIT_WINDOW_SEC` - prompts per IP per window.
- `ALLOWED_URL_PREFIXES` - override at your own risk; the default (inline raster
  data URIs only) is what makes exfiltration impossible.
- `SELF_URL` - normally unset. The validator's browser runs inside this
  container, so the address it needs is loopback plus our own port, and the port
  is learned from the socket uvicorn bound (`scope["server"]`). It is
  deliberately *not* derived from `request.base_url` or the `Host` header:
  those are attacker-controlled, and this is the URL whose rendering gets
  screenshotted and handed to the model, so a forged host would be an SSRF with
  a prompt-injection payload attached.

## Tests

The tests are mounted into the container rather than baked into the image.

```bash
docker compose exec slopbox python -m tests.test_guard      # sanitiser, 39 cases
docker compose exec slopbox python -m tests.test_steering   # queue/context, 12 cases
docker compose exec slopbox python -m tests.test_validator  # browser gate, 20 cases
```

`test_guard` fires the CSS attack payloads that matter - `@import`,
protocol-relative and `javascript:` URLs, SVG and font data URIs,
`expression()`, `-moz-element()`, guard-selector tampering - and asserts that
genuinely wild-but-fine stylesheets still pass. `test_steering` also covers a sharp edge in the tool loop: arguments that do not
parse must never enter the transcript. The transcript is resent on every request
and the server parses tool-call arguments when applying its chat template, so one
truncated `write_css` - the model running out of output budget mid-CSS, leaving
`{"css": "body{color:red` behind - used to make every later request in that run
fail with a 400. It is now replaced with `{}` and the model is told it was cut
off, so the run recovers. The advertised CSS size budget is derived from
`MAX_TOKENS` for the same reason: advertising the 100KB sanitiser cap invited a
stylesheet that could not fit in one response.

`test_steering` covers the queue: that a prompt arriving too late to be consumed
becomes its own job instead of being silently dropped, and that a follow-up run
is told what the previous attempt did so "try again" has a referent.

`test_validator` publishes sixteen
different ways of breaking the page (input hidden, zero-size, off-screen,
transparent, covered, invisible text, blurred, animated off-screen, `body`
hidden, chat log hidden, collapsed, transparent or unreadable) and asserts the
gate catches each one, plus four deliberately garish stylesheets - including one
with a permanently animating input - that must pass.

One trap worth knowing if you add fixtures: base.css uses `.msg.user` (0,2,0),
so a bare `.msg { background: ... }` in a candidate loses the cascade. A fixture
meant to break the chat log needs an ID in the selector, or it will be caught
being legible and look like a validator miss.

## Building

Pushes to `main` and `dev` build and publish `whosmatt/slopbox` to Docker Hub
(`.github/workflows/build-push-action.yml`), multi-arch for `linux/amd64` and
`linux/arm64`, with a build-provenance attestation. Two repository secrets are
required: `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.

The sanitiser suite gates the build - it is the security boundary for everything
the agent can write, it needs no browser and no secrets, and it runs in seconds.
The validator suite is not in CI because it needs a live container with Chromium;
run it locally as above. If the project is ever renamed, the image name appears
in exactly two places in that workflow: `images:` and the attestation's
`subject-name:`.

Emulated arm64 builds stay cheap despite the image size, because only
`pip install` and a small `chown` run under QEMU - Chromium and its system
libraries come from the base image, which is already multi-arch.

## License

WTFPL. See [LICENSE](LICENSE).

## Known limits

- The agent can only write CSS, so it cannot add elements or change copy. Asking
  for "a second input box" will not work, by design.
- No web access and no web fonts. Both are exfiltration channels in a page whose
  stylesheet is written by an untrusted party, so they are off rather than
  filtered. A font allowlist would be the natural first extension if the loss of
  typography matters more than the closed channel.
- One agent at a time, and one shared stylesheet: this is a single global page,
  not per-visitor styling.
