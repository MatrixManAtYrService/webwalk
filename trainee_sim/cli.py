"""trainee-sim CLI — a stateless front-end to the trainee-sim server.

  trainee-sim serve            start the server (holds the live browser session)
  trainee-sim connect [url]    open the browser at a URL (session persists)
  trainee-sim wait-for         block until a URL/element appears (scripted login)
  trainee-sim type … --into    type into a field (--secret for passwords)
  trainee-sim click --element  click an element
  trainee-sim telemetry on|off gate the observer (OFF during manual auth)
  trainee-sim status           connection + telemetry state
  trainee-sim observe          capture the current browser state as a step
  trainee-sim step             inject an action (click/type/navigate) + capture
  trainee-sim network          show captured training API traffic (gated)
  trainee-sim check            is the server up?

The server usually runs on another machine on the LAN. Point at it with
TS_SERVER_URL (default http://localhost:22133).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from trainee_sim.constants import DEFAULT_PORT

# Where fetched captures land on THIS (client) machine, so they're easy to read
# regardless of where the server/browser lives. Overridable for scripting.
LOCAL_DATA_DIR = Path(os.environ.get("TS_LOCAL_DATA_DIR", "./data"))

app = typer.Typer(
    help="trainee-sim — drive a Selenium browser through training material and capture the flow.\n\n"
    "Start `trainee-sim serve` on the target machine, then `connect`, do auth by hand, "
    "`telemetry on`, and start capturing with `observe` / `step`.",
    no_args_is_help=True,
)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Host to bind to (0.0.0.0 so a LAN client can reach it)")] = "0.0.0.0",  # noqa: S104
    port: Annotated[int, typer.Option(help="Port to bind to")] = DEFAULT_PORT,
    reload: Annotated[bool, typer.Option(help="Auto-reload for development")] = False,
) -> None:
    """Start the trainee-sim server (holds the live browser session in memory)."""
    import uvicorn

    from trainee_sim.server.logging import setup_logging

    setup_logging()
    uvicorn.run("trainee_sim.server.main:app", host=host, port=port, reload=reload)


@app.command()
def check(timeout: Annotated[float, typer.Option(help="Seconds to wait")] = 5.0) -> None:
    """Check whether the trainee-sim server is running."""
    import httpx

    from trainee_sim.client import get_server_url

    url = f"{get_server_url()}/health"
    try:
        resp = httpx.get(url, timeout=timeout)
        typer.echo(json.dumps({"healthy": resp.status_code == 200, "url": url, "body": resp.json()}))
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        typer.echo(json.dumps({"healthy": False, "url": url, "error": exc.__class__.__name__}))
        raise typer.Exit(1)


def _call(path: str, **kwargs) -> dict[str, Any]:
    """Make a server request, returning the JSON result (friendly error on down)."""
    from trainee_sim.client import ServerUnreachable, server_request

    try:
        return server_request(path, **kwargs)
    except ServerUnreachable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


def _request(path: str, **kwargs) -> None:
    """Make a server request and print the JSON result."""
    typer.echo(json.dumps(_call(path, **kwargs), indent=2, default=str))


def _next_local_path(subdir: str, label: str) -> Path:
    """Next numbered ./data/<subdir>/NNNNN-<label>.json path (continues numbering)."""
    from trainee_sim.server.capture import slugify

    out = LOCAL_DATA_DIR / subdir
    out.mkdir(parents=True, exist_ok=True)
    seq = 1 + max(
        (int(p.stem.split("-", 1)[0]) for p in out.glob("*.json") if p.stem.split("-", 1)[0].isdigit()),
        default=0,
    )
    return out / f"{seq:05d}-{slugify(label)}.json"


def _save_capture(record: dict[str, Any]) -> str:
    """Write a fetched capture record to ./data/captures/, numbered + slugified."""
    path = _next_local_path("captures", record.get("label", "step"))
    path.write_text(json.dumps(record, indent=2, default=str))
    return str(path)


def _save_network(payload: dict[str, Any]) -> str:
    """Write a network dump to ./data/network/, numbered — bodies can be large."""
    path = _next_local_path("network", "network")
    path.write_text(json.dumps(payload, indent=2, default=str))
    return str(path)


def _request_capturing(path: str, save: bool = False, **kwargs) -> None:
    """Make an observe/step request and print a summary.

    The full record is saved locally to ./data/ only when telemetry is on
    (`recorded`) or `--save` is passed — so a telemetry-off login flow, incl.
    the password step, writes nothing to disk by default.
    """
    result = _call(path, **kwargs)
    record = result.pop("record", None)
    if record is not None and (save or result.get("recorded")):
        result["local_path"] = _save_capture(record)
    typer.echo(json.dumps(result, indent=2, default=str))
    # A failed action (e.g. selector not found) returns an "error" body; exit
    # non-zero so a scripted chain (login.nu) halts instead of barreling on.
    if result.get("error"):
        raise typer.Exit(1)


@app.command()
def connect(
    url: Annotated[str, typer.Argument(help="URL to open (defaults to TS_START_URL)")] = "",
) -> None:
    """Open the browser on the server and navigate to a URL.

    Telemetry stays OFF: complete login/MFA and load the training in the headed
    window, then run `trainee-sim telemetry on` before capturing anything.

    The browser profile persists across restarts, so once you've logged in the
    session is usually remembered and connect drops you straight in.
    """
    _request("/session/connect", json={"start_url": url or None})


@app.command()
def telemetry(
    state: Annotated[str, typer.Argument(help="'on' or 'off'")],
) -> None:
    """Open or close the telemetry gate. OFF means the observer doesn't look."""
    if state not in ("on", "off"):
        typer.echo("state must be 'on' or 'off'", err=True)
        raise typer.Exit(1)
    _request(f"/telemetry/{state}")


