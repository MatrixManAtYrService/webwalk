"""Mission step-matching — the "which step are we on, and how do we do it?" core.

A mission (docs/mission-api.md) is a second observe→match→act→verify loop one
level *down* from the page-state graph. Its "state" is the current forward step
(instruction[activeIndex] from the mission API), and its "rules" are the reusable,
parameterized step-handlers in courses/missions.yaml.

Crucially, a handler matches on the *step's own signature* — its title and
description — never on which mission we're in. The same "Log in as …" step recurs
across many missions (they're near-copies with one value changed), so a handler
authored once must fire in every mission that reuses that step. This mirrors how
matcher.py matches page states globally rather than per-flow.

Pure, like matcher.py: no I/O. Given the current step and the handler library it
answers "which handler applies, with what extracted values" and renders the
handler's `do` actions (templating the extracted values in) ready for the runner
to execute through its normal act path.
"""

from __future__ import annotations

import re
from typing import Any

# `{name}` placeholders in a handler's `do` — filled from values extracted out of
# the step text. Kept local (not imported from runner) so runner can import this
# module without a cycle.
_VAR = re.compile(r"\{(\w+)\}")


def _fill(value: str, ctx: dict[str, Any]) -> str:
    """Replace `{name}` with `ctx[name]`; unknown names are left as-is."""
    return _VAR.sub(lambda m: str(ctx.get(m.group(1), m.group(0))), value)


def _step_matches(match: dict[str, Any], step: dict[str, Any]) -> bool:
    """True if `match`'s predicates all hold for the current step.

    An empty/absent `match` never fires — a handler must state at least one
    positive signal, or it would claim every step (the matcher.py rule).
    """
    if not match:
        return False
    title = step.get("title") or ""
    desc = step.get("description") or ""
    if "title_regex" in match and not re.search(match["title_regex"], title):
        return False
    if "title_contains" in match and str(match["title_contains"]).lower() not in title.lower():
        return False
    if "desc_regex" in match and not re.search(match["desc_regex"], desc):
        return False
    return not ("desc_contains" in match and str(match["desc_contains"]).lower() not in desc.lower())


def _extract(extract: dict[str, Any] | None, step: dict[str, Any]) -> dict[str, Any]:
    """Pull the "one thing they changed" out of the step text into template vars.

    Each var reads from a field (`from`, default `description`), applies `regex`
    (group 1 unless `group` is given), and falls back to `default` when the
    pattern doesn't hit (e.g. "enter any password you like" → no password token).
    """
    ctx: dict[str, Any] = {}
    for var, spec in (extract or {}).items():
        src = step.get(spec.get("from", "description")) or ""
        value = spec.get("default")
        if "regex" in spec:
            m = re.search(spec["regex"], src)
            if m:
                value = m.group(spec.get("group", 1))
        ctx[var] = value
    return ctx


def _render(obj: Any, ctx: dict[str, Any]) -> Any:
    """Template `{vars}` into every string within a nested action structure."""
    if isinstance(obj, str):
        return _fill(obj, ctx)
    if isinstance(obj, list):
        return [_render(x, ctx) for x in obj]
    if isinstance(obj, dict):
        return {k: _render(v, ctx) for k, v in obj.items()}
    return obj


def match_step(steps: list[dict[str, Any]], step: dict[str, Any]) -> dict[str, Any] | None:
    """Find the first handler whose signature matches `step`, or None if it's an
    un-encoded step (→ the runner stops with context so a handler can be added).

    Returns `{"id", "ctx", "do"}`: the handler id, the values extracted from the
    step text, and the handler's `do` actions with those values already templated
    in — ready to hand straight to the runner's act path.
    """
    for rule in steps or []:
        if _step_matches(rule.get("match") or {}, step):
            ctx = _extract(rule.get("extract"), step)
            return {"id": rule.get("id"), "ctx": ctx, "do": _render(rule.get("do"), ctx)}
    return None
