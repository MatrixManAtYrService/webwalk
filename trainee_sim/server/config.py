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


@dataclass
class Settings:
    data_dir: Path = field(default_factory=_data_dir)

    # Selenium endpoint. If set (e.g. http://localhost:4444 for a standalone
    # -chrome / Grid on the target machine), we connect with webdriver.Remote.
    # If empty, we launch a local Chrome via Selenium Manager on the server.
    webdriver_url: str = os.environ.get("TS_WEBDRIVER_URL", "")

    # Where the browser starts — the training entry point / login page. The
    # human drives auth from here before telemetry is turned on.
    start_url: str = os.environ.get("TS_START_URL", "about:blank")

    # Auth must be headed (a human logs in), so the browser is headed by
    # default. Set TS_HEADLESS=1 only for unattended replay of captured flows.
    headless: bool = os.environ.get("TS_HEADLESS", "").lower() in ("1", "true", "yes")

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.captures_dir.mkdir(parents=True, exist_ok=True)


SETTINGS = Settings()
