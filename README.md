# webwalk

A test harness that drives a Selenium browser through a web flow and **observes
where it is** to decide what to do next — so a flow can be resumed from the
beginning *or* the middle. It captures what it sees so a flow can be codified into
**rules**, then *walked* autonomously: observe → match state → act → verify.

`webwalk` is the **flow-agnostic core** (the browser session, the observe/act
primitives, the rule engine). A **flow** teaches it one particular site: its
state signatures, rules, and any specialized handlers. Each flow ships its own
CLI. The first flow is **Secure Code Warrior** (`scw-sim`); `example` is a
template to copy for the next one.

## Architecture

```
   your machine (client)                        target machine (server)
   ─────────────────────         HTTP           ────────────────────────
   webwalk  <verb>      ───────────────────▶    webwalk  serve   (or a flow's
   scw-sim  <verb>                              (FastAPI; owns ONE live         serve)
   (stateless CLIs)                              Selenium browser in memory)
   engine / matcher / runner                     └─ observe · act · telemetry
```

- The **server** runs on the machine that has the browser and can reach the site.
  It owns a single long-lived Chrome (a **persistent profile**, so the logged-in
  session survives restarts). One process, all live state in memory.
- The **CLIs** are stateless: one HTTP request per invocation. They run anywhere
  on the LAN and find the server via `TS_SERVER_URL`.
