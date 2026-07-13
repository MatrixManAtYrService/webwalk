"""Tests for the rule runner — the observe → match → act → verify loop.

These are browser-free: `observe` and `act` are injected fakes, so we exercise
the runner's control flow (resume, discovery, verification, dead-ends) without a
server. Mirrors how the runner itself is pure (see engine/runner.py)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from trainee_sim.engine.runner import UNKNOWN, Runner, build_machine

# Two states and one rule that connects them, plus one undiscovered destination.
STATES = [
    {"id": "breakdown", "url_contains": "/breakdown/"},
    {"id": "task", "url_contains": "/course-task/"},
]
RULE_VERIFIED = {
    "id": "resume",
    "when": "breakdown",
    "do": {"ui": {"action": "click", "selector": "x"}},
    "expect": "task",
}
RULE_DISCOVER = {**RULE_VERIFIED, "expect": None}  # destination not yet known


def _payload(url: str) -> dict[str, Any]:
    return {"url": url, "title": "", "elements": []}


class _Fakes:
    """Scripted observe results + a record of the actions performed."""

    def __init__(self, urls: list[str]) -> None:
        self._urls = urls
        self._i = 0
        self.acted: list[dict[str, Any]] = []

    def observe(self) -> dict[str, Any]:
        url = self._urls[min(self._i, len(self._urls) - 1)]
        self._i += 1
        return _payload(url)

    def act(self, ui: dict[str, Any]) -> None:
        self.acted.append(ui)


def _edges(m) -> set[tuple[str, str, str]]:
    """(source, trigger, dest) for every declared transition (trigger lives on
    the Event, not the Transition object)."""
    return {
        (source, name, t.dest)
        for name, event in m.events.items()
        for source, trans_list in event.transitions.items()
        for t in trans_list
    }


def test_machine_has_declared_edge_and_unknown_state() -> None:
    m = build_machine(STATES, [RULE_VERIFIED], SimpleNamespace())
    assert UNKNOWN in m.states
    assert ("breakdown", "resume", "task") in _edges(m)


def test_rule_with_no_expect_lands_on_unknown_edge() -> None:
    m = build_machine(STATES, [RULE_DISCOVER], SimpleNamespace())
    assert {dest for src, trig, dest in _edges(m) if trig == "resume"} == {UNKNOWN}


def test_verified_edge_acts_then_confirms_destination() -> None:
    # breakdown → (click) → task, matching the rule's `expect`.
    fakes = _Fakes(["/breakdown/x", "/course-task/y", "/course-task/y"])
    runner = Runner(STATES, [RULE_VERIFIED], fakes.observe, fakes.act)
    result = runner.run(to="task", auto=True)
    assert result["status"] == "reached"
    assert result["state"] == "task"
    assert len(fakes.acted) == 1  # clicked exactly once


def test_expect_list_verifies_against_any_destination() -> None:
    # Resume-style rule that can land on either of two states; landing on the
    # second still verifies the step (no `unexpected`).
    states = [*STATES, {"id": "mission", "url_contains": "/mission/"}]
    rule = {**RULE_VERIFIED, "expect": ["task", "mission"]}
    fakes = _Fakes(["/breakdown/x", "/mission/z", "/mission/z"])
    result = Runner(states, [rule], fakes.observe, fakes.act).run(auto=True)
    # Landed on `mission` (one of the accepted expects) → the step verified (not
    # `unexpected`). `mission` has no outgoing rule and isn't terminal, so the walk
    # then reports stuck *there* — which itself proves the edge was taken.
    assert result["status"] == "stuck"
    assert result["state"] == ["mission"]


def test_expect_list_still_flags_a_true_miss() -> None:
    # None of the accepted destinations matched where we actually landed.
    rule = {**RULE_VERIFIED, "expect": ["task", "mission"]}
    fakes = _Fakes(["/breakdown/x", "/breakdown/x"])  # click did nothing
    result = Runner(STATES, [rule], fakes.observe, fakes.act).run(auto=True)
    assert result["status"] == "unexpected"
    assert result["expected"] == ["task", "mission"]


def test_unexpected_landing_is_reported() -> None:
    # Rule expects `task` but we stay on breakdown (the click did nothing).
    fakes = _Fakes(["/breakdown/x", "/breakdown/x"])
    runner = Runner(STATES, [RULE_VERIFIED], fakes.observe, fakes.act)
    result = runner.run(auto=True)
    assert result["status"] == "unexpected"
    assert result["expected"] == "task"
    assert result["landed"] == ["breakdown"]  # matched set after acting


def test_discovery_reports_where_it_landed() -> None:
    # No `expect`: act, then report the (here unknown) landing for a human to name.
    fakes = _Fakes(["/breakdown/x", "/somewhere-new/z"])
    runner = Runner(STATES, [RULE_DISCOVER], fakes.observe, fakes.act)
    result = runner.run(auto=True)
    assert result["status"] == "discovered"
    assert result["landed"] == []  # no signature matched the new page


def test_unencoded_leaf_reports_stuck_with_context() -> None:
    # A matched state with no outgoing rule that isn't declared terminal means "we
    # haven't encoded how to act here" — stuck, with context to author the rule.
    fakes = _Fakes(["/course-task/y"])  # task has no outgoing rule, not terminal
    runner = Runner(STATES, [RULE_VERIFIED], fakes.observe, fakes.act)
    result = runner.run(auto=True)
    assert result["status"] == "stuck"
    assert result["state"] == ["task"]
    assert "context" in result
    assert fakes.acted == []


def test_declared_terminal_leaf_is_done() -> None:
    # A state flagged `terminal: true` is a legitimate endpoint, not un-encoded —
    # so no rule there is `done`, and it carries no context.
    states = [
        {"id": "breakdown", "url_contains": "/breakdown/"},
        {"id": "task", "url_contains": "/course-task/", "terminal": True},
    ]
    fakes = _Fakes(["/course-task/y"])
    result = Runner(states, [RULE_VERIFIED], fakes.observe, fakes.act).run(auto=True)
    assert result["status"] == "done"
    assert result["state"] == ["task"]
    assert "context" not in result


def test_http_action_templates_captures_and_guards() -> None:
    # A rule whose `do` is an http sequence: derive ids from the page URL + an
    # eval'd session id, GET to capture an attempt id, then a guarded POST.
    calls: list[dict[str, Any]] = []

    def http(method: str, url: str, headers: dict[str, str], body: str | None) -> dict[str, Any]:
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        # First call (GET) returns the attempt id + type the POST depends on.
        if url.endswith("/challenges"):
            return {"status": 200, "ok": True, "body": {"taskAttemptId": "AT-9", "taskType": "video"}}
        return {"status": 200, "ok": True, "body": {"status": "done"}}

    rule = {
        "id": "video-advance",
        "when": "task",
        "do": {
            "http": {
                "context": {
                    "sid": {"eval": "localStorage.sessionId"},
                    "mod": {"url_regex": r"/course-task/([^/]+)/([^/]+)", "group": 2},
                },
                "requests": [
                    {"method": "GET", "url": "https://api/x/{mod}/challenges",
                     "headers": {"X-Session-ID": "{sid}"},
                     "capture": {"attempt": "taskAttemptId", "ttype": "taskType"}},
                    {"method": "POST", "only_if": "{ttype}==video",
                     "url": "https://api/answer/{attempt}",
                     "headers": {"X-Session-ID": "{sid}"}, "body": "{}"},
                ],
            }
        },
        "expect": "task",
    }
    fakes = _Fakes(["/course-task/COURSE/MOD-1", "/course-task/COURSE/MOD-1"])
    runner = Runner(STATES, [rule], fakes.observe, fakes.act, http=http, eval_js=lambda js: "SID-42")
    runner.run(max_steps=1, auto=True)

    assert [c["method"] for c in calls] == ["GET", "POST"]
    assert calls[0]["url"] == "https://api/x/MOD-1/challenges"      # url_regex group 2
    assert calls[0]["headers"] == {"X-Session-ID": "SID-42"}         # eval'd + templated
    assert calls[1]["url"] == "https://api/answer/AT-9"              # captured from GET response
    assert calls[1]["body"] == "{}"                                  # JSON body left intact


def test_http_guard_skips_when_false() -> None:
    calls: list[str] = []

    def http(method: str, url: str, headers: dict[str, str], body: str | None) -> dict[str, Any]:
        calls.append(url)
        return {"status": 200, "ok": True, "body": {"taskType": "guideline"}}

    rule = {
        "id": "maybe-post", "when": "task", "expect": "task",
        "do": {"http": {"requests": [
            {"method": "GET", "url": "https://api/challenges", "capture": {"ttype": "taskType"}},
            {"method": "POST", "only_if": "{ttype}==video", "url": "https://api/answer"},
        ]}},
    }
    fakes = _Fakes(["/course-task/y", "/course-task/y"])
    Runner(STATES, [rule], fakes.observe, fakes.act, http=http).run(max_steps=1, auto=True)
    assert calls == ["https://api/challenges"]  # POST skipped: taskType != video


# --- mission sub-loop (steps one level down from page states) ---

MISSION_STATES = [{"id": "mission", "url_contains": "/mission/"}]
MISSION_RULE = {"id": "do-mission", "when": "mission", "do": {"mission": {}}}
LOGIN_LIB = [{
    "id": "login-as",
    "match": {"title_regex": "(?i)^log in"},
    "extract": {"email": {"regex": "`([^`]+@[^`]+)`"}},
    "do": [{"ui": {"action": "type", "frame": [0, 0, 0], "selector": "#Username", "value": "{email}"}}],
}]
LOGIN_STEP = {"order": 0, "id": "s0", "title": "Log in as Alice", "description": "log in as `alice@alice.com`"}
LOGOUT_STEP = {"order": 1, "id": "s1", "title": "Log out", "description": "log out and read the code"}


class _MissionFake:
    """A fixed playbook + entry index, a scripted per-step /complete verifier, and a
    record of driven actions — the injected surface the runner's mission loop uses."""

    def __init__(self, active_index: int, steps: list[dict[str, Any]], completes: bool = True) -> None:
        self._active_index = active_index
        self._steps = steps
        self._completes = completes
        self.acted: list[dict[str, Any]] = []
        self.verified: list[str] = []

    def read(self) -> dict[str, Any]:
        return {"active_index": self._active_index, "steps": self._steps}

    def act(self, ui: dict[str, Any]) -> None:
        self.acted.append(ui)

    def verify(self, step_id: str) -> bool:
        self.verified.append(step_id)
        return self._completes


