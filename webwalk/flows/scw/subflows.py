"""The SCW sub-flows — the "one level down" handlers the runner hands off to.

Each is a `SubFlow` (see `webwalk/engine/subflow.py`): the engine knows only the
generic contract (action key, success statuses, drive → result), while the
SCW-specific machinery — the mission step-handler library, the challenge/coding-lab
answer keys, and how to read/verify each — lives here. This is the seam that keeps
the core flow-agnostic: a different flow registers its own sub-flows (or none).

The drive methods reuse the live `Runner`'s primitives (`_observe`, `_act`,
`_do_action`, `_build_context`, `_confirm`) so nothing here re-implements I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from webwalk.engine.runner import actionable_elements, summarize_network
from webwalk.engine.subflow import SubFlow
from webwalk.flows.scw.challenge import match_challenge
from webwalk.flows.scw.coding_lab import assemble_view, match_coding_lab
from webwalk.flows.scw.mission import match_step, render_actions

if TYPE_CHECKING:
    from webwalk.engine.runner import Runner

# () -> {active_index, steps:[instruction…], checkable:{orders…}} for the mission
# we're on, or None. `active_index` is the current step: derived from the observable
# step-checkboxes (the first unticked one), not portal's stale `activeIndex`.
# `checkable` is the set of step orders that actually have a checklist checkbox;
# orders missing from it are informational (no box, no verification).
ReadMissionFn = Callable[[], dict[str, Any] | None]
# (step order) -> did the pod tick that step's checkbox within a timeout? The
# checkbox `checked` state is the synchronous, in-snapshot completion signal.
VerifyStepFn = Callable[[int | None], bool]


def _step_brief(step: dict[str, Any]) -> dict[str, Any]:
    """The human-facing slice of a mission step (for reporting / stuck context)."""
    return {"order": step.get("order"), "title": step.get("title"),
            "description": (step.get("description") or "").strip()}


class MissionSubFlow(SubFlow):
    """Walk a mission's checklist of steps (server-verified — no cheap API skip)."""

    action_key = "mission"
    history_key = "mission"
    success_statuses = frozenset({"complete"})
    # A finished mission may offer a results "Continue"; only when nothing else
    # applies is `mission-complete` a real checkpoint (see SubFlow.check_onward).
    check_onward = True
    checkpoint_status = "mission-complete"

    def __init__(self, read_mission: ReadMissionFn | None, mission_steps: list[dict[str, Any]],
                 verify_step: VerifyStepFn | None) -> None:
        self.read_mission = read_mission     # reads {active_index, steps, checkable}
        self.mission_steps = mission_steps   # the shared step-handler library
        self.verify_step = verify_step       # confirms a step's checkbox ticked

    def _context(self, runner: Runner, step: dict[str, Any]) -> dict[str, Any]:
        """Stuck-context for a step we can't handle: the step text plus the live app
        form, so the agent can perform it and then author a handler."""
        payload = runner._observe()
        return {
            "url": payload.get("url"),
            "title": payload.get("title"),
            "step": _step_brief(step),
            "actionable": actionable_elements(payload),
            "network": summarize_network(payload, runner.api_hosts),
        }

    def drive(self, runner: Runner, payload: dict[str, Any], auto: bool, max_steps: int = 15) -> dict[str, Any]:
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
        if self.read_mission is None:
            return {"status": "mission-unreadable", "reason": "no mission reader wired"}
        ms = self.read_mission()
        if not ms:
            return {"status": "mission-unreadable"}
        steps = ms.get("steps") or []
        idx = ms.get("active_index") or 0  # portal's coarse entry point (resume hint)
        # Orders that have a checklist checkbox — i.e. pod-gated steps. A step whose
        # order is absent from a NON-EMPTY set is *informational* (e.g. "Inspect the
        # URL"): the checklist renders no box for it, so there's nothing to do and
        # nothing to verify — we just advance past it. When the set is empty/None the
        # pod boxes simply weren't captured, so we don't skip anything.
        checkable = ms.get("checkable")
        steps_done: list[dict[str, Any]] = []

        for _ in range(max_steps):
            if idx >= len(steps):
                return {"status": "complete", "active_index": idx, "steps_done": steps_done}

            step = steps[idx]
            if checkable and step.get("order") not in checkable:
                steps_done.append({"index": idx, "handler": None, "step_id": step.get("id"),
                                   "informational": True, "completed": True})
                idx += 1
                continue

            handler = match_step(self.mission_steps, step)
            if handler is None:
                return {"status": "mission-stuck", "active_index": idx, "step": _step_brief(step),
                        "context": self._context(runner, step), "steps_done": steps_done}
            if not auto and runner._confirm is not None and not runner._confirm(
                [f"mission step {idx}: {step.get('title')}"], {"id": handler["id"]}
            ):
                return {"status": "aborted", "active_index": idx, "steps_done": steps_done}

            # A handler may source vars from the LIVE page (its `context` block) — a
            # value scraped from the DOM/URL, like a stolen session token — not just
            # from the step text. match_step left those `{page_var}` placeholders in
            # `do`; resolve them now (needs I/O, so it's here not in the pure matcher)
            # and template them in with a second render pass before acting.
            do = handler["do"]
            if handler.get("context"):
                page_ctx = runner._build_context(handler["context"], runner._observe())
                do = render_actions(do, page_ctx)
            runner._do_action(do, {})
            completed = bool(self.verify_step and self.verify_step(step.get("order")))
            steps_done.append({"index": idx, "handler": handler["id"],
                               "step_id": step.get("id"), "completed": completed})
            if not completed:
                return {"status": "mission-blocked", "active_index": idx, "handler": handler["id"],
                        "step": _step_brief(step), "context": self._context(runner, step),
                        "steps_done": steps_done}
            idx += 1
        return {"status": "mission-max-steps", "steps_done": steps_done}


