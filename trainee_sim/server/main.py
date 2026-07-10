"""trainee-sim FastAPI server — owns the live Selenium session in memory.

Runs on the target machine (the one with the browser + training access). The
stateless CLI drives it over HTTP. Endpoints fall in three groups:
  - session lifecycle (connect / status / shutdown)
  - the telemetry gate (on / off)
  - observe / step (capture state, inject actions)
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from trainee_sim.server.config import SETTINGS
from trainee_sim.server.session import SESSION, NotConnected

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    from trainee_sim.server.logging import setup_logging

    setup_logging()
    SETTINGS.ensure_dirs()
    logger.info("server_starting", data_dir=str(SETTINGS.data_dir), remote=bool(SETTINGS.webdriver_url))
    yield
    logger.info("server_stopping")
    await SESSION.shutdown()


app = FastAPI(
    title="trainee-sim-server",
    description="Drives a Selenium browser through training material via a live, server-resident session.",
    lifespan=lifespan,
)


@app.exception_handler(NotConnected)
async def _not_connected(request: Request, exc: NotConnected) -> JSONResponse:
    # 499 "action required": the CLI turns this into a "run connect first" hint.
    return JSONResponse(
        status_code=499,
        content={
            "error": "not_connected",
            "message": str(exc),
            "instructions": "Run `trainee-sim connect` to open the browser first.",
        },
    )


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException) -> JSONResponse:
    content = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=content)


# --- session lifecycle ---


class ConnectBody(BaseModel):
    start_url: str | None = None


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/session/status")
async def session_status() -> dict[str, Any]:
    return await SESSION.status()


@app.post("/session/connect")
async def session_connect(body: ConnectBody | None = None) -> dict[str, Any]:
    return await SESSION.connect(start_url=body.start_url if body else None)


@app.post("/session/shutdown")
async def session_shutdown() -> dict[str, Any]:
    await SESSION.shutdown()
    return {"status": "ok"}


# --- telemetry gate ---


@app.post("/telemetry/{state}")
async def telemetry(state: str) -> dict[str, Any]:
    if state not in ("on", "off"):
        raise HTTPException(status_code=400, detail={"error": f"state must be 'on' or 'off', got {state!r}"})
    return await SESSION.set_telemetry(state == "on")


# --- observe / step ---


class ObserveBody(BaseModel):
    label: str = "observe"


class StepBody(BaseModel):
    action: str  # click | type | navigate
    selector: str | None = None
    value: str | None = None
    label: str | None = None


@app.post("/observe")
async def observe(body: ObserveBody | None = None) -> dict[str, Any]:
    return await SESSION.observe(label=body.label if body else "observe")


@app.post("/step")
async def step(body: StepBody) -> dict[str, Any]:
    try:
        return await SESSION.act(
            action=body.action, selector=body.selector, value=body.value, label=body.label
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