def _mission_runner(mf: _MissionFake, lib: list[dict[str, Any]]) -> Runner:
    return Runner(MISSION_STATES, [MISSION_RULE], _rich_observe(_payload("/mission/x")), mf.act,
                  read_mission=mf.read, mission_steps=lib, verify_step=mf.verify)


def test_mission_drives_encoded_step_verifies_complete_then_finishes() -> None:
    # activeIndex 0 (login step) → drive login-as → its /complete fires → advance
    # past the end (len 1) → complete. Credentials are templated into the action,
    # and verification is keyed on the step's id (not portal activeIndex).
    mf = _MissionFake(0, [LOGIN_STEP])
    result = _mission_runner(mf, LOGIN_LIB).run(auto=True)
    assert result["status"] == "mission-complete"
    assert mf.acted == [{"action": "type", "frame": [0, 0, 0], "selector": "#Username", "value": "alice@alice.com"}]
    assert mf.verified == ["s0"]
    assert result["mission"]["steps_done"] == [
        {"index": 0, "handler": "login-as", "step_id": "s0", "completed": True}]


def test_mission_stuck_on_unencoded_step_carries_context() -> None:
    # Entry index points at a logout step with no handler → stuck, with the step
    # text + live form surfaced so a handler can be authored.
    mf = _MissionFake(1, [LOGIN_STEP, LOGOUT_STEP])
    result = _mission_runner(mf, LOGIN_LIB).run(auto=True)
    assert result["status"] == "stuck"
    assert result["mission"]["status"] == "mission-stuck"
    assert result["context"]["step"]["title"] == "Log out"
    assert mf.acted == []  # nothing driven — we don't know how yet


