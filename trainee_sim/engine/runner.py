"""The rule runner — walking the training as a state graph.

`matcher.py` answers "where am I?". This answers "what do I do, and did it
work?". A *rule* is plain data (authored in YAML beside the states):

    - id: resume-course
      when: scw-course-breakdown        # a state signature id (the matcher's job)
      do: { ui: { action: click, frame: [0], by: xpath, selector: '…' } }
      expect: scw-task                  # optional: the state we should land in

The runner loop is observe-driven, so it resumes from wherever the browser
actually is rather than assuming a start:

    observe → match state → pick an applicable rule → act → observe → verify

The **state graph** is held in a `transitions` (pytransitions) `Machine`: states
are signature ids, and each rule is an edge (trigger=rule id, source=`when`,
dest=`expect`). We keep the machine as the source of truth for the *flow* and
reconcile it against reality every iteration — observing the live page and
snapping the model to the matched state. That reconciliation is what makes
resume free, and the machine is the groundwork for the eventual "no dead ends"
graph analysis (does every state reach the goal?).

`expect` is optional: while a destination state is still undiscovered, omit it.
The runner then executes the rule, re-observes, and *reports* where it landed —
so a human can add that state's signature and, later, the rule's `expect`. That
is how the graph gets discovered "as we go".

This module is pure: it takes injected `observe`/`act` callables (the CLI wires
them to the server's /observe and /step), so it stays testable and server-free —
mirroring how `matcher.py` is pure and the CLI owns the HTTP.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from transitions import Machine

from trainee_sim.engine.matcher import match_states

# Model state for "we observed the page but no signature matched it" — an
# undiscovered state. Rules with no `expect` point their edge here.
UNKNOWN = "unknown"

# Types for the two injected side-effecting callables.
ObserveFn = Callable[[], dict[str, Any]]          # () -> observe payload (state dict)
ActFn = Callable[[dict[str, Any]], None]          # (ui-action dict) -> perform it
ConfirmFn = Callable[[str, dict[str, Any]], bool]  # (state_id, rule) -> proceed?


def build_machine(states: list[dict[str, Any]], rules: list[dict[str, Any]], model: object) -> Machine:
    """Build the pytransitions state machine: states are signature ids, rules are
    edges. A rule without `expect` lands on the sentinel UNKNOWN state.

    `auto_transitions=False` keeps the model to exactly the edges we declare, so
    later graph analysis sees the real reachability — not pytransitions' implicit
    "to_<state>" shortcuts.
    """
    ids = [s["id"] for s in states if "id" in s]
    state_ids: list[str] = list(dict.fromkeys([*ids, UNKNOWN]))
    transitions: list[dict[str, str]] = []
    for rule in rules:
        source = rule["when"]
        dest = rule.get("expect") or UNKNOWN
        for sid in (source, dest):
            if sid not in state_ids:
                state_ids.append(sid)  # a rule may reference a not-yet-authored state
        transitions.append({"trigger": rule["id"], "source": source, "dest": dest})
    return Machine(
        model=model,
        states=state_ids,
        transitions=transitions,
        initial=UNKNOWN,
        auto_transitions=False,
        ignore_invalid_triggers=True,
    )


class Runner:
    """Observe-driven walker over a course's states + rules.

    Inject `observe` (returns an observe payload) and `act` (performs a ui-action
    dict). `confirm`, if given, is asked before each step unless `auto` is set —
    the design's "human confirms each step first, autonomous later".
    """

    def __init__(
        self,
        states: list[dict[str, Any]],
        rules: list[dict[str, Any]],
        observe: ObserveFn,
        act: ActFn,
        confirm: ConfirmFn | None = None,
    ) -> None:
        self.states = states
        self.rules = rules
        self._observe = observe
        self._act = act
        self._confirm = confirm
        self.machine = build_machine(states, rules, self)

    # --- location ---

    def locate(self) -> tuple[dict[str, Any], list[str]]:
        """Observe the live browser and match it against the course states."""
        payload = self._observe()
        return payload, match_states(self.states, payload)

    def applicable_rules(self, state_id: str) -> list[dict[str, Any]]:
        """Rules whose `when` matches the current state (greedy: first wins)."""
        return [r for r in self.rules if r.get("when") == state_id]

    # --- the loop ---

    def run(self, to: str | None = None, max_steps: int = 20, auto: bool = False) -> dict[str, Any]:
        """Walk from wherever we are toward `to` (or until no rule applies).

        Returns a structured result whose `status` is one of:
          reached     — we're in the goal state `to`
          done        — no goal set and no applicable rule (a terminal/leaf state)
          discovered  — a rule with no `expect` landed us somewhere; its `landed`
                        state is the thing to add a signature for
          unexpected  — a rule's `expect` didn't match where we actually landed
          stuck        — a goal was set but no rule applies here
          ambiguous / unknown — 0 or >1 states matched (tighten/add signatures)
          aborted     — a human declined a step
          max_steps   — safety bound hit
        `history` lists every step taken (rule, from, expected, landed).
        """
        history: list[dict[str, Any]] = []
        for _ in range(max_steps):
            payload, matched = self.locate()
            url = payload.get("url")
            if len(matched) != 1:
                status = "ambiguous" if matched else "unknown"
                return {"status": status, "matched": matched, "url": url, "history": history}

            current = matched[0]
            self.machine.set_state(current)  # reconcile the model with reality
            if to and current == to:
                return {"status": "reached", "state": current, "url": url, "history": history}

            rules = self.applicable_rules(current)
            if not rules:
                status = "stuck" if to else "done"
                return {"status": status, "state": current, "url": url, "history": history}

            rule = rules[0]
            if not auto and self._confirm is not None and not self._confirm(current, rule):
                return {"status": "aborted", "state": current, "rule": rule["id"], "history": history}

            self._act(rule["do"]["ui"])
            # Fire the pytransitions trigger so the model walks the declared edge.
            # With ignore_invalid_triggers this is a no-op if the edge is absent,
            # but normally it advances the flow model to the rule's `expect`.
            getattr(self, rule["id"])()

            after_payload, after_matched = self.locate()
            landed = after_matched[0] if len(after_matched) == 1 else None
            expect = rule.get("expect")
            step = {
                "rule": rule["id"],
                "from": current,
                "expected": expect,
                "landed": landed,
                "matched": after_matched,
                "url": after_payload.get("url"),
            }
            history.append(step)

            if expect is None:
                # Discovery: we walked into territory with no declared destination.
                # Stop and report so a signature (and later `expect`) can be added.
                return {"status": "discovered", **step, "history": history}
            if landed != expect:
                return {"status": "unexpected", **step, "history": history}
            # Verified edge; loop again from the new state (natural resume point).
        return {"status": "max_steps", "history": history}