@app.command()
def status() -> None:
    """Show connection + telemetry state."""
    _request("/session/status", method="GET")


def _parse_duration(s: str) -> float:
    """Parse a short duration like '5s', '500ms', '2m' (bare number = seconds)."""
    s = s.strip().lower()
    try:
        if s.endswith("ms"):
            return float(s[:-2]) / 1000
        if s.endswith("s"):
            return float(s[:-1])
        if s.endswith("m"):
            return float(s[:-1]) * 60
        return float(s)
    except ValueError:
        raise typer.BadParameter(f"bad duration {s!r} (try 5s, 500ms, 2m)")


@app.command()
def network(
    since: Annotated[str, typer.Option(help="Only events from the last <dur> (e.g. 5s, 500ms, 2m)")] = "",
    full: Annotated[bool, typer.Option(help="Print every event's full JSON to stdout (noisy)")] = False,
    save: Annotated[bool, typer.Option(help="Write the full dump to ./data/network/ (on by default)")] = True,
) -> None:
    """Show the captured training API traffic (requests/responses + ws frames).

    Bodies can be huge, so by default this writes the full dump to
    ./data/network/ and prints a compact one-line-per-event index plus the file
    path — paste that path to inspect the detail. Pass --full to dump everything
    to stdout instead. Events are the ones buffered since the last observe/step
    (a non-destructive peek). Empty while telemetry is off — capture is gated by
    the same switch, so auth traffic is never recorded. The same per-request
    detail also rides inside each observe/step capture (a `network` key).

    Tip: set TS_NETWORK_HOSTS on the server (e.g. "securecodewarrior.com") to
    filter out analytics/RUM noise and keep only the training's own API.

    \b
    Examples:
        trainee-sim network                 # everything buffered since last step
        trainee-sim network --since 5s      # just the last 5 seconds
    """
    params = {"since": _parse_duration(since)} if since else None
    result = _call("/network", method="GET", params=params)
    events = result.get("events", []) or []
    if full:
        typer.echo(json.dumps(result, indent=2, default=str))
        return
    saved = _save_network(result) if save else None
    summary = {k: result.get(k) for k in ("enabled", "hosts", "buffered", "pending", "since", "shown")}
    summary["saved"] = saved
    typer.echo(json.dumps(summary, indent=2, default=str))
    for e in events:
        if e.get("type") == "ws":
            payload = (e.get("payload") or "")[:70]
            typer.echo(f"  ws {e.get('direction', ''):8} op={e.get('opcode')} {payload}")
        else:
            method = str(e.get("method") or "")
            status = str(e.get("status") or "···")
            url = str(e.get("url") or "")[:110]
            typer.echo(f"  {method:4} {status:>3} {url}")


