"""Server configuration for trainee-sim.

The server runs on the *target* machine (the one with the Selenium browser and
access to the training material). The client runs elsewhere on the LAN and
reaches the server via TS_SERVER_URL. Everything the browser touches is
configured here, but deliberately kept minimal — the interesting behaviour
(what to observe, which actions to take) is discovered by driving the browser,
not hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _data_dir() -> Path:
    base = os.environ.get("TS_DATA_DIR")
    if base:
        return Path(base).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "trainee-sim"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


def _network_hosts() -> tuple[str, ...]:
    """Comma-separated substrings the request host must contain to be recorded.

    Empty (the default) means "no host filter" — resource-type filtering
    (xhr/fetch/websocket only) already trims most of the CDN/telemetry noise.
    """
    raw = os.environ.get("TS_NETWORK_HOSTS", "")
    return tuple(h.strip() for h in raw.split(",") if h.strip())


@dataclass
class Settings:
    data_dir: Path = field(default_factory=_data_dir)

    # Selenium endpoint. If set (e.g. http://localhost:4444 for a standalone
    # -chrome / Grid on the target machine), we connect with webdriver.Remote.
    # If empty, we launch a local Chrome via Selenium Manager on the server.
    webdriver_url: str = os.environ.get("TS_WEBDRIVER_URL", "")

    # Path to the Chromium-family binary to drive (Chrome, Chromium, Brave,
    # Edge, …). Empty = let Selenium find the default Chrome. Any of these speak
    # CDP, so network capture works the same across them.
    browser_binary: str = os.environ.get("TS_BROWSER_BINARY", "")

    # Where the browser starts — the training entry point / login page. The
    # human drives auth from here before telemetry is turned on.
    start_url: str = os.environ.get("TS_START_URL", "about:blank")

    # Auth must be headed (a human logs in), so the browser is headed by
    # default. Set TS_HEADLESS=1 only for unattended replay of captured flows.
    headless: bool = os.environ.get("TS_HEADLESS", "").lower() in ("1", "true", "yes")

    # --- network capture (see docs/network-capture-design.md) ---
    #
    # Records the training's API traffic alongside each step, so rules can
    # eventually fire requests directly instead of puppeting the UI. Enabling it
    # turns on Chrome's performance log at browser-build time; the telemetry gate
    # still decides whether anything is *retained* (auth traffic is never kept).
    network_capture: bool = field(default_factory=lambda: _env_flag("TS_NETWORK_CAPTURE", True))
    # Only record requests whose host contains one of these (empty = no filter).
    network_hosts: tuple[str, ...] = field(default_factory=_network_hosts)
    # Ring-buffer cap: newest events win so a long session stays bounded.
    network_max_events: int = field(default_factory=lambda: int(os.environ.get("TS_NETWORK_MAX_EVENTS", "500")))
    # Per-body size cap (bytes) — bodies larger than this are truncated.
    network_max_body_bytes: int = field(default_factory=lambda: int(os.environ.get("TS_NETWORK_MAX_BODY", "65536")))

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    @property
    def browser_profile_dir(self) -> Path:
        """Persistent Chrome profile, so the login/session survives restarts."""
        override = os.environ.get("TS_BROWSER_PROFILE")
        return Path(override).expanduser() if override else self.data_dir / "browser-profile"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)


SETTINGS = Settings()
