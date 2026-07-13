# Design: websocket-aware settling (verifying a step took effect) — TODO

Status: **proposed / not started (this is a TODO).** Audience: an engineer/agent
picking this up cold. Read `README.md` (the "Settling" section), then
`docs/rule-runner-design.md` (the observe→act→**verify** loop) and
`docs/network-capture-design.md` (we already capture websocket frames). This is
the robustness gap most likely to bite the runner *first*, before any of the
graph work.

## Why

The whole runner rests on one assumption: **act → re-observe → assert we reached
`expect`**. That assumption is only as good as the "re-observe" catching the page
*after* it has settled into its new state. For a plain request/response page,
"wait for the new URL / an element to appear" is enough. But the training's real
content is a **socket-mediated SPA**:

- the SCW portal SPA is kept in sync by a **Phoenix channel** websocket (we've
  captured `TrackEvent` / `server_message` frames);
- a **mission** is driven by a per-pod **Socket.IO** channel
  (`/labSession/socket.io/`) — the pod watches the real container and pushes
  `activeIndex`/step-completion updates (see `docs/mission-api.md`).

So the failure mode is: we act, the DOM hasn't received the socket update yet, we
snapshot, the matcher sees the *old* state (or a transient half-state), and the
runner reports `unexpected` or `stuck` — a **false negative** on a step that
actually worked. Retrying blindly then risks double-acting. This is exactly the
synchronization problem Ringer (web-automation-by-demonstration) had to solve by
*inferring which server response gates an action* — we have the same problem and,
usefully, we already see the frames.

## What we have today (and why it's not enough)

`session.act()` offers two settle strategies (`session.py`):

- **`settle: <seconds>`** — a fixed `asyncio.sleep`. Blunt: too short and we
  snapshot early; too long and every step pays the worst-case tax. Most rules in
  `courses/secure-code-warrior.yaml` use `settle: 2–4`, a guess.
- **`wait_after: <selector>`** — poll until a selector is visible
  (`_wait_sync`). Better (it waits for a *cause*), but it only knows about the
  **DOM**. When the state change arrives over the socket and only *then* mutates
  the DOM, the selector we'd wait for is often the very thing that's racing.

Neither watches the actual signal that carries the state change: the **websocket
frame** (or the settling of network activity). We capture those frames in the
`NetworkRecorder` but don't yet *wait on* them.

## The idea: quiescence, and frame-triggered waits

Add settle strategies that watch the network recorder, not just the DOM:

1. **Network quiescence (the cheap, general win).** After acting, poll the
   recorder until traffic goes idle: no new HTTP responses *and* no new ws frames
   for a short quiet window (e.g. 500 ms), bounded by a timeout. "The page stopped
   talking to the server" is a strong, content-agnostic proxy for "the state
   update has arrived and been applied." The recorder already timestamps every
   event (`_pump_network_sync` drains CDP perf messages; ws frames get a wall-
   clock time), so this is: pump, look at the newest event time, wait for a gap.
2. **Frame-triggered wait (the precise win).** Wait until a ws frame *matching a
   predicate* arrives — e.g. a Socket.IO frame whose payload signals the step
   completed, or a Phoenix `server_message`. This is Ringer's "synchronize on the
   response that gates the action". More work (we must characterize which frame
   means "done"), so it's the second cut, per action that needs it.
3. **Settle predicate = re-observe until the matcher agrees.** For the runner
   specifically, the real settle condition is "the matcher now reports `expect`".
   A settle helper that re-observes and re-matches in a poll loop (bounded)
   directly encodes "wait until we're in the state we expect", which is stronger
   than any single selector. This lives in the runner, not the session.

These compose: quiescence is the default floor; a frame trigger or a re-match
predicate tightens it where a step is known to race.

## Transient states are first-class, not errors