@app.command()
def windows() -> None:
    """List open browser windows/tabs (index, url, title, which is active).

    Selenium only sees the focused window, so a "launch course" button that
    opens a new tab won't be observed until you `switch` to it.
    """
    _request("/session/windows", method="GET")


@app.command()
def switch(
    target: Annotated[
        str, typer.Argument(help="Window to focus: an index (0,1,…), 'latest', 'first', or a url/title substring")
    ],
) -> None:
    """Switch the driver's focus to another window/tab."""
    _request("/session/switch", json={"target": target})


@app.command()
def observe(
    label: Annotated[str, typer.Option(help="Short label for this step's capture file")] = "observe",
    window: Annotated[str, typer.Option(help="Switch to this window first (index/'latest'/url substring)")] = "",
    save: Annotated[bool, typer.Option(help="Save the full capture to ./data/ even when telemetry is off")] = False,
) -> None:
    """Capture the current browser state; a compact summary prints to stdout.

    The full page state (elements, DOM, open windows) is written to
    ./data/captures/ when telemetry is on, or when you pass --save (for
    inspection during a telemetry-off login flow).
    """
    _request_capturing("/observe", save=save, json={"label": label, "window": window or None})


@app.command()
def step(
    action: Annotated[str, typer.Argument(help="click | type | navigate")],
    selector: Annotated[str, typer.Option(help="Selector for click/type (CSS by default; see --by)")] = "",
    value: Annotated[str, typer.Option(help="Text to type, or URL to navigate to")] = "",
    label: Annotated[str, typer.Option(help="Short label for this step's capture file")] = "",
    window: Annotated[str, typer.Option(help="Switch to this window first (index/'latest'/url substring)")] = "",
    by: Annotated[str, typer.Option(help="Selector type: css or xpath")] = "css",
    frame: Annotated[str, typer.Option(help="Target an element inside a (nested) iframe, e.g. '0' or '0/1'")] = "",
    settle: Annotated[float, typer.Option(help="Seconds to wait before the after-snapshot (let SPA render)")] = 0.0,
    wait_after: Annotated[str, typer.Option(help="Or: wait until this selector is visible before snapshotting")] = "",
    wait_after_timeout: Annotated[float, typer.Option(help="Timeout for --wait-after (seconds)")] = 10.0,
    enter: Annotated[bool, typer.Option(help="For type: press Enter after typing (submit the field)")] = False,
    save: Annotated[bool, typer.Option(help="Save the full capture to ./data/ even when telemetry is off")] = False,
) -> None:
    """Inject an action and capture before/after state.

    \b
    Examples:
        trainee-sim step click --selector "button.next"
        trainee-sim step click --by xpath --selector "//button[contains(.,'Launch')]"
        trainee-sim step type  --selector "#search" --value "hello"
        trainee-sim step navigate --value "https://training.local/module/2"
    """
    _request_capturing(
        "/step",
        save=save,
        json={
            "action": action,
            "selector": selector or None,
            "value": value or None,
            "label": label or None,
            "by": by,
            "window": window or None,
            "frame": frame or None,
            "settle": settle,
            "wait_after": wait_after or None,
            "wait_after_timeout": wait_after_timeout,
            "enter": enter,
        },
    )


