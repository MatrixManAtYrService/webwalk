"""Tests for the mission step-matcher — the "which step, and how" core.

Pure like the runner tests: no browser, no server. These load the *real*
courses/missions.yaml so the authored handlers + regexes are checked against the
actual step text captured live from vb-52 (web.auth.missingauth) — the point
being that one `login-as` handler fires across every login step regardless of
which mission it came from."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from trainee_sim.engine.mission import match_step

STEPS: list[dict[str, Any]] = yaml.safe_load(Path("courses/missions.yaml").read_text())["steps"]

# The real vb-52 playbook (from `trainee-sim mission` live), verbatim.
S0_ALICE = {"order": 0, "title": "Log in as Alice",
            "description": "You can log in with the user `alice@alice.com`, and password "
                           "`P@ssw0rd1!`. Take note of Alice's account number. You might need it later."}
S1_LOGOUT = {"order": 1, "title": "Log out",
             "description": "Log out, and have a look at the code snippet in the right pane. "
                            "Do you see anything that could bypass access control?"}
S2_BADPW = {"order": 2, "title": "Log in with an incorrect password",
            "description": "Log in again as Alice with `alice@alice.com` but this time, you enter "
                           "any password you like. Surprisingly, this works!"}
S3_BOB = {"order": 3, "title": "Log in as Bob",
          "description": "Log out again. This time, you log in as Bob using `bob@bob.com`, and "
                         "enter any password you like."}
S4_TRANSFER = {"order": 4, "title": "Transfer money from Bob to Alice",
               "description": "Go to the __transfer__ page and make a money transfer to Alice's "
                              "account (`94923054`) for any amount."}


def _typed(result: dict[str, Any], selector: str) -> str | None:
    """The value the rendered `do` types into `selector` (for asserting extraction)."""
    for action in result["do"]:
        ui = action.get("ui", {})
        if ui.get("action") == "type" and ui.get("selector") == selector:
            return ui.get("value")
    return None


def test_login_as_fires_for_alice_and_extracts_real_credentials() -> None:
    result = match_step(STEPS, S0_ALICE)
    assert result is not None
    assert result["id"] == "login-as"
    assert result["ctx"] == {"email": "alice@alice.com", "password": "P@ssw0rd1!"}
    # …and the credentials are templated into the concrete form actions.
    assert _typed(result, "#Username") == "alice@alice.com"
    assert _typed(result, "#Password") == "P@ssw0rd1!"


def test_same_handler_fires_for_bob_with_a_different_value() -> None:
    # The "one thing they changed": a different email, no explicit password token.
    result = match_step(STEPS, S3_BOB)
    assert result is not None and result["id"] == "login-as"
    assert result["ctx"]["email"] == "bob@bob.com"
    assert result["ctx"]["password"] == "x"  # "any password you like" → the default


def test_login_handler_also_covers_the_incorrect_password_variant() -> None:
    result = match_step(STEPS, S2_BADPW)
    assert result is not None and result["id"] == "login-as"
    assert result["ctx"] == {"email": "alice@alice.com", "password": "x"}


def test_logout_and_transfer_are_unencoded_so_far() -> None:
    # No handler yet → None, which is what makes the runner stop with context for
    # the agent to perform it once and then add the handler here.
    assert match_step(STEPS, S1_LOGOUT) is None
    assert match_step(STEPS, S4_TRANSFER) is None


def test_step_frame_is_carried_through_rendering() -> None:
    # The mocked-app form lives in frame [0,0,0]; rendering must not mangle it.
    result = match_step(STEPS, S0_ALICE)
    assert all(a["ui"]["frame"] == [0, 0, 0] for a in result["do"])
