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

import re
from collections.abc import Callable
from typing import Any

from transitions import Machine

from trainee_sim.engine.matcher import match_report, match_states
from trainee_sim.engine.mission import match_step

# Tags whose elements are worth offering as things to act on when the walker
# stops — the raw material for a UI rule's selector. Everything else (divs,
# spans, layout) is noise for that purpose.
_INTERACTIVE_TAGS = {"button", "a", "input", "select", "textarea"}

# Model state for "we observed the page but no signature matched it" — an
# undiscovered state. Rules with no `expect` point their edge here.
UNKNOWN = "unknown"

# `{name}` placeholders in http-action templates. Word-char-only, so a JSON body
# like `{}` (no word chars between the braces) is left untouched.
_VAR = re.compile(r"\{(\w+)\}")

# Types for the injected side-effecting callables.
ObserveFn = Callable[[], dict[str, Any]]          # () -> observe payload (state dict)
ActFn = Callable[[dict[str, Any]], None]          # (ui-action dict) -> perform it
ConfirmFn = Callable[[list[str], dict[str, Any]], bool]  # (matched states, rule) -> proceed?
HttpFn = Callable[[str, str, dict[str, str], str | None], dict[str, Any]]  # (method,url,headers,body)->resp
EvalFn = Callable[[str], Any]                     # (js) -> evaluated value
# () -> {active_index, steps:[instruction…]} for the mission we're on, or None.
# `active_index` is the current step: derived from the observable step-checkboxes
# (the first unticked one), not portal's stale `activeIndex` — see the CLI wiring.
ReadMissionFn = Callable[[], dict[str, Any] | None]
# (step order) -> did the pod tick that step's checkbox within a timeout? The
# checkbox `checked` state is the synchronous, in-snapshot completion signal.
VerifyStepFn = Callable[[int], bool]


def _step_brief(step: dict[str, Any]) -> dict[str, Any]:
    """The human-facing slice of a mission step (for reporting / stuck context)."""
    return {"order": step.get("order"), "title": step.get("title"),
            "description": (step.get("description") or "").strip()}


def _is_mission_action(do: Any) -> bool:
    """True if a rule's `do` is (or contains) a `mission` sub-flow action."""
    return any(isinstance(a, dict) and "mission" in a for a in (do if isinstance(do, list) else [do]))


def _template(value: str, ctx: dict[str, Any]) -> str:
    """Replace `{name}` with `ctx[name]`; unknown names are left as-is."""
    return _VAR.sub(lambda m: str(ctx.get(m.group(1), m.group(0))), value)


def _expect_list(expect: Any) -> list[str]:
    """Normalize a rule's `expect` (str | list | None) to a list of destination
    ids. None → the UNKNOWN sentinel (a discovery edge)."""
    if expect is None:
        return [UNKNOWN]
    return list(expect) if isinstance(expect, list) else [expect]


def actionable_elements(payload: dict[str, Any], limit: int = 40) -> list[dict[str, Any]]:
    """The interactive elements on the page, trimmed to what's useful for authoring
    a UI rule: the `frame` path, `enabled` flag (a disabled Continue is the tell of
    a gate), and whichever stable attrs the element actually carries. This is what
    lets an agent pick a selector without a separate `observe` round-trip.
    """
    out: list[dict[str, Any]] = []
    for el in payload.get("elements") or []:
        tag = (el.get("tag") or "").lower()
        if tag not in _INTERACTIVE_TAGS and not el.get("href"):
            continue
        item: dict[str, Any] = {"frame": el.get("frame"), "tag": tag, "enabled": el.get("enabled")}
        for key in ("type", "id", "name", "text", "aria_label", "href"):
            if el.get(key):
                item[key] = el[key]
        out.append(item)
    return out[:limit]


