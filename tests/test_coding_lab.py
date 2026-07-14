"""Tests for the coding-lab answer key — observe snapshot -> paste+Submit acts.

Pure like the challenge/mission tests: no browser. `assemble_view` reduces an
observe snapshot to `{text, file, active_step}`, and `match_coding_lab` turns that
plus the answer-key handlers into the ready-to-fire acts for the active step. The
fixtures mirror the shapes a real coding-lab snapshot carries (frame text with the
STEPS copy + "step N is complete" markers, and the code-server editor textarea).
"""

from __future__ import annotations

from typing import Any

from trainee_sim.engine.coding_lab import (
    EDITOR_FRAME,
    EDITOR_SELECTOR,
    SUBMIT_FRAME,
    SUBMIT_SELECTOR,
    assemble_view,
    match_coding_lab,
)


def _payload(step_complete: int = 0, file: str = "models.py") -> dict[str, Any]:
    """An observe snapshot: the STEPS copy plus `step_complete` done-markers, and the
    code-server editor textarea (whose aria-label is the open file)."""
    markers = "".join(f"\nstep {i} is complete" for i in range(1, step_complete + 1))
    return {
        "frames": [
            {"text": "INTRODUCTION\nThis lab is about implementing MFA with the PyOTP library." + markers},
            {"text": "unrelated other frame"},
        ],
        "elements": [
            {"tag": "textarea", "aria_label": file, "frame": EDITOR_FRAME},
            {"tag": "button", "text": "Submit", "frame": SUBMIT_FRAME},
        ],
    }


HANDLERS: list[dict[str, Any]] = [
    {
        "id": "mfa-python",
        "match": {"text_contains": "implementing MFA with the PyOTP", "file": "models.py"},
        "steps": [
            {"content": "STEP ONE CONTENT"},
            {"content": "STEP TWO CONTENT"},
        ],
    }
]


# --- assemble_view ---


def test_active_step_is_zero_when_nothing_complete() -> None:
    view = assemble_view(_payload(step_complete=0))
    assert view["active_step"] == 0
    assert view["file"] == "models.py"
    assert "implementing mfa" in view["text"].lower()


def test_active_step_tracks_the_highest_completion_marker() -> None:
    assert assemble_view(_payload(step_complete=1))["active_step"] == 1
    assert assemble_view(_payload(step_complete=2))["active_step"] == 2


def test_file_is_none_when_no_editor_textarea() -> None:
    payload = {"frames": [{"text": "some steps"}], "elements": []}
    assert assemble_view(payload)["file"] is None


def _badge_payload(panel: str) -> dict[str, Any]:
    """A multi-step lab snapshot whose STEPS panel uses number badges (no 'step N is
    complete' markers) — a done step drops its number, so the panel is the signal.
    A trailing frame mimics the editor's line-number gutter (must NOT be counted)."""
    return {
        "frames": [{"text": panel}, {"text": "editor\n1\n2\n3\n4\nimport logging"}],
        "elements": [{"tag": "textarea", "aria_label": "logger.py", "frame": EDITOR_FRAME}],
    }


def test_active_step_from_number_badges_when_no_complete_markers() -> None:
    # 0 done: step 1's badge ("1") still shows -> active 0.
    assert assemble_view(_badge_payload("STEPS\n1\nConfigure logging\n2\nLog info\n3\nLog warn"))["active_step"] == 0
    # 1 done: step 1's number gone (checkmark), first badge is "2" -> active 1.
    assert assemble_view(_badge_payload("STEPS\nConfigure logging\n2\nLog info\n3\nLog warn"))["active_step"] == 1
    # 2 done: first badge is "3" -> active 2 (and the editor gutter '1\n2..' is ignored).
    assert assemble_view(_badge_payload("STEPS\nConfigure logging\nLog info\n3\nLog warn"))["active_step"] == 2


# --- match_coding_lab ---


def test_matches_and_builds_paste_then_submit_for_active_step() -> None:
    result = match_coding_lab(HANDLERS, assemble_view(_payload(step_complete=0)))
    assert result is not None
    assert result["id"] == "mfa-python"
    assert result["active_step"] == 0 and result["done"] is False
    paste, submit = result["acts"]
    assert paste["action"] == "paste"
    assert paste["value"] == "STEP ONE CONTENT"
    assert paste["selector"] == EDITOR_SELECTOR and paste["frame"] == EDITOR_FRAME
    assert submit["action"] == "click"
    assert submit["selector"] == SUBMIT_SELECTOR and submit["frame"] == SUBMIT_FRAME


def test_active_step_selects_the_next_steps_content() -> None:
    result = match_coding_lab(HANDLERS, assemble_view(_payload(step_complete=1)))
    assert result["acts"][0]["value"] == "STEP TWO CONTENT"


def test_done_when_every_step_is_complete() -> None:
    result = match_coding_lab(HANDLERS, assemble_view(_payload(step_complete=2)))
    assert result["done"] is True
    assert result["acts"] == []


def test_no_handler_returns_none() -> None:
    payload = _payload()
    payload["frames"][0]["text"] = "a totally different lab about SQL injection"
    assert match_coding_lab(HANDLERS, assemble_view(payload)) is None


def test_empty_match_never_fires() -> None:
    assert match_coding_lab([{"id": "x", "match": {}, "steps": [{"content": "c"}]}], assemble_view(_payload())) is None


def test_file_mismatch_blocks_but_unknown_file_does_not() -> None:
    # A handler pinned to a different file must not match.
    assert match_coding_lab(HANDLERS, assemble_view(_payload(file="other.py"))) is None
    # But when the view couldn't read the open file, the file check is skipped.
    view = assemble_view(_payload())
    view["file"] = None
    assert match_coding_lab(HANDLERS, view) is not None


def test_open_file_step_prepends_a_tab_click() -> None:
    handlers = [{
        "id": "multi", "match": {"text_contains": "implementing MFA with the PyOTP"},
        "steps": [{"content": "C1"}, {"open_file": "user.py", "content": "C2"}],
    }]
    # step 0: no open_file -> just [paste, submit].
    assert [a["action"] for a in match_coding_lab(handlers, assemble_view(_payload(0)))["acts"]] == ["paste", "click"]
    # step 1: open_file -> [click tab, paste, submit], and the tab click targets user.py.
    acts = match_coding_lab(handlers, assemble_view(_payload(step_complete=1)))["acts"]
    assert [a["action"] for a in acts] == ["click", "paste", "click"]
    assert "user.py" in acts[0]["selector"] and acts[0]["frame"] == EDITOR_FRAME
    assert acts[1]["value"] == "C2"


def test_bare_string_step_is_accepted_as_content() -> None:
    handlers = [{"id": "h", "match": {"text_contains": "implementing MFA with the PyOTP"}, "steps": ["JUST A STRING"]}]
    result = match_coding_lab(handlers, assemble_view(_payload()))
    assert result["acts"][0]["value"] == "JUST A STRING"
