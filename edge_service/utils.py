from __future__ import annotations

import logging
import re
import socket
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger("edge.utils")


def get_local_ip() -> str:
    """获取本机真实以太网 IP 地址（以太网适配器 以太网 或 以太网适配器 以太网 N）"""
    try:
        ps_cmd = r"""
Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.InterfaceAlias -match '^以太网$' -or
    $_.InterfaceAlias -match '^以太网 \d+$' -or
    $_.InterfaceAlias -match '^Ethernet$' -or
    $_.InterfaceAlias -match '^Ethernet \d+$'
} | Select-Object -First 1 -ExpandProperty IPAddress
"""
        result = subprocess.run(
            ["powershell", "-Command", ps_cmd.strip()],
            capture_output=True, text=True, timeout=2
        )
        ip = result.stdout.strip()
        if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
            _log.info("local ip from ethernet adapter: %s", ip)
            return ip
    except Exception as e:
        _log.warning("get local ip via powershell failed: %s", e)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("198.18.") and not ip.startswith("10.1."):
            _log.info("local ip from socket: %s", ip)
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def safe_name(s: str) -> str:
    """将字符串中的文件系统非法字符替换为下划线，用于构造目录/文件名。"""
    return re.sub(r'[<>:"/\\|?*]', "_", str(s or "").strip()) or "_"


def fmt_duration(sec: float) -> str:
    """将秒数格式化为 H:MM:SS 或 M:SS 字符串，用于进度/日志展示。"""
    total = max(0, int(sec or 0))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def as_int(v) -> int | None:
    try:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip()
        return int(s) if s else None
    except Exception:
        return None


def resolve_lesson_date(lesson_date: object = "", *fallbacks: object, default: str = "") -> str:
    primary = str(lesson_date or "").strip()
    if primary:
        return primary
    for raw in fallbacks:
        text = str(raw or "").strip()
        if not text:
            continue
        return (text.split("T")[0] if "T" in text else (text.split(" ")[0] if " " in text else text)) or default
    return default


def task_type_prefix(task_type: object) -> str:
    value = as_int(task_type)
    return {1: "teacher", 2: "student", 3: "ppt"}.get(value, "view")


def load_download_path() -> str:
    """从 monitor_config.json 读取 downloadPath，默认返回 D:\\Videos。"""
    from .monitor_config import load_monitor_cfg
    try:
        p = str(load_monitor_cfg().get("downloadPath") or r"D:\Videos").strip()
        return p or r"D:\Videos"
    except Exception:
        return r"D:\Videos"


def _default_package_capabilities() -> dict[str, bool]:
    return {
        "download": True,
        "transcode": True,
        "speech": True,
        "subtitle": True,
        "analysis": True,
    }


def _package_capabilities_for_mode(mode: str) -> dict[str, bool]:
    normalized = str(mode or "").strip().lower()
    if normalized == "lite":
        return {
            "download": True,
            "transcode": True,
            "speech": False,
            "subtitle": False,
            "analysis": True,
        }
    return _default_package_capabilities()


def _detect_runtime_package_mode() -> str | None:
    try:
        if not getattr(sys, "frozen", False):
            return None
        root_dir = Path(sys.executable).resolve().parent.parent
        name = root_dir.name.strip().lower()
        if name.startswith("edgeserviceclientall"):
            return "all"
        if name.startswith("edgeserviceclient"):
            return "lite"
    except Exception:
        return None
    return None


def load_package_mode() -> str:
    runtime_mode = _detect_runtime_package_mode()
    if runtime_mode:
        return runtime_mode
    from .monitor_config import load_monitor_cfg
    try:
        data = load_monitor_cfg() or {}
    except Exception:
        return "all"
    mode = str(data.get("packageMode") or "").strip().lower()
    return mode or "all"


def load_package_capabilities() -> dict[str, bool]:
    runtime_mode = _detect_runtime_package_mode()
    if runtime_mode:
        return _package_capabilities_for_mode(runtime_mode)
    from .monitor_config import load_monitor_cfg
    defaults = _default_package_capabilities()
    try:
        data = load_monitor_cfg() or {}
    except Exception:
        return defaults
    raw = data.get("packageCapabilities") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return defaults
    result = dict(defaults)
    for key in list(result.keys()):
        if key in raw:
            result[key] = bool(raw.get(key))
    return result


def load_task_control() -> dict[str, bool]:
    from .monitor_config import load_monitor_cfg
    defaults = {
        "download": True,
        "transcode": True,
        "speech": True,
        "subtitle": True,
        "analysis": True,
    }
    try:
        data = load_monitor_cfg() or {}
    except Exception:
        return defaults
    raw = data.get("taskControl") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return defaults
    result = dict(defaults)
    for key in list(result.keys()):
        if key not in raw:
            continue
        value = raw.get(key)
        if isinstance(value, bool):
            result[key] = value
            continue
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            result[key] = True
        elif text in {"0", "false", "no", "n", "off"}:
            result[key] = False
    package_caps = load_package_capabilities()
    for key in list(result.keys()):
        result[key] = bool(result.get(key, True) and package_caps.get(key, True))
    return result


def is_task_step_enabled(step_code: object) -> bool:
    step = str(step_code or "").strip().upper()
    key = {
        "DOWNLOAD": "download",
        "TRANSCODE": "transcode",
        "SPEECH": "speech",
        "SUBTITLE": "subtitle",
        "ANALYSIS": "analysis",
    }.get(step, "")
    if not key:
        return True
    return bool(load_task_control().get(key, True))


def get_lesson_dir(lesson_date: str, lesson_id: str, download_root: Path | None = None) -> Path:
    """根据课次日期和课次ID构造标准课次目录路径。

    路径规则：
      若 download_root 已经是 .../Videos 则直接用，否则追加 Videos 子目录。
      最终路径：<root>/Videos/<safe_date>/<safe_id>
    """
    if download_root is None:
        download_root = Path(load_download_path())
    base_dir = download_root if download_root.name.lower() == "videos" else (download_root / "Videos")
    return base_dir / safe_name(lesson_date) / safe_name(lesson_id)
