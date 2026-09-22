"""SCW flow assembly — reusable across `scw-sim` and any external SCW-based flow.

This is the code half of the SCW flow, separated from its CLI so the *knowledge*
(states/rules/answer keys) can live in a different repo while the *mechanics* are
imported from webwalk. A flow manifest wires it up by dotted path::

    plugins:
      server:   webwalk.flows.scw.observers
      subflows: webwalk.flows.scw.reader:build_subflows
      cli:      webwalk.flows.scw.reader:register_cli

`build_subflows` constructs the mission/challenge/coding-lab sub-flows (including
the mission reader/verifier that reads the observable step-checkboxes rather than
portal's stale `activeIndex`). `register_cli` adds the SCW-specific `mission` and
`challenge` verbs. Everything site-specific about *how to read* SCW lives here;
what to answer lives in the flow's `data/*.yaml`.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from webwalk.cli._shared import _call
from webwalk.flows.scw.subflows import ChallengeSubFlow, CodingLabSubFlow, MissionSubFlow

if TYPE_CHECKING:
    from webwalk.cli._walk import WalkCallbacks
    from webwalk.engine.subflow import SubFlow
    from webwalk.flow import Flow

_INSTR_CHECK = re.compile(r"instr-check-(\d+)")


def _load_handlers(path: Path | None, key: str) -> list[dict[str, Any]]:
    """Read an answer-key / handler library (optional — empty if absent)."""
    import yaml

    if not path or not Path(path).exists():
        return []
    return (yaml.safe_load(Path(path).read_text()) or {}).get(key, [])


# --- mission checkbox helpers (the synchronous, in-snapshot completion signal) ---


def _step_checks(elements: list[dict[str, Any]]) -> dict[int, bool]:
    """Map step order -> checked, from the captured `instr-check-{n}` checkboxes."""
    checks: dict[int, bool] = {}
    for el in elements:
        m = _INSTR_CHECK.match(str(el.get("id") or ""))
        if m:
            checks[int(m.group(1))] = bool(el.get("checked"))
    return checks


def _active_index_from_checkboxes(elements: list[dict[str, Any]]) -> int | None:
    """Current step = first unticked checkbox among those that EXIST (all ticked →
    past the end). None when no checkboxes are captured."""
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
    """Decode a captured event's response body (may be a JSON string or dict)."""
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
    (same-origin, its own auth via page_auth) — deterministic, no ring-buffer
    dependence and no pod-destroying reload. None if not on a mission."""
    endpoint = _mission_playbook_endpoint()
    if not endpoint:
        return None
    resp = _call("/fetch", json={"url": endpoint, "frame": "0/0", "page_auth": True})
    body = resp.get("body") if isinstance(resp, dict) else None
    return body if isinstance(body, dict) else None


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


# --- the sub-flow factory (referenced by the manifest's plugins.subflows) ---


def build_subflows(flow: Flow, callbacks: WalkCallbacks, window: str = "") -> list[SubFlow]:
    """Build the SCW mission / challenge / coding-lab sub-flows for a walk.

    The answer-key libraries come from the flow's own `data/*.yaml` (so they can
    live in an external repo); the reading/verification mechanics come from here.
    """
    mission_steps = _load_handlers(flow.data.get("missions"), "steps")
    challenge_handlers = _load_handlers(flow.data.get("challenges"), "challenges")
    coding_lab_handlers = _load_handlers(flow.data.get("coding_labs"), "coding_labs")

    # The step playbook (instruction[]) is static per mission, so fetch it once.
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
        payload = callbacks.observe()
        checks = _step_checks(payload.get("elements") or [])
        active_index = _active_index_from_checkboxes(payload.get("elements") or [])
        if active_index is None:
            active_index = _portal_active_index(cur) or 0
        return {"active_index": active_index, "steps": steps, "checkable": set(checks)}

    def verify_step(order: int | None, timeout: float = 20.0, interval: float = 2.0) -> bool:
        """A step is done when its checklist checkbox ticks (`instr-check-{order}`)."""
        if order is None:
            return False
        for _ in range(max(1, int(timeout / interval))):
            payload = callbacks.observe()
            if _checkbox_checked(payload.get("elements") or [], order):
                return True
            time.sleep(interval)
        return False

    return [
        MissionSubFlow(read_mission, mission_steps, verify_step),
        ChallengeSubFlow(challenge_handlers),
        CodingLabSubFlow(coding_lab_handlers),
    ]


# --- extra CLI verbs (referenced by the manifest's plugins.cli) ---


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


def register_cli(app: typer.Typer, flow: Flow) -> None:
    """Add SCW's `mission` and `challenge` verbs to a flow CLI."""

    @app.command()
    def mission(
        reload: Annotated[bool, typer.Option(help="Reload the mission to capture fresh API traffic (re-inits the pod)")] = False,  # noqa: E501
        window: Annotated[str, typer.Option(help="Observe/act on this window (index/'latest'/url substring)")] = "",
    ) -> None:
        """Expose the current mission's step playbook (titles + copy-paste values).

        Reads the experiential-mission API (the steps) and portal `/challenges` (the
        current `activeIndex`). Telemetry must be ON. By default it fetches the
        playbook directly from the player frame (deterministic — no buffer scrape,
        no reload). --reload is a legacy last resort that re-navigates the mission.
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

    @app.command()
    def challenge(
        source: Annotated[bool, typer.Option(help="Print the full line-numbered source, not the decision view")] = False,  # noqa: E501
    ) -> None:
        """Read the current SCW challenge as a decision: stage, options, source.

        A challenge is a choice among fixed options. Prints the stage (`locate` =
        identify the vulnerable block, `fix` = pick a solution), the enumerated
        options each with a ready-to-fire `act`, and the code as context. `--source`
        dumps the raw line-numbered file(s). Requires the SCW server plugin.
        """
        result = _call("/challenge", method="GET")
        if source:
            result = _challenge_source(result)
        typer.echo(json.dumps(result, indent=2, default=str))
