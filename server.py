#!/usr/bin/env python3
"""Production-oriented server entrypoint."""

import os

import uvicorn


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    workers = int(os.environ.get("WORKERS", "1"))
    reload = _as_bool(os.environ.get("RELOAD"), default=False)
    if reload and workers > 1:
        workers = 1
    log_level = os.environ.get("LOG_LEVEL", "info")
    uvicorn.run("app:app", host=host, port=port, workers=workers, reload=reload, log_level=log_level)


if __name__ == "__main__":
    main()