@app.command(name="wait-for")
def wait_for(
    url: Annotated[str, typer.Option(help="Wait until the page URL contains this substring")] = "",
    element: Annotated[str, typer.Option(help="Wait until this selector is visible")] = "",
    by: Annotated[str, typer.Option(help="Selector type for --element: css or xpath")] = "css",
    timeout: Annotated[float, typer.Option(help="Seconds to wait before giving up")] = 30.0,
    window: Annotated[str, typer.Option(help="Switch to this window first")] = "",
    frame: Annotated[str, typer.Option(help="Look for --element inside a (nested) iframe, e.g. '0' or '0/1'")] = "",
) -> None:
    """Block until the page reaches a URL and/or shows an element (or times out).

    Lets a scripted login ride out redirects. Exits non-zero on timeout, so a
    recalled chain of commands stops if a step never appears.

    \b
    Examples:
        trainee-sim wait-for --url login.microsoftonline.com
        trainee-sim wait-for --element "input#i0116"
    """
    result = _call(
        "/wait",
        json={
            "url": url or None,
            "element": element or None,
            "by": by,
            "timeout": timeout,
            "window": window or None,
            "frame": frame or None,
        },
    )
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("matched"):
        raise typer.Exit(1)


@app.command()
def type(  # noqa: A001 — the CLI verb really is "type"
    value: Annotated[str, typer.Argument(help="Text to type (omit if using --env)")] = "",
    into: Annotated[str, typer.Option("--into", help="Selector of the field to type into")] = "",
    by: Annotated[str, typer.Option(help="Selector type: css or xpath")] = "css",
    env: Annotated[
        str, typer.Option(help="Read the value from this env var instead (keeps it out of shell history)")
    ] = "",
    secret: Annotated[bool, typer.Option(help="Redact the value everywhere it's recorded (use for passwords)")] = False,
    label: Annotated[str, typer.Option(help="Short label for this step")] = "",
    window: Annotated[str, typer.Option(help="Switch to this window first")] = "",
    frame: Annotated[str, typer.Option(help="Type into a field inside a (nested) iframe, e.g. '0' or '0/1'")] = "",
    enter: Annotated[bool, typer.Option(help="Press Enter after typing (submit the field, e.g. an address bar)")] = False,  # noqa: E501
    settle: Annotated[float, typer.Option(help="Seconds to wait before the after-snapshot (let SPA render)")] = 0.0,
    save: Annotated[bool, typer.Option(help="Save the capture to ./data/ even when telemetry is off")] = False,
) -> None:
    """Type text into a field (a friendlier `step type`).

    \b
    Examples:
        trainee-sim type Firstname.Lastname@Fiserv.com --into "input#i0116"
        trainee-sim type --into "input[type=password]" --env TS_PW --secret
        trainee-sim type "/auth/logout" --into "#browserBar" --frame 0/0 --enter
    """
    if env:
        value = os.environ.get(env, "")
        if not value:
            typer.echo(f"Env var {env!r} is empty or unset.", err=True)
            raise typer.Exit(1)
    if not into:
        typer.echo("Which field? Pass --into with a selector.", err=True)
        raise typer.Exit(1)
    _request_capturing(
        "/step",
        save=save,
        json={
            "action": "type",
            "selector": into,
            "value": value,
            "by": by,
            "secret": secret,
            "label": label or ("type-secret" if secret else None),
            "window": window or None,
            "frame": frame or None,
            "enter": enter,
            "settle": settle,
        },
    )


@app.command()
def click(
    element: Annotated[str, typer.Option("--element", help="Selector of the element to click")] = "",
    by: Annotated[str, typer.Option(help="Selector type: css or xpath")] = "css",
    label: Annotated[str, typer.Option(help="Short label for this step")] = "",
    window: Annotated[str, typer.Option(help="Switch to this window first")] = "",
    frame: Annotated[str, typer.Option(help="Click an element inside a (nested) iframe, e.g. '0' or '0/1'")] = "",
    settle: Annotated[float, typer.Option(help="Seconds to wait before the after-snapshot (let SPA render)")] = 0.0,
    wait_after: Annotated[str, typer.Option(help="Or: wait until this selector is visible before snapshotting")] = "",
    wait_after_timeout: Annotated[float, typer.Option(help="Timeout for --wait-after (seconds)")] = 10.0,
    save: Annotated[bool, typer.Option(help="Save the capture to ./data/ even when telemetry is off")] = False,
) -> None:
    """Click an element (a friendlier `step click`).

    \b
    Examples:
        trainee-sim click --element "input#idSIButton9"
        trainee-sim click --by xpath --element "//button[contains(.,'Launch')]"
        trainee-sim click --frame 0 --element "button.next"   # inside the first iframe
        trainee-sim click --frame 0 --element "a.course" --wait-after "button.resume"
    """
    if not element:
        typer.echo("Which element? Pass --element with a selector.", err=True)
        raise typer.Exit(1)
    _request_capturing(
        "/step",
        save=save,
        json={
            "action": "click",
            "selector": element,
            "by": by,
            "label": label or None,
            "window": window or None,
            "frame": frame or None,
            "settle": settle,
            "wait_after": wait_after or None,
            "wait_after_timeout": wait_after_timeout,
        },
    )


