"""example-sim — a template flow CLI for webwalk.

This is the smallest useful flow: it reuses webwalk's browser verbs (connect /
observe / step / … via the core CLI) and adds a `run` walker over its own
`data/states.yaml`. Because the observe→match→act→verify plumbing lives in
webwalk/cli/_walk.py, a flow's `run` is just: load the course, build callbacks,
walk. A flow with no "one level down" activities registers no sub-flows.

To start a real flow, copy `webwalk/flows/example` to `webwalk/flows/<yourflow>`,
rename it, fill in `data/states.yaml`, and add a `<yourflow>-sim` script in
pyproject.toml. If the flow needs a server-side observer (like SCW's challenge
scraper), add an `observers.py` and pass `server_plugins=` to `build_app` below.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from webwalk.cli._shared import _call
from webwalk.cli._walk import execute_walk, load_course, make_callbacks
from webwalk.cli.core import build_app

_DATA = Path(__file__).resolve().parent / "data"

app = build_app(
    "example-sim — a template webwalk flow. Copy webwalk/flows/example to start a "
    "real flow, then author its states/rules in data/states.yaml.",
    # A flow needing a server-side snapshot observer would pass, e.g.,
    # server_plugins="webwalk.flows.example.observers"
)


@app.command()
def where(
    states: Annotated[str, typer.Option(help="YAML file of state signatures")] = str(_DATA / "states.yaml"),
    window: Annotated[str, typer.Option(help="Observe this window (index/'latest'/url substring)")] = "",
) -> None:
    """Observe the browser and report which known state(s) you're in."""
    import yaml

    from webwalk.engine.matcher import match_states

    states_path = Path(states)
    if not states_path.exists():
        typer.echo(f"States file not found: {states_path}", err=True)
        raise typer.Exit(1)
    state_defs = (yaml.safe_load(states_path.read_text()) or {}).get("states", [])

    result = _call("/observe", json={"label": "where", "window": window or None})
    payload = (result.get("record") or {}).get("state") or {}
    matched = match_states(state_defs, payload)
    typer.echo(json.dumps(
        {"matched": matched, "unambiguous": len(matched) == 1,
         "url": payload.get("url"), "title": payload.get("title")},
        indent=2, default=str,
    ))
    if not matched:
        raise typer.Exit(1)


@app.command()
def run(
    to: Annotated[str, typer.Option(help="Walk until we reach this state id (else run until no rule applies)")] = "",
    states: Annotated[str, typer.Option(help="YAML file of state signatures + rules")] = str(_DATA / "states.yaml"),
    window: Annotated[str, typer.Option(help="Observe/act on this window (index/'latest'/url substring)")] = "",
    once: Annotated[bool, typer.Option(help="Take a single step, then stop")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't prompt before each step (autonomous)")] = False,
    brief: Annotated[bool, typer.Option("--brief", help="Print a compact text summary instead of the full JSON")] = False,  # noqa: E501
) -> None:
    """Walk this flow's state graph: observe → match → act → verify, resumable.

    \b
    Examples:
        example-sim run --once      # take one step, confirm first
        example-sim run -y --brief  # walk autonomously, compact output
    """
    state_defs, rule_defs, api_hosts = load_course(states)
    # No sub-flows: a plain page-state walk. A flow with mission/challenge-like
    # activities would pass subflows=[...] here (see webwalk/flows/scw/cli.py).
    execute_walk(
        state_defs, rule_defs, api_hosts, subflows=[],
        callbacks=make_callbacks(window), to=to, once=once, yes=yes, brief=brief,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
