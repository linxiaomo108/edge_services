from __future__ import annotations

import os

import uvicorn

from .app import build_app


def main() -> None:
    host = os.getenv("EDGE_MONITOR_BIND_HOST") or "0.0.0.0"
    port = int(os.getenv("EDGE_MONITOR_BIND_PORT") or "18081")
    app = build_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

