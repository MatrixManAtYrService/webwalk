# Design: network capture (learning the training's API)

Status: **implemented** (M1–M3) — HTTP request/response + JSON bodies + websocket
frames are captured, gated by telemetry, and attached to each step under a
`network` key. M4 (API replay) still waits on the rule runner. The event
subscription question (M0) was resolved by feeding on **Chrome's performance
log** rather than subscribing to async CDP events — a synchronous, pull-based
CDP stream we drain per snapshot (see `trainee_sim/server/recorder.py` and the
"network capture" section of `session.py`). Selenium-wire and the Playwright fork
were not needed.

Audience: an engineer/agent picking this up cold. Read the top-level `README.md`
first for the architecture and vocabulary (server owns the browser; stateless
CLI; observe/act primitives; telemetry gate; frames/windows).

## Why

The in-course "browser" (Secure Code Warrior, rendered inside nested iframes on
the LMS tab) is not just a static UI. When you type a URL into its simulated
address bar or click its simulated **Submit**, it appears to fire a **real
request to a backend**, and the UI updates afterward — sometimes in response to
actions we didn't directly take, which suggests a **websocket or polling loop**
keeping the page in sync with server state.

Today the engine can only **puppet the UI** (click/type in frames). That works
but is slow and fragile to UI change. If we can *see the traffic*, we can:

1. **Understand the API** the training uses — the requests, their shapes, auth,
   and the websocket protocol.
2. Eventually let **rules fire requests directly** (an `http_request` action)
   instead of driving the UI — faster, and robust to cosmetic UI churn.

This doc is about capability #1 (capture the traffic) with an eye toward #2.

## Goal

Extend the capture so that, **while telemetry is on**, `observe`/`step` records
the network activity the page produced: HTTP requests/responses (method, url,
status, headers, and JSON bodies) and websocket frames — funneled into the same
per-step capture files we already write, with secrets redacted.

Non-goals (for the first cut): replaying requests, a full HAR export, capturing
non-JSON/binary bodies.

## Constraints that shape the design

- **We use Selenium, not Playwright.** The sibling repo `finmap` uses Playwright,
  which has first-class network events (`page.on("response")`, `page.route`) and
  would make this trivial. We're on Selenium because the browser runs remotely on
  the target machine as a persistent, human-driven session. See "Open questions".
- **Chrome.** So the **Chrome DevTools Protocol (CDP)** is available.
- **Telemetry gate is sacred.** Auth traffic (Microsoft SSO, SAML, cookies,
  bearer tokens) must never be captured. Network capture MUST be gated by the
  same `telemetry.enabled` flag and MUST redact auth headers/cookies even then.
- **Frames.** The interesting traffic originates inside deeply nested iframes.
  CDP Network events are per-*target*/session, not per-frame-switch, so unlike
  our DOM code we do NOT need to `switch_to.frame` to see a frame's requests —
  but we DO need to make sure we're attached to the right target(s).
- **Volume.** Response bodies and websocket frames can be large and continuous.
  Need buffering with size caps, host/type filtering, and a ring buffer so a
  long session doesn't grow unbounded.

## Approaches considered

1. **CDP via Selenium `execute_cdp_cmd` + event listening (recommended).**
   Enable the `Network` domain and listen for `Network.requestWillBeSent`,
   `Network.responseReceived` (+ `Network.getResponseBody` on demand), and
   `Network.webSocketFrameSent/Received`. Chrome-only, no extra deps.
   - Wrinkle: Selenium's Python bindings expose **synchronous** CDP *commands*
     (`driver.execute_cdp_cmd`) easily, but **subscribing to async CDP events**
     is awkward. Options: Selenium's BiDi/CDP helper (`driver.get_devtools()` /
     `add_cdp_listener`, version-sensitive), or run a small CDP websocket client
     against the debugger endpoint ourselves. Spike this first (see Milestone 0).

2. **selenium-wire.** A library that transparently proxies the browser and
   records requests. Easy request/response capture, but: it inserts a MITM proxy
   (TLS interception — extra cert setup, and awkward with corporate proxy already
   in play on the target), weaker websocket support, and maintenance concerns.
   Rejected unless the CDP spike proves too painful.

