"""The browser-resident training session.

The server owns a single long-lived Selenium browser on the target machine (a
local Chrome, or a Remote WebDriver / Grid pointed at by TS_WEBDRIVER_URL). A
human drives the initial auth + training load directly in the headed window;
after that the same session is used to *observe* browser state and *inject*
actions (clicks, typing, navigation).

Two things funnel through here and nowhere else:
  - observation (snapshot of the page — url, title, interactive elements, DOM)
  - action (click / type / navigate)

Both go through the telemetry gate (`self.capture`). While the gate is closed
we still let the human interact with the browser, but we don't observe it — so
auth never lands in the capture log.

Selenium's WebDriver client is synchronous, so every driver call is pushed to a
worker thread with asyncio.to_thread to keep the FastAPI event loop responsive.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from trainee_sim.server.capture import Capture
from trainee_sim.server.config import SETTINGS

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver

logger = structlog.get_logger()


class NotConnected(RuntimeError):
    """An observation/action was attempted before the browser was connected."""


class BrowserSession:
    """Owns the live Selenium browser. One per server process."""

    def __init__(self) -> None:
        self._driver: WebDriver | None = None
        self._lock = asyncio.Lock()
        self.capture = Capture(SETTINGS.captures_dir, enabled=False)

    # --- lifecycle ---

    def _build_driver(self) -> WebDriver:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        if SETTINGS.headless:
            options.add_argument("--headless=new")

        if SETTINGS.webdriver_url:
            logger.info("connecting_remote_webdriver", url=SETTINGS.webdriver_url)
            return webdriver.Remote(command_executor=SETTINGS.webdriver_url, options=options)
        logger.info("launching_local_chrome")
        return webdriver.Chrome(options=options)

    async def connect(self, start_url: str | None = None) -> dict[str, Any]:
        """Open (or reuse) the browser and navigate to the training entry point.

        Telemetry stays OFF here by design: the human completes auth and loads
        the training in the headed window with nothing observed.
        """
        async with self._lock:
            SETTINGS.ensure_dirs()
            if self._driver is None:
                self._driver = await asyncio.to_thread(self._build_driver)
                logger.info("browser_connected", remote=bool(SETTINGS.webdriver_url))
            url = start_url or SETTINGS.start_url
            await asyncio.to_thread(self._driver.get, url)
            return {
                "status": "ok",
                "url": self._driver.current_url,
                "telemetry": self.capture.enabled,
                "instructions": (
                    "Browser opened on the server machine. Complete any login/MFA and load "
                    "the training material directly in that window. Telemetry is OFF — nothing "
                    "is being observed. Run `trainee-sim telemetry on` once you're past auth "
                    "and ready to start capturing."
                ),
            }

    def _require(self) -> WebDriver:
        if self._driver is None:
            raise NotConnected("No browser session. Run `trainee-sim connect` first.")
        return self._driver

    async def status(self) -> dict[str, Any]:
        connected = self._driver is not None
        url: str | None = None
        title: str | None = None
        if connected:
            try:
                url, title = await asyncio.to_thread(lambda: (self._driver.current_url, self._driver.title))
            except Exception as exc:  # driver alive but page not queryable
                logger.warning("status_probe_failed", error=str(exc))
        return {
            "connected": connected,
            "telemetry": self.capture.enabled,
            "current_url": url,
            "title": title,
            "steps_recorded": self.capture.sequence,
        }

    async def shutdown(self) -> None:
        if self._driver is not None:
            try:
                await asyncio.to_thread(self._driver.quit)
            except Exception as exc:
                logger.warning("driver_quit_failed", error=str(exc))
            self._driver = None

    # --- windows / tabs ---
    #
    # Selenium only ever "sees" the one window it is focused on. New tabs (e.g. a
    # "launch course" button that opens the course in a second tab) are NOT
    # followed automatically — the driver stays put. So observing/acting on the
    # right tab means listing the open windows and switching focus explicitly.

    def _list_windows_sync(self, driver: WebDriver) -> list[dict[str, Any]]:
        """Enumerate all open windows (url/title/active), leaving focus unchanged."""
        original = driver.current_window_handle
        out: list[dict[str, Any]] = []
        for i, handle in enumerate(driver.window_handles):
            driver.switch_to.window(handle)
            out.append(
                {
                    "index": i,
                    "url": driver.current_url,
                    "title": driver.title,
                    "active": handle == original,
                }
            )
        driver.switch_to.window(original)  # restore focus; enumeration is side-effect-free
        return out

    async def windows(self) -> dict[str, Any]:
        driver = self._require()
        windows = await asyncio.to_thread(self._list_windows_sync, driver)
        return {"windows": windows, "count": len(windows)}

    def _switch_sync(self, driver: WebDriver, target: str) -> dict[str, Any]:
        """Focus a window by index, 'latest'/'first', or url/title substring."""
        handles = driver.window_handles
        chosen: str | None = None
        if target == "latest":
            chosen = handles[-1]
        elif target == "first":
            chosen = handles[0]
        elif target.lstrip("-").isdigit():
            idx = int(target)
            if not -len(handles) <= idx < len(handles):
                raise ValueError(f"window index {idx} out of range (0..{len(handles) - 1})")
            chosen = handles[idx]
        else:
            needle = target.lower()
            for handle in handles:
                driver.switch_to.window(handle)
                if needle in (driver.current_url or "").lower() or needle in (driver.title or "").lower():
                    chosen = handle
                    break
            if chosen is None:
                raise ValueError(f"no open window matching {target!r} by url or title")
        driver.switch_to.window(chosen)
        return {
            "handle": chosen,
            "index": handles.index(chosen),
            "url": driver.current_url,
            "title": driver.title,
        }

    async def switch(self, target: str) -> dict[str, Any]:
        driver = self._require()
        result = await asyncio.to_thread(self._switch_sync, driver, target)
        logger.info("window_switched", index=result["index"], url=result["url"])
        return {"status": "ok", **result}

    # --- telemetry gate ---

    async def set_telemetry(self, on: bool) -> dict[str, Any]:
        if on:
            self.capture.enable()
        else:
            self.capture.disable()
        return {"status": "ok", "telemetry": self.capture.enabled}

    # --- observation ---

    def _snapshot_sync(self, driver: WebDriver) -> dict[str, Any]:
        """Capture the page state rules will eventually match on."""
        from selenium.webdriver.common.by import By

        elements: list[dict[str, Any]] = []
        for tag in ("button", "a", "input", "select", "textarea"):
            for el in driver.find_elements(By.TAG_NAME, tag):
                try:
                    if not el.is_displayed():
                        continue
                    elements.append(
                        {
                            "tag": tag,
                            "text": (el.text or "").strip()[:120],
                            "enabled": el.is_enabled(),
                            "type": el.get_attribute("type"),
                            "id": el.get_attribute("id"),
                            "name": el.get_attribute("name"),
                            "aria_label": el.get_attribute("aria-label"),
                            "href": el.get_attribute("href") if tag == "a" else None,
                        }
                    )
                except Exception:
                    # Elements can go stale mid-scan; skip and keep going.
                    continue
        return {
            "url": driver.current_url,
            "title": driver.title,
            "elements": elements,
            "dom": driver.page_source,
            # Record the whole window set with each step, so a capture shows what
            # other tabs were open (and which one this snapshot is of).
            "windows": self._list_windows_sync(driver),
        }

    async def snapshot(self) -> dict[str, Any]:
        driver = self._require()
        return await asyncio.to_thread(self._snapshot_sync, driver)

    async def observe(self, label: str = "observe", window: str | None = None) -> dict[str, Any]:
        """Record the current browser state as one step (if telemetry is on).

        If `window` is given, switch focus to it first (index/'latest'/substring).
        """
        if window is not None:
            await self.switch(window)
        state = await self.snapshot()
        path = self.capture.record(label, {"state": state})
        return _step_summary(path, self.capture.enabled, state)

    # --- action ---

    def _perform_sync(
        self, driver: WebDriver, action: str, selector: str | None, value: str | None
    ) -> dict[str, Any]:
        from selenium.webdriver.common.by import By

        if action == "navigate":
            if not value:
                raise ValueError("navigate requires a value (the URL)")
            driver.get(value)
            return {"action": "navigate", "url": value}

        if selector is None:
            raise ValueError(f"{action} requires a CSS selector")
        el = driver.find_element(By.CSS_SELECTOR, selector)

        if action == "click":
            el.click()
            return {"action": "click", "selector": selector}
        if action == "type":
            el.clear()
            el.send_keys(value or "")
            return {"action": "type", "selector": selector}
        raise ValueError(f"Unknown action: {action!r} (use click, type, or navigate)")

    async def act(
        self,
        action: str,
        selector: str | None = None,
        value: str | None = None,
        label: str | None = None,
        window: str | None = None,
    ) -> dict[str, Any]:
        """Inject an action, capturing before/after state as one step.

        The before/after pair is exactly the raw material for authoring a rule:
        "in this state I did X, and it moved me to that state." If `window` is
        given, switch focus to it before acting.
        """
        driver = self._require()
        if window is not None:
            await asyncio.to_thread(self._switch_sync, driver, window)
        before = await self.snapshot()
        result = await asyncio.to_thread(self._perform_sync, driver, action, selector, value)
        after = await self.snapshot()
        step_label = label or f"{action}-{selector or value or ''}"
        path = self.capture.record(
            step_label,
            {
                "action": {"action": action, "selector": selector, "value": value},
                "result": result,
                "before": before,
                "after": after,
            },
        )
        summary = _step_summary(path, self.capture.enabled, after)
        summary["performed"] = result
        return summary


def _step_summary(path: str | None, telemetry: bool, state: dict[str, Any]) -> dict[str, Any]:
    """A compact result for the CLI — the full state is in the capture file."""
    return {
        "status": "ok",
        "recorded": path is not None,
        "path": path,
        "telemetry": telemetry,
        "url": state["url"],
        "title": state["title"],
        "element_count": len(state["elements"]),
    }


# Module-level singleton — the server process's one live browser session.
SESSION = BrowserSession()
