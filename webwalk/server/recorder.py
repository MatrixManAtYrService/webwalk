"""Network capture — the gated network observer.

Records the API traffic the training produces (HTTP requests/responses and
websocket frames) so the engine can eventually fire requests directly instead of
puppeting the UI. See `docs/network-capture-design.md` for the why and the shape.

Mechanism note: `finmap`'s sibling recorder attaches to Playwright's first-class
`page.on("response")` events. We are on Selenium, which has no such API, so we
instead feed on Chrome's **performance log** — a synchronous, pull-based stream
of Chrome DevTools Protocol (CDP) `Network.*` events. `session.py` reads that log
off the driver and hands the raw CDP messages here via `ingest()`; response
bodies are fetched separately (they need a driver round-trip) and handed back via
`attach_body()`. This module itself is pure — no Selenium — so it can be unit
tested by feeding it recorded CDP messages.

The data model (redaction set, host/type filtering, one record per request)
mirrors `finmap/server/recorder.py`; only the plumbing differs.

Gating: like `Capture`, this is governed by a single `enabled` flag flipped in
lock-step with the telemetry gate. While disabled, `ingest()` is a no-op and the
buffer stays empty, so auth traffic (SSO/SAML/cookies) is never retained. Even
while enabled, auth headers/cookies are redacted from every event.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger()

# Auth-bearing headers to scrub from every recorded event. Mirrors finmap's set.
_REDACT = {"cookie", "authorization", "set-cookie", "x-csrf-token", "x-xsrf-token"}
# CDP resourceType values worth keeping — the API traffic, not page chrome/CDN.
_INTERESTING_TYPES = {"XHR", "Fetch"}


def _redact(headers: dict[str, str] | None) -> dict[str, str]:
    return {k: ("<redacted>" if k.lower() in _REDACT else v) for k, v in (headers or {}).items()}


def _is_json(content_type: str | None) -> bool:
    return "json" in (content_type or "").lower()


class NetworkRecorder:
    """Gated, bounded buffer of recent network events.

    Correlates CDP request/response pairs by their `requestId` into one event
    each, keeps a size-capped ring of completed events (plus websocket frames),
    and hands the batch to a step capture via `drain()`.
    """

    def __init__(
        self,
        hosts: Iterable[str] = (),
        max_events: int = 500,
        max_body_bytes: int = 65_536,
        enabled: bool = False,
    ) -> None:
        self.hosts = tuple(hosts)
        self.max_body_bytes = max_body_bytes
        self.enabled = enabled
        # Completed events, newest-wins ring buffer (HTTP round-trips + ws frames).
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        # In-flight HTTP requests awaiting their response, keyed by CDP requestId.
        # Capped so a burst of never-answered requests can't grow unbounded.
        self._pending: dict[str, dict[str, Any]] = {}
        # Completed events still awaiting an out-of-band body fetch, by requestId.
        self._await_body: dict[str, dict[str, Any]] = {}
        self._pending_cap = max_events
        # CDP stamps websocket frames with a MONOTONIC clock, but HTTP requests
        # carry both monotonic (`timestamp`) and wall-clock (`wallTime`). We learn
        # the offset from any request and use it to give ws frames a real
        # wall-clock `ts`, so everything is filterable on one timeline (--since).
        self._wall_offset: float | None = None

    # --- gate ---

    def enable(self) -> None:
        if not self.enabled:
            self.enabled = True
            self._reset()
            logger.info("network_capture_enabled", hosts=self.hosts or "all")

    def disable(self) -> None:
        if self.enabled:
            self.enabled = False
            self._reset()
            logger.info("network_capture_disabled")

    def _reset(self) -> None:
        self._events.clear()
        self._pending.clear()
        self._await_body.clear()
        self._wall_offset = None

    # --- filtering ---

    def _host_ok(self, url: str) -> bool:
        if not self.hosts:
            return True
        host = urlparse(url).netloc
        return any(h in host for h in self.hosts)

    def _cap_body(self, body: str | None) -> str | None:
        if body is None:
            return None
        if len(body) > self.max_body_bytes:
            return body[: self.max_body_bytes] + f"…<truncated {len(body) - self.max_body_bytes} bytes>"
        return body

    # --- ingest ---

    def ingest(self, messages: Iterable[dict[str, Any]]) -> list[str]:
        """Fold a batch of raw CDP messages into the buffer.

        Each message is a `{"method": "Network.…", "params": {…}}` dict, exactly
        as it arrives in a Chrome performance-log entry. Returns the list of
        requestIds whose (JSON, host-matched) response bodies are now worth
        fetching — the caller does the driver round-trip and calls `attach_body`.

        No-op (returns `[]`) while disabled, so nothing is retained during auth.
        """
        if not self.enabled:
            return []
        want_bodies: list[str] = []
        for msg in messages:
            method = msg.get("method", "")
            params = msg.get("params") or {}
            if method == "Network.requestWillBeSent":
                self._on_request(params)
            elif method == "Network.responseReceived":
                rid = self._on_response(params)
                if rid is not None:
                    want_bodies.append(rid)
            elif method in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
                self._on_ws_frame(method, params)
        return want_bodies

    def _on_request(self, params: dict[str, Any]) -> None:
        request = params.get("request") or {}
        url = request.get("url", "")
        # Learn the monotonic→wall offset from any request (even a filtered one),
        # so websocket frames get a real wall-clock timestamp.
        wall, mono = params.get("wallTime"), params.get("timestamp")
        if wall is not None and mono is not None:
            self._wall_offset = wall - mono
        if not self._host_ok(url):
            return
        # Only track types we care about; CDP tags the type on the request event.
        rtype = params.get("type", "")
        if rtype and rtype not in _INTERESTING_TYPES:
            return
        if len(self._pending) >= self._pending_cap:
            # Drop the oldest in-flight request to stay bounded (rare: a page that
            # fires far more requests than it ever gets answers for).
            self._pending.pop(next(iter(self._pending)), None)
        self._pending[params.get("requestId", "")] = {
            "ts": params.get("wallTime") or params.get("timestamp"),
            "type": "http",
            "resource_type": rtype or None,
            "method": request.get("method"),
            "url": url,
            "request_headers": _redact(request.get("headers")),
            "request_body": self._cap_body(request.get("postData")),
            "status": None,
            "response_headers": None,
            "response_content_type": None,
            "response_body": None,
        }

    def _on_response(self, params: dict[str, Any]) -> str | None:
        rid = params.get("requestId", "")
        event = self._pending.pop(rid, None)
        if event is None:
            return None  # response for a request we filtered out (or missed)
        response = params.get("response") or {}
        content_type = (response.get("headers") or {}).get("content-type") or response.get("mimeType")
        event["status"] = response.get("status")
        event["response_headers"] = _redact(response.get("headers"))
        event["response_content_type"] = content_type
        # The round-trip's metadata is complete now; move it into the buffer. The
        # body (if JSON) fills in later via attach_body, mutating this same dict.
        self._events.append(event)
        if _is_json(content_type):
            self._await_body[rid] = event  # attach_body finds it here after the fetch
            return rid
        return None

    def _on_ws_frame(self, method: str, params: dict[str, Any]) -> None:
        # webSocketFrame{Sent,Received} carry the payload inline — no body fetch.
        # There's no url on a frame event, so ws frames aren't host-filterable;
        # we keep them all (the websocket channel is exactly what we want to see).
        response = params.get("response") or {}
        payload = response.get("payloadData")
        mono = params.get("timestamp")
        # Convert the monotonic frame time to wall-clock if we've learned the
        # offset (from an HTTP request); otherwise fall back to the raw value.
        ts = mono + self._wall_offset if (mono is not None and self._wall_offset is not None) else mono
        self._events.append(
            {
                "ts": ts,
                "type": "ws",
                "direction": "sent" if method.endswith("Sent") else "received",
                "opcode": response.get("opcode"),
                "payload": self._cap_body(payload if isinstance(payload, str) else None),
            }
        )

    def attach_body(self, request_id: str, body: str | None, base64_encoded: bool = False) -> None:
        """Fill in a response body fetched out-of-band (via CDP getResponseBody).

        Silently ignored if the request is unknown — the metadata event still
        stands, just without a body.
        """
        event = self._await_body.pop(request_id, None)
        if event is None:
            return
        event["response_body"] = "<binary body omitted>" if base64_encoded else self._cap_body(body)

    # --- output ---

    def drain(self) -> list[dict[str, Any]]:
        """Return the buffered events (oldest→newest) and clear the buffer.

        Called by a step snapshot so each step owns the traffic it produced.
        In-flight requests without a response yet are left pending for a later
        drain; already-completed events (even if their body fetch is still
        outstanding) are handed over now.
        """
        events = list(self._events)
        self._events.clear()
        return events

    def peek(self) -> list[dict[str, Any]]:
        """Non-destructive view of the current buffer (for the debug endpoint)."""
        return list(self._events)

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "hosts": list(self.hosts),
            "buffered": len(self._events),
            "pending": len(self._pending),
            "max_events": self._events.maxlen,
            "max_body_bytes": self.max_body_bytes,
        }
