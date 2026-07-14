"""Solving an SCW coding-lab — writing code into a code-server editor.

A coding lab is the THIRD SCW task type (see mission.py, challenge.py). Unlike a
challenge (pick one of a fixed set of options), a coding lab asks the trainee to
*write code* into a cross-origin code-server (VS Code web) editor nested at frame
`[0, 0, 0]`, then Submit. The thinking — what code to write — is done once, by
hand; what's encoded here is the ASSOCIATION: a lab's content signature -> the
full file content to paste for each of its steps. This mirrors challenge.py's
answer key one task type over, and courses/coding-labs.yaml holds the keys.

Pure, like the rest of the engine: matching + act-building only, no I/O. The
runner supplies the observe snapshot and performs the acts. An act is the same
vocabulary the runner already executes: a `paste` (the clipboard action that puts
multi-line code into Monaco verbatim — synthetic events are ignored, and typing
fights auto-indent) followed by a Submit click.

DOM facts (captured live 2026-07-13, see the scw-coding-lab memory):
  - Editor: `.monaco-editor` in frame `[0, 0, 0]` (code-server, auto-save on).
  - Submit: `[data-testid="submit-enabled"]` in the wrapper frame `[0, 0]`.
  - Per-step completion shows as the text "step N is complete" in the wrapper;
    the highest N tells us how many steps are done → which step is active now.
"""

from __future__ import annotations

import re
from typing import Any

# Where the editor and its Submit control live (both nested, cross-origin frames).
EDITOR_SELECTOR = ".monaco-editor"
EDITOR_FRAME = [0, 0, 0]
SUBMIT_SELECTOR = '[data-testid="submit-enabled"]'
SUBMIT_FRAME = [0, 0]

# "step 2 is complete" — the per-step done marker some labs render in the wrapper's
# STEPS panel. Not universal: multi-step labs instead mark a done step by swapping its
# NUMBER badge for a checkmark (no text), so the panel simply stops showing that step's
# number — see `_active_step_from_badges`.
_COMPLETE_RE = re.compile(r"step\s+(\d+)\s+is complete", re.IGNORECASE)

# A step-number badge on its own line in the STEPS panel (e.g. "\n2\n"). A DONE step
# shows a checkmark instead of its number, so the FIRST number still present after the
# "STEPS" heading is the active step (1-based). Captured live 2026-07-13 on the 3-step
# "Improve Python logging" lab: 0 done → "STEPS\n1\nConfigure…"; 1 done → "STEPS\n
# Configure…\n2\nLog an informative…"; 2 done → "…\n3\nLog a warning…".
_STEP_BADGE_RE = re.compile(r"\n\s*(\d+)\s*\n")


def _active_step_from_badges(text: str) -> int | None:
    """0-based active step from the STEPS-panel number badges, or None if the panel
    isn't recognizable. The active step is the first whose number badge is still shown
    (done steps swap the number for a checkmark), so `active_step = first_number - 1`.

    Searches only *after* the "STEPS" heading so the code editor's line-number gutter
    (a later frame in the concatenated text) can't be mistaken for a step badge.
    """
    i = text.find("STEPS")
    if i < 0:
        return None
    m = _STEP_BADGE_RE.search(text[i:])
    return int(m.group(1)) - 1 if m else None


