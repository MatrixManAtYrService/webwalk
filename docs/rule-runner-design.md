# Design: the rule runner (walking the training as a state graph)

Status: **proposed** — the next build. Audience: an engineer/agent picking this
up cold. Read the top-level `README.md` first (architecture, observe/act, the
telemetry gate, frames/windows, and the **state signatures** in `courses/*.yaml`
+ `trainee-sim where`), then `docs/network-capture-design.md` (how we record the
training's API — the raw material for firing requests directly).

## Why

`login.nu` proved the primitives end-to-end, but it is a **linear script**: a
fixed sequence of `wait-for` / `click` / `type`. That shape has three problems
this project exists to solve:

1. **It can't resume.** It assumes a starting point. Our real training is
   *half complete* — the walker must observe where it is and carry on, not
   restart. (We already hit the non-linear reality during login: Azure sometimes
   shows the email page, sometimes jumps straight to password; SCW sometimes
   shows an SSO login, sometimes single-signs-on straight to the course list.
   Each of those became a `try`-branch. A rule runner generalizes that.)
2. **It's UI-only.** Every step puppets the DOM, which is slow and breaks on
   cosmetic change. We can now *see the API* (this session captured SCW's
   `GET …/breakdown` returning the whole course as JSON, and its Phoenix
   websocket) — so some steps should fire a request instead.
3. **It can't reason about coverage.** The original mission is to prove the
   training has **no dead ends** — that a user can always get from any state to
   the end. A linear script can't answer that; a state graph can.

## Goal

An observe-driven **runner** that advances the browser from *wherever it is* to a
goal state, choosing UI or API per step, resumable from the middle, human-guided
first and autonomous later. Concretely, a `trainee-sim run <course> [--to
<state>]` loop:

1. **observe** the live browser,
2. **match** the current state (reuse `engine/matcher.py`),
3. **select** an applicable rule,
4. **execute** its action — a UI act *or* an HTTP request,
5. **verify** we reached the rule's expected next state (observe again),
6. **repeat** until the goal, skipping steps already done (resume).

Non-goals (first cut): full shortest-path planning, autonomous unattended runs,
replaying arbitrary recorded traffic. Start with a human confirming each step.

## What we already have (the foundation)

- **Primitives**: observe (state + frames + windows + `network`), act
  (click/type/navigate), wait/settle, readable errors — all in `session.py`.
- **State recognition**: `courses/*.yaml` signatures + `match_states()` — this is
  the `when` half of a rule already built and tested. `where` prints the current
  match(es); "exactly one" is the healthy case.
- **API shapes**: telemetry-on step captures carry a `network` key, and
  `trainee-sim network` dumps live traffic. This is where the request templates
  for HTTP actions come from — no guessing.

## Constraints & principles

- **Always observe first; never assume a start.** Every loop iteration
  re-matches state. This is what makes resume free.
- **Steps must be safe to re-enter.** After acting, re-observe; if the state
  already advanced (someone did it by hand, or we're resuming), don't redo it.
  Detecting "already done" likely means richer state signatures (progress %,
  a "completed" marker) — see open questions.
- **Prefer the API when a captured, safe shape exists; UI is the fallback.**
  Each UI step we can swap for a request is a robustness win. The runner should
  make this a *ratchet*, not a rewrite (see below).
- **The content is low-reasoning.** The training mostly says "copy *this*, paste
  it *there*." So most rules are: extract a value from the observed page, then
  act with it. Keep genuine judgment in the human/agent loop, not hardcoded.
- **Don't break the auth model.** Auth stays by-hand with telemetry off; the
  runner operates *after* the gate is open. Never fire auth requests.

## Proposed shape

**Rules live beside states** — extend `courses/*.yaml` (or a sibling
`*.rules.yaml`). A rule is plain data:

```yaml
rules:
  - id: open-python-basic
    when: scw-language-list           # a state signature id (reuse the matcher)
    do:                               # the action to take
      ui: { action: click, frame: [0], by: xpath,
            element: '//a[contains(normalize-space(.),"Python Basic")]' }
    expect: scw-course-breakdown      # the state we should land in

  - id: submit-answer
    when: scw-task-editor
    do:
      # "copy this → paste there": pull a value out of the observed page…
      ui: { action: type, selector: '#answer',
            value_from: { selector: '.instruction code', attr: text } }
    expect: scw-task-solved
```

- **`when`** references a state id (and may add extra element predicates for
  finer control). The runner only considers rules whose `when` matches now.
- **`do`** is one action, either:
  - **`ui`**: the existing `click|type|navigate` with `selector`/`by`/`frame`,
    plus **`value_from`** — extract text/attribute from an observed element for
    the copy/paste tasks (the common case).
  - **`http`**: `{ method, url (templated), body }`, fired *inside the page* so it
    carries the real session — mirror finmap's `session.fetch_json` (add a
    `fetch_in_page` to `session.py`). Rate-limit it (finmap pattern).
- **`expect`** is the state id we should be in afterward — used both to **verify**
  the step and to define an **edge** in the state graph.

**The runner loop** (new `engine/runner.py`, driven by a `run` CLI verb):
observe → `match_states` → applicable rules → *(human confirms, or auto)* →
execute the action → observe → assert `expect` (else stop and report the actual
state) → repeat until `--to` (or a terminal state). Start **greedy**: take any
applicable rule that makes progress; add real pathfinding later.

**Sub-flows / macros** (the README's "log in as Alice"): a rule whose `do` is
"run sub-flow X" with parameters, so near-duplicate flows share one definition.

## The API-vs-UI ratchet

Don't rewrite everything as HTTP up front — *earn* each conversion:

1. Author the step as a **UI rule** and run it with telemetry on.
2. The step capture's `network` key now holds the request(s) that UI action
   produced (e.g. clicking a task fired `POST …/activity/log`; the breakdown page
   loaded via `GET …/breakdown`).
3. Codify an **`http` rule** from that captured shape, and prefer it.
4. **Keep the UI rule as a labeled fallback** — if the API drifts or a state
   can't be reached by request, fall back to puppeting.

Guardrails: rate-limit; never fire auth; and **verify by re-observing**, because
SCW's state is mediated by a **Phoenix websocket** (we saw `TrackEvent` /
`server_message` frames) — an API write may not reflect in the UI instantly.

## Milestones

- **M0 — one rule.** Schema + a runner that executes a *single* rule
  (observe → match → act → verify), human-confirmed. Convert one `login.nu` step.
- **M1 — walk to a goal.** Greedy multi-step to a `--to` state, resumable from any
  start. Re-derive the post-login half of `login.nu` as rules.
- **M2 — value extraction.** `value_from` for the copy/paste tasks; solve one real
  training task end-to-end via UI.
- **M3 — HTTP actions.** `fetch_in_page` + `http` rules (depends on network
  capture's M4). Convert one UI step to an API call and keep the UI fallback.
- **M4 — sub-flows.** Parameterized macros; dedupe the repeated "log in as Alice"
  prefix across flows.
- **M5 — no dead ends.** Treat states+rules as a graph; a `reach`/`verify` command
  that asserts every state can reach the goal. **This is the original mission.**
- **M6 — autonomy.** Drop the per-step confirm; the agent iterates on its own,
  preferring API, recording new shapes as it goes, within safety bounds.

## Open questions

- **Rule location & schema.** Alongside states in `courses/*.yaml`, or a sibling
  file? How much predicate power does `when` need beyond a state id?
- **The extraction DSL.** How to express "copy from here, paste there" —
  `value_from` selectors, or a small templating language for multi-field steps?
- **Verifying effect through the websocket.** When a Phoenix channel / polling
  loop mediates state, how do we confirm a step took effect — re-observe with
  settle, or watch specific ws frames? (See the captured Phoenix protocol.)
- **"Already done" detection (resume/idempotency).** How does a state signature
  encode progress so the runner skips completed steps? (Progress %, completion
  markers — the breakdown JSON has `completedTasks/totalTasks`.)
- **Planning depth.** Greedy-progress vs. real shortest-path over the graph —
  when does greedy get stuck?
- **Autonomy safety.** Bounding an agent iterating on a live corporate LMS: rate
  limits, no destructive/irreversible actions without confirmation, and a hard
  stop on an unrecognized state.

## Code pointers

- `trainee_sim/engine/matcher.py` — state matching; the `when` half is done.
- `courses/secure-code-warrior.yaml` — signatures to extend with `rules:`.
- `trainee_sim/server/session.py` — the observe/act primitives the runner
  drives; add `fetch_in_page` here for `http` actions (see finmap `fetch_json`).
- `trainee_sim/server/recorder.py` + step captures' `network` key — where HTTP
  action shapes come from (`trainee-sim network` to inspect live).
- `login.nu` — the linear flow to supersede; each step is essentially an
  un-generalized rule, and its `try`-branches prefigure state-based dispatch.
- `../finmap` `fetch_json` / accessors — reference for in-page authenticated
  requests and the "codify a discovered endpoint" loop.
