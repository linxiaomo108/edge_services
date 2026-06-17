from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _service_version_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_path = str(os.getenv("EDGE_SERVICE_VERSION_FILE") or "").strip()
    if env_path:
        candidates.append(Path(env_path))
    edge_root = str(os.getenv("EDGE_ROOT") or "").strip()
    if edge_root:
        root_path = Path(edge_root)
        candidates.extend([
            root_path / "version.json",
            root_path / "core" / "service_version.json",
            root_path / "service_version.json",
        ])
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            exe_dir / "service_version.json",
            exe_dir.parent / "version.json",
            exe_dir.parent / "service_version.json",
        ])
    module_dir = Path(__file__).resolve().parent
    candidates.extend([
        module_dir / "service_version.json",
        module_dir.parent / "version.json",
    ])
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def load_service_version_info() -> dict:
    env_version = (os.getenv("EDGE_VERSION") or "1.0.0").strip() or "1.0.0"
    info: dict[str, object] = {
        "version": env_version,
        "buildTime": "",
        "gitCommit": "",
        "gitBranch": "",
        "dirty": False,
        "packageMode": "",
        "packageName": "",
        "versionFile": "",
    }
    for cfg_path in _service_version_candidates():
        try:
            if not cfg_path.exists():
                continue
            data = json.loads(cfg_path.read_text(encoding="utf-8", errors="ignore") or "{}") or {}
            if not isinstance(data, dict):
                continue
            version = str(data.get("version") or data.get("clientVersion") or "").strip()
            if version:
                info["version"] = version
            build_time = str(data.get("buildTime") or data.get("build_time") or "").strip()
            if build_time:
                info["buildTime"] = build_time
            git_commit = str(data.get("gitCommit") or data.get("git_commit") or "").strip()
            if git_commit:
                info["gitCommit"] = git_commit
            git_branch = str(data.get("gitBranch") or data.get("git_branch") or "").strip()
            if git_branch:
                info["gitBranch"] = git_branch
            if "dirty" in data:
                info["dirty"] = bool(data.get("dirty"))
            package = data.get("package") if isinstance(data.get("package"), dict) else {}
            package_mode = str(data.get("packageMode") or package.get("mode") or "").strip()
            if package_mode:
                info["packageMode"] = package_mode
            package_name = str(data.get("packageName") or package.get("name") or "").strip()
            if package_name:
                info["packageName"] = package_name
            info["versionFile"] = str(cfg_path)
            info["raw"] = copy.deepcopy(data)
            return info
        except Exception:
            continue
    return info


def _load_service_version() -> str:
    return str(load_service_version_info().get("version") or "1.0.0").strip() or "1.0.0"


def _load_client_config_json() -> dict:
    if getattr(sys, "frozen", False):
        path = Path(sys.executable).resolve().parent.parent / "config.json"
    else:
        path = Path(__file__).resolve().parent.parent / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "{}") or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class EdgeConfig:
    server_base_url: str
    task_list_path: str
    task_report_path: str
    edge_id: str
    version: str
    version_info: dict
    poll_interval_sec: int
    report_interval_sec: int
    bind_host: str
    bind_port: int
    simulate: bool
    log_dir: str
    runtime_log_retention_days: int
    error_log_retention_days: int
    db_log_retention_days: int


def load_config() -> EdgeConfig:
    client_cfg = _load_client_config_json()
    server_cfg = client_cfg.get("server") if isinstance(client_cfg.get("server"), dict) else {}
    local_cfg = client_cfg.get("local") if isinstance(client_cfg.get("local"), dict) else {}
    server_base_url = (os.getenv("EDGE_SERVER_BASE_URL") or str(server_cfg.get("address") or "http://localhost:8080")).rstrip("/")
    task_list_path = os.getenv("EDGE_TASK_LIST_PATH") or "/api/edge/tasks"
    task_report_path = os.getenv("EDGE_TASK_REPORT_PATH") or "/api/edge/tasks/report"
    edge_id = os.getenv("EDGE_ID") or "local-dev"
    version_info = load_service_version_info()
    version = str(version_info.get("version") or _load_service_version()).strip() or "1.0.0"
    poll_interval_sec = _env_int("EDGE_POLL_INTERVAL_SEC", 5)
    report_interval_sec = _env_int("EDGE_REPORT_INTERVAL_SEC", 10)
    bind_host = os.getenv("EDGE_BIND_HOST") or "0.0.0.0"
    bind_port = _env_int("EDGE_BIND_PORT", _env_int("EDGE_CFG_BIND_PORT", _env_int("EDGE_PORT", int(local_cfg.get("bindPort") or 18080))))
    simulate = _env_bool("EDGE_SIMULATE", False)
    log_dir = str(os.getenv("EDGE_LOG_DIR") or local_cfg.get("logDir") or "output/logs").strip() or "output/logs"
    default_log_retention_days = _env_int("EDGE_LOG_RETENTION_DAYS", int(local_cfg.get("logRetentionDays") or 30))
    runtime_log_retention_days = _env_int("EDGE_RUNTIME_LOG_RETENTION_DAYS", int(local_cfg.get("runtimeLogRetentionDays") or default_log_retention_days))
    error_log_retention_days = _env_int("EDGE_ERROR_LOG_RETENTION_DAYS", int(local_cfg.get("errorLogRetentionDays") or default_log_retention_days))
    db_log_retention_days = _env_int("EDGE_DB_LOG_RETENTION_DAYS", int(local_cfg.get("dbLogRetentionDays") or default_log_retention_days))
    return EdgeConfig(
        server_base_url=server_base_url,
        task_list_path=task_list_path,
        task_report_path=task_report_path,
        edge_id=edge_id,
        version=version,
        version_info=version_info,
        poll_interval_sec=poll_interval_sec,
        report_interval_sec=report_interval_sec,
        bind_host=bind_host,
        bind_port=bind_port,
        simulate=simulate,
        log_dir=log_dir,
        runtime_log_retention_days=max(1, int(runtime_log_retention_days)),
        error_log_retention_days=max(1, int(error_log_retention_days)),
        db_log_retention_days=max(1, int(db_log_retention_days)),
    )
