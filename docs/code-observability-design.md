# Design: challenge observability (surface the choices, read the code)

Status: **proposed** — a small, high-value primitive. Audience: an engineer/agent
picking this up cold. Read `README.md` (observe/act, frames, the telemetry gate)
first. This doc is the build spec for making an SCW challenge **legible to the
engine**: what decision is in front of it, what the enumerated choices are, and
the code context needed to choose.

## The problem we hit

On an SCW "Locate the vulnerability" challenge, the candidate is line
`auth.py:100`, but every `observe` came back capped around line 93. The agent
solved it *by elimination without ever seeing the line* — clever, but it won't
scale. Reading the code was the *blocker*; but note the code was never the
*goal*.

## Design principle: surface the choices, not just the code

A challenge is a **decision among a fixed set of options**, and the interface
should say so. The engine's job at a challenge is to *pick*, not to read a file
and re-derive the UI:

- **Locate stage** — "identify which 1 of N highlighted blocks is vulnerable."
  Choices = the N candidate blocks (each a code range + its source).
- **Fix stage** — "pick which of N candidate solutions to submit." Choices = the
  N solutions (each a diff against the vulnerable code).

So the deliverable is an **affordance-focused view**: the decision, the
enumerated options, the concrete control to act on each option, and the source
attached to each option as *context for choosing*. Reading "the whole file" is a
means (context), not the product. Everything below builds toward that view; the
raw extractor is the substrate under it.

## Root cause (verified live — NOT what it looked like)

A previous pass guessed the editor was **Monaco**, **virtualized** (only ~40
visible lines mounted), and in a **cross-origin** frame, and proposed
`eval --frame "monaco.editor.getModels()[0].getValue()"`. **All three guesses are
wrong**, and that call would throw (`monaco is not defined`). What's actually
true, confirmed by probing the live page:

1. **It's not Monaco / CodeMirror / Ace.** `window.monaco` is undefined; there is
   no `.monaco-editor`, `.cm-editor`, `.CodeMirror`, or `.ace_editor` anywhere.
   The renderer is a plain **`<table id="editor" aria-label="Code editor">`**
   inside `<pre><code class="language-python">` (Prism-style highlighting).
2. **Nothing is virtualized.** The table holds **every** line. For the file we
   tested it had all 173 rows — line 100, 101, 173, all present in the DOM at
   once. There is no viewport-mounting to fight and no scrolling required.
