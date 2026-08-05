"""github-tempo — a webwalk flow for driving corp GitHub (github.corp.clover.com).

The end goal of this flow is a timecard aid: gather the pull requests you've
merged over a window and the Jira keys attached to them, so the dates show which
Jiras you spent time on. It reuses webwalk's browser verbs (connect / observe /
step / … via the core CLI) and adds a `run` walker over its own
`data/states.yaml`.

The flow starts with a login. Auth against corp GitHub is scripted in
`login.nu` (beside this file) the same way the SCW flow scripts its login:
credentials come from env vars, the username is prepopulated into the sign-in
form, and MFA is left to the human. See that file to (re)establish a cold session.

No server-side observer is needed — PR/Jira collection is done by hitting the
page's own search from inside the logged-in session (fetch / eval), not by a
snapshot extension, so this app registers no server plugins.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Annotated

import typer

from webwalk.cli._shared import LOCAL_DATA_DIR, _call
from webwalk.cli._walk import execute_walk, load_course, make_callbacks
from webwalk.cli.core import build_app

_DATA = Path(__file__).resolve().parent / "data"
# Reviewable artifacts land here so `plan` and `fill` can hand off between runs.
_PLAN_PATH = LOCAL_DATA_DIR / "timecard" / "plan.json"

app = build_app(
    "github-tempo — drive corp GitHub (github.corp.clover.com): browser verbs "
    "(connect / observe / step / …) plus a `run` walker. Start a cold session with "
    "login.nu (prepopulates your username, password from $env.TS_PW, MFA by hand)."
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
        github-tempo run --once      # take one step, confirm first
        github-tempo run -y --brief  # walk autonomously, compact output
    """
    state_defs, rule_defs, api_hosts = load_course(states)
    execute_walk(
        state_defs, rule_defs, api_hosts, subflows=[],
        callbacks=make_callbacks(window), to=to, once=once, yes=yes, brief=brief,
    )


# --- timecard pipeline: merged PRs -> Jira keys -> Tempo worklogs --------------
#
# The whole job, encoded so it's repeatable (see timecard.py for the API details):
#   github-tempo prs  --since 2026-06-28     # merged PRs -> Jira keys (read-only)
#   github-tempo gaps                        # which Tempo periods are unfilled
#   github-tempo plan --since 2026-06-28     # one Jira/day, saved for review
#   github-tempo fill                        # dry-run the saved plan
#   github-tempo fill --yes                  # write it (reopens closed issues)
# Both hosts must be logged in first (login.nu for GitHub; the Jira section there
# for Tempo). Run from a machine that can reach the server (WW_SERVER).


@app.command()
def prs(
    since: Annotated[str, typer.Option(help="Merged on/after this date (YYYY-MM-DD)")],
    save: Annotated[bool, typer.Option(help="Write the collected PRs to ./data/timecard/")] = True,
) -> None:
    """List the PRs you merged since <since> and the tracked Jira key(s) in each."""
    from webwalk.flows.github_tempo import timecard as tc

    prs = tc.collect_prs(since)
    for p in prs:
        merged = (p["merged"] or "")[:10]
        typer.echo(f"  {merged}  {','.join(p['keys']):<24} {p['pr']}")
    typer.echo(f"{len(prs)} PRs with a tracked Jira key (projects: {', '.join(tc.PROJECTS)})")
    if save:
        _PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        (_PLAN_PATH.parent / "prs.json").write_text(json.dumps(prs, indent=2))


@app.command()
def gaps(
    periods: Annotated[int, typer.Option(help="How many recent periods to show")] = 8,
) -> None:
    """Show each Tempo period's status and logged-vs-required hours (find the gap)."""
    from webwalk.flows.github_tempo import timecard as tc

    for p in tc.period_statuses(periods):
        per = p["period"]
        w, r = p["workedSeconds"] / 3600, p["requiredSeconds"] / 3600
        flag = "  <-- MISSING" if (w == 0 and r > 0 and per["dateTo"] < dt.date.today().isoformat()) else ""
        typer.echo(f"  {per['dateFrom']}..{per['dateTo']}  {p['status']:<16}{w:>5.0f}h /{r:>4.0f}h{flag}")


@app.command()
def plan(
    since: Annotated[str, typer.Option(help="Consider PRs merged on/after this date (YYYY-MM-DD)")],
) -> None:
    """Compute one Jira per required working day across the unfilled periods, and save it."""
    from webwalk.flows.github_tempo import timecard as tc

    prs = tc.collect_prs(since)
    miss = tc.missing_periods(tc.period_statuses(8))
    if not miss:
        typer.echo("No unfilled past periods — nothing to plan.")
        return
    the_plan = tc.allocate(prs, miss)
    for day, key in the_plan.items():
        typer.echo(f"  {day}  {key}")
    typer.echo(f"{len(the_plan)} day(s) across periods: " + ", ".join(f"{a}..{b}" for a, b in miss))
    _PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PLAN_PATH.write_text(json.dumps(the_plan, indent=2))
    typer.echo(f"saved plan -> {_PLAN_PATH}")


@app.command()
def fill(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Actually write worklogs (default is a dry run)")] = False,
) -> None:
    """Write the saved plan into Tempo, reopening/restoring closed issues as needed.

    Without --yes this only reports what it would do. Periods are left unsubmitted.
    """
    from webwalk.flows.github_tempo import timecard as tc

    if not _PLAN_PATH.exists():
        typer.echo(f"No plan at {_PLAN_PATH}. Run `github-tempo plan --since <date>` first.", err=True)
        raise typer.Exit(1)
    the_plan = json.loads(_PLAN_PATH.read_text())
    results = tc.fill(the_plan, dry_run=not yes)
    for r in results:
        if not yes:
            typer.echo(f"  would log {r['day']}  {r['key']}  ({r['seconds'] / 3600:.0f}h)")
        else:
            ro = " (reopened)" if r.get("reopened") else ""
            typer.echo(f"  {r['day']}  {r['key']}  ok={r['ok']} status={r['status']} id={r['worklogId']}{ro}")
    if not yes:
        typer.echo(f"\nDry run — {len(results)} worklog(s) would be written. Re-run with --yes to commit.")
    else:
        bad = [r for r in results if not r["ok"]]
        tail = f" {len(bad)} FAILED." if bad else ""
        typer.echo(f"\nWrote {len(results) - len(bad)}/{len(results)} worklogs.{tail}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
