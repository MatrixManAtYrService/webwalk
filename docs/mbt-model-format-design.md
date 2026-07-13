# Design: model-based-testing model format (a standard graph export) — TODO

Status: **proposed / not started (this is a TODO).** Audience: an engineer/agent
picking this up cold. Read `README.md` (states + rules), then
`docs/rule-runner-design.md` (the `transitions` machine the runner builds) and
`docs/reachability-design.md` (the *first* consumer of this export). This is a
deliberately *small* adoption: borrow a standard file format and vocabulary, not
a runtime.

## Why

We independently reinvented the **model-based testing (MBT)** executor: our
states are MBT **vertices**, our rules are MBT **edges** (with guards/actions),
and our runner "observe current state → pick an edge → act → verify → repeat" is
exactly what tools like **GraphWalker** (Java) and **AltWalker** (Python wrapper
over GraphWalker) do. That's a validation of the design, not a reason to rewrite:
those tools assume automated crawling and can't cleanly host our human-SSO
handoff, telemetry gate, nested-iframe addressing, or socket settling. We keep
our runner.

What's worth taking is the **interchange format**. If our states+rules can be
*emitted* as a GraphWalker-style graph (vertices + edges as plain JSON), we get,
for free and without a runtime dependency:

- a standard, tooling-friendly serialization of the course graph;
- **path generation / coverage** (GraphWalker's `vertex_coverage(100)`,
  `edge_coverage(100)`, `a_star(reached_vertex(goal))`) if we ever want to
  *generate* walks rather than greedily follow rules;
- a clean substrate for the reachability proof (`docs/reachability-design.md`) and
  for a later NuSMV/TLA+ dump — all three want "the graph as data", and this doc
  is where that extraction lives once, reused by each.

The philosophy fit: this is the "smart agent authors the model once, dumb code
executes/checks it forever" pattern (code-as-skill / MBT), which is the project's
whole thesis. The model file is the authored artifact.

## What we already have to export from

`build_machine()` in `engine/runner.py` already computes the graph we'd serialize:

- **vertices** = state ids (`states[].id`) plus the `UNKNOWN` sentinel;
- **edges** = one per `(rule, dest)` pair, where `dest` ranges over
  `_expect_list(rule.expect)` — so a rule with `expect: [a, b]` is two edges, and
  a rule with no `expect` is an edge into `UNKNOWN` (a discovery edge). The edge
  carries `trigger = rule.id`, `source = rule.when`.

So the export is a *projection* of data we already assemble; the task is choosing
the target schema and writing a serializer, not building a graph.

## Proposed shape

A pure function `engine/model_export.py: to_graphwalker(states, rules) -> dict`
producing the GraphWalker JSON shape (roughly):

```json
{
  "name": "secure-code-warrior",
  "models": [{
    "name": "scw",
    "generator": "random(edge_coverage(100))",
    "startElementId": "v_UNKNOWN",
    "vertices": [
      {"id": "v_scw-course-breakdown", "name": "scw_course_breakdown",
       "properties": {"terminal": false}},
      ...
    ],
    "edges": [
      {"id": "e_resume-course__scw-course-task", "name": "resume_course",
       "sourceVertexId": "v_scw-course-breakdown",
       "targetVertexId": "v_scw-course-task",
       "properties": {"rule": "resume-course", "kind": "ui"}},
      ...
    ]
  }]
}
```

Notes / decisions to make while writing it:

- **Naming.** GraphWalker vertex/edge `name`s become code identifiers in its
  world, so map our kebab-case ids to safe names (`scw-course-task` →
  `scw_course_task`) and keep the original in an `id`/`properties` field so the
  round-trip back to our YAML is unambiguous.
- **`UNKNOWN` and discovery edges.** Export them, but mark them
  (`properties.discovery = true`) so consumers (the reachability check especially)
  can treat "reaches goal only via UNKNOWN" as *unproven*, not *proven*. Don't
  silently drop them — they're the map's frontier.
- **Terminals.** Carry `terminal: true` from the state into vertex properties;
  reachability's goal defaults to the terminal vertex.