3. **The frame is same-origin-reachable.** A top-document `eval` walking
   `iframe.contentWindow` reached the table's `document` and read it with no
   cross-origin error. (Selenium `observe` also already switches into this frame
   and scrapes its `body.text`, so it's reachable both ways.)

**So the only real bug is on our side:** `observe` truncates each frame's text at
**4000 chars** (`session.py:450`, `entry["text"] = (body.text or "").strip()[:4000]`).
4000 chars of sidebar-chrome + code reaches ~line 93, which is exactly the cap we
saw. It was never virtualization — it was our own slice. And even un-truncated,
`body.text` interleaves gutter line-numbers with code and duplicates every line
(see below), so raw text is a poor substrate anyway. We want a **structured
extractor**, not just a bigger cap.

## Two modes, one renderer

The same `<table id="editor" aria-label="Code editor">` drives **two challenge
stages**, and the extractor must know which it's looking at:

- **Locate mode** — *one* code column. Four (or N) highlighted candidate blocks;
  you tick the checkbox on the one that's vulnerable to **identify** it. No
  solutions offered. This is the stage the earlier agent was stuck on.
- **Fix mode** — *two* code columns rendered as a **diff**. The left is a
  **"Comparison base"**, the right is one of **N candidate solutions**; each pane
  is driven by its own dropdown (`aria-label`s: `"On the left side, show
  solution 1"`…`4`, `"On the right side, show solution 1"`…`4`, `"Comparison
  base"`, `"Selected solution"`). You surf solutions and **submit** the fix.

They share the table markup but are not the same shape, so a solver navigates them
differently (identify-a-block vs pick-a-solution).

## The DOM shape (so the extractor is exact)

Each `<tbody > tr>` is a sequence of `<td>`s. The first `<td>` is a **marker
gutter** (`"CB1"` = code-block 1, or empty). After it come one or more
**(lineNo, code) column-pairs** — one pair in locate mode, two in fix mode:

| mode | `td`s | meaning |
|------|-------|---------|
| Locate | `[marker, lineNo, code]` | single file |
| Fix    | `[marker, baseNo, baseCode, solNo, solCode]` | base ‖ solution diff |

**Each pane numbers itself.** In fix mode a base line and its aligned solution
line carry *different* numbers (we saw base `100` ↔ solution `102`) because a
solution inserts/deletes lines above. This offset is **inherent to a diff, not a
bug** — do not "reconcile" it.

⚠️ **Do not merge panes by "first integer wins."** An earlier draft keyed every
integer-`td`→next-`td` pair into one line map; in fix mode that silently
**interleaves base and solution** into a plausible-looking-but-fake file (it read
a clean gap-free 1..173, which was really two versions merged). Extract **by
column position**: count the (lineNo, code) pairs per row → 1 = locate, 2 = fix,
and return each pane as its own sequence.

### Proven extractor (runs from the top document; no frame-switch needed)

```js
return (function () {
  function childFrames(win) {
    var a = [];
    try {
      var f = win.document.querySelectorAll("iframe,frame");
      for (var i = 0; i < f.length; i++) { try { a.push(f[i].contentWindow); } catch (e) {} }
    } catch (e) {}
    return a;
  }
  // breadth-first walk of same-origin frames
  var all = [window];
  for (var i = 0; i < all.length; i++) childFrames(all[i]).forEach(function (w) { all.push(w); });

  var editors = [];
  all.forEach(function (win) {
    var d; try { d = win.document; } catch (e) { return; }
    d.querySelectorAll("table#editor, table[aria-label='Code editor']").forEach(function (tbl) {
      // Per row, collect (lineNo, code) pairs left-to-right; pair index 0 = first
      // column (base/only), 1 = second column (solution). Build one sequence per
      // column so fix-mode's base and solution stay separate.
      var panes = [];  // panes[col] = { byLine: {n: code} }
      tbl.querySelectorAll("tbody > tr").forEach(function (tr) {
        var tds = tr.children, col = 0;
        for (var i = 0; i + 1 < tds.length; i++) {
          var t = (tds[i].innerText || "").trim();
          if (/^\d+$/.test(t)) {
            (panes[col] = panes[col] || { byLine: {} });
            var n = +t;
            if (!(n in panes[col].byLine))
              panes[col].byLine[n] = (tds[i + 1].innerText || "").replace(/\n$/, "");
            col++; i++;  // consume the code td too
          }
        }
      });
      function seq(p) {
        var nums = Object.keys(p.byLine).map(Number).sort(function (a, b) { return a - b; });
        return nums.map(function (n) { return { n: n, code: p.byLine[n] }; });
      }
      editors.push({
        mode: panes.length >= 2 ? "fix" : "locate",
        file: (d.querySelector("[role=tab][aria-selected=true], .Mui-selected") || {}).innerText || null,
        base: panes[0] ? seq(panes[0]) : [],           // locate: the file; fix: comparison base
        solution: panes[1] ? seq(panes[1]) : null,     // fix only: the selected solution
      });
    });
  });
  return editors;
})();
```

## Candidate blocks & solutions (the highest-value bit for challenges)

**Locate mode** asks which highlighted block is vulnerable. The candidate ranges
are observable two ways; pair each with its source text and you've handed the
solver exactly what it needs:

- **Highlight divs**: `[data-from][data-to]` carry the ranges
  (e.g. `{from:101,to:101}`, `{from:30,to:31}`).
- **Answer / jump buttons**: `aria-label`s carry the canonical range strings you
  actually submit — `"Select auth.py:100-100 as answer"`, `"Jump to previous
  highlighted lines auth.py:100-100"`, `"This code block contains code that is
  vulnerable."`

**Fix mode** asks which *solution* to submit. The choices are **toggle buttons**
(`aria-pressed`), in two groups, verified live:

- **Left pane** (the comparison base): `"On the left side, show the vulnerable
  code"` (pressed by default) + `"On the left side, show solution 1"`…`N`.
- **Right pane** (what you submit): `"On the right side, show solution 1"`…`N`
  (solution 1 pressed by default). The pressed right-side button **is** the
  answer; then click **`"Submit answer"`**.

So the natural default view is `vulnerable ‖ solution 1`, and the engine surfs the
right group to compare each solution's diff, then submits the chosen one. (This
particular challenge had one vulnerable region and 4 solutions; a challenge with
multiple vulnerable blocks would show them in the left group too.)

⚠️ **Range-label vs data-from gotcha.** The submittable range in the answer
`aria-label` (`auth.py:100-100`) did **not** equal the `data-from` value (`101`)
on our test — the two use different numbering. When reporting a block, read its
source **from the rows the highlight div actually spans** and report the range
**string straight from the answer-button `aria-label`** (that string is what gets
submitted). Confirm the label↔row correspondence on one challenge before trusting
it.

## Proposed commands

