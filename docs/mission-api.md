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

## How the engine should do a mission

1. Read `activeIndex` from portal `/challenges` → the current step.
2. Read `instruction[]` from experiential `/api/missions/{alias}` → the playbook.
3. **Drive the mocked app** in frame `f[0,0,0]` per the step (type
   `alice@alice.com`/`P@ssw0rd1!` into Username/Password → submit, log out, etc.).
   The pod detects the real action and ticks the box; repeat until done.

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
- The experiential bearer token: minted by `/api/session/login` from the portal
  session — worth capturing its exchange to fetch `/api/missions` without a
  page reload.
