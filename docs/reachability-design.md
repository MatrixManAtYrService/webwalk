# Design: no dead ends (reachability over the state graph) — TODO

Status: **proposed / not started (this is a TODO).** This is the *original
mission* of the project — everything else (observe/act, matcher, rule runner) is
scaffolding for it. Audience: an engineer/agent picking this up cold. Read the
top-level `README.md` first (states, rules, `where`, the runner loop), then
`docs/rule-runner-design.md` (the graph the runner already builds). This doc and
`docs/mbt-model-format-design.md` are siblings: that one is about *exporting* the
graph in a standard shape; this one is about *proving a property over it*.

## Why

The point of modelling the training as states + rules was never just to walk it —
it was to **prove the training has no dead ends**: from every reachable state, a
user can still get to the end. A linear script (`login.nu`) can't answer that; a
state graph can. We now have that graph — `build_machine()` in
`engine/runner.py` turns states + rules into a `transitions` `Machine` whose
edges are exactly the rules — so the analysis is finally buildable. It just
hasn't been built yet.

Right now "does this course have a dead end?" is answered only *operationally*:
run the walker and see if it gets `stuck`. That finds a dead end by falling into
it. We want to find them **statically**, before walking, and to get the specific
offending state handed back — not "the walk stopped somewhere".

## The property, named precisely

"Every state can reach the goal" is **co-reachability of the goal** (equivalently,
**absence of dead ends** — states from which the goal is *not* reachable). In
temporal logic over the state graph it is the CTL formula:

```
AG EF goal      "on All paths, Globally (in every reachable state),
                 there Exists a path that Finally reaches `goal`"
```

A **dead end** is a state that violates `EF goal`. A **deadlock/sink** is the
special case of a state with no outgoing edges at all (and which is not itself a
declared terminal). Note `AG EF goal` is a genuinely branching-time property — it
is *not* expressible in LTL — which is why "just check every path" is the wrong
mental model; the right one is a backward reachability fixpoint.

## The core algorithm (cheap; no heavy tooling needed first)

For a bounded, static course graph this is **reverse reachability**, not model
checking:

1. Build the directed graph: nodes = state ids, edges = rules
   (`source = when`, `dest ∈ expect`). `build_machine()` already enumerates
   exactly these edges (one per `(rule, dest)` pair, since `expect` may be a
   list). Reuse it, or read the same `states`/`rules` data directly.
2. Compute the **co-reachable set**: reverse-BFS/DFS from `goal` over reversed
   edges. Everything reached can get to the goal.
3. **Dead ends = (all reachable states) − (co-reachable set)**, minus any state
   flagged `terminal: true` (a declared endpoint is allowed to not reach the
   goal — it *is* an end). `self.terminals` in `Runner` already carries these.
4. For each dead end, emit a **witness**: a forward path from the start (or from
   `UNKNOWN`) *into* the dead end, so the report says not just "state X is a dead
   end" but "here's how a user gets trapped there". This is the artifact that
   makes the result actionable.

That's ~30 lines of pure Python over data we already have, and it belongs beside
the matcher (pure, server-free, testable) — a new `engine/reach.py`, exposed as a
`trainee-sim reach <course> [--goal <state>]` verb.

## What counts as a node, and the graph's honesty caveats

Getting the *property* right is easy; getting the *graph* right is the work. Read
these before trusting a green result:

- **`UNKNOWN` is not a real state.** Rules with no `expect` land on the `UNKNOWN`
  sentinel (a discovery edge). A path that only reaches the goal *through*
  `UNKNOWN` has not been proven — it's been deferred. The reachability check must
  treat `UNKNOWN` as **not co-reachable** (a hole in the map), and its report
  should list "states whose only route to the goal is via an undiscovered edge"
  separately from hard dead ends. Those are "go discover this", not "this is
  broken".
- **`expect` lists widen the graph.** A rule like `resume-course` has
  `expect: [scw-course-task, scw-mission, scw-challenge]` — three edges. Co-
  reachability through *any* of them counts, which is correct (the rule really
  can land on any). But it means a green result asserts "at least one landing
  reaches the goal", not "all landings do". Decide which you want; for "no dead
  ends" the existential is right.
- **Overlapping signatures / greedy order.** The matcher can match >1 state at
  once (`scw-course-task` vs `scw-video-task`), and `applicable_rules` is greedy
  in file order. The static graph doesn't model "greedy picks the first" — it
  sees all edges. A path the graph says exists might be one the greedy runner
  never takes. First cut: ignore this (over-approximate reachability — we'll
  claim reachable more often than the runner achieves it, so a *dead-end* finding
  is still sound; a *reachable* finding is optimistic). Note it in the report.
