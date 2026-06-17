from __future__ import annotations

import atexit
import logging
import queue
import sys
from dataclasses import dataclass
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from pathlib import Path
from threading import Lock

from .config import EdgeConfig

_LOG_LOCK = Lock()
_LOG_QUEUE: queue.Queue | None = None
_LOG_LISTENER: QueueListener | None = None
_LOGGING_READY = False


@dataclass(frozen=True)
class LoggingConfig:
    base_log_dir: Path
    runtime_retention_days: int
    error_retention_days: int


class _MinLevelFilter(logging.Filter):
    def __init__(self, min_level: int) -> None:
        super().__init__()
        self._min_level = int(min_level)

    def filter(self, record: logging.LogRecord) -> bool:
        return int(record.levelno) >= self._min_level


def _default_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parent


def _resolve_logging_config(cfg: EdgeConfig | None) -> LoggingConfig:
    base_dir = _default_base_dir()
    configured_dir = Path(str(cfg.log_dir).strip()) if cfg is not None and str(cfg.log_dir).strip() else Path("output/logs")
    base_log_dir = configured_dir if configured_dir.is_absolute() else (base_dir / configured_dir)
    runtime_retention_days = max(1, int(cfg.runtime_log_retention_days)) if cfg is not None else 30
    error_retention_days = max(1, int(cfg.error_log_retention_days)) if cfg is not None else 30
    return LoggingConfig(
        base_log_dir=base_log_dir,
        runtime_retention_days=runtime_retention_days,
        error_retention_days=error_retention_days,
    )


def _build_rotating_handler(path: Path, *, level: int, retention_days: int) -> TimedRotatingFileHandler:
    handler = TimedRotatingFileHandler(
        str(path),
        when="midnight",
        interval=1,
        backupCount=max(1, int(retention_days)),
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(int(level))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    return handler


def configure_logging(cfg: EdgeConfig | None = None) -> None:
    global _LOGGING_READY, _LOG_QUEUE, _LOG_LISTENER
    with _LOG_LOCK:
        if _LOGGING_READY:
            return
        log_cfg = _resolve_logging_config(cfg)
        logs_dir = log_cfg.base_log_dir
        runtime_dir = logs_dir / "runtime"
        error_dir = logs_dir / "error"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        error_dir.mkdir(parents=True, exist_ok=True)
        runtime_handler = _build_rotating_handler(runtime_dir / "edge-service.log", level=logging.INFO, retention_days=log_cfg.runtime_retention_days)
        error_handler = _build_rotating_handler(error_dir / "edge-error.log", level=logging.WARNING, retention_days=log_cfg.error_retention_days)
        error_handler.addFilter(_MinLevelFilter(logging.WARNING))
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        log_queue = queue.Queue()
        listener = QueueListener(log_queue, runtime_handler, error_handler, console_handler, respect_handler_level=True)
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        queue_handler = QueueHandler(log_queue)
        queue_handler.setLevel(logging.INFO)
        root.addHandler(queue_handler)
        root.setLevel(logging.INFO)
        logging.getLogger("edge.runner").setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.error").setLevel(logging.INFO)
        listener.start()
        _LOG_QUEUE = log_queue
        _LOG_LISTENER = listener
        _LOGGING_READY = True
        atexit.register(shutdown_logging)


def shutdown_logging() -> None:
    global _LOGGING_READY, _LOG_QUEUE, _LOG_LISTENER
    with _LOG_LOCK:
        listener = _LOG_LISTENER
        _LOG_LISTENER = None
        _LOG_QUEUE = None
        _LOGGING_READY = False
    if listener is not None:
        try:
            listener.stop()
        except Exception:
            pass