def assemble_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce an observe snapshot to the coding-lab decision view.

    Returns `{text, file, active_step}`:
      - `text` — all frame text concatenated (the lab's INTRODUCTION/STEPS copy),
        the stable content signature a handler matches on (`text_contains`).
      - `file` — the open editor tab (from the code-server textarea's aria-label),
        a secondary match signal.
      - `active_step` — the 0-based index of the step to solve now = how many
        steps are already complete (the highest "step N is complete" marker, 0 if
        none). Line up with the handler's `steps` list.
    """
    frames = payload.get("frames") or []
    text = "\n".join((f.get("text") or "") for f in frames)
    done = [int(n) for n in _COMPLETE_RE.findall(text)]
    # Two ways a lab signals progress: the "step N is complete" markers (single-step
    # labs) or the STEPS-panel number badges (multi-step labs, which show no such
    # marker — see _active_step_from_badges). Prefer the explicit marker; fall back to
    # the badge; default to 0 (mid-first-step) when neither is present.
    active_step = max(done) if done else (_active_step_from_badges(text) or 0)
    file = None
    for el in payload.get("elements") or []:
        if el.get("tag") == "textarea" and el.get("aria_label") and el.get("frame") == EDITOR_FRAME:
            file = el["aria_label"]
            break
    return {"text": text, "file": file, "active_step": active_step}


def _paste_act(content: str, selector: str, frame: list[int]) -> dict[str, Any]:
    """A ready-to-fire `paste` action: replace the editor's content with `content`."""
    return {"action": "paste", "by": "css", "selector": selector, "frame": frame,
            "value": content, "settle": 2}


def _open_file_act(filename: str) -> dict[str, Any]:
    """Click the editor tab for `filename`, so a step that lives in a different file than
    the one currently open (a multi-file lab) is pasted into the right editor. The tabs
    are `[role=tab]` elements in the code-server frame; match by the filename text."""
    return {"action": "click", "by": "xpath",
            "selector": f"//*[@role='tab'][contains(., '{filename}')]",
            "frame": EDITOR_FRAME, "settle": 2}


def _submit_act() -> dict[str, Any]:
    """The Submit click. A generous settle lets the pod run its tests and mark the
    step complete before the runner re-observes (so the next pass sees it done)."""
    return {"action": "click", "by": "css", "selector": SUBMIT_SELECTOR,
            "frame": SUBMIT_FRAME, "settle": 6}


def _lab_matches(match: dict[str, Any], view: dict[str, Any]) -> bool:
    """True if `match`'s predicates all hold for the coding-lab `view`.

    An empty/absent `match` never fires — a handler must state at least one positive
    signal, or it would claim every lab (the matcher.py rule). `file` is only checked
    when the view actually captured one, so an un-read tab doesn't block a match.
    """
    if not match:
        return False
    text = (view.get("text") or "").lower()
    if "text_contains" in match and str(match["text_contains"]).lower() not in text:
        return False
    return not ("file" in match and view.get("file") and match["file"] != view["file"])


def match_coding_lab(handlers: list[dict[str, Any]], view: dict[str, Any]) -> dict[str, Any] | None:
    """Find the answer-key handler for this lab and build the acts for its active step.

    Returns `{"id", "active_step", "done", "acts"}` — the acts being `[paste, submit]`
    for the step at `view.active_step` — or None if no handler matches (→ the runner
    stops with the lab as context so a key can be added). `done` is True when every
    step is already complete (active_step past the end); the runner then yields to the
    outer loop, which sees the completion screen. Matched on the lab's own content, so
    the same key fires on any replay.
    """
    if not view:
        return None
    for handler in handlers or []:
        if not _lab_matches(handler.get("match") or {}, view):
            continue
        steps = handler.get("steps") or []
        i = view.get("active_step") or 0
        if i >= len(steps):
            return {"id": handler.get("id"), "active_step": i, "done": True, "acts": []}
        step = steps[i]
        content = step.get("content", "") if isinstance(step, dict) else str(step)
        selector = step.get("selector", EDITOR_SELECTOR) if isinstance(step, dict) else EDITOR_SELECTOR
        frame = step.get("frame", EDITOR_FRAME) if isinstance(step, dict) else EDITOR_FRAME
        # A step may target a different file than the one currently open — `open_file`
        # switches the editor tab first (multi-file labs, e.g. logger.py then user.py).
        open_file = step.get("open_file") if isinstance(step, dict) else None
        acts = ([_open_file_act(open_file)] if open_file else []) + [_paste_act(content, selector, frame), _submit_act()]
        return {"id": handler.get("id"), "active_step": i, "done": False, "acts": acts}
    return None