def summarize_network(
    payload: dict[str, Any], api_hosts: list[str] | None = None, limit: int = 40
) -> list[dict[str, Any]]:
    """A compact view of the API traffic the last step produced — the raw material
    for converting a UI step into an `http` rule (the API↔UI ratchet). Filtered to
    `api_hosts` when given, so RUM/analytics noise doesn't drown the training's own
    calls.
    """
    out: list[dict[str, Any]] = []
    for e in payload.get("network") or []:
        url = str(e.get("url") or "")
        if api_hosts and not any(h in url for h in api_hosts):
            continue
        if e.get("type") == "ws":
            out.append({"type": "ws", "direction": e.get("direction"), "opcode": e.get("opcode")})
        else:
            out.append({"method": e.get("method"), "status": e.get("status"), "url": url})
    return out[:limit]


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
        # `expect` may be a single state id, a list of acceptable destinations (a
        # rule like Resume that can land on either a task or a mission), or absent
        # (→ UNKNOWN, a discovery edge). Each destination becomes its own edge, so
        # the reachability graph records every place the rule can actually go.
        for dest in _expect_list(rule.get("expect")):
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
        http: HttpFn | None = None,
        eval_js: EvalFn | None = None,
        api_hosts: list[str] | None = None,
        read_mission: ReadMissionFn | None = None,
        mission_steps: list[dict[str, Any]] | None = None,
        verify_step: VerifyStepFn | None = None,
    ) -> None:
        self.states = states
        self.rules = rules
        self._observe = observe
        self._act = act
        self._confirm = confirm
        self._http = http          # for `http` actions (fire an in-page request)
        self._eval = eval_js       # for context sourced from page JS (e.g. sessionId)
        self.api_hosts = api_hosts  # host filter for the network view in diagnostics
        self._read_mission = read_mission     # reads {active_index, steps} for a mission
        self.mission_steps = mission_steps or []  # the shared step-handler library
        self._verify_step = verify_step       # confirms a step's /complete event fired
        # States explicitly flagged `terminal: true` are legitimate endpoints (e.g.
        # course complete). Any *other* state with no applicable rule is "stuck" —
        # we simply haven't encoded how to act there yet — not "done".
        self.terminals = {s["id"] for s in states if s.get("terminal")}
        self.machine = build_machine(states, rules, self)

    # --- diagnostics ---

    def _context(self, payload: dict[str, Any], matched: list[str]) -> dict[str, Any]:
        """Everything an agent needs to try the next thing by hand when the walk
        stops — built from the payload the runner already has, so it costs no extra
        round-trip. `actionable` feeds a UI rule, `network` feeds an http rule, and
        `near_states` (only when nothing matched) says whether to loosen an existing
        signature or author a new one.
        """
        ctx: dict[str, Any] = {
            "url": payload.get("url"),
            "title": payload.get("title"),
            "actionable": actionable_elements(payload),
            "network": summarize_network(payload, self.api_hosts),
        }
        if not matched:
            near = sorted(
                (r for r in match_report(self.states, payload) if not r["matched"] and r["signals"] > 0),
                key=lambda r: -r["signals"],
            )[:5]
            ctx["near_states"] = [
                {"id": r["id"], "signals": r["signals"], "reasons": r["reasons"]} for r in near
            ]
        return ctx

    def _mission_context(self, step: dict[str, Any]) -> dict[str, Any]:
        """Stuck-context for a mission step we can't handle: the step text plus the
        live app form, so the agent can perform it and then author a handler."""
        payload = self._observe()
        return {
            "url": payload.get("url"),
            "title": payload.get("title"),
            "step": _step_brief(step),
            "actionable": actionable_elements(payload),
            "network": summarize_network(payload, self.api_hosts),
        }

    # --- the mission sub-loop (one level down: steps, not page states) ---

    def _drive_mission(self, auto: bool, max_steps: int = 15) -> dict[str, Any]:
        """Walk a mission's steps: read the playbook + entry point once, then for
        each step match it against the shared step-handler library, drive the
        handler, and verify the pod ticked that step's checkbox. Stops on
        completion, on an un-encoded step (`mission-stuck`), or on a step that
        didn't take (`mission-blocked`).

        Both the entry point (`active_index`) and per-step verification come from
        the step-checkboxes captured in the observe snapshot — the synchronous,
        always-present completion signal — NOT portal's `activeIndex`, which is a
        stale mirror that doesn't move as steps complete (see docs/mission-api.md).
        We advance the step index locally once a step is confirmed.

        Handlers match on the step's own signature, so the same `login-as` fires in
        any mission — the recurring-prefix reuse that motivates this design.
        """
        if self._read_mission is None:
            return {"status": "mission-unreadable", "reason": "no mission reader wired"}
        ms = self._read_mission()
        if not ms:
            return {"status": "mission-unreadable"}
        steps = ms.get("steps") or []
        idx = ms.get("active_index") or 0  # portal's coarse entry point (resume hint)
        steps_done: list[dict[str, Any]] = []

        for _ in range(max_steps):
            if idx >= len(steps):
                return {"status": "complete", "active_index": idx, "steps_done": steps_done}

            step = steps[idx]
            handler = match_step(self.mission_steps, step)
            if handler is None:
                return {"status": "mission-stuck", "active_index": idx, "step": _step_brief(step),
                        "context": self._mission_context(step), "steps_done": steps_done}
            if not auto and self._confirm is not None and not self._confirm(
                [f"mission step {idx}: {step.get('title')}"], {"id": handler["id"]}
            ):
                return {"status": "aborted", "active_index": idx, "steps_done": steps_done}

            self._do_action(handler["do"], {})
            completed = bool(self._verify_step and self._verify_step(step.get("order")))
            steps_done.append({"index": idx, "handler": handler["id"],
                               "step_id": step.get("id"), "completed": completed})
            if not completed:
                return {"status": "mission-blocked", "active_index": idx, "handler": handler["id"],
                        "step": _step_brief(step), "context": self._mission_context(step),
                        "steps_done": steps_done}
            idx += 1
        return {"status": "mission-max-steps", "steps_done": steps_done}

    # --- location ---

    def locate(self) -> tuple[dict[str, Any], list[str]]:
        """Observe the live browser and match it against the course states."""
        payload = self._observe()
        return payload, match_states(self.states, payload)

    def applicable_rules(self, matched: list[str]) -> list[dict[str, Any]]:
        """Rules whose `when` is one of the currently-matched states.

        Greedy: the first (in file order) wins. States can overlap (e.g. a video
        task matches both `scw-course-task` and `scw-video-task`), so order the
        more specific rule first if two apply at once.
        """
        return [r for r in self.rules if r.get("when") in matched]

    # --- actions (ui or http) ---

    def _do_action(self, do: Any, payload: dict[str, Any]) -> None:
        """Execute a rule's `do` — a single action or a list, ui and/or http."""
        for action in do if isinstance(do, list) else [do]:
            if "ui" in action:
                self._act(action["ui"])
            elif "http" in action:
                if self._http is None:
                    raise RuntimeError("this rule needs an http action but no http fn was wired")
                self._run_http(action["http"], payload)
            else:
                raise ValueError(f"rule action has neither 'ui' nor 'http': {action!r}")

    def _build_context(self, spec: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
        """Seed http-template variables from page JS (`eval`) or the page URL (`url_regex`)."""
        ctx: dict[str, Any] = {}
        url = payload.get("url") or ""
        for name, src in (spec or {}).items():
            if "eval" in src:
                if self._eval is None:
                    raise RuntimeError("context uses `eval` but no eval fn was wired")
                ctx[name] = self._eval(src["eval"])
            elif "url_regex" in src:
                m = re.search(src["url_regex"], url)
                ctx[name] = m.group(src.get("group", 1)) if m else None
        return ctx

    @staticmethod
    def _guard_passes(expr: str, ctx: dict[str, Any]) -> bool:
        """Tiny guard DSL for a request's `only_if`: `{a}==b` / `{a}!=b`."""
        for op, want in (("==", True), ("!=", False)):
            if op in expr:
                lhs, rhs = (_template(s.strip(), ctx) for s in expr.split(op, 1))
                return (lhs == rhs) is want
        return bool(_template(expr, ctx))

    def _run_http(self, spec: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Run an `http` action: build context, then fire each request in order.

        Each request may template `{vars}` into url/headers/body, be gated by
        `only_if`, and `capture` top-level fields of the JSON response back into
        the context for later requests (e.g. a taskAttemptId → the next URL).
        """
        ctx = self._build_context(spec.get("context"), payload)
        results: list[dict[str, Any]] = []
        for req in spec.get("requests") or []:
            guard = req.get("only_if")
            if guard and not self._guard_passes(guard, ctx):
                results.append({"skipped": guard})
                continue
            url = _template(req["url"], ctx)
            headers = {k: _template(str(v), ctx) for k, v in (req.get("headers") or {}).items()}
            body = _template(req["body"], ctx) if "body" in req else None
            resp = self._http(req.get("method", "GET"), url, headers, body)  # type: ignore[misc]
            rb = resp.get("body") if isinstance(resp, dict) else None
            for var, field in (req.get("capture") or {}).items():
                ctx[var] = rb.get(field) if isinstance(rb, dict) else None
            results.append({"status": resp.get("status"), "ok": resp.get("ok")})
        return results

    # --- the loop ---

    def run(self, to: str | None = None, max_steps: int = 20, auto: bool = False) -> dict[str, Any]:
        """Walk from wherever we are toward `to` (or until no rule applies).

        Returns a structured result whose `status` is one of:
          reached     — we're in the goal state `to`
          done        — a state flagged `terminal: true` with no applicable rule
                        (a real endpoint, e.g. course complete)
          mission-complete — a mission sub-flow drove every step to done
                        (`mission` holds the per-step trail)
          discovered  — a rule with no `expect` landed us somewhere; its `landed`
                        state is the thing to add a signature for
          unexpected  — a rule's `expect` didn't match where we actually landed
          stuck        — a matched (non-terminal) state has no applicable rule: an
                        action we haven't encoded yet. Carries `context`.
          ambiguous / unknown — 0 or >1 states matched (tighten/add signatures)
          aborted     — a human declined a step
          max_steps   — safety bound hit
        `history` lists every step taken (rule, from, expected, landed). The
        non-progress exits (`unknown`, `stuck`, `unexpected`, `discovered`,
        `max_steps`) also carry a `context` block — the actionable elements, the
        API traffic, and (when nothing matched) the near-miss states — so whoever
        picks it up can try the un-encoded action and then codify it as a rule.
        """
        history: list[dict[str, Any]] = []
        last_payload: dict[str, Any] = {}
        for _ in range(max_steps):
            payload, matched = self.locate()
            last_payload = payload
            url = payload.get("url")
            if not matched:
                return {"status": "unknown", "matched": [], "url": url,
                        "context": self._context(payload, matched), "history": history}
            if to and to in matched:
                return {"status": "reached", "state": to, "url": url, "history": history}

            rules = self.applicable_rules(matched)
            if not rules:
                # A declared terminal with no goal pending is a real endpoint (done).
                # Everything else with no rule is un-encoded work — stuck, and it
                # carries the context to author the missing rule from.
                if not to and any(m in self.terminals for m in matched):
                    return {"status": "done", "state": matched, "url": url, "history": history}
                return {"status": "stuck", "state": matched, "url": url,
                        "context": self._context(payload, matched), "history": history}

            rule = rules[0]
            if not auto and self._confirm is not None and not self._confirm(matched, rule):
                return {"status": "aborted", "state": matched, "rule": rule["id"], "history": history}

            self.machine.set_state(rule["when"])  # reconcile the model with reality

            # A mission rule hands off to the step sub-loop, which is atomic within
            # this outer step: it drives the mission to completion or to a stop, and
            # we report either way (a mission never silently continues the walk).
            if _is_mission_action(rule["do"]):
                mres = self._drive_mission(auto)
                history.append({"rule": rule["id"], "from": matched, "mission": mres})
                if mres.get("status") == "complete":
                    getattr(self, rule["id"])()  # advance the flow model past the mission
                    # A finished mission shouldn't dead-end the walk: if the page now
                    # offers a way onward (a results "Continue" → some page rule), keep
                    # going. Only when nothing non-mission applies is this a genuine
                    # checkpoint we report as `mission-complete`.
                    _, after_matched = self.locate()
                    onward = [r for r in self.applicable_rules(after_matched) if not _is_mission_action(r["do"])]
                    if to and to in after_matched:
                        return {"status": "reached", "state": to, "url": url, "history": history}
                    if onward:
                        continue
                    return {"status": "mission-complete", "rule": rule["id"], "mission": mres,
                            "url": url, "history": history}
                result = {"status": "stuck", "rule": rule["id"], "mission": mres,
                          "url": url, "history": history}
                if mres.get("context"):
                    result["context"] = mres["context"]
                return result

            self._do_action(rule["do"], payload)
            # Fire the pytransitions trigger so the model walks the declared edge.
            # With ignore_invalid_triggers this is a no-op if the edge is absent,
            # but normally it advances the flow model to the rule's `expect`.
            getattr(self, rule["id"])()

            after_payload, after_matched = self.locate()
            expect = rule.get("expect")
            step = {
                "rule": rule["id"],
                "from": matched,
                "expected": expect,
                "landed": after_matched,
                "url": after_payload.get("url"),
            }
            history.append(step)

            if expect is None:
                # Discovery: we walked into territory with no declared destination.
                # Stop and report so a signature (and later `expect`) can be added.
                return {"status": "discovered", **step,
                        "context": self._context(after_payload, after_matched), "history": history}
            # `expect` may list several acceptable destinations (a multi-landing
            # rule like Resume → task OR mission); any one of them verifies the step.
            if not any(e in after_matched for e in _expect_list(expect)):
                return {"status": "unexpected", **step,
                        "context": self._context(after_payload, after_matched), "history": history}
            # Verified edge; loop again from the new state (natural resume point).
        return {"status": "max_steps", "context": self._context(last_payload, []), "history": history}
