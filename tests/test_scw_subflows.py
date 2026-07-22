"""Tests for the SCW sub-flows — the mission / challenge / coding-lab handlers the
runner hands off to (one level down from page states).

Browser-free, like test_runner.py: `observe`/`act` are injected fakes and the
sub-flows are wired onto a plain `Runner` via `subflows=[…]`, so we exercise the
mission step loop, the challenge decision, and the coding-lab paste without a
server. The generic walk (resume, discovery, http, diagnostics) lives in
test_runner.py; this is the SCW-specific "one level down"."""

from __future__ import annotations

from typing import Any

from webwalk.engine.runner import Runner
from webwalk.flows.scw.subflows import ChallengeSubFlow, CodingLabSubFlow, MissionSubFlow


def _payload(url: str) -> dict[str, Any]:
    return {"url": url, "title": "", "elements": []}


def _rich_observe(payload: dict[str, Any]) -> Any:
    """An observe fake that always returns a fixed, fully-populated payload."""
    return lambda: payload


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
    record of driven actions — the injected surface the mission sub-flow uses."""

    def __init__(self, active_index: int, steps: list[dict[str, Any]], completes: bool = True,
                 checkable: set[int] | None = None) -> None:
        self._active_index = active_index
        self._steps = steps
        self._completes = completes
        self._checkable = checkable
        self.acted: list[dict[str, Any]] = []
        self.verified: list[Any] = []

    def read(self) -> dict[str, Any]:
        out: dict[str, Any] = {"active_index": self._active_index, "steps": self._steps}
        if self._checkable is not None:
            out["checkable"] = self._checkable
        return out

    def act(self, ui: dict[str, Any]) -> None:
        self.acted.append(ui)

    def verify(self, order: int | None) -> bool:
        self.verified.append(order)
        return self._completes


def _mission_runner(mf: _MissionFake, lib: list[dict[str, Any]], **kw: Any) -> Runner:
    return Runner(MISSION_STATES, [MISSION_RULE], _rich_observe(_payload("/mission/x")), mf.act,
                  subflows=[MissionSubFlow(mf.read, lib, mf.verify)], **kw)


def test_mission_drives_encoded_step_verifies_complete_then_finishes() -> None:
    # active_index 0 (login step) → drive login-as → its checkbox ticks → advance
    # past the end (len 1) → complete. Credentials are templated into the action,
    # and verification is keyed on the step's order (its checkbox), not portal's
    # activeIndex. Nothing non-mission applies afterward, so it's a clean checkpoint.
    mf = _MissionFake(0, [LOGIN_STEP])
    result = _mission_runner(mf, LOGIN_LIB).run(auto=True)
    assert result["status"] == "mission-complete"
    assert mf.acted == [{"action": "type", "frame": [0, 0, 0], "selector": "#Username", "value": "alice@alice.com"}]
    assert mf.verified == [0]
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


def test_mission_skips_informational_step_with_no_checkbox() -> None:
    # An informational step (order 1) has no checklist checkbox — its order is absent
    # from `checkable`. The loop must advance past it without acting or verifying,
    # then drive the real step that follows. Mirrors SCW's "Inspect the URL" gap.
    info_step = {"order": 1, "id": "s1", "title": "Inspect the URL", "description": "just read this"}
    login2 = {"order": 2, "id": "s2", "title": "Log in as Bob", "description": "log in as `bob@bob.com`"}
    mf = _MissionFake(1, [LOGIN_STEP, info_step, login2], checkable={0, 2})
    result = _mission_runner(mf, LOGIN_LIB).run(auto=True)
    assert result["status"] == "mission-complete"
    # The informational step drove nothing and was not verified; only the real step 2 did.
    assert mf.acted == [{"action": "type", "frame": [0, 0, 0], "selector": "#Username", "value": "bob@bob.com"}]
    assert mf.verified == [2]
    done = result["mission"]["steps_done"]
    assert done[0] == {"index": 1, "handler": None, "step_id": "s1", "informational": True, "completed": True}
    assert done[1]["handler"] == "login-as"


def test_mission_handler_resolves_page_sourced_context() -> None:
    # A handler can pull a var from the LIVE page (its `context` block), not just from
    # the step text — e.g. a stolen session token sitting in the mission browser's URL
    # bar — evaluated in a nested frame, and template it into `do`. Mirrors SCW's
    # "Take over Bobby's session" step.
    lib = [{
        "id": "take-over",
        "match": {"title_regex": "(?i)^take over"},
        "context": {"token": {"eval": "grab-token", "frame": "0/0"}},
        "do": [{"ui": {"action": "type", "frame": [0, 0], "selector": "#cookie", "value": "{token}"}}],
    }]
    step = {"order": 0, "id": "s0", "title": "Take over Bobby's session", "description": "steal it"}
    mf = _MissionFake(0, [step])
    evals: list[tuple[str, str | None]] = []

    def eval_js(js: str, frame: str | None = None) -> Any:
        evals.append((js, frame))
        return "BOBBY-TOKEN"

    result = _mission_runner(mf, lib, eval_js=eval_js).run(auto=True)
    assert result["status"] == "mission-complete"
    assert evals == [("grab-token", "0/0")]  # eval ran in the specified frame
    # …and the page-sourced token was templated into the action.
    assert mf.acted == [{"action": "type", "frame": [0, 0], "selector": "#cookie", "value": "BOBBY-TOKEN"}]


def test_mission_eval_action_runs_templated_js_in_a_frame() -> None:
    # An `eval` action runs JS in a (nested) frame — the escape hatch for what
    # click/type can't do, e.g. writing a stolen cookie into the app frame. The
    # page-sourced token is templated into the script before it runs. Mirrors the
    # take-over-session handler's cookie write.
    lib = [{
        "id": "steal",
        "match": {"title_regex": "(?i)^take over"},
        "context": {"token": {"eval": "grab", "frame": "0/0"}},
        "do": [{"eval": {"frame": "0/0/0", "script": "document.cookie='S={token}'"}}],
    }]
    step = {"order": 0, "id": "s0", "title": "Take over the session", "description": "x"}
    mf = _MissionFake(0, [step])
    calls: list[tuple[str, str | None]] = []

    def eval_js(js: str, frame: str | None = None) -> Any:
        calls.append((js, frame))
        return "TOK"

    result = _mission_runner(mf, lib, eval_js=eval_js).run(auto=True)
    assert result["status"] == "mission-complete"
    # First call resolves the context token; second runs the templated cookie write in the app frame.
    assert calls == [("grab", "0/0"), ("document.cookie='S=TOK'", "0/0/0")]
    assert mf.acted == []  # this handler drives no click/type — only eval


def test_mission_blocked_when_complete_event_never_fires() -> None:
    # Handler matches and drives, but the step's /complete never arrives → blocked.
    mf = _MissionFake(0, [LOGIN_STEP], completes=False)
    result = _mission_runner(mf, LOGIN_LIB).run(auto=True)
    assert result["status"] == "stuck"
    assert result["mission"]["status"] == "mission-blocked"
    assert len(mf.acted) == 1 and mf.verified == [0]  # it tried once and checked


def test_completed_mission_continues_the_walk_when_a_page_rule_applies() -> None:
    # A finished mission must not dead-end: once it completes, the results panel
    # shows a "Continue" (a new page state with a non-mission rule), and the walk
    # carries on through it to the next state instead of stopping at mission-complete.
    states = [
        {"id": "mission", "url_contains": "/mission/"},
        # Same URL as `mission`, but only matches once the results Continue appears.
        {"id": "mission-done", "url_contains": "/mission/", "elements": [{"id": "continue"}]},
        {"id": "next", "url_contains": "/next/", "terminal": True},
    ]
    rules = [
        # Listed first so it wins when a completed mission matches both states.
        {"id": "advance", "when": "mission-done",
         "do": {"ui": {"action": "click", "selector": "#continue"}}, "expect": "next"},
        {"id": "do-mission", "when": "mission", "do": {"mission": {}}},
    ]
    # Observe script: in-progress (no Continue) → [complete] → results w/ Continue
    # ×2 → the next task ×2. (The post-complete locate + the loop re-observe both
    # see the Continue; then `advance` clicks and we land on `next`.)
    without = {"url": "/mission/x", "title": "", "elements": []}
    withcont = {"url": "/mission/x", "title": "", "elements": [{"id": "continue"}]}
    nxt = {"url": "/next/y", "title": "", "elements": []}
    payloads = [without, withcont, withcont, nxt, nxt]
    i = {"n": 0}

    def observe() -> dict[str, Any]:
        pl = payloads[min(i["n"], len(payloads) - 1)]
        i["n"] += 1
        return pl

    acted: list[dict[str, Any]] = []
    mf = _MissionFake(0, [LOGIN_STEP])
    runner = Runner(states, rules, observe, lambda ui: acted.append(ui),
                    subflows=[MissionSubFlow(mf.read, LOGIN_LIB, mf.verify)])
    result = runner.run(auto=True)

    assert result["status"] == "done"       # walked past the mission and reached the terminal
    assert result["state"] == ["next"]
    assert {"action": "click", "selector": "#continue"} in acted  # the Continue was clicked


# --- challenge sub-flow (a decision one level down: pick the answer, then Submit) ---

CHALLENGE_STATES = [
    {"id": "challenge", "url_contains": "/challenge/"},
    {"id": "done", "url_contains": "/done/", "terminal": True},
]
CHALLENGE_RULE = {"id": "do-challenge", "when": "challenge", "do": {"challenge": {}},
                  "expect": ["challenge", "done"]}


# The decision view the runner reads from `payload["challenge"]` (embedded in observe).
def _sel_act(label: str) -> dict[str, Any]:
    return {"action": "click", "by": "css", "selector": f'[aria-label="Select {label} as answer"]', "frame": [0]}


LOCATE_VIEW = {
    "stage": "locate", "file": "auth.py",
    "vulnerability": "Session Handling - Exposed Session Tokens",
    "decision": "Identify which 1 of 2 highlighted blocks is vulnerable",
    "options": [
        {"id": "auth.py:34-36", "code": [{"n": 34, "code": "leak"}], "act": _sel_act("auth.py:34-36")},
        {"id": "auth.py:24-25", "code": [{"n": 24, "code": "safe"}], "act": _sel_act("auth.py:24-25")},
    ],
    "submit": None,  # locate: no Submit until a block is selected -> runner constructs it
}
CHALLENGE_KEY = [{
    "id": "est-locate",
    "match": {"stage": "locate", "vulnerability_contains": "Exposed Session Tokens"},
    "answer": {"select": "auth.py:34-36"},
}]


class _ChFake:
    """Scripted observe payloads (some carrying a `challenge` view) + acted record."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._p = payloads
        self._i = 0
        self.acted: list[dict[str, Any]] = []

    def observe(self) -> dict[str, Any]:
        p = self._p[min(self._i, len(self._p) - 1)]
        self._i += 1
        return p

    def act(self, ui: dict[str, Any]) -> None:
        self.acted.append(ui)


