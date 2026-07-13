"""Reading an SCW challenge as a *decision*, not a wall of code.

A challenge task asks the trainee to pick among a fixed set of options. This
module turns the code editor's DOM into that decision: the stage we're in, the
enumerated choices, a ready-to-fire action for each, and the source code as
context for choosing. See `docs/code-observability-design.md`.

Split in two, mirroring the rest of the engine (pure logic, no browser):

  - `CHALLENGE_JS` — a browser scraper (run via `execute_script` from the top
    document; it walks same-origin frames itself). It only *scrapes* — it finds
    the `<table id="editor">`, reads its rows and the surrounding controls, and
    hands back raw material. No interpretation.
  - `assemble(raw)` — pure Python that shapes that raw material into the
    choice-focused view. This is the testable seam.

The editor is a plain `<table id="editor" aria-label="Code editor">` (Prism
highlighting), NOT Monaco/CodeMirror — the whole file is in the DOM at once, so
there is nothing to scroll or virtualize. Each `<tr>` is a marker gutter cell
followed by one or more (lineNo, code) column-pairs: one pair = the **locate**
stage (identify the vulnerable block), two pairs = the **fix** stage (a
base‖solution diff). See the design doc for the DOM autopsy.
"""

from __future__ import annotations

import re
from typing import Any

# `Select auth.py:100-100 as answer` — the locate-stage candidate controls.
_ANSWER_RE = re.compile(r"Select\s+(?P<file>.+?):(?P<from>\d+)-(?P<to>\d+)\s+as answer")
# `On the right side, show solution 3` — the fix-stage pane toggles.
_SOLUTION_RE = re.compile(r"On the (?P<side>left|right) side, show solution (?P<n>\d+)")

# The browser-side scraper. Returns a raw dict (see module docstring); `assemble`
# turns it into the decision view. Walks same-origin child frames from the top to
# find the editor, tracking the integer frame path so an action can target it
# (Selenium's `iframe, frame` index order matches querySelectorAll here).
CHALLENGE_JS = r"""
return (function () {
  function findEditor(win, path) {
    var d;
    try { d = win.document; } catch (e) { return null; }  // cross-origin: skip
    if (d.querySelector("table#editor, table[aria-label='Code editor']")) return { doc: d, path: path };
    var frames = d.querySelectorAll("iframe, frame");
    for (var i = 0; i < frames.length; i++) {
      var r = findEditor(frames[i].contentWindow, path.concat(i));
      if (r) return r;
    }
    return null;
  }

  var found = findEditor(window, []);
  if (!found) return { editor: null };
  var d = found.doc;

  // Rows -> panes. Per row, walk tds left-to-right; each (integer td, next td)
  // is a (lineNo, code) pair. Pair index 0 = first column, 1 = second column —
  // kept SEPARATE so fix-mode's base and solution never merge.
  var tbl = d.querySelector("table#editor, table[aria-label='Code editor']");
  var panesByCol = [];
  tbl.querySelectorAll("tbody > tr").forEach(function (tr) {
    var tds = tr.children, col = 0;
    for (var i = 0; i + 1 < tds.length; i++) {
      var t = (tds[i].innerText || "").trim();
      if (/^\d+$/.test(t)) {
        if (!panesByCol[col]) panesByCol[col] = {};
        var n = +t;
        if (!(n in panesByCol[col])) panesByCol[col][n] = (tds[i + 1].innerText || "").replace(/\n+$/, "");
        col++; i++;  // consume the code td too
      }
    }
  });
  var panes = panesByCol.map(function (byLine) {
    return Object.keys(byLine).map(Number).sort(function (a, b) { return a - b; })
      .map(function (n) { return { n: n, code: byLine[n] }; });
  });

  // File tabs (active + all), affordance controls, and best-effort context text.
  var tabs = [].map.call(d.querySelectorAll("[role=tab]"), function (el) {
    return { text: (el.innerText || "").trim(), selected: el.getAttribute("aria-selected") === "true" };
  }).filter(function (t) { return t.text; });

  var answers = [], seen = {};
  d.querySelectorAll("[aria-label]").forEach(function (el) {
    var a = el.getAttribute("aria-label") || "";
    if (/ as answer$/.test(a) && !seen[a]) { seen[a] = 1; answers.push(a); }
  });

  var toggles = [];
  d.querySelectorAll("button[aria-label], [role=button][aria-label]").forEach(function (el) {
    var a = el.getAttribute("aria-label") || "";
    if (/On the (left|right) side, show/.test(a))
      toggles.push({ label: a, pressed: el.getAttribute("aria-pressed") === "true" });
  });

  var submit = null;
  d.querySelectorAll("button[aria-label], button").forEach(function (el) {
    var a = el.getAttribute("aria-label") || el.innerText || "";
    if (!submit && /^\s*Submit answer\s*$/.test(a)) submit = "Submit answer";
  });

  // Best-effort context: the vulnerability category (text after that heading).
  var vuln = null;
  d.querySelectorAll("*").forEach(function (el) {
    if (vuln) return;
    if (/^Vulnerability Category$/.test((el.innerText || "").trim())) {
      var sib = el.nextElementSibling;
      if (sib) vuln = (sib.innerText || "").trim().slice(0, 120);
    }
  });

  return {
    editor: {
      framePath: found.path,
      file: (tabs.filter(function (t) { return t.selected; })[0] || {}).text || null,
      files: tabs.map(function (t) { return t.text; }),
      panes: panes,
    },
    answers: answers,
    toggles: toggles,
    submit: submit,
    vulnerability: vuln,
  };
})();
"""


