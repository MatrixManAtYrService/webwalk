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


def test_unexpected_landing_is_reported() -> None:
    # Rule expects `task` but we stay on breakdown (the click did nothing).
    fakes = _Fakes(["/breakdown/x", "/breakdown/x"])
    runner = Runner(STATES, [RULE_VERIFIED], fakes.observe, fakes.act)
    result = runner.run(auto=True)
    assert result["status"] == "unexpected"
    assert result["expected"] == "task"
    assert result["landed"] == "breakdown"


def test_discovery_reports_where_it_landed() -> None:
    # No `expect`: act, then report the (here unknown) landing for a human to name.
    fakes = _Fakes(["/breakdown/x", "/somewhere-new/z"])
    runner = Runner(STATES, [RULE_DISCOVER], fakes.observe, fakes.act)
    result = runner.run(auto=True)
    assert result["status"] == "discovered"
    assert result["landed"] is None  # no signature matched the new page
    assert result["matched"] == []


def test_terminal_state_with_no_rule_is_done() -> None:
    fakes = _Fakes(["/course-task/y"])  # task has no outgoing rule
    runner = Runner(STATES, [RULE_VERIFIED], fakes.observe, fakes.act)
    result = runner.run(auto=True)
    assert result["status"] == "done"
    assert result["state"] == "task"
    assert fakes.acted == []


def test_unknown_state_stops_the_walk() -> None:
    fakes = _Fakes(["/nowhere/"])  # matches no signature
    runner = Runner(STATES, [RULE_VERIFIED], fakes.observe, fakes.act)
    result = runner.run(auto=True)
    assert result["status"] == "unknown"
    assert result["matched"] == []
