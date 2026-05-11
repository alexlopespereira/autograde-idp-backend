from __future__ import annotations

import logging.config
from importlib import metadata

from fastapi import FastAPI

from app.auth import AuthMiddleware
from app.endpoints import router as endpoints_router
from app.logging_config import LOG_CONFIG

logging.config.dictConfig(LOG_CONFIG)


def _get_version() -> str:
    try:
        return metadata.version("autograde-backend")
    except metadata.PackageNotFoundError:
        return "0.0.0"


VERSION = _get_version()

app = FastAPI(title="autograde-backend", version=VERSION)
app.add_middleware(AuthMiddleware)
app.include_router(endpoints_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}