3. **Switch the session to Playwright.** Clean network APIs, but a large rewrite
   of `session.py` and loses nothing-we-need from Selenium only if Playwright's
   remote/persistent-context story fits the target machine. Big fork — see below.

**Recommendation:** go with CDP (option 1). Time-box a spike (Milestone 0) to
prove we can subscribe to Network events on this remote/persistent Chrome; if
that's a dead end, escalate to the Playwright question rather than selenium-wire.

## Proposed shape

- A `NetworkRecorder` on the session (sibling to `Capture`), holding a bounded
  buffer of recent network events. Enabled/disabled **together with** the
  telemetry gate (`set_telemetry` flips both).
- On `connect`, enable CDP `Network` on the driver. On telemetry-on, start
  buffering; on telemetry-off, stop and clear.
- Each buffered event is a small dict: `{ts, type: request|response|ws, method,
  url, status, resource_type, request_headers (redacted), request_body (JSON
  only, size-capped), response_headers (redacted), response_body (JSON only,
  size-capped)}`. Websocket: `{ts, type: ws, direction, opcode, payload
  (size-capped)}`.
- Filter to the training hosts (like `finmap`'s recorder `api_hosts`) so we don't
  drown in CDN/telemetry noise. Redact `cookie`, `authorization`, `set-cookie`,
  `x-csrf-token`, `x-xsrf-token`, etc. (reuse `finmap/server/recorder.py`'s
  `_REDACT` set as a starting point).
- Attach the drained buffer to the step capture: add a `"network"` key alongside
  `elements`/`frames`/`dom` in the snapshot payload, containing the events that
  occurred during/just-before the snapshot. (Design decision: snapshot drains
  "events since the previous snapshot" so each step owns its traffic.)

## Milestones

- **M0 — spike (half day).** Prove we can subscribe to `Network.responseReceived`
  on the live remote Chrome and read one JSON response body. Decide the
  event-subscription mechanism. If impossible, stop and raise the Playwright
  fork.
- **M1 — request/response metadata.** Buffer method/url/status/headers (redacted),
  host-filtered, gated by telemetry. Expose via a `/network` debug endpoint and
  attach to snapshots.
- **M2 — JSON bodies.** Add request + response bodies (JSON, size-capped) via
  `Network.getResponseBody`. This is where "understand the API" pays off.
- **M3 — websockets.** Capture `webSocketFrameSent/Received`. Characterize the
  update channel (is it push, or are we seeing polling?).
- **M4 — API replay (depends on the rule runner).** An `http_request` rule action
  that issues a captured-shape request from inside the page (so it carries the
  real session), à la `finmap`'s `session.fetch_json`. Rate-limit it.

## Telemetry-gate integration (must-not-break)

- Network capture is **off whenever telemetry is off.** No buffering during auth.
- Even with the gate open, **redact** auth headers/cookies in every event.
- The client-side `--save`-when-off rule still applies: nothing hits disk during
  a telemetry-off login.
- Add a test: with telemetry off, drive a request and assert the buffer is empty.

## Open questions

- **Selenium CDP event subscription** on a persistent, remote, human-driven
  Chrome — which mechanism is reliable across the pinned Selenium/Chrome versions
  on the target? (M0 answers this.)
- **The Playwright fork.** If CDP-in-Selenium is too painful, is it worth moving
  the *session* to Playwright (which `finmap` already uses well) for network
  capture — accepting a `session.py` rewrite and re-validating the
  remote/persistent-profile story? Decide only if M0 fails.
- **Body size / retention.** What caps and host filters keep captures useful but
  bounded over a long training session?
- **Do we even need the UI after M4?** If the API is learnable and stable, some
  flows might run entirely as `http_request` rules with occasional `observe` to
  confirm server state — but the websocket/polling behavior may make "did it take
  effect?" non-trivial. Keep UI puppeting as the fallback.

## Code pointers

- `trainee_sim/server/session.py` — `BrowserSession`; `_build_driver` (add CDP
  enable here), `set_telemetry` (flip the network recorder too), `_snapshot_sync`
  (attach `network`), the `Capture` gate pattern to mirror.
- `trainee_sim/server/capture.py` — the enabled-gated writer to model
  `NetworkRecorder` on.
- `../finmap/finmap/server/recorder.py` — a working Playwright network recorder
  with redaction + host filtering; good reference for the data model even though
  the mechanism differs.
