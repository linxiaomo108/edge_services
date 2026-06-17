from __future__ import annotations

import json
import threading
from pathlib import Path


_lock = threading.Lock()
_cfg_path: Path | None = None


def _get_cfg_path() -> Path:
    global _cfg_path
    if _cfg_path is None:
        import sys
        if getattr(sys, 'frozen', False):
            # 打包后的可执行文件，output 目录在 core 目录的上一级
            exe_dir = Path(sys.executable).resolve().parent
            _cfg_path = exe_dir.parent / "output" / "monitor_config.json"
        else:
            # 开发环境
            base = Path(__file__).resolve().parent.parent
            _cfg_path = base / "output" / "monitor_config.json"
    return _cfg_path


def _get_client_cfg_path() -> Path | None:
    """获取客户端 config.json 路径（打包后位于可执行文件同级目录）"""
    import sys
    
    if getattr(sys, 'frozen', False):
        # 打包后的可执行文件，config.json 在 core 目录的上一级
        exe_dir = Path(sys.executable).resolve().parent
        path = exe_dir.parent / "config.json"
    else:
        # 开发环境
        base = Path(__file__).resolve().parent.parent
        path = base / "config.json"
    
    return path if path.exists() else None


def _read_json(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        data = json.loads(text or "{}") or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")


def _default_package_capabilities() -> dict[str, bool]:
    return {
        "download": True,
        "transcode": True,
        "speech": True,
        "subtitle": True,
        "analysis": True,
    }


def _to_monitor_cfg(data: dict) -> dict:
    result: dict = {}
    server = data.get("server", {}) if isinstance(data.get("server"), dict) else {}
    local = data.get("local", {}) if isinstance(data.get("local"), dict) else {}
    speech = data.get("speech", {}) if isinstance(data.get("speech"), dict) else {}
    stream_proxy = data.get("streamProxy", {}) if isinstance(data.get("streamProxy"), dict) else {}
    task_control = data.get("taskControl", {}) if isinstance(data.get("taskControl"), dict) else {}
    concurrency = data.get("concurrency", {}) if isinstance(data.get("concurrency"), dict) else {}
    package = data.get("package", {}) if isinstance(data.get("package"), dict) else {}
    report_backfill = data.get("reportBackfill", {}) if isinstance(data.get("reportBackfill"), dict) else {}

    if server.get("address"):
        result["serverAddress"] = server.get("address")
    if server.get("campusCode"):
        result["campusCode"] = server.get("campusCode")
    if server.get("serverId"):
        result["serverId"] = server.get("serverId")
    if server.get("connectionName"):
        result["connectionName"] = server.get("connectionName")
    if server.get("schoolAreaName"):
        result["schoolAreaName"] = server.get("schoolAreaName")
    if "startDate" in server:
        result["startDate"] = str(server.get("startDate") or "")

    if local.get("downloadPath"):
        result["downloadPath"] = local.get("downloadPath")
    if "bindPort" in local:
        result["bindPort"] = local.get("bindPort")

    if speech.get("model"):
        result["speechModel"] = speech.get("model")
    if "wordTimestamps" in speech:
        result["speechWordTimestamps"] = bool(speech.get("wordTimestamps"))
    if "promptMode" in speech:
        result["speechPromptMode"] = str(speech.get("promptMode") or "")
    if "vadMode" in speech:
        result["speechVadMode"] = str(speech.get("vadMode") or "")
    if "promptText" in speech:
        result["speechPromptText"] = str(speech.get("promptText") or "")
    if "hallucinationFilter" in speech:
        result["speechHallucinationFilter"] = bool(speech.get("hallucinationFilter"))
    if "temperature" in speech:
        result["speechTemperature"] = speech.get("temperature")
    if "retryTemperature" in speech:
        result["speechRetryTemperature"] = speech.get("retryTemperature")

    if isinstance(stream_proxy, dict):
        if "enableStreamProxy" in stream_proxy:
            result["enableStreamProxy"] = bool(stream_proxy.get("enableStreamProxy"))
        if "publicHost" in stream_proxy:
            result["publicHost"] = str(stream_proxy.get("publicHost") or "")
        if "publicPort" in stream_proxy:
            result["publicPort"] = str(stream_proxy.get("publicPort") or "")
        if "publicScheme" in stream_proxy:
            result["publicScheme"] = str(stream_proxy.get("publicScheme") or "")
        if "publicBaseUrl" in stream_proxy:
            result["publicBaseUrl"] = str(stream_proxy.get("publicBaseUrl") or "")

    if task_control:
        result["taskControl"] = {
            "download": bool(task_control.get("download", True)),
            "transcode": bool(task_control.get("transcode", True)),
            "speech": bool(task_control.get("speech", True)),
            "subtitle": bool(task_control.get("subtitle", True)),
            "analysis": bool(task_control.get("analysis", True)),
        }

    if concurrency:
        result["concurrency"] = concurrency
    if report_backfill:
        result["reportBackfill"] = report_backfill

    package_mode = str(package.get("mode") or "").strip().lower()
    if package_mode:
        result["packageMode"] = package_mode
    package_caps_raw = package.get("capabilities") if isinstance(package.get("capabilities"), dict) else {}
    package_caps = dict(_default_package_capabilities())
    for key in list(package_caps.keys()):
        if key in package_caps_raw:
            package_caps[key] = bool(package_caps_raw.get(key))
    result["packageCapabilities"] = package_caps

    for key in [
        "serviceId",
        "registerInfo",
        "accessKey",
        "accessSecret",
        "authToken",
        "executionMode",
    ]:
        if key in data:
            result[key] = data.get(key)

    return result


def _load_client_cfg() -> dict:
    """从客户端 config.json 加载配置"""
    return _to_monitor_cfg(_read_json(_get_client_cfg_path()))


def _save_client_cfg(data: dict) -> None:
    """将配置同步保存到客户端 config.json"""
    import sys
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).resolve().parent
        # 打包后 config.json 在 core 目录的上一级
        path = exe_dir.parent / "config.json"
    else:
        base = Path(__file__).resolve().parent.parent
        path = base / "config.json"
    
    existing = _read_json(path)
    server = existing.get("server") if isinstance(existing.get("server"), dict) else {}
    local = existing.get("local") if isinstance(existing.get("local"), dict) else {}
    speech = existing.get("speech") if isinstance(existing.get("speech"), dict) else {}
    stream_proxy = existing.get("streamProxy") if isinstance(existing.get("streamProxy"), dict) else {}
    task_control = existing.get("taskControl") if isinstance(existing.get("taskControl"), dict) else {}
    package = existing.get("package") if isinstance(existing.get("package"), dict) else {}

    if "serverAddress" in data:
        server["address"] = str(data.get("serverAddress") or "")
    if "campusCode" in data:
        server["campusCode"] = str(data.get("campusCode") or "")
    if "serverId" in data:
        server["serverId"] = str(data.get("serverId") or "")
    if "connectionName" in data:
        server["connectionName"] = str(data.get("connectionName") or "")
    if "schoolAreaName" in data:
        server["schoolAreaName"] = str(data.get("schoolAreaName") or "")
    if "startDate" in data:
        server["startDate"] = str(data.get("startDate") or "")

    if "downloadPath" in data:
        local["downloadPath"] = str(data.get("downloadPath") or "")
    if "bindPort" in data:
        local["bindPort"] = data.get("bindPort")

    if "speechModel" in data:
        speech["model"] = str(data.get("speechModel") or "")
    if "speechWordTimestamps" in data:
        speech["wordTimestamps"] = bool(data.get("speechWordTimestamps"))
    if "speechPromptMode" in data:
        speech["promptMode"] = str(data.get("speechPromptMode") or "")
    if "speechVadMode" in data:
        speech["vadMode"] = str(data.get("speechVadMode") or "")
    if "speechPromptText" in data:
        speech["promptText"] = str(data.get("speechPromptText") or "")
    if "speechHallucinationFilter" in data:
        speech["hallucinationFilter"] = bool(data.get("speechHallucinationFilter"))
    if "speechTemperature" in data:
        speech["temperature"] = data.get("speechTemperature")
    if "speechRetryTemperature" in data:
        speech["retryTemperature"] = data.get("speechRetryTemperature")

    if "enableStreamProxy" in data:
        stream_proxy["enableStreamProxy"] = bool(data.get("enableStreamProxy"))
    if "publicHost" in data:
        stream_proxy["publicHost"] = str(data.get("publicHost") or "")
    if "publicPort" in data:
        stream_proxy["publicPort"] = str(data.get("publicPort") or "")
    if "publicScheme" in data:
        stream_proxy["publicScheme"] = str(data.get("publicScheme") or "")
    if "publicBaseUrl" in data:
        stream_proxy["publicBaseUrl"] = str(data.get("publicBaseUrl") or "")

    raw_task_control = data.get("taskControl") if isinstance(data.get("taskControl"), dict) else {}
    for key in ["download", "transcode", "speech", "subtitle", "analysis"]:
        if key in raw_task_control:
            task_control[key] = bool(raw_task_control.get(key))

    if server:
        existing["server"] = server
    if local:
        existing["local"] = local
    if speech:
        existing["speech"] = speech
    if stream_proxy:
        existing["streamProxy"] = stream_proxy
    if task_control:
        existing["taskControl"] = task_control
    if "concurrency" in data and isinstance(data.get("concurrency"), dict):
        existing["concurrency"] = data.get("concurrency") or {}
    if "reportBackfill" in data and isinstance(data.get("reportBackfill"), dict):
        existing["reportBackfill"] = data.get("reportBackfill") or {}
    if "packageMode" in data:
        package["mode"] = str(data.get("packageMode") or "all").strip().lower() or "all"
    raw_package_caps = data.get("packageCapabilities") if isinstance(data.get("packageCapabilities"), dict) else {}
    if raw_package_caps:
        merged_caps = dict(_default_package_capabilities())
        existing_caps = package.get("capabilities") if isinstance(package.get("capabilities"), dict) else {}
        for key in list(merged_caps.keys()):
            if key in existing_caps:
                merged_caps[key] = bool(existing_caps.get(key))
            if key in raw_package_caps:
                merged_caps[key] = bool(raw_package_caps.get(key))
        package["capabilities"] = merged_caps
    if package:
        existing["package"] = package
    for key in ["serviceId", "registerInfo", "accessKey", "accessSecret", "authToken", "executionMode"]:
        if key in data:
            existing[key] = data.get(key)

    _write_json(path, existing)


def load_monitor_cfg() -> dict:
    path = _get_cfg_path()
    legacy_data = _read_json(path)
    client_cfg = _load_client_cfg()
    if client_cfg:
        return client_cfg
    return dict(legacy_data) if legacy_data else {}


def save_monitor_cfg(data: dict) -> None:
    """将配置统一保存到 config.json。"""
    _save_client_cfg(data)