The matcher's healthy case is "exactly one match" (`match_states`); zero or many
is treated as unknown/ambiguous. But a socket-mediated SPA **legitimately passes
through** zero-match and multi-match states mid-update (the old panel is tearing
down, the new one hasn't mounted). Today that transient reads as a hard
`unknown`/`ambiguous` stop.

Proposal: model **settling as an expected condition**, not a failure. The runner's
verify step should *tolerate a transient* by re-observing within the settle
window before declaring `unexpected`/`unknown`. Optionally, add an explicit
`transient`/`settling` state class (a signature that recognizes the in-between,
e.g. a spinner/overlay) so the walker can say "still settling, keep waiting"
rather than "I'm lost". Start with the tolerance-window approach (no new
signatures); add explicit transient states only where a specific in-between keeps
tripping the walk.

## Proposed shape

- **Session:** a new settle mode on `act()` (and a standalone `settle_until`
  helper) — `settle_quiescent(quiet_ms, timeout)` that pumps the recorder and
  waits for a traffic gap, and `wait_for_frame(predicate, timeout)` that waits for
  a matching ws frame. Both reuse `_pump_network_sync` + the recorder's
  timestamped buffer; neither needs a new capture mechanism.
- **Rules:** let a rule's `ui`/`http` action declare its settle intent as data,
  e.g. `settle: { quiescent: 500 }` or `settle: { frame_contains: "…" }`,
  alongside today's numeric `settle`. Keep the plain number working (it's fine for
  non-socket steps).
- **Runner:** wrap the post-action re-observe (`self.locate()` in `run()`) in a
  bounded re-match poll, so verification waits for `expect` to become true instead
  of snapshotting once. This is the highest-leverage single change — it fixes the
  false-negative for *every* rule without per-rule tuning.

## Milestones

- **M0 — quiescence helper.** `settle_quiescent` on the session; unit/integration
  test that a socket-driven step settles reliably where a short fixed `settle`
  flakes. *Depends only on the existing recorder.*
- **M1 — runner re-match verify.** Replace the single post-action `locate()` with
  a bounded "re-observe until `expect` matches (or timeout)" loop. Measure the
  drop in false `unexpected`/`stuck` on the mission and challenge flows.
- **M2 — transient tolerance.** Treat mid-settle zero/multi-match as "keep
  waiting", not a stop, within the window. Only then declare `unknown`/`ambiguous`.
- **M3 — frame-triggered waits.** Characterize the "step done" frame for the
  mission Socket.IO channel and the portal Phoenix channel (from captured
  traffic), and add `wait_for_frame`. Convert the mission step-verify
  (`_verify_step`, today a checkbox poll) to optionally key on the frame.
- **M4 — data-driven settle in rules.** The `settle: {quiescent|frame_contains}`
  schema; migrate the guessed numeric `settle`s in
  `courses/secure-code-warrior.yaml` where they're load-bearing.

## Open questions

- **How idle is idle?** The Phoenix/Socket.IO channels may **heartbeat** (keep-
  alive pings), so "no ws frames at all" may never be true. Quiescence probably
  has to ignore heartbeat frames — needs the captured protocol to tell a
  heartbeat from a real update (`opcode`/payload shape in the recorder).
- **Which frame means "done"?** Characterizing the completion frame per channel
  is real reverse-engineering; until then, quiescence + re-match is the pragmatic
  floor and `_verify_step`'s checkbox poll stays the mission signal.
- **Cost of pumping.** `_pump_network_sync` drains CDP perf logs and fetches
  bodies; polling it tightly in a settle loop may be heavy. Poll interval and
  "metadata-only during settle" (skip body fetch) are the knobs.
- **Interaction with the telemetry gate.** Settling watches the recorder, which
  is only populated while telemetry is on. A settle-on-frames strategy therefore
  only works post-gate — which is exactly when the runner operates, so fine, but
  worth stating so no one wires it into the auth phase.
- **Does quiescence alone over-wait?** A page that legitimately keeps polling
  (RUM/analytics) never goes quiet. Host-filtering the quiescence check to the
  training's own API (`api_hosts`, already in the recorder) probably required.

## Code pointers

- `trainee_sim/server/session.py` — `act()` (`settle` / `wait_after` today),
  `_wait_sync` (the DOM poll to generalize), `_pump_network_sync` /
  `network_events` (drain + timestamps for quiescence), `snapshot`/`observe`.
- `trainee_sim/server/recorder.py` — `NetworkRecorder`: ws frames captured in
  `_on_ws_frame` (`direction`/`opcode`/payload), timestamped; `peek`/`drain`/
  `stats` are the read side a settle helper polls.
- `trainee_sim/engine/runner.py` — `run()`'s post-action `self.locate()` (wrap in
  the re-match loop), `_drive_mission`'s `_verify_step` (the checkbox completion
  poll, the M3 candidate for frame-triggering).
- `docs/mission-api.md` — the mission Socket.IO channel and why completion is
  server-verified (why we can't just assume a step took).
- `docs/network-capture-design.md` — how ws frames get captured in the first
  place (the raw material this doc waits on).
