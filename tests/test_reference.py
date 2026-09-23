"""Tests for `<cli> reference` — the schema dump, and its drift guards."""

from __future__ import annotations

from webwalk.engine.matcher import ELEMENT_PREDICATE_FIELDS, element_matches
from webwalk.engine.runner import RUN_STATUSES, UI_ACTION_FIELDS
from webwalk.reference import reference_data, render_reference


def test_render_reference_covers_the_schema() -> None:
    text = render_reference()
    for token in ("url_contains", "text_contains", "wait_after", "capture", "stuck", "EXAMPLES"):
        assert token in text, token


def test_reference_data_shape() -> None:
    data = reference_data()
    assert set(data) == {
        "state_signature", "element_predicate", "rule",
        "action_ui", "action_http", "action_eval", "run_statuses",
    }
    assert "id" in data["state_signature"]
    assert data["action_ui"]["action"].startswith("click")


def test_documented_predicate_fields_are_all_enforced() -> None:
    # Guard against documenting an element-predicate field the matcher ignores: every
    # documented field, given a value, must be able to REJECT a bare element.
    samples = {
        "frame": [9], "tag": "zzz", "type": "zzz", "id": "zzz", "name": "zzz",
        "text": "zzz", "text_contains": "zzz", "aria_contains": "zzz", "href_contains": "zzz",
    }
    assert set(samples) == set(ELEMENT_PREDICATE_FIELDS), "sample set must track the documented fields"
    empty_element = {"frame": []}
    for field, value in samples.items():
        assert element_matches({field: value}, empty_element) is False, field


def test_run_statuses_include_the_common_exits() -> None:
    assert {"reached", "done", "stuck", "unknown", "discovered"} <= set(RUN_STATUSES)


def test_ui_action_fields_are_forwarded_by_make_callbacks() -> None:
    # Guard against documenting a ui field the walker doesn't forward to /step.
    import inspect

    from webwalk.cli import _walk

    src = inspect.getsource(_walk.make_callbacks)
    for field in UI_ACTION_FIELDS:
        assert f'"{field}"' in src or f"ui.get(\"{field}\"" in src, field
