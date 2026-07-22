"""Unit tests for the network recorder.

The recorder is deliberately pure (no Selenium) so it can be exercised by
feeding it raw CDP messages exactly as they arrive in Chrome's performance log.
That covers the interesting behaviour — the telemetry gate, request/response
correlation, redaction, host/type filtering, body caps, and the ring buffer —
without a live browser.
"""

from __future__ import annotations

from webwalk.server.recorder import NetworkRecorder


def _request(request_id: str, url: str, method: str = "GET", rtype: str = "XHR", headers=None, post_data=None):
    return {
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": request_id,
            "type": rtype,
            "wallTime": 1000.0,
            "request": {"url": url, "method": method, "headers": headers or {}, "postData": post_data},
        },
    }


def _response(request_id: str, status: int = 200, content_type: str = "application/json", headers=None):
    hdrs = {"content-type": content_type, **(headers or {})}
    return {
        "method": "Network.responseReceived",
        "params": {"requestId": request_id, "response": {"status": status, "headers": hdrs}},
    }


def _ws_frame(request_id: str, sent: bool, payload: str, opcode: int = 1, timestamp: float = 5.0):
    which = "Sent" if sent else "Received"
    return {
        "method": f"Network.webSocketFrame{which}",
        "params": {
            "requestId": request_id,
            "timestamp": timestamp,
            "response": {"opcode": opcode, "payloadData": payload},
        },
    }


def test_disabled_recorder_keeps_empty_buffer():
    # The must-not-break gate: with telemetry off, driving traffic records nothing.
    rec = NetworkRecorder(enabled=False)
    want = rec.ingest([_request("1", "https://api.example.com/x"), _response("1")])
    assert want == []
    assert rec.drain() == []
    assert rec.peek() == []


def test_disable_clears_buffer():
    rec = NetworkRecorder(enabled=True)
    rec.ingest([_request("1", "https://api.example.com/x"), _response("1")])
    assert rec.peek()  # something buffered
    rec.disable()
    assert rec.peek() == []
    assert rec.stats()["enabled"] is False


def test_request_response_correlate_into_one_event():
    rec = NetworkRecorder(enabled=True)
    want = rec.ingest(
        [
            _request("42", "https://api.example.com/accounts", method="POST"),
            _response("42", status=201),
        ]
    )
    assert want == ["42"]  # JSON response → body fetch requested
    events = rec.drain()
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "http"
    assert ev["method"] == "POST"
    assert ev["status"] == 201
    assert ev["url"].endswith("/accounts")
    assert rec.drain() == []  # drain cleared it


def test_json_body_attaches_to_event():
    rec = NetworkRecorder(enabled=True)
    (rid,) = rec.ingest([_request("7", "https://api.example.com/x"), _response("7")])
    rec.attach_body(rid, '{"ok": true}')
    (ev,) = rec.drain()
    assert ev["response_body"] == '{"ok": true}'


def test_non_json_response_asks_for_no_body():
    rec = NetworkRecorder(enabled=True)
    want = rec.ingest(
        [_request("9", "https://api.example.com/page"), _response("9", content_type="text/html")]
    )
    assert want == []  # no body fetch for non-JSON
    (ev,) = rec.drain()
    assert ev["status"] == 200
    assert ev["response_body"] is None


def test_headers_are_redacted_both_directions():
    rec = NetworkRecorder(enabled=True)
    rec.ingest(
        [
            _request("1", "https://api.example.com/x", headers={"Cookie": "secret", "Accept": "application/json"}),
            _response("1", headers={"Set-Cookie": "sess=abc"}),
        ]
    )
    (ev,) = rec.drain()
    assert ev["request_headers"]["Cookie"] == "<redacted>"
    assert ev["request_headers"]["Accept"] == "application/json"
    assert ev["response_headers"]["Set-Cookie"] == "<redacted>"


def test_host_filter_drops_offhost_traffic():
    rec = NetworkRecorder(hosts=("api.training.local",), enabled=True)
    rec.ingest(
        [
            _request("1", "https://cdn.example.com/analytics.js"),
            _response("1"),
            _request("2", "https://api.training.local/state"),
            _response("2"),
        ]
    )
    events = rec.drain()
    assert len(events) == 1
    assert "training.local" in events[0]["url"]


