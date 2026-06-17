"""
版本管理模块
用于客户端版本检查和更新
"""

import os
import json
import logging
import hashlib
from pathlib import Path
from typing import Optional, Any
from dataclasses import dataclass

log = logging.getLogger("edge.version")

# 版本文件路径
VERSION_FILE = "version.json"


@dataclass
class ComponentVersion:
    """组件版本信息"""
    name: str
    version: str
    hash: str
    size: int = 0
    update_url: str = ""


@dataclass
class ClientVersion:
    """客户端版本信息"""
    client_version: str
    build_time: str
    components: dict[str, ComponentVersion]


def _get_root_dir() -> Path:
    """获取客户端根目录"""
    # 优先使用环境变量
    root = os.getenv("EDGE_ROOT")
    if root:
        return Path(root)
    # 否则使用当前文件所在目录的上级
    return Path(__file__).parent.parent


def load_local_version() -> Optional[ClientVersion]:
    """加载本地版本信息"""
    try:
        version_file = _get_root_dir() / VERSION_FILE
        if not version_file.exists():
            log.warning("版本文件不存在: %s", version_file)
            return None
        
        data = json.loads(version_file.read_text(encoding="utf-8"))
        components = {}
        for name, comp in data.get("components", {}).items():
            components[name] = ComponentVersion(
                name=name,
                version=comp.get("version", ""),
                hash=comp.get("hash", ""),
                size=comp.get("size", 0),
                update_url=comp.get("updateUrl", ""),
            )
        
        return ClientVersion(
            client_version=data.get("clientVersion", "0.0.0"),
            build_time=data.get("buildTime", ""),
            components=components,
        )
    except Exception as e:
        log.error("加载版本信息失败: %s", e)
        return None


def get_version_string() -> str:
    """获取版本字符串"""
    version = load_local_version()
    if version:
        return version.client_version
    return "unknown"


def compare_versions(v1: str, v2: str) -> int:
    """
    比较版本号
    返回: -1 (v1 < v2), 0 (v1 == v2), 1 (v1 > v2)
    """
    def parse(v: str) -> list[int]:
        parts = []
        for p in v.split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return parts
    
    p1, p2 = parse(v1), parse(v2)
    # 补齐长度
    max_len = max(len(p1), len(p2))
    p1.extend([0] * (max_len - len(p1)))
    p2.extend([0] * (max_len - len(p2)))
    
    for a, b in zip(p1, p2):
        if a < b:
            return -1
        if a > b:
            return 1
    return 0


async def check_for_updates(api_client) -> Optional[dict[str, Any]]:
    """
    检查服务端是否有更新
    
    Returns:
        更新信息字典，如果没有更新则返回None
    """
    local_version = load_local_version()
    if not local_version:
        log.warning("无法获取本地版本信息")
        return None
    
    try:
        # 调用服务端API获取最新版本
        remote_version = await api_client.get_client_version()
        if not remote_version:
            return None
        
        latest = remote_version.get("latestVersion", "")
        if not latest:
            return None
        
        if compare_versions(local_version.client_version, latest) < 0:
            # 有新版本
            log.info("发现新版本: %s -> %s", local_version.client_version, latest)
            return {
                "current_version": local_version.client_version,
                "latest_version": latest,
                "release_notes": remote_version.get("releaseNotes", ""),
                "mandatory": remote_version.get("mandatory", False),
            }
        
        log.info("当前已是最新版本: %s", local_version.client_version)
        return None
    except Exception as e:
        log.warning("检查更新失败: %s", e)
        return None


def calc_file_hash(file_path: Path) -> str:
    """计算文件SHA256哈希"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def calc_dir_hash(dir_path: Path) -> str:
    """计算目录的整体哈希"""
    sha256 = hashlib.sha256()
    for file in sorted(dir_path.rglob("*")):
        if file.is_file():
            sha256.update(file.name.encode())
            sha256.update(calc_file_hash(file).encode())
    return sha256.hexdigest()[:16]


def get_component_info() -> dict[str, dict]:
    """获取各组件的当前信息"""
    root = _get_root_dir()
    components = {}
    
    # 检查各组件目录
    for name, subdir in [
        ("core", "core"),
        ("sdk", "sdk"),
        ("ffmpeg", "ffmpeg"),
        ("models", "models"),
        ("docs", "docs"),
        ("ui", "monitor_ui"),
    ]:
        path = root / subdir
        if path.exists():
            components[name] = {
                "exists": True,
                "path": str(path),
                "hash": calc_dir_hash(path),
            }
        else:
            components[name] = {
                "exists": False,
                "path": str(path),
                "hash": "",
            }
    
    return components
