"""Tests for the challenge assembler — raw editor scrape -> decision view.

Pure like the runner/mission tests: no browser. The fixtures are the raw shapes
the browser scraper (`CHALLENGE_JS`) actually returned from a live SCW challenge
(captured while building docs/code-observability-design.md) — a locate stage
(4 candidate blocks across auth.py/clients.py) and a fix stage (base‖solution
diff with 4 solution toggles). The point is that `assemble` turns each into a
pick-an-option decision with ready-to-fire actions.
"""

from __future__ import annotations

from typing import Any

from trainee_sim.engine.challenge import assemble, match_challenge

# --- fixtures: raw scraper output, verbatim shape ---

LOCATE_RAW: dict[str, Any] = {
    "editor": {
        "framePath": [0],
        "file": "auth.py",
        "files": ["auth.py", "clients.py"],
        "panes": [
            [
                {"n": 99, "code": "                if client_data:"},
                {"n": 100, "code": "                    self._set_session_cookie(sessionid=client_data[0])"},
                {"n": 101, "code": "                else:"},
            ]
        ],
    },
    "answers": [
        "Select auth.py:33-33 as answer",
        "Select auth.py:100-100 as answer",
        "Select clients.py:37-38 as answer",
        "Select clients.py:52-56 as answer",
    ],
    "toggles": [],
    "submit": "Submit answer",
    "vulnerability": "Authentication - Improper Authentication",
}

FIX_RAW: dict[str, Any] = {
    "editor": {
        "framePath": [0],
        "file": "auth.py",
        "files": ["auth.py"],
        "panes": [
            [{"n": 100, "code": "        self._set_session_cookie(sessionid=client_data[0])"}],
            [{"n": 102, "code": "        if client.is_authenticate:"}],
        ],
    },
    "answers": [],
    "toggles": [
        {"label": "On the left side, show the vulnerable code", "pressed": True},
        {"label": "On the left side, show solution 1", "pressed": False},
        {"label": "On the right side, show solution 1", "pressed": True},
        {"label": "On the right side, show solution 2", "pressed": False},
        {"label": "On the right side, show solution 3", "pressed": False},
        {"label": "On the right side, show solution 4", "pressed": False},
    ],
    "submit": "Submit answer",
    "vulnerability": None,
}


# --- locate stage ---

def test_locate_stage_detected() -> None:
    view = assemble(LOCATE_RAW)
    assert view["stage"] == "locate"
    assert view["file"] == "auth.py"
    assert view["vulnerability"] == "Authentication - Improper Authentication"


def test_locate_options_are_the_candidate_blocks() -> None:
    view = assemble(LOCATE_RAW)
    ids = [o["id"] for o in view["options"]]
    assert ids == ["auth.py:33-33", "auth.py:100-100", "clients.py:37-38", "clients.py:52-56"]
    assert "4 highlighted blocks" in view["decision"]


def test_locate_option_act_is_fireable() -> None:
    view = assemble(LOCATE_RAW)
    opt = next(o for o in view["options"] if o["id"] == "auth.py:100-100")
    assert opt["act"] == {
        "action": "click",
        "by": "css",
        "selector": '[aria-label="Select auth.py:100-100 as answer"]',
        "frame": [0],
    }
    assert opt["from"] == 100 and opt["to"] == 100


def test_locate_pairs_source_for_open_file_only() -> None:
    view = assemble(LOCATE_RAW)
    # auth.py:100-100 is in the open pane -> code attached.
    opt_open = next(o for o in view["options"] if o["id"] == "auth.py:100-100")
    assert opt_open["code"] == [
        {"n": 100, "code": "                    self._set_session_cookie(sessionid=client_data[0])"}
    ]
    # clients.py block is in an unopened file -> no rows present.
    opt_other = next(o for o in view["options"] if o["id"].startswith("clients.py"))
    assert opt_other["code"] is None


def test_locate_submit_action() -> None:
    view = assemble(LOCATE_RAW)
    assert view["submit"] == {
        "action": "click", "by": "css", "selector": '[aria-label="Submit answer"]', "frame": [0],
    }


# --- fix stage ---

def test_fix_stage_detected_by_two_panes() -> None:
    view = assemble(FIX_RAW)
    assert view["stage"] == "fix"
    assert "4 candidate solutions" in view["decision"]


def test_fix_options_are_right_side_solutions() -> None:
    view = assemble(FIX_RAW)
    assert [o["id"] for o in view["options"]] == [1, 2, 3, 4]
    selected = [o["id"] for o in view["options"] if o["selected"]]
    assert selected == [1]
    assert view["options"][2]["act"]["selector"] == '[aria-label="On the right side, show solution 3"]'


def test_fix_panes_do_not_merge() -> None:
    # The two panes keep their own numbering (a diff), not a merged file.
    view = assemble(FIX_RAW)
    assert view["left"]["shows"] == "the vulnerable code"
    assert view["left"]["code"][0]["n"] == 100
    assert view["right"]["shows"] == "solution 1"
    assert view["right"]["code"][0]["n"] == 102