@app.command()
def where(
    states: Annotated[str, typer.Option(help="YAML file of state signatures")] = "courses/secure-code-warrior.yaml",
    window: Annotated[str, typer.Option(help="Observe this window (index/'latest'/url substring)")] = "",
) -> None:
    """Observe the browser and report which known course-state(s) you're in.

    The foundation of the engine: match the live page against a course's state
    signatures to locate our position — so a flow can be resumed from anywhere.
    Zero matches means "unknown" (add a signature); more than one means the
    signatures overlap and should be tightened.
    """
    import yaml

    from trainee_sim.engine.matcher import match_states

    states_path = Path(states)
    if not states_path.exists():
        typer.echo(f"States file not found: {states_path}", err=True)
        raise typer.Exit(1)
    state_defs = (yaml.safe_load(states_path.read_text()) or {}).get("states", [])

    result = _call("/observe", json={"label": "where", "window": window or None})
    payload = (result.get("record") or {}).get("state") or {}
    matched = match_states(state_defs, payload)
    typer.echo(
        json.dumps(
            {
                "matched": matched,
                "unambiguous": len(matched) == 1,
                "url": payload.get("url"),
                "title": payload.get("title"),
            },
            indent=2,
            default=str,
        )
    )
    if not matched:
        raise typer.Exit(1)


def _frame_to_str(frame: Any) -> str | None:
    """Turn a YAML frame path ([0] or [3,1]) into the '/'-joined string /step wants."""
    if frame is None:
        return None
    if isinstance(frame, list):
        return "/".join(str(s) for s in frame)
    return str(frame)