One general escape hatch, one choice-centric verb, then the engine-native
integration. The verb is the real deliverable; the extractor is its substrate.

### 1. `eval --frame` (general primitive, ~20 min)

Add `--frame` to the existing `eval`, reusing the frame-path syntax
`step`/`click`/`observe` already take (`_parse_frame` + `_enter_frame_path` in
`session.py`). Switch into the frame, `execute_script`, switch back to
`default_content`. Not strictly required (the extractor runs from the top via
`contentWindow`), but a cheap, broadly useful primitive — and the natural place
the extractor *could* run per-frame.

### 2. `trainee-sim challenge` — the choice-focused view (the real deliverable)

Returns the **decision + enumerated options + how to act on each**, with code as
per-option context. One shape, two stages:

```jsonc
// Locate stage
{
  "stage": "locate",
  "file": "auth.py",
  "vulnerability": "Authentication - Improper Authentication",
  "decision": "Identify which 1 of 4 highlighted blocks is vulnerable",
  "options": [
    { "id": "auth.py:33-33",   "from": 33, "to": 33, "code": ["…"],
      "act": { "click": { "aria_label": "Select auth.py:33-33 as answer" } } },
    { "id": "auth.py:100-100", "from": 100,"to": 100,"code": ["…"], "act": { "click": { … } } }
    // …
  ],
  "submit": { "click": { "text": "Submit answer" } }
}

// Fix stage
{
  "stage": "fix",
  "file": "auth.py",
  "decision": "Pick which of 4 candidate solutions to submit",
  "vulnerable_code": { "range": "auth.py:100-100", "lines": ["…"] },
  "options": [
    { "id": 1, "selected": true,  "diff_vs_vulnerable": ["…"],
      "act": { "click": { "aria_label": "On the right side, show solution 1" } } },
    { "id": 2, "selected": false, "diff_vs_vulnerable": ["…"], "act": { "click": { … } } }
    // …
  ],
  "submit": { "click": { "text": "Submit answer" } }
}
```

Each `act`/`submit` is a ready-to-run action dict (same frame/selector vocabulary
`click`/`step` already take), so the engine picks an `id` and fires its `act`
without re-deriving the UI. Keep a raw-code escape hatch for humans/debugging:

```
trainee-sim challenge            # the structured decision above (default)
trainee-sim challenge --source   # full line-numbered file(s), no interpretation
```

Backed by a new `/challenge` endpoint + session method running the mode-aware
extractor plus the affordance inventory (toggle buttons / checkboxes / answer
labels) shown above.

### 3. Fold it into `observe` (engine-native, the end state)

Attach the same structured challenge view to the capture when a code editor is
detected — `state.frames[*].editor = { stage, file, options, … }` — so a future
`solve-challenge` rule (see `docs/rule-runner-design.md`) gets `(instruction,
decision, options)` in every snapshot and just chooses. `challenge` stays a thin
human/debug front-end over the same extractor. **Also fix the truncation** while
here: `session.py:450`'s flat `[:4000]` is what blinded us — lift the cap for code
frames or make it a knob (`TS_FRAME_TEXT_MAX`), and prefer the structured field
over raw `text` for editors.

## Build checklist

- [ ] `eval --frame` (CLI option + wire `_enter_frame_path`/restore in the `/eval` path).
- [ ] Mode-aware extractor as a shared JS constant (server-side): detect
      locate-vs-fix by column-pair count; return each pane separately. Keep
      Monaco/CM/Ace fallbacks *only* if another course needs them — SCW does not.
- [ ] Affordance inventory: toggle buttons (`aria-pressed`), locate checkboxes,
      answer/jump `aria-label`s → the `options[].act` and `submit` dicts.
- [ ] `challenge` verb + `/challenge` endpoint returning the choice-focused view;
      `--source` for the raw line-numbered fallback.
- [ ] Resolve the range-label↔row-numbering correspondence empirically.
- [ ] `observe` integration: `frames[*].editor`, and fix/relax the 4000-char cap.
- [ ] Regression test on a live challenge (both stages): assert the flawed line is
      present/numbered and every option carries a working `act` — a guard against
      the blind-solve.

## Code pointers

- `trainee_sim/server/session.py:450` — the 4000-char frame-text truncation (the bug).
- `trainee_sim/server/session.py` `_collect_elements` / `_snapshot_sync` — where an
  `editor` field would be attached per frame.
- `trainee_sim/server/session.py` `eval_js` / `_eval_sync` — add `--frame` here.
- `trainee_sim/cli.py` `eval` (~L670) — the CLI verb to extend; model `code` on it.
- `trainee_sim/engine/runner.py` — the eventual `solve-challenge` rule consumer.
</content>
</invoke>
