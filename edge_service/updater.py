"""
客户端更新器模块
负责检查更新、下载更新包、应用更新
"""

import os
import json
import logging
import shutil
import zipfile
import hashlib
import tempfile
from pathlib import Path
from typing import Optional, Any, Callable
from dataclasses import dataclass
from urllib.request import urlretrieve

from .version_manager import (
    load_local_version,
    compare_versions,
    calc_file_hash,
    ClientVersion,
)

log = logging.getLogger("edge.updater")


@dataclass
class UpdateInfo:
    """更新信息"""
    component: str
    from_version: str
    to_version: str
    update_type: str  # "incremental" or "full"
    url: str
    size: int
    hash: str


@dataclass
class UpdateResult:
    """更新结果"""
    success: bool
    message: str
    updated_components: list[str]


def _get_root_dir() -> Path:
    """获取客户端根目录"""
    root = os.getenv("EDGE_ROOT")
    if root:
        return Path(root)
    return Path(__file__).parent.parent


def _download_file(
    url: str,
    dest: Path,
    on_progress: Optional[Callable[[int, int], None]] = None
) -> bool:
    """
    下载文件
    
    Args:
        url: 下载URL
        dest: 目标路径
        on_progress: 进度回调 (downloaded, total)
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        
        def _report(block_num, block_size, total_size):
            if on_progress and total_size > 0:
                downloaded = block_num * block_size
                on_progress(min(downloaded, total_size), total_size)
        
        urlretrieve(url, dest, reporthook=_report)
        return True
    except Exception as e:
        log.error("下载失败 %s: %s", url, e)
        return False


def _verify_hash(file_path: Path, expected_hash: str) -> bool:
    """验证文件哈希"""
    if not expected_hash:
        return True
    actual = calc_file_hash(file_path)
    if actual.lower() != expected_hash.lower():
        log.error("哈希校验失败: expected=%s, actual=%s", expected_hash, actual)
        return False
    return True


def _extract_update(zip_path: Path, target_dir: Path) -> bool:
    """解压更新包"""
    try:
        # 先解压到临时目录
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(tmp_path)
            
            # 复制到目标目录
            for item in tmp_path.iterdir():
                dest = target_dir / item.name
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
        
        return True
    except Exception as e:
        log.error("解压更新包失败: %s", e)
        return False


def _backup_component(component: str) -> Optional[Path]:
    """备份组件"""
    root = _get_root_dir()
    component_dir = root / component
    if not component_dir.exists():
        return None
    
    backup_dir = root / "backup" / f"{component}.bak"
    try:
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        shutil.copytree(component_dir, backup_dir)
        log.info("已备份组件: %s -> %s", component, backup_dir)
        return backup_dir
    except Exception as e:
        log.error("备份组件失败 %s: %s", component, e)
        return None


def _restore_component(component: str, backup_path: Path) -> bool:
    """恢复组件"""
    root = _get_root_dir()
    component_dir = root / component
    try:
        if component_dir.exists():
            shutil.rmtree(component_dir)
        shutil.copytree(backup_path, component_dir)
        log.info("已恢复组件: %s", component)
        return True
    except Exception as e:
        log.error("恢复组件失败 %s: %s", component, e)
        return False


class Updater:
    """更新器"""
    
    def __init__(self, api_client):
        self._api = api_client
        self._root = _get_root_dir()
    
    async def check_for_updates(self) -> Optional[dict[str, Any]]:
        """
        检查是否有更新
        
        Returns:
            更新信息字典，如果没有更新则返回None
        """
        local = load_local_version()
        if not local:
            log.warning("无法获取本地版本信息")
            return None
        
        try:
            remote = await self._api.get_client_version()
            if not remote:
                log.info("服务端未返回版本信息")
                return None
            
            latest = remote.get("latestVersion", "")
            if not latest:
                return None
            
            if compare_versions(local.client_version, latest) < 0:
                log.info("发现新版本: %s -> %s", local.client_version, latest)
                return {
                    "current_version": local.client_version,
                    "latest_version": latest,
                    "release_notes": remote.get("releaseNotes", ""),
                    "mandatory": remote.get("mandatory", False),
                }
            
            log.info("当前已是最新版本: %s", local.client_version)
            return None
        except Exception as e:
            log.warning("检查更新失败: %s", e)
            return None
    
    async def get_update_manifest(
        self,
        from_version: str,
        to_version: str
    ) -> list[UpdateInfo]:
        """
        获取更新清单
        
        Args:
            from_version: 当前版本
            to_version: 目标版本
        
        Returns:
            更新信息列表
        """
        try:
            data = await self._api.get_client_updates(from_version, to_version)
            if not data or "updates" not in data:
                return []
            
            updates = []
            for item in data["updates"]:
                updates.append(UpdateInfo(
                    component=item.get("component", ""),
                    from_version=item.get("fromVersion", ""),
                    to_version=item.get("toVersion", ""),
                    update_type=item.get("type", "full"),
                    url=item.get("url", ""),
                    size=item.get("size", 0),
                    hash=item.get("hash", ""),
                ))
            
            return updates
        except Exception as e:
            log.error("获取更新清单失败: %s", e)
            return []
    
    async def apply_update(
        self,
        update: UpdateInfo,
        on_progress: Optional[Callable[[str, int, int], None]] = None
    ) -> bool:
        """
        应用单个组件更新
        
        Args:
            update: 更新信息
            on_progress: 进度回调 (stage, current, total)
        
        Returns:
            是否成功
        """
        component = update.component
        log.info("开始更新组件: %s (%s -> %s)", component, update.from_version, update.to_version)
        
        # 1. 备份
        if on_progress:
            on_progress("backup", 0, 100)
        backup = _backup_component(component)
        
        # 2. 下载
        if on_progress:
            on_progress("download", 0, update.size)
        
        download_dir = self._root / "downloads"
        download_dir.mkdir(parents=True, exist_ok=True)
        zip_path = download_dir / f"{component}-{update.to_version}.zip"
        
        def _dl_progress(downloaded, total):
            if on_progress:
                on_progress("download", downloaded, total)
        
        if not _download_file(update.url, zip_path, _dl_progress):
            log.error("下载更新包失败: %s", component)
            return False
        
        # 3. 验证
        if on_progress:
            on_progress("verify", 0, 100)
        
        if not _verify_hash(zip_path, update.hash):
            log.error("更新包校验失败: %s", component)
            zip_path.unlink(missing_ok=True)
            return False
        
        # 4. 解压应用
        if on_progress:
            on_progress("apply", 0, 100)
        
        target_dir = self._root / component
        if not _extract_update(zip_path, target_dir):
            log.error("应用更新失败: %s", component)
            # 尝试恢复
            if backup:
                _restore_component(component, backup)
            return False
        
        # 5. 清理
        zip_path.unlink(missing_ok=True)
        if backup and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        
        if on_progress:
            on_progress("done", 100, 100)
        
        log.info("组件更新完成: %s", component)
        return True
    
    async def apply_all_updates(
        self,
        updates: list[UpdateInfo],
        on_progress: Optional[Callable[[str, str, int, int], None]] = None
    ) -> UpdateResult:
        """
        应用所有更新
        
        Args:
            updates: 更新列表
            on_progress: 进度回调 (component, stage, current, total)
        
        Returns:
            更新结果
        """
        if not updates:
            return UpdateResult(True, "没有需要更新的组件", [])
        
        updated = []
        failed = []
        
        for update in updates:
            def _progress(stage, current, total):
                if on_progress:
                    on_progress(update.component, stage, current, total)
            
            if await self.apply_update(update, _progress):
                updated.append(update.component)
            else:
                failed.append(update.component)
        
        if failed:
            return UpdateResult(
                False,
                f"部分组件更新失败: {', '.join(failed)}",
                updated
            )
        
        return UpdateResult(
            True,
            f"更新完成: {', '.join(updated)}",
            updated
        )
    
    def update_version_file(self, new_version: str, updated_components: list[str]) -> bool:
        """更新版本文件"""
        try:
            version_file = self._root / "version.json"
            if version_file.exists():
                data = json.loads(version_file.read_text(encoding="utf-8"))
            else:
                data = {"components": {}}
            
            data["clientVersion"] = new_version
            
            # 更新组件版本
            for comp in updated_components:
                if comp in data.get("components", {}):
                    data["components"][comp]["version"] = new_version
            
            version_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            
            log.info("版本文件已更新: %s", new_version)
            return True
        except Exception as e:
            log.error("更新版本文件失败: %s", e)
            return False