- The core is **flow-agnostic**. A flow extends it two ways: **sub-flows** in the
  engine (a "one level down" activity like a mission's step checklist) and
  **server plugins** (a snapshot observer + endpoints, e.g. SCW's challenge
  scraper). Neither is baked into the core.

Modeled on the sibling repos `../ebb-agent-data-access` and `../finmap` (same
stateless-CLI / live-session-server split).

## Flows and their CLIs

| CLI | Flow | Adds on top of the core verbs |
|---|---|---|
| `webwalk` | *(none — the bare core)* | just the flow-agnostic browser verbs |
| `scw-sim` | Secure Code Warrior | `run` walker, `where`, `mission`, `challenge` |
| `example-sim` | template | `run`, `where` (copy it to start a new flow) |

Every flow CLI includes **all** the core browser verbs (connect / observe / step
/ …) plus its own `serve` that loads that flow's server plugins. So for SCW you
can drive everything through `scw-sim`; `webwalk` is the bare tool for a new flow
before it has any rules.

### Adding a flow

1. `cp -r webwalk/flows/example webwalk/flows/<yourflow>` and rename it.
2. Author `data/states.yaml` — state signatures + rules — by driving the browser
   (`connect`, `observe --brief`, then encode what you did as a rule).
3. Add a `<yourflow>-sim` console script in `pyproject.toml` →
   `webwalk.flows.<yourflow>.cli:main`.
4. Optional: a `SubFlow` (engine/subflow.py) for a repeated "one level down"
   activity, and/or an `observers.py` server plugin for a custom snapshot scraper
   (pass `server_plugins=` to `build_app`). SCW (`webwalk/flows/scw/`) is the
   worked example of both.

## Quick start (SCW)

```bash
# on the target machine
uv run scw-sim serve                             # binds 0.0.0.0:22133, loads the SCW plugin

# on your machine
export TS_SERVER_URL=http://<target-host>:22133
uv run scw-sim check                             # {"healthy": true}
uv run scw-sim connect https://…                 # opens the browser on the target
#   … complete login/MFA by hand in that window …
uv run scw-sim telemetry on                      # start recording
uv run scw-sim where                             # which known course-state are we in?
uv run scw-sim run --once                        # take one walk step (confirm first)
uv run scw-sim run -y --brief                    # walk autonomously, compact output
```

`login.nu` is a worked example: it drives the Fiserv → Secure Code Warrior login
end-to-end (pausing once for the human MFA code) and lands on the course.

## Core concepts

### The telemetry gate

Auth is done by hand and must **not** be captured. Telemetry starts **OFF**:
`connect`, log in and load the site in the headed window (nothing is observed),
then `telemetry on`. Off means the observer simply doesn't look — it never
records-then-discards, so credentials can't leak into the capture log. The
password step additionally supports `--secret` (redacted everywhere) and `--env`
(value from an env var, out of shell history).

### Captures

`observe` and `step` capture full page state — url, title, the interactive
elements, the visible text of each nested frame, open windows, the **network
traffic** the step produced (see below), and any **flow snapshot extensions**
(e.g. SCW folds a `challenge` decision view in). Interactive elements include
hidden **checkbox/radio** state (`checked` plus a nearby `label`), so
step-completion boxes are readable even when the real input is visually hidden.
Captures are:

- written server-side to `$TS_DATA_DIR/captures/` **only while telemetry is on**;
- fetched back over HTTP and written **locally** to `./data/captures/` for easy
  reading, when telemetry is on *or* you pass `--save`.

### Network capture

While telemetry is on, each capture also carries a `network` key: the HTTP
requests/responses (method, url, status, redacted headers, JSON bodies) and
websocket frames the page fired since the previous snapshot — so the site's API
can be learned, not just its UI. Same **telemetry gate**: nothing is retained
while telemetry is off (auth traffic never lands), and auth headers/cookies are
redacted even when it's on. `<cli> network` prints the buffered traffic without
consuming it. Rides on Chrome's DevTools performance log, host/type-filtered and
ring-buffered; tune with the `TS_NETWORK_*` env vars. See
`docs/network-capture-design.md`.

### Windows and frames

Selenium only ever sees the one window/frame it's focused on.

- **Windows/tabs:** `windows` lists them; `--window <index|latest|first|substr>`
  targets one. New tabs (e.g. "launch course") are not followed automatically.
- **Frames/iframes:** `observe` recurses into nested iframes; every element
  carries a `frame` path (`[]` = top, `[0]`, `[3,1]` = nested), and every frame
  entry carries its visible `text`. Actions take `--frame "3/1"` to act inside a
  frame. Course content lives behind frames, so this matters constantly.

### Selectors, settling, clicking, errors

- `--by css` (default) or `--by xpath`. Gotcha: **XPath text matching uses the
  raw DOM text, not the CSS-rendered text** — target a stable attribute, not the
  visible label.
- SPA pages render asynchronously; let them settle with `--wait-after <selector>`
  or `--settle <seconds>`.
- When a `click` finds its element but the native click raises *not interactable*
  / *intercepted*, it retries as a DOM-level click (reported as `js_click: true`).
- A failed action returns a readable **422** and the CLI exits non-zero so a
  scripted chain halts.

## The engine

A flow is described by plain-data **state signatures** and **rules** in its
`data/states.yaml`:

- A **state** recognizes "where we are" from an `observe` payload (url/title
  substrings + element predicates by frame/text/id/href). `<cli> where` reports
  the matching state(s).
- A **rule** is `when` (a state) + `do` (a `ui`/`http`/`eval` action) + `expect`
  (the destination state; omit while still discovering it). `<cli> run` walks the
  graph: observe → match → act → verify → repeat, from any starting point (a
  half-finished flow resumes rather than restarts), preferring the API where a
  captured request shape exists and falling back to the UI.

**Sub-flows** handle a repeated activity one level down from page states — for SCW
that's a mission's step checklist, a challenge decision, or a coding-lab paste.
They're pluggable (`webwalk/engine/subflow.py`): the core dispatches to whichever
sub-flow a rule's `do` names; the SCW ones live in `webwalk/flows/scw/subflows.py`.

The endgame is proving a flow has **no dead ends** — every state can reach the
end. See `docs/rule-runner-design.md` and the follow-on `docs/*-design.md`.

## Command reference

Core verbs (on every CLI): `serve`, `check`, `connect`, `telemetry on|off`,
`status`, `windows`/`switch`, `observe [--save|--brief]`, `network`, `step`,
`click`, `type`, `paste`, `wait-for`, `fetch`, `eval`, `shutdown`. Actions share
`--window`, `--frame`, `--by css|xpath`, `--settle`, `--wait-after[/-timeout]`,
`--save`.

SCW flow verbs (`scw-sim`): `run` (walk the course), `where` (which state),
`mission` (step playbook), `challenge` (the current decision view).

## Configuration

Env vars keep the `TS_` prefix for continuity (they predate the rename). Key ones:

| Env var | Meaning | Default |
|---|---|---|
| `TS_SERVER_URL` | Where the CLI finds the server | `http://localhost:22133` |
| `WEBWALK_SERVER_PLUGINS` | Comma-sep flow server-plugin modules the server loads | *(set by each flow's `serve`)* |
| `TS_WEBDRIVER_URL` | Remote WebDriver / Grid endpoint; empty = local Chrome | *(empty)* |
| `TS_START_URL` | URL `connect` opens by default | `about:blank` |
| `TS_DATA_DIR` | Server-side captures + browser profile | `~/.local/share/trainee-sim` |
| `TS_LOCAL_DATA_DIR` | Client-side fetched captures | `./data` |
| `TS_BROWSER_PROFILE` | Override the persistent Chrome profile dir | `$TS_DATA_DIR/browser-profile` |
| `TS_BROWSER_BINARY` | Chromium-family binary to drive | *(default Chrome)* |
| `TS_HEADLESS` | Headless browser (unattended replay only) | off |
| `TS_TIMEOUT` | Client HTTP timeout (seconds) | 120 |
| `TS_NETWORK_CAPTURE` | Enable Chrome's perf log for network capture | on |
| `TS_NETWORK_HOSTS` | Comma-sep host substrings to record (empty = all) | *(empty)* |
| `TS_NETWORK_MAX_EVENTS` | Ring-buffer cap on retained events | 500 |
| `TS_NETWORK_MAX_BODY` | Per-body truncation cap (bytes) | 65536 |

(The `~/.local/share/trainee-sim` data dir keeps its old name so an existing
logged-in browser profile survives the rename.)

## Layout

```
webwalk/
  cli/           core.py (browser verbs + register_core_commands), _shared.py
                 (request/capture/brief helpers), _walk.py (the shared walk
                 plumbing a flow's `run` reuses)
  engine/        matcher.py (where am I?), runner.py (the walk), subflow.py
                 (the pluggable "one level down" contract)
  server/        main.py (FastAPI), session.py (the Selenium session),
                 plugins.py (snapshot-extension + route registry), capture.py,
                 recorder.py, config.py, logging.py
  flows/
    scw/         cli.py (scw-sim), subflows.py (mission/challenge/coding-lab),
                 observers.py (challenge server plugin), challenge.py/coding_lab.py/
                 mission.py (pure handlers), data/*.yaml (states + answer keys)
    example/     cli.py + data/states.yaml — the copy-me template
tests/           test_runner.py (generic engine), test_scw_subflows.py (SCW
                 sub-flows), test_challenge/mission/coding_lab/recorder.py
docs/            design docs (network-capture, rule-runner, reachability, …)
login.nu         worked example: scripted SCW login
```

## Status

Working (validated end-to-end against the real Secure Code Warrior training): the
client/server spine, telemetry gate, window + nested-frame handling, scriptable
actions with settle/readable-errors, local capture fetch, gated network capture,
the "where am I" state detector, and the **rule runner** — walking the state
graph (UI or API) from any starting point, with the SCW mission / challenge /
coding-lab sub-flows. The core is now flow-agnostic, with SCW as the first flow
behind `scw-sim` and `example` as the template for the next.