@app.command()
def run(
    to: Annotated[str, typer.Option(help="Walk until we reach this state id (else run until no rule applies)")] = "",
    states: Annotated[str, typer.Option(help="YAML file of state signatures + rules")] = "courses/secure-code-warrior.yaml",  # noqa: E501
    missions: Annotated[str, typer.Option(help="YAML file of reusable mission step-handlers")] = "courses/missions.yaml",  # noqa: E501
    window: Annotated[str, typer.Option(help="Observe/act on this window (index/'latest'/url substring)")] = "",
    once: Annotated[bool, typer.Option(help="Take a single step, then stop")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't prompt before each step (autonomous)")] = False,
) -> None:
    """Walk the course state graph: observe → match → act → verify, resumable.

    The engine (engine/runner.py) observes the live browser, matches the current
    state, runs an applicable rule (from the `rules:` in the states file), then
    re-observes to check it landed where the rule expected. It resumes from
    wherever the browser actually is. A rule with no `expect` is a discovery
    step: the runner reports where it landed so you can add that state's
    signature. Telemetry should be ON so each step's capture records the traffic.

    \b
    Examples:
        trainee-sim run --once            # take one step (e.g. click Resume), confirm first
        trainee-sim run --to scw-task -y  # walk toward a goal, no prompts
    """
    import yaml

    from trainee_sim.engine.runner import Runner

    states_path = Path(states)
    if not states_path.exists():
        typer.echo(f"States file not found: {states_path}", err=True)
        raise typer.Exit(1)
    doc = yaml.safe_load(states_path.read_text()) or {}
    state_defs = doc.get("states", [])
    rule_defs = doc.get("rules", [])
    api_hosts = doc.get("api_hosts")  # filters the network view in stuck-context
    if not rule_defs:
        typer.echo(f"No rules in {states_path} — add a `rules:` block.", err=True)
        raise typer.Exit(1)

    # The shared, cross-mission step-handler library (optional — only used once the
    # walker enters a mission).
    missions_path = Path(missions)
    mission_steps = (yaml.safe_load(missions_path.read_text()) or {}).get("steps", []) if missions_path.exists() else []

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

    def http_fetch(method: str, url: str, headers: dict[str, str], body: str | None) -> dict[str, Any]:
        return _call("/fetch", json={"url": url, "method": method, "headers": headers or None, "body": body})

    def eval_js(script: str) -> Any:
        return _call("/eval", json={"script": script}).get("result")

    def confirm(matched: list[str], rule: dict[str, Any]) -> bool:
        return typer.confirm(f"[{', '.join(matched)}] run rule '{rule['id']}'?", default=True)

    # Mission reader: `activeIndex` is read fresh from portal /challenges each call
    # (it changes as steps complete); the step playbook (instruction[]) is static
    # per mission, so it's fetched once from captured /api/missions traffic and
    # cached. Auth mirrors the video-gate: X-Session-ID = localStorage.sessionId.
    mission_playbook: dict[str, Any] = {}

    def _playbook(url: str) -> list[dict[str, Any]]:
        if "steps" not in mission_playbook:
            events = _call("/network", method="GET").get("events") or []
            md = _event_json(_find_event(events, "/api/missions/"))
            if not md:  # not buffered — bounce through about:blank to re-fire it
                _call("/step", json={"action": "navigate", "value": "about:blank"})
                rec = _call("/step", json={"action": "navigate", "value": url, "settle": 6.0})
                events = ((rec.get("record") or {}).get("after") or {}).get("network") or []
                md = _event_json(_find_event(events, "/api/missions/"))
            mission_playbook["steps"] = sorted((md or {}).get("instruction") or [], key=lambda s: s.get("order", 0))
        return mission_playbook["steps"]

    def read_mission() -> dict[str, Any] | None:
        cur = (_call("/session/status", method="GET").get("current_url")) or ""
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
        active_index = body.get("activeIndex") if isinstance(body, dict) else None
        return {"active_index": active_index, "steps": _playbook(cur)}

    def verify_step(step_id: str, timeout: float = 20.0, interval: float = 2.0) -> bool:
        """A step is done when the player posts /api/instructions/{id}/complete — the
        live signal the pod drives (portal's activeIndex lags). /network is a
        non-destructive peek, so we poll the buffer as the (async) event arrives."""
        if not step_id:
            return False
        needle = f"/api/instructions/{step_id}/complete"
        for _ in range(max(1, int(timeout / interval))):
            for e in _call("/network", method="GET").get("events") or []:
                ok = e.get("ok") or str(e.get("status") or "").startswith("2")
                if e.get("type") == "http" and needle in str(e.get("url") or "") and ok:
                    return True
            time.sleep(interval)
        return False

    runner = Runner(
        state_defs, rule_defs, observe, act, confirm=confirm, http=http_fetch, eval_js=eval_js,
        api_hosts=api_hosts, read_mission=read_mission, mission_steps=mission_steps, verify_step=verify_step,
    )
    result = runner.run(to=to or None, max_steps=1 if once else 20, auto=yes)
    typer.echo(json.dumps(result, indent=2, default=str))
    if result.get("status") in ("unknown", "ambiguous", "unexpected", "stuck"):
        raise typer.Exit(1)


@app.command()
def fetch(
    url: Annotated[str, typer.Argument(help="URL to request (the training's own API)")],
    method: Annotated[str, typer.Option("--method", "-X", help="HTTP method")] = "GET",
    data: Annotated[str, typer.Option(help="Request body — a raw string (JSON string sends as JSON)")] = "",
    header: Annotated[list[str], typer.Option("--header", "-H", help="Extra header 'Name: value' (repeatable)")] = [],  # noqa: B006
    page_auth: Annotated[bool, typer.Option("--page-auth", "-a", help="Attach the SPA's own bearer token from web storage")] = False,  # noqa: E501
) -> None:
    """Fire a request from inside the logged-in page (carries its session).

    The UI→API ratchet: hit the training's API directly instead of puppeting the
    UI. Runs in the top document's origin with credentials, so a same-origin API
    gets the page's cookies. SCW's portal-api instead needs a bearer token — pass
    --page-auth to reuse the SPA's own token (kept in-browser), or set it yourself
    with -H 'Authorization: Bearer …'. Non-2xx comes back normally (see status/ok).

    \b
    Examples:
        trainee-sim fetch https://portal-api.securecodewarrior.com/ping
        trainee-sim fetch .../auth/resume --page-auth
        trainee-sim fetch .../taskAttempt/<id>/answer -X POST --data '{}' -a
    """
    headers: dict[str, str] = {}
    for h in header:
        if ":" not in h:
            typer.echo(f"Bad --header {h!r}; use 'Name: value'.", err=True)
            raise typer.Exit(1)
        name, value = h.split(":", 1)
        headers[name.strip()] = value.strip()
    result = _call(
        "/fetch",
        json={"url": url, "method": method, "headers": headers or None, "body": data or None, "page_auth": page_auth},
    )
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise typer.Exit(1)


@app.command(name="eval")
def eval_js(
    script: Annotated[str, typer.Argument(help="JS to run in the page (a bare expression is auto-returned)")],
) -> None:
    """Run JS in the logged-in page and print its result (introspection).

    The escape hatch for what `observe` doesn't show — web storage, app state, or
    how the SPA authenticates. Runs in the top document. A bare expression is
    wrapped in `return (…)`; use explicit `return` for multi-statement scripts.

    \b
    Examples:
        trainee-sim eval "Object.keys(localStorage)"
        trainee-sim eval "return Object.entries(localStorage).map(([k,v])=>k+'='+v.slice(0,40))"
    """
    _request("/eval", json={"script": script})


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


@app.command()
def mission(
    reload: Annotated[bool, typer.Option(help="Reload the mission to capture fresh API traffic (re-inits the pod)")] = False,  # noqa: E501
    window: Annotated[str, typer.Option(help="Observe/act on this window (index/'latest'/url substring)")] = "",
) -> None:
    """Expose the current mission's step playbook (titles + copy-paste values).

    Missions are server-verified challenges (see docs/mission-api.md) — no cheap
    API skip — but the experiential-mission API hands us the steps as data. This
    reads the captured `/api/missions/*` (the steps) and portal `/challenges`
    (the current `activeIndex`) and prints them. Telemetry must be ON.

    By default it reads what's already buffered; pass --reload to navigate the
    mission afresh (fires the API again, but re-initialises the pod). If nothing
    is found, you're probably not on a mission, or telemetry was off on load.
    """
    if reload:
        status = _call("/session/status", method="GET")
        url = status.get("current_url") or ""
        if "/mission/" not in url:
            typer.echo("Not on a mission (url has no /mission/). Navigate to one first.", err=True)
            raise typer.Exit(1)
        # Re-navigating to the same hash URL is a no-op in the SPA, so bounce
        # through about:blank to force a real reload (re-fires /api/missions).
        _call("/step", json={"action": "navigate", "value": "about:blank"})
        rec = _call("/step", json={"action": "navigate", "value": url, "settle": 6.0, "window": window or None})
        after = (rec.get("record") or {}).get("after") or {}
        events = after.get("network") or []
    else:
        events = _call("/network", method="GET").get("events") or []

    mission_def = _event_json(_find_event(events, "/api/missions/"))
    challenges = _event_json(_find_event(events, "/task/")) or {}
    if not mission_def:
        typer.echo(
            "No /api/missions/* traffic captured. Ensure telemetry is on and you're on a "
            "mission; try `trainee-sim mission --reload`.",
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
def shutdown() -> None:
    """Close the browser session on the server."""
    _request("/session/shutdown")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
