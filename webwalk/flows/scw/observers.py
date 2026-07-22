"""SCW server-side plugin — the challenge observer + `/challenge` endpoint.

Loaded into the server process via `WEBWALK_SERVER_PLUGINS=webwalk.flows.scw.observers`
(the SCW CLI's `serve` sets this). Importing it registers:

  * a **snapshot extension** that folds the code-editor challenge decision view
    into every `observe` snapshot under `challenge` (only when a decidable editor
    is on the page), so a solve rule gets the decision for free; and
  * the **`/challenge` endpoint**, which runs the same scraper on demand.

An SCW challenge is a decision among fixed options; `scw/challenge.py` does the
pure shaping (stage, enumerated choices each with a ready-to-fire action, source
as context). This module just wires that scraper into the generic server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from webwalk.flows.scw.challenge import CHALLENGE_JS, assemble
from webwalk.server.plugins import flow_router, register_snapshot_extension
from webwalk.server.session import SESSION

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver


def _challenge_view(driver: WebDriver) -> dict[str, Any]:
    """Run the code-editor scraper and shape it into a decision view. `stage: "none"`
    when no editor is on the page."""
    driver.switch_to.default_content()  # the scraper walks frames itself
    raw = driver.execute_script(CHALLENGE_JS) or {"editor": None}
    return assemble(raw)


def _snapshot_challenge(driver: WebDriver) -> dict[str, Any] | None:
    """Snapshot extension: fold the view into an observe only when a decidable
    editor is up (else None, so the key is absent — a normal page observes clean)."""
    view = _challenge_view(driver)
    return view if view.get("stage") not in (None, "none") else None


register_snapshot_extension("challenge", _snapshot_challenge)


@flow_router.get("/challenge")
async def challenge() -> dict[str, Any]:
    """The current SCW challenge as a decision: stage, options, source context."""
    return await SESSION.extract(_challenge_view)
