"""trainee-sim CLI — a stateless front-end to the trainee-sim server.

  trainee-sim serve            start the server (holds the live browser session)
  trainee-sim connect          open the browser at the training entry point
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
from typing import Annotated

import typer

from trainee_sim.constants import DEFAULT_PORT

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


def _request(path: str, **kwargs) -> None:
    """Run a server request and print the JSON result, with a friendly error."""
    from trainee_sim.client import ServerUnreachable, server_request

    try:
        result = server_request(path, **kwargs)
    except ServerUnreachable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def connect(
    start_url: Annotated[str, typer.Option(help="Override the training entry-point URL")] = "",
) -> None:
    """Open the browser on the server and navigate to the training entry point.

    Telemetry stays OFF: complete login/MFA and load the training in the headed
    window, then run `trainee-sim telemetry on` before capturing anything.
    """
    _request("/session/connect", json={"start_url": start_url or None})


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
) -> None:
    """Capture the current browser state as one step (only if telemetry is on)."""
    _request("/observe", json={"label": label, "window": window or None})


@app.command()
def step(
    action: Annotated[str, typer.Argument(help="click | type | navigate")],
    selector: Annotated[str, typer.Option(help="CSS selector (for click/type)")] = "",
    value: Annotated[str, typer.Option(help="Text to type, or URL to navigate to")] = "",
    label: Annotated[str, typer.Option(help="Short label for this step's capture file")] = "",
    window: Annotated[str, typer.Option(help="Switch to this window first (index/'latest'/url substring)")] = "",
) -> None:
    """Inject an action and capture before/after state as one step.

    \b
    Examples:
        trainee-sim step click --selector "button.next"
        trainee-sim step type  --selector "#search" --value "hello"
        trainee-sim step navigate --value "https://training.local/module/2"
    """
    _request(
        "/step",
        json={
            "action": action,
            "selector": selector or None,
            "value": value or None,
            "label": label or None,
            "window": window or None,
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
