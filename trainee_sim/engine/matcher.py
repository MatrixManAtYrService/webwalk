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


def state_matches(sig: dict[str, Any], payload: dict[str, Any]) -> bool:
    """True if the observe `payload` satisfies the state signature `sig`."""
    url = payload.get("url") or ""
    title = payload.get("title") or ""
    element_preds = sig.get("elements") or []

    # A signature needs at least one positive signal, or it would match anything.
    if "url_contains" not in sig and "title_contains" not in sig and not element_preds:
        return False

    if "url_contains" in sig and str(sig["url_contains"]).lower() not in url.lower():
        return False
    if "title_contains" in sig and str(sig["title_contains"]).lower() not in title.lower():
        return False

    elements = payload.get("elements") or []
    return all(any(element_matches(pred, el) for el in elements) for pred in element_preds)


def match_states(states: list[dict[str, Any]], payload: dict[str, Any]) -> list[str]:
    """Return the ids of every state whose signature matches the payload.

    Ideally exactly one. Zero means "unknown" (add/loosen a signature); more than
    one means signatures overlap and should be tightened.
    """
    return [s["id"] for s in states if "id" in s and state_matches(s, payload)]
