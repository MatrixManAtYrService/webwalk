# Hypothesis: the video-task "Continue" gate (and its API bypass)

Status: **hypothesis** — completion mechanism observed once (a "Video" task in
the XSS/DOM course) and the *gate* confirmed to reproduce on a second video
(Continue starts `enabled:false` again after `continue-task` advances to it). The
API bypass itself is not yet tested — that needs `fetch_in_page` (M3). Captured
via the record-manual method: a clean `observe` baseline, a human completing the
video, then a second `observe` whose `network` key held the completion traffic
(see `data/captures/00029/00030` and `data/network/00004`).

Current fixture: we are parked on **video #2** (module `318bc9fa…`, task
`1e60b988…`), Continue disabled — ready for the bypass test below.

## What we saw

On a video task (`url … /course-task/…`, title "Video | …"), the frame-`[0]`
**Continue** button starts **`enabled: false`**. It flips to `enabled: true` the
moment the video is watched to the end. At that instant two SCW calls fire:

1. **The gate** — marks the task done:
   ```
   POST /courses/challenges/language/{languageId}/module/{moduleId}/taskAttempt/{taskAttemptId}/answer
   body: {}
   → 200 {"taskType":"video","points":100,"status":"done","courseCompleted":false}
   ```
2. **Watch-time log** (probably *not* the gate, just analytics):
   ```
   POST /learning/log
   body: {"learningresource":"…:xss:dom","totalduration":108.42,"playmode":"courses","courseId":"…"}
   ```

The burst of Phoenix websocket `client_message` frames in the same window is a
**red herring** — it's Userflow product-tour telemetry (`flowId`,
`ToggleClientCondition`), unrelated to task completion.

## The hypothesis

**The `/answer` POST (body `{}`) is what marks a video done and ungates
Continue.** If so, we can *bypass* "watch the whole video" by firing that request
directly — an `http` rule (the rule-runner's M3 / network-capture's M4 ratchet)
— then clicking Continue. No playback, no seeking.

## The one unknown: where `taskAttemptId` comes from

The three path ids:

| id            | source                                                        |
|---------------|---------------------------------------------------------------|
| `languageId`  | 2nd `/course-task/` URL segment (== the `/breakdown/` id)     |
| `moduleId`    | 3rd `/course-task/` URL segment                               |
| `taskAttemptId` | **not in the URL** — minted when the task opens (unknown)    |

`taskAttemptId` appeared only in the `/answer` call itself. It's presumably
returned by a task-load GET (or lives in the breakdown JSON). We never captured
that call — it fired inside the `resume-course` step's snapshot drain.

## Test plan for the next video

1. Reach a fresh video task with **telemetry on**.
2. Before completing it, inspect the task-load traffic (`trainee-sim network`)
   for a call whose response carries the `taskAttempt` / attempt id → confirms
   the provenance and lets us template the URL.
3. Fire the `/answer` POST directly (once `fetch_in_page` exists) and check
   Continue goes `enabled: true` **without** playing — that confirms the bypass.
4. Keep the "watch + click Continue" UI path as the labeled fallback.

## Related open question (state model)

The matcher can't yet tell "video not done" (Continue disabled) from "video done"
(Continue enabled) — `element_matches` ignores the `enabled` flag. A `continue`
rule is therefore only *safe* to fire once Continue is enabled. Two ways to model
it: split `scw-course-task` into gated/done states (needs an `enabled` predicate
in the matcher), or have the runner check enabled before clicking. This is the
"already done / idempotency" concern from `rule-runner-design.md`.
