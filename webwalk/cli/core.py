"""webwalk core CLI — the flow-agnostic browser verbs.

These drive the live browser session held by the server: connect, capture state
(`observe`), inject actions (`step`/`click`/`type`/`paste`), fire in-page requests
(`fetch`), run JS (`eval`), and gate the observer (`telemetry`). None of them know
about any particular training — a flow CLI (e.g. `scw-sim`) registers these same
verbs via `register_core_commands` and adds its own flow-specific commands on top.

The bare `webwalk` command exposes exactly these, with a flow-agnostic server
(no snapshot extensions). Point at the server with WW_SERVER.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from webwalk.cli._shared import (
    _brief_observe,
    _call,
    _parse_duration,
    _request,
    _request_capturing,
    _save_capture,
    _save_network,
)
from webwalk.constants import DEFAULT_PORT


def register_core_commands(app: typer.Typer, server_plugins: str = "") -> None:
    """Register the flow-agnostic browser verbs onto `app`.

    `server_plugins` (a comma-separated import-path list) is baked into this app's
    `serve`: it selects which flow plugins the launched server loads (its snapshot
    extensions + routes). The bare core CLI passes none; a flow CLI passes its own
    observers module so `<flow>-sim serve` brings up a server that understands it.
    """

    @app.command()
    def serve(
        host: Annotated[str, typer.Option(help="Host to bind to (0.0.0.0 so a LAN client can reach it)")] = "0.0.0.0",  # noqa: S104
        port: Annotated[int, typer.Option(help="Port to bind to")] = DEFAULT_PORT,
        reload: Annotated[bool, typer.Option(help="Auto-reload for development")] = False,
        plugins: Annotated[str, typer.Option(help="Comma-separated flow server-plugin modules to load")] = "",
    ) -> None:
        """Start the server (holds the live browser session in memory)."""
        import uvicorn

        from webwalk.server.logging import setup_logging

        # This app's flow selects the server plugins; --plugins / an existing env
        # value override. Set before importing the app so both serve and --reload
        # (which re-imports in a worker) see it.
        chosen = plugins or os.environ.get("WEBWALK_SERVER_PLUGINS") or server_plugins
        if chosen:
            os.environ["WEBWALK_SERVER_PLUGINS"] = chosen
        setup_logging()
        uvicorn.run("webwalk.server.main:app", host=host, port=port, reload=reload)

    @app.command()
    def check(timeout: Annotated[float, typer.Option(help="Seconds to wait")] = 5.0) -> None:
        """Check whether the server is running."""
        import httpx

        from webwalk.client import get_server_url

        url = f"{get_server_url()}/health"
        try:
            resp = httpx.get(url, timeout=timeout)
            typer.echo(json.dumps({"healthy": resp.status_code == 200, "url": url, "body": resp.json()}))
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            typer.echo(json.dumps({"healthy": False, "url": url, "error": exc.__class__.__name__}))
            raise typer.Exit(1)

    @app.command()
    def connect(
        url: Annotated[str, typer.Argument(help="URL to open (defaults to TS_START_URL)")] = "",
    ) -> None:
        """Open the browser on the server and navigate to a URL.

        Telemetry stays OFF: complete login/MFA and load the target in the headed
        window, then run `telemetry on` before capturing anything. The browser
        profile persists across restarts, so once you've logged in the session is
        usually remembered and connect drops you straight in.
        """
        _request("/session/connect", json={"start_url": url or None})

    @app.command()
    def telemetry(state: Annotated[str, typer.Argument(help="'on' or 'off'")]) -> None:
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
    def network(
        since: Annotated[str, typer.Option(help="Only events from the last <dur> (e.g. 5s, 500ms, 2m)")] = "",
        full: Annotated[bool, typer.Option(help="Print every event's full JSON to stdout (noisy)")] = False,
        save: Annotated[bool, typer.Option(help="Write the full dump to ./data/network/ (on by default)")] = True,
    ) -> None:
        """Show the captured API traffic (requests/responses + ws frames).

        Bodies can be huge, so by default this writes the full dump to
        ./data/network/ and prints a compact one-line-per-event index plus the file
        path. Pass --full to dump everything to stdout instead. Empty while
        telemetry is off — capture is gated by the same switch.

        Tip: set TS_NETWORK_HOSTS on the server to filter analytics/RUM noise.
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
                st = str(e.get("status") or "···")
                url = str(e.get("url") or "")[:110]
                typer.echo(f"  {method:4} {st:>3} {url}")

    @app.command()
    def windows() -> None:
        """List open browser windows/tabs (index, url, title, which is active).

        Selenium only sees the focused window, so a "launch" button that opens a new
        tab won't be observed until you `switch` to it.
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
        brief: Annotated[bool, typer.Option("--brief", help="Print a compact text digest instead of JSON")] = False,
    ) -> None:
        """Capture the current browser state; a compact summary prints to stdout.

        The full page state (elements, DOM, open windows) is written to
        ./data/captures/ when telemetry is on, or when you pass --save. `--brief`
        prints the interactive elements and traffic as text so there's no JSON to
        post-process.
        """
        if brief:
            result = _call("/observe", json={"label": label, "window": window or None})
            record = result.pop("record", None)
            if record is not None and (save or result.get("recorded")):
                result["local_path"] = _save_capture(record)
            typer.echo(_brief_observe((record or {}).get("state") or {}, result))
            return
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
        wait_after: Annotated[str, typer.Option(help="Or: wait until this selector is visible before snapshotting")] = "",  # noqa: E501
        wait_after_timeout: Annotated[float, typer.Option(help="Timeout for --wait-after (seconds)")] = 10.0,
        enter: Annotated[bool, typer.Option(help="For type: press Enter after typing (submit the field)")] = False,
        save: Annotated[bool, typer.Option(help="Save the full capture to ./data/ even when telemetry is off")] = False,
    ) -> None:
        """Inject an action and capture before/after state.

        \b
        Examples:
            step click --selector "button.next"
            step click --by xpath --selector "//button[contains(.,'Launch')]"
            step type  --selector "#search" --value "hello"
            step navigate --value "https://training.local/module/2"
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

        Lets a scripted login ride out redirects. Exits non-zero on timeout.

        \b
        Examples:
            wait-for --url login.microsoftonline.com
            wait-for --element "input#i0116"
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
        secret: Annotated[bool, typer.Option(help="Redact the value everywhere it's recorded (use for passwords)")] = False,  # noqa: E501
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
            type Firstname.Lastname@example.com --into "input#i0116"
            type --into "input[type=password]" --env TS_PW --secret
            type "/auth/logout" --into "#browserBar" --frame 0/0 --enter
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
    def paste(
        into: Annotated[str, typer.Option("--into", help="Selector to paste into (e.g. '.monaco-editor')")] = "",
        file: Annotated[str, typer.Option(help="Read the text to paste from this file (for multi-line code)")] = "",
        value: Annotated[str, typer.Option(help="Literal text to paste (use --file for multi-line code)")] = "",
        by: Annotated[str, typer.Option(help="Selector type: css or xpath")] = "css",
        label: Annotated[str, typer.Option(help="Short label for this step")] = "",
        window: Annotated[str, typer.Option(help="Switch to this window first")] = "",
        frame: Annotated[str, typer.Option(help="Paste into an element inside a (nested) iframe, e.g. '0/0/0'")] = "",
        settle: Annotated[float, typer.Option(help="Seconds to wait before the after-snapshot (let the editor settle)")] = 0.0,  # noqa: E501
        save: Annotated[bool, typer.Option(help="Save the capture to ./data/ even when telemetry is off")] = False,
    ) -> None:
        """Replace a focused field/editor's content via a real clipboard paste.

        The reliable way to put multi-line code into a cross-origin code-server
        (Monaco/VS Code web) editor: it sets the OS clipboard, focuses the target,
        selects-all, and pastes — so the text lands verbatim.

        \b
        Examples:
            paste --into ".monaco-editor" --frame 0/0/0 --file solution.py
            paste --into "textarea#notes" --value "hello"
        """
        if not into:
            typer.echo("Which element? Pass --into with a selector.", err=True)
            raise typer.Exit(1)
        if file:
            path = Path(file)
            if not path.exists():
                typer.echo(f"Paste file not found: {path}", err=True)
                raise typer.Exit(1)
            value = path.read_text()
        elif not value:
            typer.echo("Nothing to paste. Pass --file or --value.", err=True)
            raise typer.Exit(1)
        _request_capturing(
            "/step",
            save=save,
            json={
                "action": "paste",
                "selector": into,
                "value": value,
                "by": by,
                "label": label or "paste",
                "window": window or None,
                "frame": frame or None,
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
        wait_after: Annotated[str, typer.Option(help="Or: wait until this selector is visible before snapshotting")] = "",  # noqa: E501
        wait_after_timeout: Annotated[float, typer.Option(help="Timeout for --wait-after (seconds)")] = 10.0,
        save: Annotated[bool, typer.Option(help="Save the capture to ./data/ even when telemetry is off")] = False,
    ) -> None:
        """Click an element (a friendlier `step click`).

        \b
        Examples:
            click --element "input#idSIButton9"
            click --by xpath --element "//button[contains(.,'Launch')]"
            click --frame 0 --element "button.next"   # inside the first iframe
            click --frame 0 --element "a.course" --wait-after "button.resume"
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
    def fetch(
        url: Annotated[str, typer.Argument(help="URL to request (the target's own API)")],
        method: Annotated[str, typer.Option("--method", "-X", help="HTTP method")] = "GET",
        data: Annotated[str, typer.Option(help="Request body — a raw string (JSON string sends as JSON)")] = "",
        header: Annotated[list[str], typer.Option("--header", "-H", help="Extra header 'Name: value' (repeatable)")] = [],  # noqa: B006, E501
        page_auth: Annotated[bool, typer.Option("--page-auth", "-a", help="Attach the SPA's own bearer token from web storage")] = False,  # noqa: E501
        frame: Annotated[str, typer.Option(help="Fire from inside a (nested) iframe's origin, e.g. '0/0'")] = "",
    ) -> None:
        """Fire a request from inside the logged-in page (carries its session).

        The UI→API ratchet: hit the target's API directly instead of puppeting the
        UI. Runs in the page's origin with credentials. For an API that wants a
        bearer token, pass --page-auth to reuse the SPA's own token, or set it with
        -H 'Authorization: Bearer …'. A cross-origin API whose token lives in a
        nested iframe is reachable with --frame (the request fires from that frame's
        origin, and --page-auth finds that frame's token).

        \b
        Examples:
            fetch https://api.example.com/ping
            fetch .../auth/resume --page-auth
            fetch .../answer -X POST --data '{}' -a
            fetch .../api/missions/vb-63 --frame 0/0 --page-auth
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
            json={"url": url, "method": method, "headers": headers or None, "body": data or None,
                  "page_auth": page_auth, "frame": frame or None},
        )
        typer.echo(json.dumps(result, indent=2, default=str))
        if not result.get("ok"):
            raise typer.Exit(1)

    @app.command(name="eval")
    def eval_js(
        script: Annotated[str, typer.Argument(help="JS to run in the page (a bare expression is auto-returned)")],
        frame: Annotated[str, typer.Option(help="Run inside a (nested) iframe, e.g. '0' or '0/1'")] = "",
    ) -> None:
        """Run JS in the logged-in page and print its result (introspection).

        The escape hatch for what `observe` doesn't show — web storage, app state,
        or how the SPA authenticates. Runs in the top document unless `--frame`
        targets a nested iframe. A bare expression is wrapped in `return (…)`.

        \b
        Examples:
            eval "Object.keys(localStorage)"
            eval --frame 0 "document.querySelector('table#editor').rows.length"
        """
        _request("/eval", json={"script": script, "frame": frame or None})

    @app.command()
    def shutdown() -> None:
        """Close the browser session on the server."""
        _request("/session/shutdown")


def build_app(help_text: str, server_plugins: str = "") -> typer.Typer:
    """A Typer app with the core verbs registered (and this flow's server plugins)."""
    app = typer.Typer(help=help_text, no_args_is_help=True)
    register_core_commands(app, server_plugins=server_plugins)
    return app


app = build_app(
    "webwalk — drive a Selenium browser and capture the flow (flow-agnostic core).\n\n"
    "Start `webwalk serve` on the target machine, then `connect`, do auth by hand, "
    "`telemetry on`, and capture with `observe` / `step`. Point `--flow <id|path>` "
    "at a flow to `where` / `run` / `graph` it; a flow with its own CLI (e.g. "
    "`scw-sim`) adds flow-specific verbs on top."
)

# The bare core also gets flow-parameterized verbs: `webwalk --flow <id|path> run`.
# Imported at the bottom to avoid a cycle (webwalk.flow imports the walk plumbing,
# not this module's app).
from webwalk.flow import add_generic_flow_commands  # noqa: E402

add_generic_flow_commands(app)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
