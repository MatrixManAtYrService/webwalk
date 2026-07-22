"""Shared walker plumbing for flow CLIs.

The `Runner` (engine/runner.py) is pure — it needs `observe`/`act`/`http`/`eval`
callables wired to the server. That wiring is identical for every flow, so it
lives here: a flow CLI loads its states+rules, builds its sub-flows, and calls
`execute_walk`. Adding a new flow's `run` is then a handful of lines (see
`webwalk/flows/example/cli.py`).

`make_callbacks` is exposed separately because a flow's sub-flows sometimes need
the same `observe` (e.g. SCW's mission reader), so the flow builds callbacks once,
threads `observe` into its sub-flows, and hands the callbacks back to
`execute_walk`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from webwalk.cli._shared import _brief_actionable, _brief_network, _call, _frame_to_str

if TYPE_CHECKING:
    from webwalk.engine.subflow import SubFlow


@dataclass
class WalkCallbacks:
    """The server-facing callables the Runner drives, all bound to one window."""

    observe: Callable[[], dict[str, Any]]
    act: Callable[[dict[str, Any]], None]
    http: Callable[[str, str, dict[str, str], str | None], dict[str, Any]]
    eval_js: Callable[[str, str | None], Any]
    confirm: Callable[[list[str], dict[str, Any]], bool]


def make_callbacks(window: str = "") -> WalkCallbacks:
    """Build the observe/act/http/eval/confirm callables for a walk on `window`."""

    def observe() -> dict[str, Any]:
        result = _call("/observe", json={"label": "run", "window": window or None})
        return (result.get("record") or {}).get("state") or {}

    def act(ui: dict[str, Any]) -> None:
        result = _call(
            "/step",
            json={
                "action": ui.get("action"),
                "selector": ui.get("selector"),
                "value": ui.get("value"),
                "by": ui.get("by", "css"),
                "label": ui.get("label") or "rule",
                "window": window or None,
                "frame": _frame_to_str(ui.get("frame")),
                "settle": ui.get("settle", 0.0),
                "wait_after": ui.get("wait_after"),
                "wait_after_timeout": ui.get("wait_after_timeout", 10.0),
                "enter": ui.get("enter", False),
            },
        )
        if result.get("error"):
            raise typer.Exit(1)

    def http(method: str, url: str, headers: dict[str, str], body: str | None) -> dict[str, Any]:
        return _call("/fetch", json={"url": url, "method": method, "headers": headers or None, "body": body})

    def eval_js(script: str, frame: str | None = None) -> Any:
        return _call("/eval", json={"script": script, "frame": frame}).get("result")

    def confirm(matched: list[str], rule: dict[str, Any]) -> bool:
        return typer.confirm(f"[{', '.join(matched)}] run rule '{rule['id']}'?", default=True)

    return WalkCallbacks(observe, act, http, eval_js, confirm)


def load_course(states: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str] | None]:
    """Read a flow's states file into (state_defs, rule_defs, api_hosts). Exits with
    a friendly message on a missing file or an empty `rules:` block."""
    import yaml

    states_path = Path(states)
    if not states_path.exists():
        typer.echo(f"States file not found: {states_path}", err=True)
        raise typer.Exit(1)
    doc = yaml.safe_load(states_path.read_text()) or {}
    rule_defs = doc.get("rules", [])
    if not rule_defs:
        typer.echo(f"No rules in {states_path} — add a `rules:` block.", err=True)
        raise typer.Exit(1)
    return doc.get("states", []), rule_defs, doc.get("api_hosts")


def execute_walk(
    state_defs: list[dict[str, Any]],
    rule_defs: list[dict[str, Any]],
    api_hosts: list[str] | None,
    subflows: list[SubFlow],
    callbacks: WalkCallbacks,
    *,
    to: str = "",
    once: bool = False,
    yes: bool = False,
    brief: bool = False,
) -> dict[str, Any]:
    """Build the Runner from a flow's states/rules/sub-flows and walk it, printing
    the result (full JSON, or a compact digest with `brief`). Exits non-zero on a
    non-progress status so a scripted chain stops."""
    from webwalk.engine.runner import Runner

    runner = Runner(
        state_defs, rule_defs, callbacks.observe, callbacks.act,
        confirm=callbacks.confirm, http=callbacks.http, eval_js=callbacks.eval_js,
        api_hosts=api_hosts, subflows=subflows,
    )
    result = runner.run(to=to or None, max_steps=1 if once else 20, auto=yes)
    typer.echo(brief_run(result) if brief else json.dumps(result, indent=2, default=str))
    if result.get("status") in ("unknown", "ambiguous", "unexpected", "stuck"):
        raise typer.Exit(1)
    return result


# --- compact (--brief) rendering of a walk result -----------------------------
#
# Sub-flow aware but flow-agnostic: the mission/challenge/coding_lab keys are read
# with .get, so a flow that registers none simply doesn't emit those lines.


def brief_context(ctx: dict[str, Any]) -> list[str]:
    """Render a stuck-context block (generic + any sub-flow fields present)."""
    lines: list[str] = []
    for k in ("url", "title", "stage", "file", "vulnerability", "decision", "active_step"):
        if ctx.get(k) is not None:
            lines.append(f"  {k}: {ctx[k]}")
    if ctx.get("step"):
        step = ctx["step"]
        lines.append(f"  step: {step.get('order')} {step.get('title')!r} — {step.get('description', '')[:80]}")
    if ctx.get("options"):
        lines.append(f"  options: {[o.get('id') for o in ctx['options']]}")
    if ctx.get("steps_text"):
        lines.append(f"  steps_text: {ctx['steps_text'][:160]}…")
    if ctx.get("actionable"):
        lines.append(f"  actionable ({len(ctx['actionable'])}):")
        lines += [f"    {r}" for r in _brief_actionable(ctx["actionable"])]
    if ctx.get("network"):
        lines.append(f"  network ({len(ctx['network'])}):")
        lines += [f"    {r}" for r in _brief_network(ctx["network"])]
    if ctx.get("near_states"):
        lines.append(f"  near: {[(n.get('id'), n.get('signals')) for n in ctx['near_states']]}")
    return lines


def brief_history(history: list[dict[str, Any]]) -> list[str]:
    """One line per walk step: `rule from [states] -> landed/sub-status extra`."""
    lines: list[str] = []
    for h in history:
        sub = next((h[k] for k in ("mission", "challenge", "coding_lab") if k in h), None)
        if sub is not None:
            extra = " ".join(
                f"{k}={sub[k]}"
                for k in ("stage", "id", "active_index", "active_step", "handler")
                if sub.get(k) is not None
            )
            lines.append(f"  {h.get('rule')} from {h.get('from')} -> {sub.get('status')} {extra}".rstrip())
        else:
            lines.append(f"  {h.get('rule')} from {h.get('from')} -> {h.get('landed')}")
    return lines


def brief_run(result: dict[str, Any]) -> str:
    """A compact text digest of a walk result — the shape an agent actually reads."""
    lines = [f"status: {result.get('status')}"]
    for k in ("rule", "state", "matched", "expected", "landed", "url"):
        if result.get(k):
            lines.append(f"{k}: {result[k]}")
    for key in ("mission", "challenge", "coding_lab"):
        sub = result.get(key)
        if sub:
            detail = " ".join(
                f"{k}={sub[k]}"
                for k in ("id", "stage", "active_index", "active_step", "handler")
                if sub.get(k) is not None
            )
            lines.append(f"{key}: {sub.get('status')} {detail}".rstrip())
    history = result.get("history") or []
    if history:
        lines.append(f"history ({len(history)}):")
        lines += brief_history(history)
    ctx = result.get("context")
    if ctx:
        lines.append("context:")
        lines += brief_context(ctx)
    return "\n".join(lines)
