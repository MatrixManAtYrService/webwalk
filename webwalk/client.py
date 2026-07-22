"""HTTP client used by the CLI to talk to the trainee-sim server.

The CLI is stateless: each invocation makes one HTTP request. The live browser
session lives only in the server, which typically runs on another machine on
the LAN — set TS_SERVER_URL to point at it (default http://localhost:22133).
"""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx

from webwalk.constants import DEFAULT_SERVER_URL

# Connecting the browser can take a while (Selenium Manager may download a
# driver on first use); keep a generous default timeout.
DEFAULT_TIMEOUT = 120.0


def get_server_url() -> str:
    return os.environ.get("TS_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")


class ServerUnreachable(RuntimeError):
    """The trainee-sim server is not running / not reachable."""


def server_request(
    path: str,
    method: str = "POST",
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Make a request to the server and return the decoded JSON body.

    Handles the 499 "action required" handshake (e.g. browser not connected) by
    printing the server's instructions and exiting non-zero, and raises
    ServerUnreachable with a friendly hint if the server isn't up.
    """
    url = f"{get_server_url()}{path}"
    timeout = timeout if timeout is not None else float(os.environ.get("TS_TIMEOUT", DEFAULT_TIMEOUT))
    try:
        resp = httpx.request(method, url, json=json, params=params, timeout=timeout)
    except httpx.ConnectError as exc:
        raise ServerUnreachable(
            "Could not reach the webwalk server. Start it with `webwalk serve` (or a flow CLI's "
            f"`serve`, e.g. `scw-sim serve`) on the target machine, and set TS_SERVER_URL if it "
            f"isn't at {get_server_url()}."
        ) from exc

    if resp.status_code == 499:
        data = resp.json()
        print(f"Action required: {data.get('message', 'unknown')}", file=sys.stderr)
        if data.get("instructions"):
            print(f"  → {data['instructions']}", file=sys.stderr)
        sys.exit(2)

    try:
        return resp.json()
    except ValueError:
        return {"error": resp.text, "status_code": resp.status_code}