- **Guards/`only_if` are invisible to the graph.** An `http` action's `only_if`
  can make an edge not fire at runtime; the static graph can't see that. Same
  over-approximation caveat.

None of these block the first cut — they're the difference between "sound dead-
end detection" (which we get immediately) and "sound reachability proof" (which
needs the caveats closed). Ship the former, document the latter as the ceiling.

## When to reach for a real model checker (later, maybe never)

The reverse-BFS gives the answer and the counterexample. A model checker
(**NuSMV**, **TLA+/TLC**) buys three things, none needed yet:

- `AG EF goal` is a one-line spec; NuSMV's own tutorial uses `SPEC AG EF …` as
  the canonical "this state is always reachable" example, and it prints a
  counterexample trace on failure — the same witness our BFS emits by hand.
- It scales to **data-extended** models (EFSM: "which user, which resume point,
  progress %") where hand-rolled reachability stops being obviously correct.
  Keep the checked model abstract (states + transitions) to avoid state
  explosion; fold in variables only if a property actually needs them.
- It's a more *credible external artifact* than "I wrote a graph search" if this
  ever needs to convince someone other than us.

Recommendation: **do the Python reverse-BFS now**; add a NuSMV/TLA+ *export*
(from the same graph the exporter in `docs/mbt-model-format-design.md` produces)
only if/when the model grows variables or the audience grows past us.

## Milestones

- **M0 — co-reachability + dead-end set.** `engine/reach.py`: reverse-BFS from a
  `--goal` (default: the single `terminal: true` state), return
  `{reachable, coreachable, dead_ends, sinks}`. Unit test on a tiny hand-built
  graph with a known trap. *No browser involved — this is pure graph code.*
- **M1 — witnesses.** For each dead end, a forward path into it. For each
  "goal only via `UNKNOWN`", flag it as *undiscovered*, not *dead*.
- **M2 — `trainee-sim reach` verb.** Load a course's states+rules, run M0/M1,
  print a readable report (dead ends with traps, undiscovered frontier, sinks).
- **M3 — honesty pass.** Close (or explicitly document per course) the caveats
  above: `UNKNOWN` handling, `expect`-list existential vs universal, greedy-order
  and guard over-approximation. This is what turns "dead-end detector" into
  "reachability proof".
- **M4 — CI gate (stretch).** `reach` exits non-zero on a hard dead end, so a
  course YAML edit that strands a state fails a check. This is the mission,
  automated.
- **M5 — NuSMV/TLA+ export (optional).** Only if M3's honesty needs it or the
  model gains variables. Reuse the exporter from the MBT-format doc.

## Open questions

- **What *is* the goal?** Today the only terminal signal is `terminal: true`
  (unused so far — no state carries it yet). Is "the goal" a single course-
  complete state, or per-course? First cut: the unique `terminal` state; error if
  zero or many until we decide.
- **Reachable-from-where?** Dead-end analysis needs a start set. Use `UNKNOWN`
  (every walk begins there) plus every state that has an inbound edge? Or the
  real entry (`ms-login-username`)? For "no dead ends" the honest start set is
  "every state the training can actually put a user in", which is basically every
  node with an authored signature.
- **Coverage vs. reachability.** "Every state reaches the goal" (this doc) is
  different from "every state/edge is exercised by some walk" (test coverage —
  that's GraphWalker's `vertex_coverage`/`edge_coverage`, see the MBT-format
  doc). We want the former for the proof; the latter would be a nice *test-
  generation* follow-on. Keep them distinct.
- **Living graph.** The graph grows as we discover states (`UNKNOWN` edges get
  `expect`s filled in). A dead-end finding may just mean "not mapped yet". The
  report must make "broken" vs "unmapped" unmistakable, or every run cries wolf.

## Code pointers

- `trainee_sim/engine/runner.py` — `build_machine()` (the graph already exists),
  `UNKNOWN`, `_expect_list()` (how `expect` becomes edges), `self.terminals`
  (declared endpoints), `run()`'s `stuck`/`done` exits (the *operational* dead-end
  today — this doc makes it static).
- `trainee_sim/engine/matcher.py` — `match_states` and the "exactly one match"
  invariant; nodes are its state ids.
- `courses/secure-code-warrior.yaml` — states + rules; no state yet carries
  `terminal: true` (M0 needs one, or a `--goal`).
- `docs/mbt-model-format-design.md` — the graph exporter; a NuSMV/TLA+ dump
  (M5) rides on the same extraction.
- `docs/rule-runner-design.md` — M5 of *that* doc ("no dead ends") is *this* doc;
  they were split so the proof isn't blocked on finishing the runner.
