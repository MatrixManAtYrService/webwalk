# trainee-sim

A test harness that drives a Selenium browser through **training material** and
captures what it sees, so training flows can be codified into **rules** — and
eventually into tests that prove the material has no dead ends (a user can always
get from any state to the end).

It is deliberately not a rote macro. It **observes where it is** and decides what
to do, so a flow can be resumed from the beginning *or* the middle. Many flows in
this training are near-copies of each other (e.g. they all start by logging in as
a character named Alice, then diverge), and observe-driven matching handles that
naturally.

## Architecture

```
   your machine (client)                        target machine (server)
   ─────────────────────         HTTP           ────────────────────────
   trainee-sim <verb>   ───────────────────▶    trainee-sim serve
   (stateless CLI)                              (FastAPI; owns ONE live
   engine / matcher                              Selenium browser in memory)
                                                 └─ observe · act · telemetry
```

- The **server** runs on the machine that has the browser and can reach the
  training. It owns a single long-lived Chrome (a **persistent profile**, so the
  logged-in session survives restarts). One process, all live state in memory.
- The **CLI** is stateless: one HTTP request per invocation. It runs anywhere on
  the LAN and finds the server via `TS_SERVER_URL`.
- The browser can be driven two ways: **observe** it (capture state) or **act**
  on it (click / type / navigate). Everything funnels through those primitives.

Modeled on the sibling repos `../ebb-agent-data-access` and `../finmap` (same
stateless-CLI / live-session-server split).

## Quick start

```bash
# on the target machine
uv run trainee-sim serve                        # binds 0.0.0.0:22133

# on your machine
export TS_SERVER_URL=http://<target-host>:22133
uv run trainee-sim check                        # {"healthy": true}
uv run trainee-sim connect https://…            # opens the browser on the target
#   … complete login/MFA by hand in that window …
uv run trainee-sim telemetry on                 # start recording
uv run trainee-sim where                         # which known state are we in?
uv run trainee-sim observe --save --label here   # capture full page state
```

`login.nu` is a worked example: it drives the whole Fiserv → Secure Code Warrior
login end-to-end (pausing once for the human MFA code) and lands on the course.

## Core concepts

### The telemetry gate

Auth is done by hand and must **not** be captured. Telemetry starts **OFF**:
`connect`, log in and load the training in the headed window (nothing is
observed), then `telemetry on`. Off means the observer simply doesn't look — it
never records-then-discards, so credentials can't leak into the capture log. The
password step additionally supports `--secret` (redacted everywhere) and `--env`
(value from an env var, out of shell history).

### Captures

`observe` and `step` capture full page state — url, title, the interactive
elements, the visible text of each nested frame, open windows, and the
**network traffic** the step produced (see below). Interactive elements include
hidden **checkbox/radio** state (`checked` plus a nearby `label`), so
step-completion boxes are readable even when the real input is visually hidden
behind an icon. Captures are:

- written server-side to `$TS_DATA_DIR/captures/` **only while telemetry is on**;
- fetched back over HTTP and written **locally** to `./data/captures/` for easy
  reading, when telemetry is on *or* you pass `--save`.

### Network capture

While telemetry is on, each capture also carries a `network` key: the HTTP
requests/responses (method, url, status, redacted headers, JSON bodies) and
websocket frames the page fired since the previous snapshot — so the training's
API can be learned, not just its UI. It's the same **telemetry gate**: nothing
is retained while telemetry is off (auth traffic never lands), and auth
headers/cookies are redacted even when it's on. `trainee-sim network` prints the
buffered traffic without consuming it, for a quick look between steps.

Under the hood this rides on Chrome's DevTools performance log (Selenium has no
live network-event API the way finmap's Playwright does), host- and
type-filtered to the API calls and bounded by a ring buffer. Tune with the
`TS_NETWORK_*` env vars. See `docs/network-capture-design.md`.

### Windows and frames

Selenium only ever sees the one window/frame it's focused on.

- **Windows/tabs:** `windows` lists them; `--window <index|latest|first|substr>`
  targets one. New tabs (e.g. "launch course") are not followed automatically.
- **Frames/iframes:** `observe` recurses into nested iframes; every element
  carries a `frame` path (`[]` = top, `[0]`, `[3,1]` = nested), and every frame
  entry carries its visible `text` (the top-level `dom` is the outer document
  only, so a frame's content — e.g. the mission instructions checklist in
  `[0,0]` — is otherwise invisible). Actions take `--frame "3/1"` to act inside a
  frame. The course content lives behind frames, so this matters constantly.

### Selectors

`--by css` (default) or `--by xpath`. Gotcha: **XPath text matching uses the raw
DOM text, not the CSS-rendered text.** A button labeled "Next" whose CSS
lowercases it to "next" is `<button>Next</button>` in the DOM — target a stable
attribute (`button[translate="NEXT"]`), not the visible text.

### Settling

SPA pages render asynchronously, so an immediate after-snapshot can catch a
half-built page. Let it settle with `--wait-after <selector>` (poll until it's
visible) or `--settle <seconds>`.

### Clicking non-interactable elements

