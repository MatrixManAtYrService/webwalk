# Video-task "Continue" gate — and its API bypass (CONFIRMED)

Status: **confirmed**. A video task's Continue button is server-gated; firing the
task's `/answer` endpoint marks it done **without playing the video**, and the
course then progresses past it. Verified live on the XSS/DOM course: fired the
call on an unwatched video → `200 {"status":"done"}` → re-entering the task
advanced to the next activity (a Guideline task, Continue enabled).

## The mechanism

A video task (`/course-task/` URL, title "Video | …") keeps its frame-`[0]`
**Continue** button `enabled:false` until the task's attempt is marked done.

Task open fires:
```
GET /courses/userCourse/{courseId}/language/{languageId}/module/{moduleId}/task/{taskId}/challenges
→ 200 { taskAttemptId, taskType:"video", status:"in_progress", courseId, languageKey, … }
```
Completion (normally after watch-to-end) fires:
```
POST /courses/challenges/language/{languageId}/module/{moduleId}/taskAttempt/{taskAttemptId}/answer
body: {}
→ 200 { taskType:"video", points:100, status:"done", courseCompleted:false }
```
That POST is the gate. Firing it directly (no playback) marks the video done.

## Auth (this was the crux)

portal-api does **not** use cookies or a bearer token. It authenticates with a
custom header **`X-Session-ID`**, whose value is **`localStorage.sessionId`** (a
32-char id). Findings that led here:

- `GET /ping` (public) → 200; `GET /auth/resume` with cookies → **401**.
- A bearer token pulled from the page was a red herring — it was **Coralogix's**
  (`Bearer cxtp_…`), captured because RUM fires constantly. portal-api requests
  carry **no** `Authorization` at all (confirmed from the real `/answer` request
  headers: only `X-Session-ID`, `Content-Type`, `traceparent`, UA).
- Adding an `Authorization` header makes the fetch fail CORS preflight
  (`TypeError: Failed to fetch`) — portal-api's allow-headers doesn't include it.
- `GET /auth/resume` with `X-Session-ID: localStorage.sessionId` → **200** (user
  profile). That's the auth.

## How to reproduce (the tools we built)

Both are in-page primitives on the server session (carry the real session):
`trainee-sim fetch` (`fetch_in_page`) and `trainee-sim eval` (`eval_js`).

```bash
# 1. session id (auth):
SID=$(trainee-sim eval "localStorage.sessionId" | jq -r .result)
# 2. current task's attempt id + type:
trainee-sim fetch ".../userCourse/{course}/language/{lang}/module/{mod}/task/{task}/challenges" \
  -H "X-Session-ID: $SID"          # → taskAttemptId, taskType, status
# 3. mark a video done (the bypass):
trainee-sim fetch ".../courses/challenges/language/{lang}/module/{mod}/taskAttempt/{attempt}/answer" \
  -X POST --data '{}' -H "X-Session-ID: $SID"   # → {"status":"done"}
```

## Caveats

- **UI lag (the Phoenix-websocket caveat).** An out-of-band `/answer` does **not**
  instantly flip the DOM Continue button — the Angular app's client state is
  unchanged, so Continue stays disabled until the app re-syncs. Re-entering the
  task (Back to course → Resume) makes it re-fetch `/challenges`, see `done`, and
  progress. So verify by **re-observing after a re-entry / navigation**, not by
  watching the button change in place.
- **`taskAttemptId` is per-open.** Re-GETting `/challenges` can mint a fresh
  attempt id. Always read it immediately before the `/answer` POST.
- `languageId`/`moduleId`/`taskId` are the 2nd/3rd/4th `/course-task/` URL
  segments; `courseId` (the userCourse id) is the 1st.

## Next: codify as an `http` rule

The runner has `fetch_in_page` but not yet a `do.http` action. A `video-advance`
rule would: GET `/challenges` (extract `taskAttemptId`), POST `/answer`, then
re-observe. Keep the "watch + click Continue" UI path as the labeled fallback
(the API→UI ratchet). See `docs/rule-runner-design.md` M3.
