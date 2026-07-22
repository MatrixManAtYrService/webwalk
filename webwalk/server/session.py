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
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from webwalk.server.capture import Capture
from webwalk.server.config import SETTINGS
from webwalk.server.recorder import NetworkRecorder

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver

logger = structlog.get_logger()


class NotConnected(RuntimeError):
    """An observation/action was attempted before the browser was connected."""


class ActionFailed(RuntimeError):
    """A click/type couldn't be performed (element not found, not clickable, …).

    Carries a readable reason so the caller gets a 4xx with a message instead of
    an opaque 500 when a selector is wrong or the element isn't interactable.
    """


_RETURN_RE = re.compile(r"\breturn\b")

# Chrome/chromedriver parse-error signatures. These surface only at parse time
# (before any statement runs), which is what makes the eval fallback safe.
_SYNTAX_ERROR_MARKERS = (
    "unexpected token",
    "unexpected identifier",
    "unexpected string",
    "unexpected number",
    "unexpected end of input",
    "invalid or unexpected token",
    "missing ) after",
)


def _has_toplevel_return(script: str) -> bool:
    """Cheap check for whether `script` already looks like a function body.

    Only a fast path — a wrong answer is harmless: a false negative just costs
    an extra probe round trip, a false positive returns undefined instead of a
    value (the same result the caller would get running the raw script anyway).
    """
    return bool(_RETURN_RE.search(script))


def _is_syntax_error(exc: Exception) -> bool:
    msg = str(getattr(exc, "msg", None) or exc).lower()
    return any(marker in msg for marker in _SYNTAX_ERROR_MARKERS)