def _ch_payload(url: str, view: dict[str, Any] | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"url": url, "title": "", "elements": []}
    if view is not None:
        p["challenge"] = view
    return p


def test_challenge_answers_named_block_then_continues() -> None:
    # observe #1: the challenge decision -> do-challenge fires select + Submit and
    # continues; observe #2: the next (terminal) activity -> done.
    fake = _ChFake([_ch_payload("/challenge/x", LOCATE_VIEW), _ch_payload("/done/y")])
    runner = Runner(CHALLENGE_STATES, [CHALLENGE_RULE], fake.observe, fake.act,
                    subflows=[ChallengeSubFlow(CHALLENGE_KEY)])
    result = runner.run(auto=True)
    assert result["status"] == "done"
    assert [a["selector"] for a in fake.acted] == [
        '[aria-label="Select auth.py:34-36 as answer"]',
        '[aria-label="Submit answer"]',  # constructed (locate view's submit was null)
    ]
    ch = result["history"][0]["challenge"]
    assert ch["status"] == "solved" and ch["id"] == "est-locate"


def test_challenge_without_answer_key_is_stuck_with_the_decision() -> None:
    fake = _ChFake([_ch_payload("/challenge/x", LOCATE_VIEW)])
    runner = Runner(CHALLENGE_STATES, [CHALLENGE_RULE], fake.observe, fake.act,
                    subflows=[ChallengeSubFlow([])])  # no keys authored yet
    result = runner.run(auto=True)
    assert result["status"] == "stuck"
    assert result["challenge"]["status"] == "challenge-stuck"
    # The stuck context is the decision itself, so a human can author the missing key.
    assert result["context"]["vulnerability"] == "Session Handling - Exposed Session Tokens"
    assert [o["id"] for o in result["context"]["options"]] == ["auth.py:34-36", "auth.py:24-25"]
    assert fake.acted == []  # nothing fired


