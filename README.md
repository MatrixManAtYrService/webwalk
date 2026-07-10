# trainee-sim

A test harness for training simulators. A **server** runs on the target machine
(the one with the Selenium browser and access to the training material) and owns
a live browser session in memory. A stateless **CLI** runs elsewhere on the LAN
and drives it over HTTP. The server can either **observe** the browser (capture
state) or **inject actions** into it (clicks, typing, navigation).

The goal: walk through training flows, capture what each step looks like
before/after, and turn those captures into **rules** ("if you see a _foo_, click
it") — and eventually into tests that prove the training material has no
dead ends.

## Shape (borrowed from ebb-agent-data-access / finmap)

```
trainee-sim serve        ← server on the target machine; owns the live browser
trainee-sim connect      ← open the browser at the training entry point
trainee-sim telemetry …  ← gate the observer on/off
trainee-sim observe      ← capture current browser state as a step
trainee-sim step …       ← inject an action (click/type/navigate) + capture
```

The CLI is stateless — one HTTP request per invocation. The server holds all
live state. Point the CLI at a remote server with `TS_SERVER_URL`.

## The telemetry gate

Auth is done by hand and must **not** be captured. Telemetry starts **OFF**: you
`connect`, log in and load the training directly in the headed window (nothing is
observed), then `telemetry on` to start capturing. Turning telemetry off means
the observer simply doesn't look — it never records-then-discards, so
credentials can't leak into the capture log.

```bash
trainee-sim serve                         # on the target machine
export TS_SERVER_URL=http://<target>:8000 # on your machine
trainee-sim connect                       # browser opens on the target machine
#   … log in + load the training by hand in that window …
trainee-sim telemetry on
trainee-sim observe --label "module-1-start"
trainee-sim step click --selector "button.next" --label "advance"
```

Captured steps are written one JSON file each to `captures/` under
`$TS_DATA_DIR` (default `~/.local/share/trainee-sim`).

## Configuration

| Env var | Meaning | Default |
|---|---|---|
| `TS_SERVER_URL` | Where the CLI finds the server | `http://localhost:8000` |
| `TS_WEBDRIVER_URL` | Remote WebDriver / Grid endpoint; empty = local Chrome | *(empty)* |
| `TS_START_URL` | Training entry point the browser opens at | `about:blank` |
| `TS_DATA_DIR` | Where captures are written | `~/.local/share/trainee-sim` |
| `TS_HEADLESS` | Headless browser (only for unattended replay) | off |

## Status

Phase 1 — the framework spine + capture. Rules (matching captured states and
acting on them) come next; they'll be plain data authored from the captures,
not codegen.
