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
  trainee-sim check            is the server up?

The server usually runs on another machine on the LAN. Point at it with
TS_SERVER_URL (default http://localhost:22133).
"""

from __future__ import annotations

import json
import os
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
        typer.echo(json.dumps({"healthy": False, "url": url, "error": type(exc).__name__}))
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


def _save_capture(record: dict[str, Any]) -> str:
    """Write a fetched capture record to ./data/captures/, numbered + slugified."""
    from trainee_sim.server.capture import slugify

    captures = LOCAL_DATA_DIR / "captures"
    captures.mkdir(parents=True, exist_ok=True)
    seq = 1 + max(
        (int(p.stem.split("-", 1)[0]) for p in captures.glob("*.json") if p.stem.split("-", 1)[0].isdigit()),
        default=0,
    )
    path = captures / f"{seq:05d}-{slugify(record.get('label', 'step'))}.json"
    path.write_text(json.dumps(record, indent=2, default=str))
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
    env: Annotated[str, typer.Option(help="Read the value from this env var instead (keeps it out of shell history)")] = "",
    secret: Annotated[bool, typer.Option(help="Redact the value everywhere it's recorded (use for passwords)")] = False,
    label: Annotated[str, typer.Option(help="Short label for this step")] = "",
    window: Annotated[str, typer.Option(help="Switch to this window first")] = "",
    frame: Annotated[str, typer.Option(help="Type into a field inside a (nested) iframe, e.g. '0' or '0/1'")] = "",
    save: Annotated[bool, typer.Option(help="Save the capture to ./data/ even when telemetry is off")] = False,
) -> None:
    """Type text into a field (a friendlier `step type`).

    \b
    Examples:
        trainee-sim type Firstname.Lastname@Fiserv.com --into "input#i0116"
        trainee-sim type --into "input[type=password]" --env TS_PW --secret
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
        },
    )


@app.command()
def click(
    element: Annotated[str, typer.Option("--element", help="Selector of the element to click")] = "",
    by: Annotated[str, typer.Option(help="Selector type: css or xpath")] = "css",
    label: Annotated[str, typer.Option(help="Short label for this step")] = "",
    window: Annotated[str, typer.Option(help="Switch to this window first")] = "",
    frame: Annotated[str, typer.Option(help="Click an element inside a (nested) iframe, e.g. '0' or '0/1'")] = "",
    save: Annotated[bool, typer.Option(help="Save the capture to ./data/ even when telemetry is off")] = False,
) -> None:
    """Click an element (a friendlier `step click`).

    \b
    Examples:
        trainee-sim click --element "input#idSIButton9"
        trainee-sim click --by xpath --element "//button[contains(.,'Launch')]"
        trainee-sim click --frame 0 --element "button.next"   # inside the first iframe
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
        },
    )


@app.command()
def shutdown() -> None:
    """Close the browser session on the server."""
    _request("/session/shutdown")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