def test_fix_left_options_let_engine_rebase() -> None:
    view = assemble(FIX_RAW)
    assert [o["id"] for o in view["left_options"]] == [1]  # only "show solution 1" is a numbered left toggle


def test_locate_cross_file_line_numbers_do_not_leak() -> None:
    # Regression: a block whose file isn't the open tab must get code=None even when
    # the open pane HAPPENS to have those line numbers (per-file numbering). Here the
    # open auth.py pane has lines 37-38, but the clients.py:37-38 option is a
    # DIFFERENT file, so it must not borrow auth.py's rows.
    raw: dict[str, Any] = {
        "editor": {
            "framePath": [0], "file": "auth.py", "files": ["auth.py", "clients.py"],
            "panes": [[{"n": 37, "code": "auth line 37"}, {"n": 38, "code": "auth line 38"}]],
        },
        "answers": ["Select auth.py:37-38 as answer", "Select clients.py:37-38 as answer"],
        "toggles": [], "submit": "Submit answer", "vulnerability": "X",
    }
    view = assemble(raw)
    by_id = {o["id"]: o for o in view["options"]}
    assert by_id["auth.py:37-38"]["code"] == [{"n": 37, "code": "auth line 37"}, {"n": 38, "code": "auth line 38"}]
    assert by_id["clients.py:37-38"]["code"] is None  # NOT auth.py's rows


# --- the answer key (match_challenge) ---

LOCATE_KEY = [{
    "id": "expose-locate",
    "match": {"stage": "locate", "vulnerability_contains": "Improper Authentication",
              "options_include": ["auth.py:100-100", "clients.py:37-38"]},
    "answer": {"select": "auth.py:100-100"},
}]

FIX_KEY = [{
    "id": "expose-fix",
    "match": {"stage": "fix", "file": "auth.py"},  # FIX_RAW has vulnerability=None -> key on file
    "answer": {"select": 3},
}]


def test_match_locate_selects_named_block_then_submit() -> None:
    view = assemble(LOCATE_RAW)
    m = match_challenge(LOCATE_KEY, view)
    assert m is not None
    assert m["id"] == "expose-locate" and m["stage"] == "locate"
    select, submit = m["acts"]
    assert select["selector"] == '[aria-label="Select auth.py:100-100 as answer"]'
    assert select["frame"] == [0] and select["settle"] == 1
    # locate's submit is null pre-selection, so it's constructed from the editor frame.
    assert submit["selector"] == '[aria-label="Submit answer"]' and submit["frame"] == [0]


def test_match_fix_selects_solution_and_uses_view_submit() -> None:
    m = match_challenge(FIX_KEY, assemble(FIX_RAW))
    assert m is not None and m["stage"] == "fix"
    select, submit = m["acts"]
    assert select["selector"] == '[aria-label="On the right side, show solution 3"]'
    assert submit["selector"] == '[aria-label="Submit answer"]'  # fix view already carries submit


def test_match_locate_by_code_regex() -> None:
    key = [{"id": "by-code", "match": {"stage": "locate"},
            "answer": {"code_regex": r"_set_session_cookie"}}]
    m = match_challenge(key, assemble(LOCATE_RAW))
    assert m is not None
    # auth.py:100-100 is the only open-pane block whose code matches -> its select act.
    assert m["acts"][0]["selector"] == '[aria-label="Select auth.py:100-100 as answer"]'


def test_no_match_when_vulnerability_differs() -> None:
    key = [{"id": "other", "match": {"stage": "locate", "vulnerability_contains": "SQL Injection"},
            "answer": {"select": "auth.py:100-100"}}]
    assert match_challenge(key, assemble(LOCATE_RAW)) is None


def test_no_match_when_fingerprint_block_absent() -> None:
    key = [{"id": "fp", "match": {"stage": "locate", "options_include": ["nope.py:1-1"]},
            "answer": {"select": "auth.py:100-100"}}]
    assert match_challenge(key, assemble(LOCATE_RAW)) is None


def test_no_match_when_answer_option_not_present() -> None:
    # The challenge matches, but the named answer block isn't among the options
    # (e.g. already answered and removed) -> no acts, so the handler doesn't fire.
    key = [{"id": "gone", "match": {"stage": "locate"}, "answer": {"select": "auth.py:999-999"}}]
    assert match_challenge(key, assemble(LOCATE_RAW)) is None


def test_wrong_stage_does_not_match() -> None:
    assert match_challenge(LOCATE_KEY, assemble(FIX_RAW)) is None  # locate key vs a fix view


def test_empty_match_never_fires() -> None:
    assert match_challenge([{"id": "x", "match": {}, "answer": {"select": 1}}], assemble(FIX_RAW)) is None


def test_match_none_when_no_editor() -> None:
    assert match_challenge(LOCATE_KEY, assemble({"editor": None})) is None


# --- no editor ---

def test_no_editor_is_stage_none() -> None:
    assert assemble({"editor": None})["stage"] == "none"
    assert assemble({})["stage"] == "none"
