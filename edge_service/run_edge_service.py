#!/usr/bin/env python3
"""
EdgeService 独立入口点
用于PyInstaller打包
"""

import sys
import os

# 确保当前目录在路径中
if getattr(sys, 'frozen', False):
    # PyInstaller打包后的路径
    base_path = sys._MEIPASS
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, base_path)

import uvicorn
from edge_service.config import load_config
from edge_service.logging_setup import configure_logging, shutdown_logging
from edge_service.web import build_app


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
