"""Server-side flow plugins — how a flow extends the generic browser session.

The core server (session + endpoints) knows nothing about any particular
training. A *flow* can still teach it two things at process start:

  * **snapshot extensions** — an extractor `(driver) -> dict | None` whose result
    is folded into every `observe` snapshot under a key. SCW uses this to fold a
    code-editor challenge's decision view into the snapshot (see
    `webwalk/flows/scw/observers.py`); a flow with no such need registers nothing
    and the snapshot carries only the generic keys.
  * **routes** — extra FastAPI endpoints on `flow_router`, included on the app
    after plugins load. SCW adds `/challenge`; another flow adds its own or none.

Which plugins load is chosen by the `WEBWALK_SERVER_PLUGINS` env var (a
comma-separated list of import paths), read once at app import time so it works
under both `serve` and `--reload`. Each flow's CLI sets it before launching the
server (see the core CLI's plugin-aware `serve`).
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver

logger = structlog.get_logger()

# key -> extractor(driver) -> dict | None. Every non-None result is merged into
# the observe snapshot under its key. Registered by flow plugins at import time.
SNAPSHOT_EXTENSIONS: dict[str, Callable[[WebDriver], dict[str, Any] | None]] = {}

# Extra endpoints a flow contributes (e.g. SCW's /challenge). The app includes
# this router after plugins have loaded and registered onto it.
flow_router = APIRouter()


def register_snapshot_extension(key: str, extractor: Callable[[WebDriver], dict[str, Any] | None]) -> None:
    """Fold `extractor(driver)`'s result into every observe snapshot under `key`."""
    SNAPSHOT_EXTENSIONS[key] = extractor


def collect_snapshot_extensions(driver: WebDriver) -> dict[str, Any]:
    """Run every registered snapshot extension, returning `{key: value}` for those
    that produced a non-None result. Best-effort: a failing extension is logged and
    skipped so it never blocks a normal observe."""
    extra: dict[str, Any] = {}
    for key, extractor in SNAPSHOT_EXTENSIONS.items():
        try:
            value = extractor(driver)
        except Exception as exc:
            logger.debug("snapshot_extension_failed", key=key, error=str(exc))
            continue
        if value is not None:
            extra[key] = value
    return extra


def load_server_plugins() -> list[str]:
    """Import the flow plugin modules named in `WEBWALK_SERVER_PLUGINS` (comma-
    separated), letting each self-register its snapshot extensions and routes.
    Returns the module paths loaded (empty for a bare, flow-agnostic server)."""
    raw = os.environ.get("WEBWALK_SERVER_PLUGINS", "")
    mods = [m.strip() for m in raw.split(",") if m.strip()]
    for mod in mods:
        importlib.import_module(mod)
        logger.info("server_plugin_loaded", module=mod)
    return mods
