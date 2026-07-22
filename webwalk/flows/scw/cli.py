"""scw-sim — the Secure Code Warrior flow CLI.

Reuses webwalk's flow-agnostic browser verbs (connect / observe / step / … via
`register_core_commands`, plus a `serve` that loads the SCW server plugin so the
challenge observer + /challenge are available) and adds the SCW-specific commands:

  * `run`        — walk the SCW course state graph (observe → match → act → verify),
                   handing off to the mission / challenge / coding-lab sub-flows.
  * `where`      — report which SCW course-state you're in.
  * `mission`    — expose the current mission's step playbook.
  * `challenge`  — read the current challenge as a decision (stage, options, source).

The state signatures, rules, and answer keys live beside this file in data/. The
generic walk plumbing lives in webwalk/cli/_walk.py; here we add only what's
SCW-specific — the mission reader/verifier and the answer-key sub-flows.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from webwalk.cli._shared import _call
from webwalk.cli._walk import execute_walk, load_course, make_callbacks
from webwalk.cli.core import build_app

# The SCW course data (state signatures + rules + answer keys) ships beside this
# module, so `scw-sim run` works from anywhere without a --states path.
_DATA = Path(__file__).resolve().parent / "data"

app = build_app(
    "scw-sim — drive Secure Code Warrior training: browser verbs (connect / observe / "
    "step / …) plus a `run` walker that resumes the course from wherever you are.",
    server_plugins="webwalk.flows.scw.observers",
)


def _load_handlers(path_str: str, key: str) -> list[dict[str, Any]]:
    """Read an answer-key / handler library (optional — empty if the file is absent)."""
    import yaml

    path = Path(path_str)
    return (yaml.safe_load(path.read_text()) or {}).get(key, []) if path.exists() else []


# --- where am I? ---


@app.command()
def where(
    states: Annotated[str, typer.Option(help="YAML file of state signatures")] = str(_DATA / "states.yaml"),
    window: Annotated[str, typer.Option(help="Observe this window (index/'latest'/url substring)")] = "",
) -> None:
    """Observe the browser and report which known course-state(s) you're in.

    Match the live page against the course's state signatures to locate our
    position — so a flow can be resumed from anywhere. Zero matches means "unknown"
    (add a signature); more than one means the signatures overlap (tighten them).
    """
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
        {
            "matched": matched,
            "unambiguous": len(matched) == 1,
            "url": payload.get("url"),
            "title": payload.get("title"),
        },
        indent=2, default=str,
    ))
    if not matched:
        raise typer.Exit(1)


# --- the walk ---


@app.command()
def run(
    to: Annotated[str, typer.Option(help="Walk until we reach this state id (else run until no rule applies)")] = "",
    states: Annotated[str, typer.Option(help="YAML file of state signatures + rules")] = str(_DATA / "states.yaml"),
    missions: Annotated[str, typer.Option(help="YAML file of reusable mission step-handlers")] = str(_DATA / "missions.yaml"),  # noqa: E501
    challenges: Annotated[str, typer.Option(help="YAML file of challenge answer-key handlers")] = str(_DATA / "challenges.yaml"),  # noqa: E501
    coding_labs: Annotated[str, typer.Option(help="YAML file of coding-lab answer-key handlers")] = str(_DATA / "coding-labs.yaml"),  # noqa: E501
    window: Annotated[str, typer.Option(help="Observe/act on this window (index/'latest'/url substring)")] = "",
    once: Annotated[bool, typer.Option(help="Take a single step, then stop")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't prompt before each step (autonomous)")] = False,
    brief: Annotated[bool, typer.Option("--brief", help="Print a compact text summary instead of the full JSON")] = False,  # noqa: E501
) -> None:
    """Walk the course state graph: observe → match → act → verify, resumable.

    The engine observes the live browser, matches the current state, runs an
    applicable rule (from the `rules:` in the states file), then re-observes to
    check it landed where the rule expected. It resumes from wherever the browser
    actually is. A rule with no `expect` is a discovery step. Telemetry should be
    ON so each step's capture records the traffic.

    \b
    Examples:
        scw-sim run --once             # take one step (e.g. click Resume), confirm first
        scw-sim run --to scw-task -y   # walk toward a goal, no prompts
    """
    from webwalk.flows.scw.subflows import ChallengeSubFlow, CodingLabSubFlow, MissionSubFlow

    state_defs, rule_defs, api_hosts = load_course(states)
    mission_steps = _load_handlers(missions, "steps")
    challenge_handlers = _load_handlers(challenges, "challenges")
    coding_lab_handlers = _load_handlers(coding_labs, "coding_labs")

    cb = make_callbacks(window)

    # Mission reader: the entry point (which step we're on) comes from the observable
    # step-checkboxes, NOT portal's `activeIndex` — portal's is a stale mirror that
    # stays 0 even mid-mission. The step playbook (instruction[]) is static per
    # mission, so it's fetched once from the experiential player frame and cached.
    mission_playbook: dict[str, Any] = {}

    def _playbook() -> list[dict[str, Any]]:
        if "steps" not in mission_playbook:
            md = _fetch_mission_def()
            if not md:
                events = _call("/network", method="GET").get("events") or []
                md = _event_json(_find_event(events, "/api/missions/"))
            mission_playbook["steps"] = sorted((md or {}).get("instruction") or [], key=lambda s: s.get("order", 0))
        return mission_playbook["steps"]

    def read_mission() -> dict[str, Any] | None:
        cur = (_call("/session/status", method="GET").get("current_url")) or ""
        if "/mission/course/" not in cur:
            return None
        steps = _playbook()
        payload = cb.observe()
        checks = _step_checks(payload.get("elements") or [])
        active_index = _active_index_from_checkboxes(payload.get("elements") or [])
        if active_index is None:
            active_index = _portal_active_index(cur) or 0
        return {"active_index": active_index, "steps": steps, "checkable": set(checks)}

    def verify_step(order: int | None, timeout: float = 20.0, interval: float = 2.0) -> bool:
        """A step is done when its checklist checkbox ticks — the synchronous signal
        the observe snapshot carries (`instr-check-{order}` with `checked`)."""
        if order is None:
            return False
        for _ in range(max(1, int(timeout / interval))):
            payload = cb.observe()
            if _checkbox_checked(payload.get("elements") or [], order):
                return True
            time.sleep(interval)
        return False

    def _portal_active_index(cur: str) -> int | None:
        """Fallback entry hint from portal /challenges (stale — only trusted when the
        checkboxes aren't captured)."""
        m = re.search(r"/mission/course/([^/]+)/([^/]+)/([^/]+)/([^/]+)", cur)
        if not m:
            return None
        course, lang, mod, task = m.groups()
        sid = _call("/eval", json={"script": "localStorage.sessionId"}).get("result")
        ch = _call("/fetch", json={
            "url": f"https://portal-api.securecodewarrior.com/courses/userCourse/{course}"
                   f"/language/{lang}/module/{mod}/task/{task}/challenges",
            "method": "GET", "headers": {"X-Session-ID": sid}})
        body = ch.get("body") if isinstance(ch, dict) else None
        return body.get("activeIndex") if isinstance(body, dict) else None

    execute_walk(
        state_defs, rule_defs, api_hosts,
        subflows=[
            MissionSubFlow(read_mission, mission_steps, verify_step),
            ChallengeSubFlow(challenge_handlers),
            CodingLabSubFlow(coding_lab_handlers),
        ],
        callbacks=cb, to=to, once=once, yes=yes, brief=brief,
    )


# --- challenge decision view ---


@app.command()
def challenge(
    source: Annotated[bool, typer.Option(help="Print the full line-numbered source, not the decision view")] = False,
) -> None:
    """Read the current SCW challenge as a decision: stage, options, source.

    A challenge is a choice among fixed options. This prints the stage
    (`locate` = identify the vulnerable block, `fix` = pick a solution), the
    enumerated options each with a ready-to-fire `act`, and the code as context.
    `--source` dumps the raw line-numbered file(s). `stage: "none"` means no editor
    is up. Requires the server to be running the SCW plugin (`scw-sim serve`).
    """
    result = _call("/challenge", method="GET")
    if source:
        result = _challenge_source(result)
    typer.echo(json.dumps(result, indent=2, default=str))


def _challenge_source(view: dict[str, Any]) -> dict[str, Any]:
    """Reduce a challenge view to just its line-numbered code pane(s)."""
    def _fmt(lines: list[dict[str, Any]]) -> list[str]:
        return [f"{ln['n']:>4} | {ln['code']}" for ln in (lines or [])]

    if view.get("stage") == "locate":
        return {"stage": "locate", "file": view.get("file"), "source": _fmt(view.get("source"))}
    if view.get("stage") == "fix":
        left, right = view.get("left") or {}, view.get("right") or {}
        return {
            "stage": "fix", "file": view.get("file"),
            "left": {"shows": left.get("shows"), "source": _fmt(left.get("code"))},
            "right": {"shows": right.get("shows"), "source": _fmt(right.get("code"))},
        }
    return view


# --- mission checkbox helpers (shared by `run`'s reader and `mission`) ---

_INSTR_CHECK = re.compile(r"instr-check-(\d+)")


def _step_checks(elements: list[dict[str, Any]]) -> dict[int, bool]:
    """Map step order -> checked, from the captured `instr-check-{n}` checkboxes."""
    checks: dict[int, bool] = {}
    for el in elements:
        m = _INSTR_CHECK.match(str(el.get("id") or ""))
        if m:
            checks[int(m.group(1))] = bool(el.get("checked"))
    return checks


def _active_index_from_checkboxes(elements: list[dict[str, Any]]) -> int | None:
    """The current step = the first unticked checkbox among the ones that EXIST
    (all ticked → past the end). None when no checkboxes are captured."""
    checks = _step_checks(elements)
    if not checks:
        return None
    for i in sorted(checks):
        if not checks[i]:
            return i
    return max(checks) + 1


def _checkbox_checked(elements: list[dict[str, Any]], order: int) -> bool:
    """Whether the step at `order` has its checklist box ticked."""
    return _step_checks(elements).get(order, False)


def _find_event(events: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
    """First captured HTTP event whose url contains `needle` (newest last wins)."""
    hit = None
    for e in events:
        if e.get("type") == "http" and needle in str(e.get("url") or ""):
            hit = e
    return hit


def _event_json(event: dict[str, Any] | None) -> Any:
    """Decode a captured event's response body (it may be a JSON string or dict)."""
    if not event:
        return None
    body = event.get("response_body")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            return None
    return body


def _mission_playbook_endpoint() -> str | None:
    """The mission's `/api/missions/{alias}` URL, derived from the experiential
    player iframe (frame [0,0]) — origin and alias both read from its own location."""
    return _call("/eval", json={
        "frame": "0/0",
        "script": "return (function(){var m=location.href.match(/\\/missions\\/[^/]+\\/([^/?]+)/);"
                  "return m ? location.origin + '/api/missions/' + m[1] : null;})();",
    }).get("result")


def _fetch_mission_def() -> dict[str, Any] | None:
    """Fetch the mission playbook directly from the experiential player frame [0,0]
    (same-origin, with that frame's own auth via page_auth) — deterministic, no
    ring-buffer dependence and no pod-destroying reload. None if not on a mission."""
    endpoint = _mission_playbook_endpoint()
    if not endpoint:
        return None
    resp = _call("/fetch", json={"url": endpoint, "frame": "0/0", "page_auth": True})
    body = resp.get("body") if isinstance(resp, dict) else None
    return body if isinstance(body, dict) else None


@app.command()
def mission(
    reload: Annotated[bool, typer.Option(help="Reload the mission to capture fresh API traffic (re-inits the pod)")] = False,  # noqa: E501
    window: Annotated[str, typer.Option(help="Observe/act on this window (index/'latest'/url substring)")] = "",
) -> None:
    """Expose the current mission's step playbook (titles + copy-paste values).

    Reads the experiential-mission API (the steps) and portal `/challenges` (the
    current `activeIndex`) and prints them. Telemetry must be ON. By default it
    fetches the playbook directly from the player frame (deterministic — no buffer
    scrape, no reload). --reload is a legacy last resort that re-navigates the
    mission. If nothing is found, you're probably not on a mission.
    """
    events: list[dict[str, Any]] = []
    if reload:
        status = _call("/session/status", method="GET")
        url = status.get("current_url") or ""
        if "/mission/" not in url:
            typer.echo("Not on a mission (url has no /mission/). Navigate to one first.", err=True)
            raise typer.Exit(1)
        _call("/step", json={"action": "navigate", "value": "about:blank"})
        rec = _call("/step", json={"action": "navigate", "value": url, "settle": 6.0, "window": window or None})
        after = (rec.get("record") or {}).get("after") or {}
        events = after.get("network") or []
        mission_def = _event_json(_find_event(events, "/api/missions/"))
    else:
        mission_def = _fetch_mission_def()
        if not mission_def:
            events = _call("/network", method="GET").get("events") or []
            mission_def = _event_json(_find_event(events, "/api/missions/"))

    challenges = _event_json(_find_event(events, "/task/")) or {}
    if not mission_def:
        typer.echo(
            "No mission playbook found. Ensure telemetry is on and you're on a mission "
            "(the experiential player must be loaded in frame [0,0]).",
            err=True,
        )
        raise typer.Exit(1)

    steps = sorted(mission_def.get("instruction") or [], key=lambda s: s.get("order", 0))
    active = challenges.get("activeIndex")
    flag = (steps[0].get("mission") or {}).get("flag") if steps else None
    pod = ((mission_def.get("missionData") or {}).get("url") or "").split("?")[0]

    typer.echo(json.dumps({
        "mission": mission_def.get("id"),
        "name": mission_def.get("name"),
        "active_index": active,
        "flag": flag,
        "pod": pod,
        "steps": [
            {"order": s.get("order"), "current": s.get("order") == active,
             "title": s.get("title"), "description": (s.get("description") or "").strip()}
            for s in steps
        ],
    }, indent=2, default=str))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
