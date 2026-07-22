"""Helpers shared by every CLI (the core `webwalk` and each flow's `*-sim`).

The CLIs are stateless: each command makes one HTTP request to the server
(TS_SERVER_URL) and prints the result. These are the request plumbing (`_call`,
`_request`, `_request_capturing`), the local capture/network writers, and the
compact `--brief` renderers — all flow-agnostic, so a flow CLI reuses them
verbatim and only adds its own flow-specific commands on top.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer

# Where fetched captures land on THIS (client) machine, so they're easy to read
# regardless of where the server/browser lives. Overridable for scripting.
LOCAL_DATA_DIR = Path(os.environ.get("TS_LOCAL_DATA_DIR", "./data"))

# The keys every observe snapshot carries. Anything else is a flow-registered
# snapshot extension (e.g. SCW's `challenge`) — rendered generically in --brief.
_GENERIC_STATE_KEYS = {"url", "title", "elements", "frames", "dom", "network", "windows"}


def _call(path: str, **kwargs: Any) -> dict[str, Any]:
    """Make a server request, returning the JSON result (friendly error on down)."""
    from webwalk.client import ServerUnreachable, server_request

    try:
        return server_request(path, **kwargs)
    except ServerUnreachable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


def _request(path: str, **kwargs: Any) -> None:
    """Make a server request and print the JSON result."""
    typer.echo(json.dumps(_call(path, **kwargs), indent=2, default=str))


def _next_local_path(subdir: str, label: str) -> Path:
    """Next numbered ./data/<subdir>/NNNNN-<label>.json path (continues numbering)."""
    from webwalk.server.capture import slugify

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


def _request_capturing(path: str, save: bool = False, **kwargs: Any) -> None:
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


def _frame_to_str(frame: Any) -> str | None:
    """Turn a YAML frame path ([0] or [3,1]) into the '/'-joined string /step wants."""
    if frame is None:
        return None
    if isinstance(frame, list):
        return "/".join(str(s) for s in frame)
    return str(frame)


# --- compact (--brief) rendering ------------------------------------------------
#
# The verbose JSON is fine for a file, but an agent driving the browser wants the
# same handful of lines it would otherwise reconstruct with an inline `python3 -c`.
# Rendering it here means one stable, allowlist-friendly command with no pipe.
# Everything below is presentation-only and flow-agnostic.


def _brief_actionable(items: list[dict[str, Any]], limit: int = 12) -> list[str]:
    """One line per interactive element: tag, id/name, text, frame, disabled."""
    lines: list[str] = []
    for el in items[:limit]:
        tag = el.get("tag") or "?"
        ident = f"#{el['id']}" if el.get("id") else (f"[{el['name']}]" if el.get("name") else "")
        label = el.get("text") or el.get("aria_label") or ""
        label = f' "{label[:40]}"' if label else ""
        frame = el.get("frame")
        frame_s = f" frame={frame}" if frame else ""
        flags = "" if el.get("enabled", True) else " DISABLED"
        lines.append(f"{tag}{ident}{label}{frame_s}{flags}")
    if len(items) > limit:
        lines.append(f"… +{len(items) - limit} more")
    return lines


def _brief_network(items: list[dict[str, Any]], limit: int = 8) -> list[str]:
    """One line per captured request/ws frame (method, status, url)."""
    lines: list[str] = []
    for e in items[:limit]:
        if e.get("type") == "ws":
            lines.append(f"ws {e.get('direction', ''):8} op={e.get('opcode')}")
        else:
            lines.append(f"{str(e.get('method') or ''):4} {str(e.get('status') or '···'):>3} {str(e.get('url') or '')[:100]}")  # noqa: E501
    if len(items) > limit:
        lines.append(f"… +{len(items) - limit} more")
    return lines


def _brief_observe(state: dict[str, Any], meta: dict[str, Any]) -> str:
    """A compact digest of an observe capture — the interactive elements (with
    selectors), the traffic, other tabs, and any flow snapshot extensions — so the
    agent needn't open the saved JSON to see what's on the page."""
    from webwalk.engine.runner import actionable_elements, summarize_network

    lines: list[str] = []
    if meta.get("local_path"):
        lines.append(f"saved: {meta['local_path']}")
    lines.append(f"url: {state.get('url')}")
    if state.get("title"):
        lines.append(f"title: {state['title']}")
    acts = actionable_elements(state)
    lines.append(f"elements: {len(state.get('elements') or [])} (actionable {len(acts)})")
    lines += [f"  {r}" for r in _brief_actionable(acts)]
    net = summarize_network(state)
    if net:
        lines.append(f"network ({len(net)}):")
        lines += [f"  {r}" for r in _brief_network(net)]
    # Flow-contributed snapshot keys (e.g. SCW's `challenge`): render each dict's
    # scalar fields on one line, so the core stays flow-agnostic but the extra
    # signal an installed flow adds still shows up.
    for key in sorted(set(state) - _GENERIC_STATE_KEYS):
        val = state.get(key)
        if isinstance(val, dict):
            scalars = " ".join(f"{k}={v}" for k, v in val.items() if isinstance(v, (str, int, float, bool)))
            lines.append(f"{key}: {scalars}".rstrip())
    others = [w for w in (state.get("windows") or []) if not w.get("active")]
    if others:
        lines.append(f"other tabs ({len(others)}):")
        lines += [f"  [{w.get('index')}] {w.get('url')}" for w in others]
    return "\n".join(lines)