def _click(aria_label: str, frame: list[int] | None) -> dict[str, Any]:
    """A ready-to-run `click` action dict (same vocabulary as `step`/`click`).

    Targets the control by its exact aria-label inside the editor frame, so the
    engine can act on an option without re-deriving a selector.
    """
    return {
        "action": "click",
        "by": "css",
        "selector": f'[aria-label="{aria_label}"]',
        "frame": frame,
    }


def _lines_in_range(pane: list[dict[str, Any]], lo: int, hi: int) -> list[dict[str, Any]] | None:
    """The pane rows in [lo, hi], or None if none are present (another file's block).

    In locate mode there is a single pane with its own numbering, so the answer
    label's line range indexes it directly.
    """
    by_line = {r["n"]: r["code"] for r in pane}
    got = [{"n": n, "code": by_line[n]} for n in range(lo, hi + 1) if n in by_line]
    return got or None


def _locate(raw: dict[str, Any], panes: list[list[dict[str, Any]]], frame: list[int] | None) -> dict[str, Any]:
    """Locate stage: identify which of the highlighted blocks is vulnerable.

    Options come from the `Select … as answer` controls (the submittable ones);
    each is paired with its source from the open file's pane. A block in a
    not-yet-open file gets `code: null` — switch to that file's tab to read it.
    """
    source = panes[0] if panes else []
    options: list[dict[str, Any]] = []
    for label in raw.get("answers") or []:
        m = _ANSWER_RE.search(label)
        if not m:
            continue
        lo, hi = int(m.group("from")), int(m.group("to"))
        options.append(
            {
                "id": f"{m.group('file')}:{lo}-{hi}",
                "file": m.group("file"),
                "from": lo,
                "to": hi,
                "code": _lines_in_range(source, lo, hi),
                "act": _click(label, frame),
            }
        )
    return {
        "decision": f"Identify which 1 of {len(options)} highlighted blocks is vulnerable",
        "options": options,
        "source": source,
    }


def _pane_shows(raw: dict[str, Any], side: str) -> str | None:
    """Which content a pane is currently showing, from its pressed toggle."""
    for t in raw.get("toggles") or []:
        if t.get("pressed"):
            m = _SOLUTION_RE.search(t.get("label") or "")
            if m and m.group("side") == side:
                return f"solution {m.group('n')}"
    return None


def _fix(raw: dict[str, Any], panes: list[list[dict[str, Any]]], frame: list[int] | None) -> dict[str, Any]:
    """Fix stage: pick which candidate solution to submit.

    The two panes are a diff — left is the comparison base (defaults to the
    vulnerable code), right is the selected solution. Options are the right-side
    solution toggles; `left_options` lets the engine re-base the left pane.
    Reading a *different* solution's diff means firing its `act` then re-reading
    (we can only see the currently-selected pane without mutating the UI).
    """
    base, solution = (panes + [[], []])[:2]
    options: list[dict[str, Any]] = []
    left_options: list[dict[str, Any]] = []
    for t in raw.get("toggles") or []:
        m = _SOLUTION_RE.search(t.get("label") or "")
        if not m:
            continue
        entry = {"id": int(m.group("n")), "selected": bool(t.get("pressed")), "act": _click(t["label"], frame)}
        (options if m.group("side") == "right" else left_options).append(entry)
    return {
        "decision": f"Pick which of {len(options)} candidate solutions to submit",
        "left": {"shows": _pane_shows(raw, "left") or "the vulnerable code", "code": base},
        "right": {"shows": _pane_shows(raw, "right"), "code": solution},
        "options": options,
        "left_options": left_options,
    }


def assemble(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape the browser scraper's raw output into the choice-focused view.

    Returns `{stage, file, vulnerability, decision, options, submit, …}`. Stage
    is `locate` (one code column), `fix` (two — a base‖solution diff), or `none`
    when no code editor is on the page. Each option carries a ready-to-fire `act`
    and its code context; `submit` is the action that commits the answer.
    """
    ed = raw.get("editor")
    if not ed:
        return {"stage": "none", "reason": "no code editor found on the page"}
    frame = ed.get("framePath")
    panes = ed.get("panes") or []
    common = {
        "file": ed.get("file"),
        "files": ed.get("files"),
        "vulnerability": raw.get("vulnerability"),
        "submit": _click(raw["submit"], frame) if raw.get("submit") else None,
    }
    stage = _fix(raw, panes, frame) if len(panes) >= 2 else _locate(raw, panes, frame)
    return {"stage": "fix" if len(panes) >= 2 else "locate", **common, **stage}