def test_resource_type_filter_keeps_only_api_traffic():
    rec = NetworkRecorder(enabled=True)
    rec.ingest(
        [
            _request("1", "https://api.example.com/app.js", rtype="Script"),
            _response("1"),
            _request("2", "https://api.example.com/data", rtype="Fetch"),
            _response("2"),
        ]
    )
    events = rec.drain()
    assert len(events) == 1
    assert events[0]["url"].endswith("/data")


def test_websocket_frames_captured_both_directions():
    rec = NetworkRecorder(enabled=True)
    rec.ingest([_ws_frame("ws1", sent=True, payload="ping"), _ws_frame("ws1", sent=False, payload="pong")])
    events = rec.drain()
    assert [e["direction"] for e in events] == ["sent", "received"]
    assert [e["payload"] for e in events] == ["ping", "pong"]
    assert all(e["type"] == "ws" for e in events)


def test_body_is_size_capped():
    rec = NetworkRecorder(max_body_bytes=10, enabled=True)
    (rid,) = rec.ingest([_request("1", "https://api.example.com/x"), _response("1")])
    rec.attach_body(rid, "x" * 50)
    (ev,) = rec.drain()
    assert ev["response_body"].startswith("x" * 10)
    assert "truncated" in ev["response_body"]


def test_base64_body_is_not_stored_raw():
    rec = NetworkRecorder(enabled=True)
    (rid,) = rec.ingest([_request("1", "https://api.example.com/x"), _response("1")])
    rec.attach_body(rid, "AAAA", base64_encoded=True)
    (ev,) = rec.drain()
    assert ev["response_body"] == "<binary body omitted>"


def test_ring_buffer_bounds_event_count():
    rec = NetworkRecorder(max_events=3, enabled=True)
    for i in range(6):
        rec.ingest([_request(str(i), f"https://api.example.com/{i}"), _response(str(i), content_type="text/plain")])
    events = rec.drain()
    assert len(events) == 3  # only the newest three survive
    assert [e["url"].rsplit("/", 1)[-1] for e in events] == ["3", "4", "5"]


def test_response_without_tracked_request_is_ignored():
    rec = NetworkRecorder(enabled=True)
    want = rec.ingest([_response("orphan")])
    assert want == []
    assert rec.drain() == []


def test_http_event_ts_is_wallclock():
    # requestWillBeSent carries wallTime (epoch); the event's ts uses it directly.
    rec = NetworkRecorder(enabled=True)
    msg = _request("1", "https://api.example.com/x")
    msg["params"]["wallTime"] = 1_700_000_000.0
    msg["params"]["timestamp"] = 500.0  # monotonic — must NOT be used for ts
    rec.ingest([msg, _response("1", content_type="text/plain")])
    (ev,) = rec.drain()
    assert ev["ts"] == 1_700_000_000.0


def test_ws_frame_ts_converted_to_wallclock():
    # ws frames only carry a monotonic timestamp; we convert it to wall-clock
    # using the offset learned from a request (wallTime - monotonic timestamp).
    rec = NetworkRecorder(enabled=True)
    req = _request("1", "https://api.example.com/x")
    req["params"]["wallTime"] = 1_700_000_000.0
    req["params"]["timestamp"] = 1000.0  # offset = 1_699_999_000.0
    rec.ingest([req, _response("1", content_type="text/plain")])
    rec.ingest([_ws_frame("ws1", sent=False, payload="hi", timestamp=1005.0)])
    events = rec.drain()
    ws = next(e for e in events if e["type"] == "ws")
    assert ws["ts"] == 1_700_000_005.0  # 1005.0 + offset


def test_ws_frame_ts_falls_back_without_offset():
    # No request seen yet → no offset → keep the raw monotonic value rather than
    # inventing a wall-clock time.
    rec = NetworkRecorder(enabled=True)
    rec.ingest([_ws_frame("ws1", sent=True, payload="hi", timestamp=42.0)])
    (ws,) = rec.drain()
    assert ws["ts"] == 42.0