- **Guards/actions as metadata, not executable.** GraphWalker guards are JS
  expressions it evaluates; ours (`only_if`, `value_from`, `mission`/`challenge`
  sub-flows) are executed by *our* runner. Export them as opaque `properties`
  (documentation/round-trip), **not** as GraphWalker-executable guards — we are
  not handing execution to GraphWalker. This keeps the export lossless-for-us and
  honest-about-scope.
- **Direction of truth.** The YAML stays the source of truth; the GraphWalker JSON
  is a generated artifact (don't hand-edit it, don't import back). If we ever want
  authoring in GraphWalker's studio, that's a separate, later question.

## What this is explicitly NOT

- **Not** adopting AltWalker/GraphWalker as the runtime executor. Our runner keeps
  the telemetry gate, frame-path acting, DOM-click fallback, API↔UI ratchet, and
  socket settling — none of which live in GraphWalker's model. The report that
  prompted this work flagged "adopt AltWalker as executor" as the *tempting wrong
  turn*; this doc is the *right-sized* version (format only).
- **Not** a JVM dependency. Export is pure Python emitting JSON. Anyone who wants
  to *run* GraphWalker against the file can, out of band; we don't shell out to it
  in the normal path.
- **Not** blocking the reachability proof. `docs/reachability-design.md` M0 reads
  the graph straight from `states`/`rules` and needs nothing here. This export is
  the *shared, standard* substrate — nice to have, and the place a NuSMV/TLA+
  dump would also hang — but reachability ships first, on the in-memory graph.

## Milestones

- **M0 — exporter.** `to_graphwalker(states, rules)` + `trainee-sim export
  <course> [--format graphwalker]`. Unit test: vertex/edge counts match
  `build_machine`'s, discovery edges marked, terminal carried.
- **M1 — validate against real GraphWalker.** Feed the JSON to an actual
  GraphWalker (offline, one-off) and confirm it loads and can generate a path —
  proving the format is genuinely standard, not just "JSON we made up".
- **M2 — second consumer.** Point `docs/reachability-design.md`'s check at the
  exported graph (or share the extraction function), so the graph is built once.
- **M3 — NuSMV/TLA+ export (optional, shared).** A second target format from the
  same extraction, for the reachability doc's M5. Only if that doc needs it.
- **M4 — coverage generation (stretch).** If we ever want generated walks (cover
  every edge) rather than greedy rule-following, wire GraphWalker's generators;
  distinct from the runner and from reachability — a *test-generation* tool.

## Open questions

- **How much of a rule survives the projection?** A rule's `do` can be a *list*
  (http + ui + ui, e.g. `video-advance`), sub-flows (`mission`/`challenge`), and
  templated context. The edge only needs source/target/rule-id for graph
  purposes; how much of the action to stash in `properties` is a round-trip-
  fidelity call. First cut: id + `kind` (ui/http/eval/mission/challenge), nothing
  more.
- **Multiple models / sub-flows.** Missions and challenges are *inner* loops
  (steps, decisions), not page states. Do they become their own GraphWalker
  models (GraphWalker supports multi-model with `sharedState` handoff), or stay
  opaque single edges at the page level? First cut: opaque edges; model the inner
  loops separately only if we want to prove *their* reachability too.
- **Is GraphWalker's JSON the right target,** or its older GraphML, or DOT for
  quick visualization? DOT is trivially useful (render the course graph to look at
  it) and might be the actual M0 — a picture of the state graph is worth a lot on
  its own, and cheaper than GraphWalker JSON. Consider shipping DOT first.
- **Drift.** A generated artifact goes stale the moment YAML changes. Regenerate
  on demand (a verb), never commit the JSON as if authored — or commit it and add
  a check that it's in sync. Decide before anyone hand-edits the output.

## Code pointers

- `trainee_sim/engine/runner.py` — `build_machine()` (`ids`, `state_ids`,
  `transitions`), `_expect_list()` (expect → edges), `UNKNOWN`, `self.terminals`.
  The exporter mirrors this enumeration.
- `trainee_sim/engine/matcher.py` — state ids are its signature ids (vertex
  identity).
- `courses/secure-code-warrior.yaml` — the authored source of truth; the export
  is a projection of it.
- `docs/reachability-design.md` — first real consumer; shares the graph
  extraction (build once).
- `docs/rule-runner-design.md` — the runner that stays the executor; explains why
  we keep it rather than adopting AltWalker.
