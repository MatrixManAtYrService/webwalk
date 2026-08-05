"""grafana-sim — a webwalk flow for exploring corp Grafana (clovernetwork.grafana.net).

Auth is corporate SSO, reachable only behind zscaler/VPN, and is done by hand in
the headed window (telemetry off, so it's never captured). Once logged in, the
flow reads Grafana's own HTTP API from *inside* the session: the calls are
same-origin, so the browser's SSO cookie authenticates them — including
/api/datasources, which a Loki data-plane access-policy token is rejected from
(that lives in a different auth realm; see skills-ebb-data-access).

The first useful verb is `datasources`: it dumps each datasource's name + uid.
That's the region → Loki-datasource-uid map used to build Grafana Explore
deep-links in billing-airflow (clover_billing.airflow_provider._grafana), so this
flow is the durable, token-free way to re-confirm those uids when they change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from webwalk.cli._shared import _call
from webwalk.cli._walk import execute_walk, load_course, make_callbacks
from webwalk.cli.core import build_app

# The corp Grafana instance (UI + HTTP API host), reachable via zscaler/VPN.
HOST = "clovernetwork.grafana.net"
_DATA = Path(__file__).resolve().parent / "data"

app = build_app(
    "grafana-sim — explore corp Grafana (clovernetwork.grafana.net): the core browser "
    "verbs (connect / observe / step / …) plus `where`, `run`, and `datasources`. Log "
    "in by hand in the headed window (SSO/MFA), then read the API from the session."
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
        grafana-sim run --once      # take one step, confirm first
        grafana-sim run -y --brief  # walk autonomously, compact output
    """
    state_defs, rule_defs, api_hosts = load_course(states)
    execute_walk(
        state_defs, rule_defs, api_hosts, subflows=[],
        callbacks=make_callbacks(window), to=to, once=once, yes=yes, brief=brief,
    )


@app.command()
def datasources(
    all_types: Annotated[bool, typer.Option("--all", help="Show every datasource type, not just Loki")] = False,
) -> None:
    """List the Grafana instance's datasources (name + uid) from the logged-in session.

    Fires GET /api/datasources from inside the page, so the browser's SSO session
    authenticates it — no service-account token needed. Loki-only by default (the
    Explore deep-link uids); pass --all for every type.
    """
    result = _call("/fetch", json={
        "url": f"https://{HOST}/api/datasources",
        "method": "GET",
        "headers": None,
        "body": None,
        "page_auth": False,
        "frame": None,
    })

    body = result.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = None
    if not isinstance(body, list):
        status = result.get("status")
        typer.echo(
            f"Unexpected /api/datasources response (status={status}). Are you logged in? "
            f"Raw: {json.dumps(result, default=str)[:400]}",
            err=True,
        )
        raise typer.Exit(1)

    rows = [d for d in body if all_types or d.get("type") == "loki"]
    rows.sort(key=lambda d: (d.get("type", ""), d.get("name", "")))
    for d in rows:
        typer.echo(f"  {d.get('type', ''):<12} {d.get('uid', ''):<40} {d.get('name', '')}")
    suffix = "" if all_types else " (Loki only; pass --all for every type)"
    typer.echo(f"{len(rows)} datasource(s){suffix}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