def test_mission_blocked_when_complete_event_never_fires() -> None:
    # Handler matches and drives, but the step's /complete never arrives → blocked.
    mf = _MissionFake(0, [LOGIN_STEP], completes=False)
    result = _mission_runner(mf, LOGIN_LIB).run(auto=True)
    assert result["status"] == "stuck"
    assert result["mission"]["status"] == "mission-blocked"
    assert len(mf.acted) == 1 and mf.verified == ["s0"]  # it tried once and checked


def test_unknown_state_stops_the_walk() -> None:
    fakes = _Fakes(["/nowhere/"])  # matches no signature
    runner = Runner(STATES, [RULE_VERIFIED], fakes.observe, fakes.act)
    result = runner.run(auto=True)
    assert result["status"] == "unknown"
    assert result["matched"] == []


# --- diagnostics context (what an agent gets when the walk stops) ---


def _rich_observe(payload: dict[str, Any]) -> Any:
    """An observe fake that always returns a fixed, fully-populated payload."""
    return lambda: payload


def test_stuck_result_carries_actionable_and_filtered_network() -> None:
    # On `task` with a goal of `breakdown`, no rule applies here → stuck.
    payload = {
        "url": "/course-task/y",
        "title": "Quiz | Courses",
        "elements": [
            {"tag": "button", "text": "Continue", "enabled": False, "frame": [0], "id": "cont"},
            {"tag": "div", "text": "just layout", "frame": []},  # non-interactive: dropped
        ],
        "network": [
            {"type": "http", "method": "POST", "url": "https://portal-api.securecodewarrior.com/answer", "status": 200},
            {"type": "http", "method": "POST", "url": "https://ingress.coralogix.com/logs", "status": 200},  # noise
        ],
    }
    runner = Runner(STATES, [RULE_VERIFIED], _rich_observe(payload), lambda ui: None,
                    api_hosts=["securecodewarrior.com"])
    result = runner.run(to="breakdown", auto=True)

    assert result["status"] == "stuck"
    ctx = result["context"]
    # Only the interactive element is offered, carrying its frame + enabled flag.
    assert [e["id"] for e in ctx["actionable"]] == ["cont"]
    assert ctx["actionable"][0]["enabled"] is False
    assert ctx["actionable"][0]["frame"] == [0]
    # Network is filtered to the training host — the Coralogix noise is dropped.
    assert [n["url"] for n in ctx["network"]] == ["https://portal-api.securecodewarrior.com/answer"]


def test_unknown_result_reports_near_miss_states() -> None:
    # url matches the `login` signature's url_contains, but its #email element is
    # absent → not matched, but it's the closest state and says why it missed.
    states = [*STATES, {"id": "login", "url_contains": "/login", "elements": [{"id": "email"}]}]
    payload = {"url": "https://x/login/step", "title": "", "elements": []}
    runner = Runner(states, [RULE_VERIFIED], _rich_observe(payload), lambda ui: None)
    result = runner.run(auto=True)

    assert result["status"] == "unknown"
    near = result["context"]["near_states"]
    assert near[0]["id"] == "login"          # ranked first: it hit the url signal
    assert near[0]["signals"] == 1
    assert any("id" in reason for reason in near[0]["reasons"])  # explains the miss