class ChallengeSubFlow(SubFlow):
    """Answer one SCW challenge decision (locate / fix) from the answer-key library."""

    action_key = "challenge"
    history_key = "challenge"
    success_statuses = frozenset({"solved"})

    def __init__(self, handlers: list[dict[str, Any]]) -> None:
        self.handlers = handlers  # the challenge answer key

    def _context(self, payload: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
        """Stuck-context for a challenge we have no answer key for: the decision
        view itself — stage, vulnerability, and the enumerated options with their
        code — so a human can pick the answer and author a `challenges.yaml` key."""
        return {
            "url": payload.get("url"),
            "stage": view.get("stage"),
            "file": view.get("file"),
            "vulnerability": view.get("vulnerability"),
            "decision": view.get("decision"),
            "options": [{"id": o.get("id"), "code": o.get("code")} for o in view.get("options") or []],
        }

    def drive(self, runner: Runner, payload: dict[str, Any], auto: bool) -> dict[str, Any]:
        """Answer ONE challenge decision: read the embedded decision view, match it
        against the answer-key library, and fire the option's select + Submit acts.

        Deliberately one decision per call, NOT a sub-loop: a challenge stage either
        completes (a success overlay appears — handled by an overlay rule in the
        OUTER loop, which is why that rule must precede this one) or advances to the
        next stage (locate -> fix), which the outer loop re-enters as `scw-challenge`
        again. So the runner re-observes between decisions and lets its page rules
        route each transition — no completion-detection guesswork here. Handlers
        match on the challenge's own content, so the same key fires on any replay.
        """
        view = payload.get("challenge")
        if not view or view.get("stage") in (None, "none"):
            # No decidable editor here — the overlay is likely up covering it; report
            # so the outer loop's overlay/next rules (or a human) can take over.
            return {"status": "challenge-none"}
        handler = match_challenge(self.handlers, view)
        if handler is None:
            return {"status": "challenge-stuck", "stage": view.get("stage"),
                    "context": self._context(payload, view)}
        if not auto and runner._confirm is not None and not runner._confirm(
            [f"challenge {view.get('stage')}: {view.get('vulnerability')}"], {"id": handler["id"]}
        ):
            return {"status": "aborted", "stage": view.get("stage")}
        for act in handler["acts"]:
            runner._act(act)
        return {"status": "solved", "id": handler["id"], "stage": view.get("stage")}


class CodingLabSubFlow(SubFlow):
    """Solve one SCW coding-lab step: paste the answer-key file content, then Submit."""

    action_key = "coding_lab"
    history_key = "coding_lab"
    success_statuses = frozenset({"solved", "coding-lab-done"})

    def __init__(self, handlers: list[dict[str, Any]]) -> None:
        self.handlers = handlers  # the coding-lab answer key

    def _context(self, runner: Runner, payload: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
        """Stuck-context for a coding lab we have no answer key for: which file/step is
        open plus the live page, so a human can solve it and author a coding-labs.yaml
        key. The lab's full STEPS copy is in `view['text']` — trimmed here to a hint."""
        return {
            "url": payload.get("url"),
            "title": payload.get("title"),
            "file": view.get("file"),
            "active_step": view.get("active_step"),
            "steps_text": (view.get("text") or "")[:1500],
            "actionable": actionable_elements(payload),
            "network": summarize_network(payload, runner.api_hosts),
        }

    def drive(self, runner: Runner, payload: dict[str, Any], auto: bool) -> dict[str, Any]:
        """Solve ONE coding-lab step: read the lab signature + active step from the
        observe snapshot, match the answer key, and fire the step's paste + Submit acts.

        One step per call, NOT a sub-loop — mirroring ChallengeSubFlow: after Submit the
        step either advances (the next step becomes active, re-entered here) or reveals
        the completion screen (scw-coding-lab-complete → its own advance rule). So the
        outer loop re-observes between steps and routes each transition; `active_step`
        (how many steps are already complete) is what makes that resumable. Handlers
        match on the lab's own content, so the same key fires on any replay.
        """
        view = assemble_view(payload)
        handler = match_coding_lab(self.handlers, view)
        if handler is None:
            return {"status": "coding-lab-stuck", "active_step": view.get("active_step"),
                    "context": self._context(runner, payload, view)}
        if handler.get("done"):
            # Every step is complete but we still matched scw-coding-lab — let the outer
            # loop re-observe and pick up the completion screen. (Shouldn't normally
            # happen: the completion screen has no Submit, so scw-coding-lab won't match.)
            return {"status": "coding-lab-done", "active_step": view.get("active_step")}
        if not auto and runner._confirm is not None and not runner._confirm(
            [f"coding-lab step {(view.get('active_step') or 0) + 1}"], {"id": handler["id"]}
        ):
            return {"status": "aborted", "active_step": view.get("active_step")}
        for act in handler["acts"]:
            runner._act(act)
        return {"status": "solved", "id": handler["id"], "active_step": view.get("active_step")}