# --- coding-lab sub-flow (write code one level down: paste the file, then Submit) ---

CODING_STATES = [
    {"id": "coding-lab", "url_contains": "/coding-lab/",
     "elements": [{"frame": [0, 0], "tag": "button", "text": "Submit"}]},
    {"id": "coding-lab-complete", "url_contains": "/coding-lab/",
     "elements": [{"frame": [0, 0], "tag": "button", "text": "Next Activity"}]},
    {"id": "done", "url_contains": "/done/", "terminal": True},
]
CODING_RULE = {"id": "do-coding-lab", "when": "coding-lab", "do": {"coding_lab": {}},
               "expect": ["coding-lab", "coding-lab-complete"]}
NEXT_RULE = {"id": "coding-lab-next", "when": "coding-lab-complete",
             "do": {"ui": {"action": "click", "frame": [0, 0], "by": "css", "selector": "next"}},
             "expect": ["done"]}
CODING_KEY = [{
    "id": "mfa", "match": {"text_contains": "implementing MFA", "file": "models.py"},
    "steps": [{"content": "STEP ONE"}, {"content": "STEP TWO"}],
}]


def _cl_payload(url: str, steps_done: int = 0, submit: bool = True) -> dict[str, Any]:
    """A coding-lab snapshot: `steps_done` completion markers + the Submit button (or,
    once done, the Next Activity button instead — i.e. the completion screen)."""
    markers = "".join(f"\nstep {i} is complete" for i in range(1, steps_done + 1))
    button = {"frame": [0, 0], "tag": "button", "text": "Submit" if submit else "Next Activity"}
    return {
        "url": url, "title": "Coding lab",
        "frames": [{"text": "INTRODUCTION implementing MFA" + markers}],
        "elements": [{"tag": "textarea", "aria_label": "models.py", "frame": [0, 0, 0]}, button],
    }


