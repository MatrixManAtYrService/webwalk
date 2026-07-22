"""Sub-flows — a pluggable "one level down" from the page-state walk.

The `Runner` walks *page states*: observe → match → act → verify. Some activities
are a loop or a decision *inside* a single page state — a mission's checklist of
steps, a challenge's pick-the-answer, a coding lab's paste-the-file. Those are
**sub-flows**: a rule whose `do` names the sub-flow (e.g. `{mission: {}}`) hands
control to it, it runs to a stop, and the runner reports either way.

Keeping this generic is what makes the core flow-agnostic: the engine knows only
that *some* sub-flow handles *some* action key and returns a status. Which
sub-flows exist — mission, challenge, coding-lab, or something a different flow
invents — is supplied by the flow package (see `webwalk/flows/scw/subflows.py`),
not baked in here.

A sub-flow is plain config plus a `drive`:

  * `action_key`   — the key in a rule's `do` that triggers it (`"mission"`).
  * `history_key`  — where its result dict is filed in the walk history/result.
  * `success_statuses` — result statuses that mean "advance the flow model and
                    keep walking". Anything else is a stop the runner reports as
                    `stuck` (carrying the sub-flow's `context`, if any).
  * `check_onward` — after a success, re-observe and, only if no other rule
                    applies, report `checkpoint_status` instead of continuing.
                    A mission uses this so a completed mission that offers no
                    "Continue" is reported as a real checkpoint rather than
                    silently ending the walk.

`drive` gets the live `Runner` so it can reuse the runner's primitives
(`_observe`, `_act`, `_do_action`, `_build_context`, `_confirm`) without the
runner needing to know anything sub-flow-specific.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from webwalk.engine.runner import Runner


class SubFlow:
    """Base class for a sub-flow. Subclasses set the class attrs and implement
    `drive`; they hold their own answer-key / handler config as instance state."""

    action_key: str = ""
    history_key: str = ""
    success_statuses: frozenset[str] = frozenset()
    check_onward: bool = False
    checkpoint_status: str | None = None

    def drive(self, runner: Runner, payload: dict[str, Any], auto: bool) -> dict[str, Any]:
        """Run the sub-flow once from the current page. Returns a result dict whose
        `status` decides what the runner does next (see `success_statuses`). A
        non-success result may carry a `context` block for the stuck report."""
        raise NotImplementedError
