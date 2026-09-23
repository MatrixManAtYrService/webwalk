"""The flow-authoring reference printed by `<cli> reference`.

Assembled from the constants that live beside the code they describe — the matcher
(state signatures + element predicates) and the runner (rules, actions, run
statuses) — so the reference can't drift from what the engine actually accepts.
An agent reads this instead of grepping the webwalk source.
"""

from __future__ import annotations

from webwalk.engine.matcher import ELEMENT_PREDICATE_FIELDS, STATE_SIGNATURE_FIELDS
from webwalk.engine.runner import (
    EVAL_ACTION_FIELDS,
    HTTP_ACTION_FIELDS,
    RULE_FIELDS,
    RUN_STATUSES,
    UI_ACTION_FIELDS,
)

_EXAMPLES = """\
EXAMPLES (data/states.yaml)

  states:
    - id: course-home                 # a page state
      url_contains: /courses/178893
      title_contains: Organic Chemistry Lab I
      elements:
        - { id: view_course_stream_btn }   # a stable, page-unique element

  rules:
    - id: open-modules                # a UI transition
      when: course-home
      do:
        ui: { action: click, selector: "#modules-link", wait_after: "#expand_collapse_all" }
      expect: modules

    - id: read-attempt               # capture data via an http action (re-runs each walk)
      when: some-task
      do:
        http:
          context: { sid: { eval: "localStorage.sessionId" } }
          requests:
            - method: GET
              url: "https://api.example.com/task/{id}"
              headers: { X-Session-ID: "{sid}" }
              capture: { attempt: taskAttemptId }   # -> {attempt} for later requests

    - id: read-dom                   # or read the DOM with an eval action
      when: modules
      do:
        eval: { script: "JSON.stringify([...document.querySelectorAll('.context_module')].map(m=>m.className))" }
"""


def reference_data() -> dict[str, dict[str, str]]:
    """The reference as structured data (for `reference --json`)."""
    return {
        "state_signature": dict(STATE_SIGNATURE_FIELDS),
        "element_predicate": dict(ELEMENT_PREDICATE_FIELDS),
        "rule": dict(RULE_FIELDS),
        "action_ui": dict(UI_ACTION_FIELDS),
        "action_http": dict(HTTP_ACTION_FIELDS),
        "action_eval": dict(EVAL_ACTION_FIELDS),
        "run_statuses": dict(RUN_STATUSES),
    }


def _section(title: str, fields: dict[str, str]) -> str:
    width = max((len(k) for k in fields), default=0)
    lines = [title]
    lines += [f"  {k:<{width}}  {v}" for k, v in fields.items()]
    return "\n".join(lines)


def render_reference() -> str:
    """The human/agent-readable reference text."""
    blocks = [
        "webwalk flow-authoring reference — the exact schema the engine accepts.",
        _section("STATE SIGNATURE (data/states.yaml -> states[])", STATE_SIGNATURE_FIELDS),
        _section("  element predicate (states[].elements[])", ELEMENT_PREDICATE_FIELDS),
        _section("RULE (data/states.yaml -> rules[])", RULE_FIELDS),
        _section("  do.ui  — a browser action", UI_ACTION_FIELDS),
        _section("  do.http — fire in-page request(s), capture fields", HTTP_ACTION_FIELDS),
        _section("  do.eval — run JS in the page", EVAL_ACTION_FIELDS),
        _section("`run` result statuses (what the walker reports)", RUN_STATUSES),
        _EXAMPLES,
    ]
    return "\n\n".join(blocks)