def test_coding_lab_pastes_each_step_then_reaches_completion() -> None:
    # obs #1: step 0 active -> paste STEP ONE + Submit; obs #2: step 1 active -> paste
    # STEP TWO + Submit; obs #3: completion screen -> coding-lab-next -> done.
    fake = _ChFake([
        _cl_payload("/coding-lab/x", steps_done=0),
        _cl_payload("/coding-lab/x", steps_done=1),
        _cl_payload("/coding-lab/x", steps_done=2, submit=False),
        _ch_payload("/done/y"),
    ])
    runner = Runner(CODING_STATES, [CODING_RULE, NEXT_RULE], fake.observe, fake.act,
                    subflows=[CodingLabSubFlow(CODING_KEY)])
    result = runner.run(auto=True)
    assert result["status"] == "done"
    # Two paste+Submit pairs (one per step), then the Next Activity click.
    assert [(a["action"], a.get("value") or a["selector"]) for a in fake.acted] == [
        ("paste", "STEP ONE"),
        ("click", '[data-testid="submit-enabled"]'),
        ("paste", "STEP TWO"),
        ("click", '[data-testid="submit-enabled"]'),
        ("click", "next"),
    ]
    assert result["history"][0]["coding_lab"]["status"] == "solved"


def test_coding_lab_without_answer_key_is_stuck_with_context() -> None:
    fake = _ChFake([_cl_payload("/coding-lab/x", steps_done=0)])
    runner = Runner(CODING_STATES, [CODING_RULE, NEXT_RULE], fake.observe, fake.act,
                    subflows=[CodingLabSubFlow([])])  # no keys authored yet
    result = runner.run(auto=True)
    assert result["status"] == "stuck"
    assert result["coding_lab"]["status"] == "coding-lab-stuck"
    assert result["context"]["file"] == "models.py"
    assert result["context"]["active_step"] == 0
    assert fake.acted == []  # nothing fired
