"""State matching for the trainee-sim engine — the "where am I?" core.

A course is described by a set of *state signatures*: predicates over an
`observe` payload (its url, title, and flattened element list with frame paths).
The engine observes the live browser and matches the payload against every
signature, so it can identify its position without assuming a fixed starting
point — the thing that lets a flow be driven from the beginning OR the middle.

Signatures are plain data (authored in YAML). A signature matches when:
  - `url_contains` / `title_contains` (if given) are substrings of the page, and
  - every predicate in `elements` is satisfied by *some* captured element.

An element predicate matches one element when every field it specifies agrees:
  frame (exact path), tag/type/id/name (exact), text (exact, whitespace-norm),
  text_contains / aria_contains / href_contains (case-insensitive substring).
"""

from __future__ import annotations

from typing import Any


def _norm(s: str | None) -> str:
    """Collapse whitespace so captured multi-line text compares cleanly."""
    return " ".join((s or "").split())


def element_matches(pred: dict[str, Any], el: dict[str, Any]) -> bool:
    """True if a single captured element satisfies every field in `pred`."""
    if "frame" in pred and el.get("frame") != pred["frame"]:
        return False
    for key in ("tag", "type", "id", "name"):
        if key in pred and (el.get(key) or "") != pred[key]:
            return False
    if "text" in pred and _norm(el.get("text")) != _norm(pred["text"]):
        return False
    if "text_contains" in pred and _norm(pred["text_contains"]).lower() not in _norm(el.get("text")).lower():
        return False
    if "aria_contains" in pred and str(pred["aria_contains"]).lower() not in (el.get("aria_label") or "").lower():
        return False
    return not (
        "href_contains" in pred and str(pred["href_contains"]).lower() not in (el.get("href") or "").lower()
    )


def explain_state(sig: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Match `sig` against `payload` and explain the outcome.

    Returns `{"id", "matched", "signals", "reasons"}`:
      - `matched`  — the same boolean `state_matches` returns;
      - `signals`  — how many of the signature's checks *passed* (its
        url/title substrings + element predicates), so a caller can rank
        near-misses: a state that hit 2 of 3 signals is "closer" than one that
        hit none — the difference between "loosen this signature" and "author a
        new one";
      - `reasons`  — a human-readable line per *failed* check, so a stuck agent
        sees exactly why a plausible state didn't match ("no element matches
        {'id': 'email'}").

    This is the single source of truth for matching; `state_matches` and
    `match_states` are thin wrappers over it.
    """
    url = payload.get("url") or ""
    title = payload.get("title") or ""
    element_preds = sig.get("elements") or []
    elements = payload.get("elements") or []

    signals = 0
    reasons: list[str] = []

    if "url_contains" in sig:
        if str(sig["url_contains"]).lower() in url.lower():
            signals += 1
        else:
            reasons.append(f"url_contains {sig['url_contains']!r} not in url")
    if "title_contains" in sig:
        if str(sig["title_contains"]).lower() in title.lower():
            signals += 1
        else:
            reasons.append(f"title_contains {sig['title_contains']!r} not in title")
    for pred in element_preds:
        if any(element_matches(pred, el) for el in elements):
            signals += 1
        else:
            reasons.append(f"no element matches {pred!r}")

    # A signature needs at least one positive signal, or it would match anything.
    has_signal = "url_contains" in sig or "title_contains" in sig or bool(element_preds)
    matched = has_signal and not reasons
    return {"id": sig.get("id"), "matched": matched, "signals": signals, "reasons": reasons}


def state_matches(sig: dict[str, Any], payload: dict[str, Any]) -> bool:
    """True if the observe `payload` satisfies the state signature `sig`."""
    return explain_state(sig, payload)["matched"]


def match_states(states: list[dict[str, Any]], payload: dict[str, Any]) -> list[str]:
    """Return the ids of every state whose signature matches the payload.

    Ideally exactly one. Zero means "unknown" (add/loosen a signature); more than
    one means signatures overlap and should be tightened.
    """
    return [r["id"] for r in match_report(states, payload) if r["matched"]]


def match_report(states: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """`explain_state` for every id'd state — the input to near-miss diagnostics."""
    return [explain_state(s, payload) for s in states if "id" in s]