class BrowserSession:
    """Owns the live Selenium browser. One per server process."""

    def __init__(self) -> None:
        self._driver: WebDriver | None = None
        self._lock = asyncio.Lock()
        # Throttle for in-page fetches (fetch_in_page): one at a time, spaced out,
        # so an automated walk stays gentle on a live LMS. See _throttle_fetch.
        self._fetch_lock = asyncio.Lock()
        self._last_fetch_ts = 0.0
        self.capture = Capture(SETTINGS.captures_dir, enabled=False)
        # Sibling to `capture`: records the training's API traffic, gated by the
        # same telemetry flag. See docs/network-capture-design.md.
        self.network = NetworkRecorder(
            hosts=SETTINGS.network_hosts,
            max_events=SETTINGS.network_max_events,
            max_body_bytes=SETTINGS.network_max_body_bytes,
            enabled=False,
        )

    # --- lifecycle ---

    def _build_driver(self) -> WebDriver:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        if SETTINGS.headless:
            options.add_argument("--headless=new")
        if SETTINGS.browser_binary:
            # Drive a specific Chromium-family binary (Brave/Chromium/Edge/pinned
            # Chrome). All speak CDP, so network capture behaves identically.
            options.binary_location = SETTINGS.browser_binary

        if SETTINGS.network_capture:
            # Network capture rides on Chrome's performance log: a pull-based
            # stream of CDP Network.* events we drain per snapshot. It must be
            # switched on at build time (there's no toggling it live) — but the
            # telemetry gate still decides whether anything is *retained*, so
            # enabling it here doesn't record auth. See recorder.py.
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            options.add_experimental_option(
                "perfLoggingPrefs", {"enableNetwork": True, "enablePage": False}
            )

        if SETTINGS.webdriver_url:
            logger.info("connecting_remote_webdriver", url=SETTINGS.webdriver_url)
            return webdriver.Remote(command_executor=SETTINGS.webdriver_url, options=options)
        # Reuse a persistent profile so cookies/device-trust survive restarts and
        # we don't re-run the whole login every time the server bounces.
        options.add_argument(f"--user-data-dir={SETTINGS.browser_profile_dir}")
        logger.info("launching_local_chrome", profile=str(SETTINGS.browser_profile_dir))
        return webdriver.Chrome(options=options)

    def _driver_alive(self, driver: WebDriver) -> bool:
        """Cheap round-trip to the browser; False if the session/window is gone.

        If the user closes the browser (e.g. to retry auth), the driver object
        lingers but its session is dead — any command then raises. We probe with
        window_handles so we can rebuild instead of crashing.
        """
        try:
            return bool(driver.window_handles)
        except Exception:
            return False

    def _navigate_sync(self, driver: WebDriver, url: str) -> str:
        """Focus a live window (the previous one may be closed), then navigate."""
        handles = driver.window_handles
        if not handles:
            driver.switch_to.new_window("tab")
        else:
            try:
                focused_ok = driver.current_window_handle in handles
            except Exception:
                focused_ok = False
            if not focused_ok:
                driver.switch_to.window(handles[-1])
        driver.get(url)
        return driver.current_url

    async def connect(self, start_url: str | None = None) -> dict[str, Any]:
        """Open (or reuse) the browser and navigate to the training entry point.

        Telemetry stays OFF here by design: the human completes auth and loads
        the training in the headed window with nothing observed.
        """
        async with self._lock:
            SETTINGS.ensure_dirs()
            # Discard a dead driver (browser was closed) before reusing it.
            if self._driver is not None and not await asyncio.to_thread(self._driver_alive, self._driver):
                logger.info("discarding_dead_driver")
                await self.shutdown()
            if self._driver is None:
                self._driver = await asyncio.to_thread(self._build_driver)
                logger.info("browser_connected", remote=bool(SETTINGS.webdriver_url))
            url = start_url or SETTINGS.start_url
            current_url = await asyncio.to_thread(self._navigate_sync, self._driver, url)
            return {
                "status": "ok",
                "url": current_url,
                "telemetry": self.capture.enabled,
                "instructions": (
                    "Browser opened on the server machine. Complete any login/MFA and load "
                    "the training material directly in that window. Telemetry is OFF — nothing "
                    "is being observed. Run `telemetry on` once you're past auth "
                    "and ready to start capturing."
                ),
            }

    def _require(self) -> WebDriver:
        if self._driver is None:
            raise NotConnected("No browser session. Run `connect` first.")
        return self._driver

    def _focus_live_window(self, driver: WebDriver) -> bool:
        """Ensure the driver is focused on a live window; return False if the
        browser is gone entirely.

        Selenium does not move focus when a tab closes, so after the human
        closes the focused tab every driver call throws until we switch to a
        surviving window. This makes that switch, and distinguishes "focused tab
        closed but others remain" (recoverable) from "browser closed" (dead).
        """
        try:
            handles = driver.window_handles
        except Exception:
            return False  # session/browser is dead
        if not handles:
            return False
        try:
            if driver.current_window_handle in handles:
                return True  # focused window still alive
        except Exception:
            pass  # focused window was closed; fall through and re-home
        try:
            driver.switch_to.window(handles[-1])
            return True
        except Exception:
            return False

    async def _ready(self) -> WebDriver:
        """Driver focused on a live window, or a clean NotConnected if it's gone.

        The single guard every observation/action runs first. If the browser was
        closed we drop the dead driver (so status is honest and the next
        `connect` rebuilds) and surface a 499, not a 500.
        """
        driver = self._require()
        if not await asyncio.to_thread(self._focus_live_window, driver):
            await self.shutdown()
            raise NotConnected("The browser was closed. Run `connect` to reopen it.")
        return driver

    async def status(self) -> dict[str, Any]:
        url: str | None = None
        title: str | None = None
        # Probe rather than trust the reference: the browser may have been closed
        # since it was created. _focus_live_window re-homes onto a surviving tab
        # if the focused one was closed, so status stays truthful either way.
        driver = self._driver
        connected = driver is not None and await asyncio.to_thread(self._focus_live_window, driver)
        if connected and driver is not None:  # second check narrows driver for the probe
            try:
                url, title = await asyncio.to_thread(lambda d=driver: (d.current_url, d.title))
            except Exception as exc:
                logger.warning("status_probe_failed", error=str(exc))
        return {
            "connected": connected,
            "telemetry": self.capture.enabled,
            "current_url": url,
            "title": title,
            "steps_recorded": self.capture.sequence,
            "network_capture": self.network.enabled,
            "network_buffered": len(self.network.peek()),
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

    def _list_windows_light(self, driver: WebDriver) -> list[dict[str, Any]]:
        """Enumerate windows WITHOUT changing focus (no visible tab-flip).

        `_list_windows_sync` switches into every window to read its url/title,
        which — in a headed browser — visibly raises each tab. Since the snapshot
        runs that on *every* observe (and each action snapshots twice, and a
        mission step re-observes up to ten times to verify), the operator sees the
        window rapidly flip through every open tab and back on each look. That's
        the flicker.

        Chrome exposes each window handle as `CDwindow-<targetId>`, so we map
        handles to CDP `Target.getTargets` infos for url/title instead of
        switching into them. Focus never moves. Falls back to reading only the
        focused window directly (still no switch) if CDP or the handle→target
        mapping isn't available (e.g. a non-Chrome Grid) — other tabs then show a
        null url/title, which is fine for this diagnostic-only field.
        """
        handles = driver.window_handles
        try:
            current = driver.current_window_handle
        except Exception:
            current = None
        targets_by_id: dict[str, dict[str, Any]] = {}
        try:
            infos = driver.execute_cdp_cmd("Target.getTargets", {}).get("targetInfos", [])
            targets_by_id = {t.get("targetId"): t for t in infos if t.get("type") == "page"}
        except Exception:
            targets_by_id = {}  # non-Chrome / CDP unavailable — degrade, don't switch
        out: list[dict[str, Any]] = []
        for i, handle in enumerate(handles):
            info = targets_by_id.get(handle.split("CDwindow-", 1)[-1])
            if info is not None:
                url, title = info.get("url"), info.get("title")
            elif handle == current:
                try:
                    url, title = driver.current_url, driver.title
                except Exception:
                    url, title = None, None
            else:
                url, title = None, None  # can't read without switching; leave blank
            out.append({"index": i, "url": url, "title": title, "active": handle == current})
        return out

    async def windows(self) -> dict[str, Any]:
        driver = await self._ready()
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
        driver = await self._ready()
        result = await asyncio.to_thread(self._switch_sync, driver, target)
        logger.info("window_switched", index=result["index"], url=result["url"])
        return {"status": "ok", **result}

    # --- telemetry gate ---

    async def set_telemetry(self, on: bool) -> dict[str, Any]:
        if on:
            self.capture.enable()
            # Discard whatever network traffic Chrome buffered while the gate was
            # closed (the manual auth) BEFORE we start retaining — so no SSO/SAML
            # request can carry over into the first recorded step.
            if self._driver is not None:
                await asyncio.to_thread(self._flush_network_backlog, self._driver)
            self.network.enable()
        else:
            self.capture.disable()
            self.network.disable()
        return {"status": "ok", "telemetry": self.capture.enabled}

    # --- network capture ---
    #
    # Selenium has no live network-event API (finmap gets that free from
    # Playwright), so we pull CDP Network.* events off Chrome's performance log
    # and fold them into the recorder. Reading the log drains it, which is
    # exactly the "events since the previous snapshot" model we want: each
    # snapshot pumps, then attaches the drained events to its capture.

    @staticmethod
    def _read_perf_messages(driver: WebDriver) -> list[dict[str, Any]]:
        """Drain Chrome's performance log into a list of raw CDP messages.

        Each perf-log entry wraps a CDP message as a JSON string; we unwrap to
        the `{"method": …, "params": …}` the recorder ingests. Best-effort: a
        driver without perf logging (some Grids) just yields nothing.
        """
        try:
            # get_log lives on Chromium drivers, not the base WebDriver type — the
            # try/except also covers a non-Chrome Grid that lacks it entirely.
            entries = driver.get_log("performance")  # ty: ignore[unresolved-attribute]
        except Exception:
            return []
        messages: list[dict[str, Any]] = []
        for entry in entries:
            try:
                messages.append(json.loads(entry["message"])["message"])
            except (KeyError, ValueError, TypeError):
                continue
        return messages

    def _flush_network_backlog(self, driver: WebDriver) -> None:
        """Read and discard the perf-log backlog (used when opening the gate)."""
        self._read_perf_messages(driver)

    def _pump_network_sync(self, driver: WebDriver) -> None:
        """Fold new CDP events into the recorder and fetch any JSON bodies.

        No-op-ish while telemetry is off: we still drain the perf log (so it
        can't overflow and so nothing stale survives), but the recorder ignores
        it and keeps its buffer empty.
        """
        messages = self._read_perf_messages(driver)
        if not self.network.enabled:
            return
        for request_id in self.network.ingest(messages):
            try:
                result = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
                self.network.attach_body(request_id, result.get("body"), result.get("base64Encoded", False))
            except Exception:
                # Body already evicted from Chrome's buffer, or CDP unavailable
                # (Grid). The metadata event still stands; just no body.
                self.network.attach_body(request_id, None)

    async def network_events(self, since: float | None = None) -> dict[str, Any]:
        """Pump and return the currently buffered events (non-destructive peek).

        Backs the `/network` debug endpoint — a way to see traffic without
        taking a full step. It does NOT drain, so a following observe/step still
        gets these events attached to its capture. `since` (seconds) keeps only
        events from the last N seconds — handy right after doing a thing to see
        just that thing's traffic (times are wall-clock, ws frames included).
        """
        import time

        stats = self.network.stats()
        if self._driver is not None and await asyncio.to_thread(self._focus_live_window, self._driver):
            await asyncio.to_thread(self._pump_network_sync, self._driver)
            stats = self.network.stats()
        events = self.network.peek()
        if since is not None:
            cutoff = time.time() - since
            events = [e for e in events if (e.get("ts") or 0) >= cutoff]
        return {**stats, "since": since, "shown": len(events), "events": events}

    # --- frames / iframes ---
    #
    # Selenium only sees the document it is currently switched into; a course
    # player renders its content inside (often nested) iframes, invisible until
    # you switch_to.frame. A "frame path" is a list of integer indices — each is
    # a position in that document's `iframe, frame` list. Collection and
    # re-entry both resolve indices through the same query, so the path observe
    # reports is exactly what click/type/wait-for take.

    FRAME_TAGS = "iframe, frame"
    MAX_FRAME_DEPTH = 5

    @staticmethod
    def _parse_frame(frame: Any) -> list[Any]:
        """Normalize a frame path: a list, or a '/'-joined string of index/CSS."""
        if frame is None:
            return []
        if isinstance(frame, list):
            return frame
        return [int(s) if s.lstrip("-").isdigit() else s for s in str(frame).split("/") if s]

    def _enter_frame_path(self, driver: WebDriver, frame: list[Any]) -> None:
        """Switch from the top document down into the nested frame path."""
        from selenium.webdriver.common.by import By

        driver.switch_to.default_content()
        for depth, seg in enumerate(frame):
            if isinstance(seg, int) or (isinstance(seg, str) and seg.lstrip("-").isdigit()):
                frames = driver.find_elements(By.CSS_SELECTOR, self.FRAME_TAGS)
                if not -len(frames) <= int(seg) < len(frames):
                    # The frame isn't there (yet) — e.g. a code-server pod still booting,
                    # so the nested iframe hasn't been inserted. Fail with a readable
                    # message instead of an IndexError 500, so a caller can retry/settle.
                    raise ActionFailed(
                        f"frame path {frame!r}: no frame at index {seg} (segment {depth}); "
                        f"only {len(frames)} frame(s) present here — is it still loading?"
                    )
                driver.switch_to.frame(frames[int(seg)])
            else:
                driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, seg))

    # --- observation ---

    def _collect_elements(
        self,
        driver: WebDriver,
        frame_path: list[int],
        elements: list[dict[str, Any]],
        frames: list[dict[str, Any]],
        depth: int,
    ) -> None:
        """Append this frame's interactive elements (tagged with frame_path),
        then recurse into child frames."""
        from selenium.webdriver.common.by import By

        if frame_path:
            entry: dict[str, Any] = {"frame": list(frame_path)}
            try:
                entry["url"] = driver.current_url
            except Exception:
                entry["url"] = None
            # Dump this frame's visible text so content rendered inside (nested)
            # iframes — e.g. the mission InstructionsPanel checklist — is
            # observable. The `dom` field is the top document's page_source only,
            # so without this a frame's step labels never reach the capture.
            try:
                body = driver.find_element(By.TAG_NAME, "body")
                entry["text"] = (body.text or "").strip()[: SETTINGS.frame_text_max]
            except Exception:
                entry["text"] = None
            frames.append(entry)

        for tag in ("button", "a", "input", "select", "textarea"):
            for el in driver.find_elements(By.TAG_NAME, tag):
                try:
                    el_type = el.get_attribute("type")
                    # Checkbox/radio inputs are often visually hidden (MUI et al.
                    # render an icon while the real checked state lives on the
                    # input). Skipping undisplayed elements would drop step-
                    # completion state, so keep toggles and record `checked`
                    # (via is_selected) plus the text of their enclosing row.
                    is_toggle = tag == "input" and el_type in ("checkbox", "radio")
                    if not is_toggle and not el.is_displayed():
                        continue
                    item: dict[str, Any] = {
                        "tag": tag,
                        "text": (el.text or "").strip()[:120],
                        "enabled": el.is_enabled(),
                        "type": el_type,
                        "id": el.get_attribute("id"),
                        "name": el.get_attribute("name"),
                        "aria_label": el.get_attribute("aria-label"),
                        "href": el.get_attribute("href") if tag == "a" else None,
                        "frame": list(frame_path),  # [] = top document
                    }
                    if is_toggle:
                        item["checked"] = el.is_selected()
                        try:
                            label = driver.execute_script(
                                "return (arguments[0].closest("
                                "'[role=heading],label,li,tr,div') || {}).innerText || null;",
                                el,
                            )
                            item["label"] = label.strip()[:120] if isinstance(label, str) else None
                        except Exception:
                            item["label"] = None
                    elements.append(item)
                except Exception:
                    continue  # element went stale mid-scan; keep going

        if depth >= self.MAX_FRAME_DEPTH:
            return
        child_count = len(driver.find_elements(By.CSS_SELECTOR, self.FRAME_TAGS))
        for idx in range(child_count):
            try:
                children = driver.find_elements(By.CSS_SELECTOR, self.FRAME_TAGS)
                driver.switch_to.frame(children[idx])
            except Exception:
                # Couldn't enter (cross-origin quirk, stale) — recover and skip.
                self._enter_frame_path(driver, frame_path)
                continue
            self._collect_elements(driver, [*frame_path, idx], elements, frames, depth + 1)
            driver.switch_to.parent_frame()

    def _snapshot_sync(self, driver: WebDriver) -> dict[str, Any]:
        """Capture page state — including elements inside (nested) iframes."""
        driver.switch_to.default_content()
        elements: list[dict[str, Any]] = []
        frames: list[dict[str, Any]] = []
        self._collect_elements(driver, [], elements, frames, depth=0)
        driver.switch_to.default_content()
        # Fold any flow-registered snapshot extensions into the capture — e.g. SCW's
        # code-editor challenge decision view (see webwalk/server/plugins.py). Each
        # is best-effort and keyed; a bare (flow-agnostic) server adds nothing here.
        from webwalk.server.plugins import collect_snapshot_extensions

        extra = collect_snapshot_extensions(driver)
        driver.switch_to.default_content()  # extensions may have changed frame focus
        # Pump the network recorder and hand this step the traffic since the last
        # snapshot. Empty while telemetry is off (the recorder ignores it). Frame
        # focus is irrelevant here — CDP Network events are per-target.
        self._pump_network_sync(driver)
        return {
            "url": driver.current_url,
            "title": driver.title,
            "elements": elements,
            "frames": frames,  # nested-frame map + each frame's visible text
            "dom": driver.page_source,  # top document only (frame text is in `frames`)
            "network": self.network.drain(),  # API traffic this step produced
            # Record the whole window set with each step, so a capture shows what
            # other tabs were open (and which one this snapshot is of). Uses the
            # focus-free enumeration — switching into each tab here would flicker
            # the headed browser on every observe (see _list_windows_light).
            "windows": self._list_windows_light(driver),
            **extra,  # flow-contributed snapshot keys (e.g. `challenge`), if any
        }

    async def snapshot(self) -> dict[str, Any]:
        driver = await self._ready()
        return await asyncio.to_thread(self._snapshot_sync, driver)

    async def observe(self, label: str = "observe", window: str | None = None) -> dict[str, Any]:
        """Record the current browser state as one step (if telemetry is on).

        If `window` is given, switch focus to it first (index/'latest'/substring).
        """
        if window is not None:
            await self.switch(window)
        state = await self.snapshot()
        path = self.capture.record(label, {"state": state})
        summary = _step_summary(path, self.capture.enabled, state)
        # The full payload rides back so the client can save it to ./data/ and
        # read it locally — the server-side file (path) is the gated copy.
        summary["record"] = {"label": label, "state": state}
        return summary

    # --- waiting (for scripted flows) ---

    def _wait_sync(
        self,
        driver: WebDriver,
        url_contains: str | None,
        selector: str | None,
        by: str,
        frame: list[Any],
        timeout: float,
        poll: float,
    ) -> dict[str, Any]:
        import time

        from selenium.webdriver.common.by import By

        locator = {"css": By.CSS_SELECTOR, "xpath": By.XPATH}.get(by, By.CSS_SELECTOR)
        deadline = time.monotonic() + timeout
        last_url: str | None = None
        while True:
            if not self._focus_live_window(driver):
                return {"matched": False, "error": "browser_closed", "url": last_url}
            try:
                driver.switch_to.default_content()
                last_url = driver.current_url  # url is always the top document's
                ok = True
                if url_contains and url_contains.lower() not in (last_url or "").lower():
                    ok = False
                if ok and selector:
                    self._enter_frame_path(driver, frame)  # look inside the target frame
                    ok = any(e.is_displayed() for e in driver.find_elements(locator, selector))
                    driver.switch_to.default_content()
                if ok:
                    return {"matched": True, "url": last_url}
            except Exception:
                pass  # page mid-navigation / frame not ready; keep polling
            if time.monotonic() >= deadline:
                return {"matched": False, "url": last_url}
            time.sleep(poll)

    async def wait_for(
        self,
        url: str | None = None,
        element: str | None = None,
        by: str = "css",
        timeout: float = 30.0,
        window: str | None = None,
        frame: Any = None,
    ) -> dict[str, Any]:
        """Block until the focused page's url contains `url` and/or `element` is
        visible (optionally inside `frame`), or until `timeout`."""
        if not url and not element:
            raise ValueError("wait-for needs --url and/or --element")
        driver = await self._ready()
        if window is not None:
            await asyncio.to_thread(self._switch_sync, driver, window)
        result = await asyncio.to_thread(
            self._wait_sync, driver, url, element, by, self._parse_frame(frame), timeout, 0.5
        )
        return {"status": "ok", **result}

    # --- action ---

    def _perform_sync(
        self,
        driver: WebDriver,
        action: str,
        selector: str | None,
        value: str | None,
        by: str = "css",
        frame: list[Any] | None = None,
        enter: bool = False,
    ) -> dict[str, Any]:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        if action == "navigate":
            if not value:
                raise ValueError("navigate requires a value (the URL)")
            driver.get(value)  # always loads the top document
            return {"action": "navigate", "url": value}

        if selector is None:
            raise ValueError(f"{action} requires a selector")
        if action not in ("click", "type", "paste"):
            raise ValueError(f"Unknown action: {action!r} (use click, type, paste, or navigate)")
        locator = {"css": By.CSS_SELECTOR, "xpath": By.XPATH}.get(by)
        if locator is None:
            raise ValueError(f"unknown selector type {by!r} (use css or xpath)")

        # Descend into the target frame (if any), act, then always return to top.
        # Any Selenium/lookup failure (no such element, not interactable, bad
        # frame index, …) becomes a readable ActionFailed rather than a 500.
        frame = frame or []
        try:
            # Set the clipboard from the TOP document, before descending into the
            # (possibly cross-origin) target frame: `execCommand('copy')` is denied in
            # a cross-origin iframe, but the OS clipboard is global, so a copy in the
            # same-origin, focused top document reaches the editor's paste all the same.
            if action == "paste" and not self._set_clipboard(driver, value or ""):
                raise ActionFailed("paste: could not write the text to the clipboard (execCommand copy failed)")
            self._enter_frame_path(driver, frame)
            el = driver.find_element(locator, selector)
            js_click = False
            if action == "click":
                from selenium.common.exceptions import (
                    ElementClickInterceptedException,
                    ElementNotInteractableException,
                )

                try:
                    el.click()
                except (ElementNotInteractableException, ElementClickInterceptedException):
                    # The element is in the DOM but not natively clickable — e.g. a
                    # side-overlay / hover-menu link (the app's Logout) that Selenium
                    # can't hit-test. Dispatch a DOM-level click on it directly; the
                    # app's own handlers respond to that just the same.
                    driver.execute_script("arguments[0].click();", el)
                    js_click = True
            elif action == "paste":
                # Replace the focused editor/field's content with `value` via the OS
                # clipboard. This is the only reliable way to put multi-line code into
                # a cross-origin code-server (Monaco) editor: synthetic (untrusted)
                # keyboard/paste events are ignored, and typing code char-by-char
                # fights Monaco's auto-indent / auto-closing brackets. A real clipboard
                # paste, by contrast, is inserted verbatim. The clipboard was already
                # set from the top document above; here we only focus + fire the chords.
                self._paste_sync(driver, el)
            else:  # type
                el.clear()
                el.send_keys(value or "")
                if enter:
                    # Submit — the mocked apps' simulated address bar / forms have no
                    # click-target, so an explicit Enter is how they navigate.
                    el.send_keys(Keys.ENTER)
            return {
                "action": action,
                "by": by,
                "selector": selector,
                "frame": frame,
                "enter": enter,
                "js_click": js_click,
            }
        except Exception as exc:
            raise ActionFailed(self._describe_action_error(exc, action, by, selector, frame))
        finally:
            driver.switch_to.default_content()

    @staticmethod
    def _set_clipboard(driver: WebDriver, text: str) -> bool:
        """Put `text` on the OS clipboard. Returns whether it succeeded.

        Prefers the OS clipboard tool (`pbcopy` on macOS, `xclip`/`xsel` on Linux):
        it's independent of browser focus, whereas `execCommand('copy')` requires the
        document to be focused — which a headed-but-backgrounded browser is not, so it
        fails there. The browser copy is kept only as a last-ditch fallback. The paste
        chord (Cmd/Ctrl+V) itself is delivered via ChromeDriver input and does NOT need
        OS focus, so an OS-set clipboard + a driver-sent paste works fully headless.
        """
        import shutil
        import subprocess
        import sys

        tools = (
            [["pbcopy"]]
            if sys.platform == "darwin"
            else [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
        )
        for tool in tools:
            if shutil.which(tool[0]):
                try:
                    subprocess.run(tool, input=text.encode("utf-8"), check=True, timeout=5)
                    return True
                except (subprocess.SubprocessError, OSError):
                    continue
        # Fallback: browser-side copy (works only if the window happens to be focused).
        return bool(
            driver.execute_script(
                """
                const t = document.createElement('textarea');
                t.style.position = 'fixed'; t.style.top = '0'; t.style.opacity = '0';
                t.value = arguments[0];
                document.body.appendChild(t);
                t.focus(); t.select();
                let ok = false;
                try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
                t.remove();
                return ok;
                """,
                text,
            )
        )

    @staticmethod
    def _paste_sync(driver: WebDriver, el: Any) -> None:
        """Focus `el`, select-all, and paste the (already-set) clipboard over it.

        The modifier key is chosen by the *browser host's* platform, because that's
        what the in-browser editor's keybindings follow: VS Code web binds select-all
        to Cmd+A on macOS (Ctrl+A there is "cursor to line start"), Ctrl+A elsewhere.
        We drive the chords with ActionChains (key_down/up) so they're trusted OS-level
        input — the whole point, since Monaco ignores untrusted synthetic events. `el`
        is typically the `.monaco-editor` / `.view-lines` container; clicking it moves
        focus into the editor so the paste lands there.
        """
        import sys

        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        el.click()  # move focus into the editor (the clipboard write stole it)
        mod = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
        ac = ActionChains(driver)
        ac.key_down(mod).send_keys("a").key_up(mod)  # select all → replace on paste
        ac.key_down(mod).send_keys("v").key_up(mod)
        ac.perform()

    @staticmethod
    def _describe_action_error(exc: Exception, action: str, by: str, selector: str | None, frame: list[Any]) -> str:
        reason = (getattr(exc, "msg", None) or str(exc) or "").strip()
        first_line = reason.splitlines()[0] if reason else type(exc).__name__
        where = f" in frame {frame}" if frame else ""
        return f"could not {action} {by}={selector!r}{where}: {type(exc).__name__}: {first_line}"

    # --- in-page fetch (the UI→API ratchet) ---
    #
    # Fire a request from *inside* the logged-in page (driver.execute_async_script
    # → window.fetch with credentials), so it carries the page's real session
    # against the training's own API. This is how a rule can hit the API directly
    # instead of puppeting the UI — mirroring finmap's `session.fetch_json`. The
    # request shapes come from the network recorder (docs/network-capture-design).
    #
    # Auth note: `credentials: 'include'` carries cookies, but SCW's portal-api
    # authenticates with a **bearer token** the SPA keeps in web storage (cookies
    # alone give 401). With `page_auth=True` the page-side script finds that token
    # in local/sessionStorage (any value containing a JWT) and attaches it as
    # `Authorization: Bearer …` — so the token is used *in the browser* and never
    # transits to the client. Explicit `headers['Authorization']` wins over it.

    async def _throttle_fetch(self) -> None:
        """Serialize in-page fetches and space them out (interval + jitter)."""
        import random
        import time

        async with self._fetch_lock:
            wait = (self._last_fetch_ts + SETTINGS.fetch_min_interval_s) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if SETTINGS.fetch_jitter_s > 0:
                await asyncio.sleep(random.uniform(0, SETTINGS.fetch_jitter_s))
            self._last_fetch_ts = time.monotonic()

    # The page-side fetch. arguments[0..3] are url/method/headers/body; Selenium
    # appends the async callback as the final argument. We never throw for a
    # non-2xx — the caller inspects `status`/`ok` — only for a thrown fetch (e.g.
    # network/CORS), which comes back as {ok:false, status:0, error}.
    _FETCH_JS = """
        const url = arguments[0], method = arguments[1];
        const headers = Object.assign({}, arguments[2] || {});
        const body = arguments[3], pageAuth = arguments[4];
        const done = arguments[arguments.length - 1];
        // Find a JWT anywhere in web storage (values may be JSON-wrapped), so we
        // can reuse the SPA's own bearer token without knowing its storage key.
        function findBearer() {
            const rx = /eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+/;
            for (const store of [window.localStorage, window.sessionStorage]) {
                if (!store) continue;
                for (let i = 0; i < store.length; i++) {
                    const v = store.getItem(store.key(i));
                    if (typeof v === 'string') { const m = v.match(rx); if (m) return m[0]; }
                }
            }
            return null;
        }
        let authAttached = false;
        if (pageAuth && !Object.keys(headers).some(k => k.toLowerCase() === 'authorization')) {
            const t = findBearer();
            if (t) { headers['Authorization'] = 'Bearer ' + t; authAttached = true; }
        }
        (async () => {
            try {
                const opts = { method, headers, credentials: 'include' };
                if (body !== null && body !== undefined) opts.body = body;
                const r = await fetch(url, opts);
                const text = await r.text();
                done({ ok: r.ok, status: r.status, body: text, authAttached });
            } catch (e) {
                done({ ok: false, status: 0, error: String(e), authAttached });
            }
        })();
    """

    def _fetch_in_page_sync(
        self,
        driver: WebDriver,
        url: str,
        method: str,
        headers: dict[str, str],
        body: str | None,
        timeout: float,
        page_auth: bool,
        frame: list[Any],
    ) -> dict[str, Any]:
        # Fire from inside the target frame's document, so the request carries
        # THAT origin's session and `page_auth` scans THAT frame's web storage.
        # Default (no frame) is the top document — same-origin with portal-api.
        # A cross-origin API (e.g. the experiential-mission player, whose bearer
        # lives in its own iframe's sessionStorage) needs `frame` to be reached.
        self._enter_frame_path(driver, frame)
        try:
            driver.set_script_timeout(timeout)
            result = driver.execute_async_script(self._FETCH_JS, url, method, headers, body, page_auth) or {}
        finally:
            driver.switch_to.default_content()
        raw = result.get("body")
        parsed: Any = raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = raw  # non-JSON body; hand back the text
        return {
            "url": url,
            "method": method,
            "status": result.get("status"),
            "ok": result.get("ok", False),
            "error": result.get("error"),
            "auth_attached": result.get("authAttached", False),  # did page_auth find+attach a token?
            "body": parsed,
        }

    async def fetch_in_page(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout: float = 30.0,
        page_auth: bool = False,
        frame: Any = None,
    ) -> dict[str, Any]:
        """Issue `method url` from inside the page and return {status, ok, body}.

        `body` may be a string (sent as-is) or JSON-able (serialized, with a
        JSON content-type unless one is given). With `page_auth`, attach the
        SPA's own bearer token from web storage (see `_FETCH_JS`). `frame` (a
        path like `0/0`) fires the request from inside that nested iframe — so a
        cross-origin API is hit from its own origin, with that frame's token.
        Rate limited. The call also shows up in the network recorder, so
        telemetry corroborates what went out.
        """
        driver = await self._ready()
        hdrs = {k: v for k, v in (headers or {}).items()}
        if body is not None and not isinstance(body, str):
            body = json.dumps(body)
        if body is not None and not any(k.lower() == "content-type" for k in hdrs):
            hdrs["content-type"] = "application/json"
        await self._throttle_fetch()
        result = await asyncio.to_thread(
            self._fetch_in_page_sync, driver, url, method.upper(), hdrs, body, timeout, page_auth,
            self._parse_frame(frame),
        )
        logger.info(
            "fetch_in_page", method=method.upper(), url=url,
            status=result.get("status"), ok=result.get("ok"), auth=result.get("auth_attached"),
        )
        return result

    # --- in-page JS eval (discovery/introspection) ---
    #
    # Run arbitrary JS in the top document and return its (JSON-able) result — the
    # escape hatch for learning things `observe` doesn't surface: web-storage
    # contents, app state, or (by installing a header hook) how the SPA
    # authenticates. A raw `fetch` can't see an in-memory bearer token; eval can
    # hook XMLHttpRequest to capture it from the app's own live requests.

    def _eval_sync(self, driver: WebDriver, script: str, args: list[Any], frame: list[Any]) -> Any:
        # Descend into the target frame (if any) so the script runs in its
        # document, then always return to top. Without a frame this is the top
        # document (the SPA origin).
        from selenium.common.exceptions import JavascriptException

        self._enter_frame_path(driver, frame)
        try:
            # execute_script only returns a value when the body uses `return`, so a
            # caller passing a bare expression (`document.title`) gets nothing back
            # unless we wrap it as `return (...)`. But wrapping is only valid for an
            # expression: statements without a return (`var x = ...; foo(x)`) become
            # `return (var x = ...)` — a parse error ("Unexpected token 'var'").
            #
            # We can't reliably tell the two apart by inspection (an expression may
            # contain the substring "return", statements may omit it), so probe:
            # try the expression wrap first, and if that fails to *parse*, run the
            # script raw. Syntax errors are raised at parse time, before any code
            # runs, so the fallback re-run causes no double side effects. A body
            # with an explicit top-level `return` skips the probe — it's already in
            # the form execute_script wants, and probing would waste a round trip.
            if _has_toplevel_return(script):
                return driver.execute_script(script, *args)
            try:
                return driver.execute_script(f"return ({script});", *args)
            except JavascriptException as exc:
                if not _is_syntax_error(exc):
                    raise
                return driver.execute_script(script, *args)
        finally:
            driver.switch_to.default_content()

    async def eval_js(
        self, script: str, args: list[Any] | None = None, frame: Any = None
    ) -> dict[str, Any]:
        """Evaluate `script` in the page; returns {"result": <json-able value>}.

        `frame` (a path like `0` or `0/1`) runs the script inside that nested
        iframe — the escape hatch for reaching content the top document can't,
        reusing the same frame vocabulary as `step`/`click`/`observe`.
        """
        driver = await self._ready()
        result = await asyncio.to_thread(self._eval_sync, driver, script, args or [], self._parse_frame(frame))
        return {"result": result}

    # --- flow extraction hook ---
    #
    # The generic escape hatch a flow endpoint uses to read the live page with a
    # custom scraper — e.g. SCW's /challenge, which runs the code-editor scraper on
    # demand (see webwalk/flows/scw/observers.py). The core session stays flow-
    # agnostic: it just runs the supplied function against the driver off-thread.

    async def extract(self, extractor: Callable[[WebDriver], Any]) -> Any:
        """Run a flow-supplied `extractor(driver)` against the live page and return
        its result. Selenium is synchronous, so it runs in a worker thread."""
        driver = await self._ready()
        return await asyncio.to_thread(extractor, driver)

    async def act(
        self,
        action: str,
        selector: str | None = None,
        value: str | None = None,
        label: str | None = None,
        window: str | None = None,
        by: str = "css",
        secret: bool = False,
        frame: Any = None,
        settle: float = 0.0,
        wait_after: str | None = None,
        wait_after_timeout: float = 10.0,
        enter: bool = False,
    ) -> dict[str, Any]:
        """Inject an action, capturing before/after state as one step.

        The before/after pair is exactly the raw material for authoring a rule:
        "in this state I did X, and it moved me to that state." If `window` is
        given, switch focus to it before acting. `by` selects css (default) or
        xpath, so a button can be targeted by visible text when it has no id.
        `frame` targets an element inside a (nested) iframe. When `secret`, the
        typed value is redacted everywhere it is recorded (server capture + the
        payload returned to the client) — for passwords.

        SPA pages render asynchronously, so an immediate after-snapshot can catch
        a half-built page. Let the result settle first with either `wait_after`
        (poll until this selector — in the action's `by`/`frame` — is visible,
        up to `wait_after_timeout`) or a fixed `settle` delay in seconds.
        """
        driver = await self._ready()
        if window is not None:
            await asyncio.to_thread(self._switch_sync, driver, window)
        parsed_frame = self._parse_frame(frame)
        before = await self.snapshot()
        result = await asyncio.to_thread(
            self._perform_sync, driver, action, selector, value, by, parsed_frame, enter
        )
        settled: dict[str, Any] | None = None
        if wait_after:
            settled = await asyncio.to_thread(
                self._wait_sync, driver, None, wait_after, by, parsed_frame, wait_after_timeout, 0.3
            )
        elif settle > 0:
            await asyncio.sleep(settle)
        after = await self.snapshot()
        step_label = label or f"{action}-{selector or ''}"
        record = {
            "label": step_label,
            "action": {
                "action": action,
                "by": by,
                "selector": selector,
                "value": "<redacted>" if secret else value,
                "settle": settle,
                "wait_after": wait_after,
                "settled": settled,  # {matched, url} if wait_after was used
            },
            "result": result,
            "before": before,
            "after": after,
        }
        path = self.capture.record(step_label, record)
        summary = _step_summary(path, self.capture.enabled, after)
        summary["performed"] = result
        summary["record"] = record
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
        "network_count": len(state.get("network") or []),
    }


# Module-level singleton — the server process's one live browser session.
SESSION = BrowserSession()