Some real controls live in side-overlays / hover menus that Selenium won't
hit-test — the mocked apps' menu **Logout** (an `<a id="menu-link-logout">` in
frame `[0,0,0]`), for one. When a `click` finds its element but the native click
raises *not interactable* / *click intercepted*, it automatically retries as a
DOM-level click (the app's own handlers fire just the same). The action result
reports `js_click: true` so a fallback is distinguishable from a native click.

### Errors

A failed action returns a readable **422** (`could not click css='…' in frame
[…]: NoSuchElementException: …`), and the CLI exits non-zero so a scripted chain
halts instead of barreling on.

## The engine (in progress)

A course is described by plain-data **state signatures** in `courses/*.yaml`: how
to recognize "where we are" from an `observe` payload (url/title substrings +
element predicates by frame/text/id/href). `trainee-sim where` observes the live
browser and reports the matching state(s) — the foundation for resuming a flow
from anywhere.

**Network capture** has landed (see below and `docs/network-capture-design.md`):
each step now records the API traffic it produced, the groundwork for rules that
fire requests directly instead of puppeting the UI.

**Next: the rule runner** — `docs/rule-runner-design.md`. It turns the state
signatures (the `when`) + actions (`do`) into a graph the engine *walks*:
observe → match state → act (UI **or** API) → verify → repeat, from any starting
point (so a half-finished course resumes rather than restarts), preferring the
API where a captured request shape exists and falling back to the UI otherwise.
The endgame is proving the training has **no dead ends** — every state can reach
the end. Reusable parameterized sub-flows (the "log in as Alice" macro) live here
too. Read that doc before starting that work.

## Command reference

| Command | What it does |
|---|---|
| `serve` | Start the server (owns the browser) |
| `check` | Is the server up? |
| `connect [url]` | Open the browser at a URL (session persists) |
| `telemetry on\|off` | Gate the observer |
| `status` | Connection + telemetry state |
| `windows` / `switch <t>` | List / focus browser tabs |
| `observe [--save]` | Capture current state (recurses frames) |
| `network` | Show captured training API traffic (gated) |
| `where [--states f]` | Report which known course-state we're in |
| `step <action>` | Low-level: click / type / navigate + capture |
| `click --element <sel>` | Click (friendlier `step click`) |
| `type <v> --into <sel>` | Type (`--secret`, `--env`) |
| `wait-for` | Block until a url/element appears (`--frame`) |
| `shutdown` | Close the browser session |

Actions share: `--window`, `--frame`, `--by css\|xpath`, `--settle`,
`--wait-after[/-timeout]`, `--save`.

## Configuration

| Env var | Meaning | Default |
|---|---|---|
| `TS_SERVER_URL` | Where the CLI finds the server | `http://localhost:22133` |
| `TS_WEBDRIVER_URL` | Remote WebDriver / Grid endpoint; empty = local Chrome | *(empty)* |
| `TS_START_URL` | URL `connect` opens by default | `about:blank` |
| `TS_DATA_DIR` | Server-side captures + browser profile | `~/.local/share/trainee-sim` |
| `TS_LOCAL_DATA_DIR` | Client-side fetched captures | `./data` |
| `TS_BROWSER_PROFILE` | Override the persistent Chrome profile dir | `$TS_DATA_DIR/browser-profile` |
| `TS_BROWSER_BINARY` | Chromium-family binary to drive (Chrome/Chromium/Brave/Edge) | *(default Chrome)* |
| `TS_HEADLESS` | Headless browser (unattended replay only; login needs headed) | off |
| `TS_TIMEOUT` | Client HTTP timeout (seconds) | 120 |
| `TS_NETWORK_CAPTURE` | Enable Chrome's perf log for network capture | on |
| `TS_NETWORK_HOSTS` | Comma-sep host substrings to record (empty = all) | *(empty)* |
| `TS_NETWORK_MAX_EVENTS` | Ring-buffer cap on retained events | 500 |
| `TS_NETWORK_MAX_BODY` | Per-body truncation cap (bytes) | 65536 |

## Layout

- `trainee_sim/cli.py` — the Typer CLI (all verbs).
- `trainee_sim/client.py` — CLI→server HTTP helper (`TS_SERVER_URL`, 499 handshake).
- `trainee_sim/server/` — FastAPI app (`main.py`), the browser session
  (`session.py`), the gated capture writer (`capture.py`), the gated network
  recorder (`recorder.py`), config, logging.
- `trainee_sim/engine/` — the matcher (`matcher.py`); rule runner to come.
- `courses/` — per-course state signatures (and rules, eventually).
- `login.nu` — worked example: scripted login to the Secure Code Warrior course.
- `docs/` — design docs: `network-capture-design.md` (shipped) and
  `rule-runner-design.md` (the next build — read before starting the engine).
  Three TODO docs scope the work after that: `reachability-design.md` (prove no
  dead ends — the original mission), `websocket-settling-design.md` (verify a
  step took effect through the Phoenix/Socket.IO channels) and
  `mbt-model-format-design.md` (export the state graph in a standard MBT shape).

## Status

Working (validated end-to-end against the real Secure Code Warrior training):
the client/server spine, telemetry gate, window + nested-frame handling
(per-frame text and hidden checkbox/step-completion state now captured),
scriptable actions with settle/readable-errors (including a DOM-level click
fallback for overlay/menu controls), local capture fetch, the "where am I"
state detector, and gated network (API) capture — including SCW's REST calls
*and* its Phoenix websocket. Next: the **rule runner**
(`docs/rule-runner-design.md`) — walk the state graph, UI or API, from any
starting point, toward proving there are no dead ends.
