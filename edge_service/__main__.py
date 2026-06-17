from __future__ import annotations

import uvicorn

from .config import load_config
from .logging_setup import configure_logging, shutdown_logging
from .web import build_app


def main() -> None:
    cfg = load_config()
    configure_logging(cfg)
    app = build_app(cfg)
    try:
        uvicorn.run(app, host=cfg.bind_host, port=cfg.bind_port, log_level="warning")
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
