# Missions — the experiential-mission service (and why you can't cheap-skip them)

Status: **characterized**. Unlike video/quiz tasks (which live entirely in
portal-api), a **mission** is an interactive, server-verified challenge run by a
separate service plus a live sandbox container. This doc maps it and explains the
one automation that *does* fall out of it: reading the step playbook as data.

Read `docs/video-gate-hypothesis.md` first for the portal-api auth model
(`X-Session-ID`) and the in-page `fetch`/`eval` primitives.

## The shape of a mission

A mission task's URL is `#/mission/course/{course}/{lang}/{mod}/{task}` (note:
**not** `/course-task/`). On load it involves three parties:

1. **portal-api** — `GET …/userCourse/{course}/language/{lang}/module/{mod}/task/{task}/challenges`
   returns the task record: `_taskType:"mission"`, an attempt `_id`, `status`,
   and **`activeIndex`** — the current (0-based) step, i.e. the first unchecked
   box in the side checklist. Also `externalPlayerUrl` → the mission service.
2. **experiential-mission.securecodewarrior.com** — the mission player API:
   `POST /api/session/login`, `GET /api/missions/{alias}` (e.g. `vb-52`), `POST
   /api/podReady`, `POST /api/tracking/lab-duration`. Auth here is a **bearer
   `Authorization`** token (its own, from `/api/session/login`), *not* portal's
   `X-Session-ID`.
3. **a per-mission pod** `mi-…mission.securecodewarrior.com` — a live container
   running the real target app (e.g. "Viking Bank"), talked to over **Socket.IO**
   (`/labSession/socket.io/`).

## Why there's no cheap API skip

- portal-api's `/answer` (the video bypass) rejects a mission:
  `400 {"message":"Invalid activity type"}`.
- Step completion is **verified server-side**: the pod watches the real container
  for the actual actions and advances `activeIndex` over the Socket.IO channel.
  Emitting "step done" events won't fool it — it checks container state, not
  client claims. So a mission must actually be *performed*.

## The automation that DOES fall out: the step playbook is data

`GET https://experiential-mission.securecodewarrior.com/api/missions/{alias}`
returns the whole mission, including `instruction[]` — the ordered steps and
their exact copy-paste values. For `vb-52` ("web.auth.missingauth"):

| order | title | value in `description` |
|---|---|---|
| 0 | Log in as Alice | `alice@alice.com` / `P@ssw0rd1!` (note the account #) |
| 1 | Log out, read the code | — |
| 2 | Log in as Alice with any password | the vuln: password isn't checked |
| 3 | Log in as Bob | `bob@bob.com` / any password |
| 4 | Transfer money Bob → Alice | to account `94923054`, any amount |

Each step also carries a `pid`; the mission carries a `flag`
(`flag_233161…`) and the pod image ref. So we **don't** have to scrape the
instructions pane out of nested iframes — the steps are structured JSON.

## Reading the playbook deterministically (frame-scoped fetch)

The playbook is `GET {experiential-origin}/api/missions/{alias}`, but reaching it
naively hits four walls, all confirmed live:

- **CORS.** A fetch from the portal origin to `experiential-mission.…` is
  cross-subdomain → `status: 0`, blocked.
- **Ring-buffer eviction.** The mission's load flood (RUM/ping/heartbeat) pushes
  `/api/missions` out of the 500-event buffer before you can peek it.
- **Body truncation.** Even when buffered, the 92KB body is cut at the 64KB
  `TS_NETWORK_MAX_BODY` cap → unterminated JSON.
- **Pod-destroying reload.** The old `--reload` (about:blank bounce) re-fires the
  API but re-initialises the pod, logging the app user out mid-mission.

All four dissolve by firing the request **from inside the experiential player
iframe, frame `[0,0]`** (see [[scw-mission-app-frame]] for the frame map):

- It is **same-origin** to the API there (no CORS).
- That frame's `sessionStorage['auth-token']` holds the experiential **bearer**
  (the only JWT in that origin's storage), attached via `page_auth`.
- Both the **origin and the alias** are read from the frame's own
  `location.href` (`/missions/{…}/{alias}` → e.g. `vb-63`), so nothing is
  hardcoded.

Concretely: `trainee-sim fetch {origin}/api/missions/{alias} --frame 0/0
--page-auth`. The CLI's `_fetch_mission_def()` derives the endpoint from frame
`[0,0]` and does exactly this; `trainee-sim mission` and the runner's playbook
read both use it (buffer scrape kept only as a fallback). Result live: `200`, the
full 92KB body, 7 steps, no pod reload.

## How the engine should do a mission

1. Read the playbook: `instruction[]` from experiential `/api/missions/{alias}`
   → the ordered steps and their copy-paste values. Fetch it directly from frame
   `[0,0]` as above — deterministic, no buffer/reload dependence.
2. Read current progress **straight from the snapshot**: the side checklist
   renders each step as a hidden `<input type=checkbox>` in frame `[0,0]`, and
   `observe` now captures these with `checked` + a `label` (e.g. `"2. Log out"`).
   The first `checked:false` is the current step — the local equivalent of
   portal `/challenges` `activeIndex`, without the round-trip. (Portal
   `/challenges` remains the authority if the local read is ever ambiguous.)
3. **Drive the mocked app** in frame `[0,0,0]` per the step (type
   `alice@alice.com`/`P@ssw0rd1!` into Username/Password → submit; log out via
   the menu's `<a id="menu-link-logout">`, which needs the click primitive's
   DOM-level fallback since it isn't natively interactable). The pod detects the
   real action and ticks the box; re-observe and read the boxes until done.

This is the design doc's `value_from` copy-paste case, except the "copy" source
is the mission API rather than the DOM. UI driving of the target app is the
"paste" half.

## Exposure tool

`trainee-sim mission` dumps a mission's playbook (steps + values + current
`activeIndex`) by reading the captured `/api/missions/*` and `/challenges`
responses. Telemetry must be on. See `trainee_sim/cli.py`.

## Open questions

- How does a completed mission report back to portal-api to flip the task to
  `done` (so the course progresses)? Presumably experiential-mission → portal on
  the final step. Capture that on a real completion.
- Can steps be driven headlessly, or does the pod need a visible/interacted DOM?
  Data point: the app's menu **Logout** isn't natively interactable (side-overlay
  / hover), so a driven click needs the DOM-level fallback — and that fallback
  click *did* register with the pod (the app returned to its login form). Whether
  the pod also credits actions in a fully headless/unrendered DOM is still open.
- ~~The experiential bearer token~~ *(resolved)*: it lives in the player iframe's
  `sessionStorage['auth-token']` (frame `[0,0]`). Firing the fetch from that frame
  with `page_auth` reuses it in-browser — no need to replay `/api/session/login`.
