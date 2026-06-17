"""
客户端更新API路由
提供版本检查和更新包下载接口
"""

import os
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, FileResponse

log = logging.getLogger("edge.update_routes")

# 更新包存放目录
UPDATES_DIR = Path(os.getenv("EDGE_UPDATES_DIR", "updates"))

# 当前服务端提供的最新客户端版本
# 实际部署时应从配置文件或数据库读取
CLIENT_VERSION_INFO = {
    "latestVersion": "1.0.0",
    "minSupportedVersion": "1.0.0",
    "releaseNotes": "",
    "mandatory": False,
}


def create_update_routes() -> APIRouter:
    """创建更新相关路由"""
    router = APIRouter()
    
    @router.get("/api/client/version")
    async def get_client_version():
        """
        获取最新客户端版本信息
        
        Returns:
            {
                "latestVersion": "1.1.0",
                "minSupportedVersion": "1.0.0",
                "releaseNotes": "修复xxx问题",
                "mandatory": false
            }
        """
        # 尝试从配置文件读取
        version_config = UPDATES_DIR / "version.json"
        if version_config.exists():
            try:
                data = json.loads(version_config.read_text(encoding="utf-8"))
                return JSONResponse({"ok": True, **data})
            except Exception as e:
                log.warning("读取版本配置失败: %s", e)
        
        return JSONResponse({"ok": True, **CLIENT_VERSION_INFO})
    
    @router.get("/api/client/updates")
    async def get_client_updates(from_version: str = "", to_version: str = ""):
        """
        获取更新清单
        
        Args:
            from_version: 当前版本
            to_version: 目标版本
        
        Returns:
            {
                "updates": [
                    {
                        "component": "core",
                        "fromVersion": "1.0.0",
                        "toVersion": "1.1.0",
                        "type": "incremental",
                        "url": "/api/client/download/core-1.0.0-to-1.1.0.zip",
                        "size": 5242880,
                        "hash": "sha256:..."
                    }
                ],
                "totalSize": 5242880,
                "mandatory": false
            }
        """
        if not from_version or not to_version:
            return JSONResponse({"ok": False, "message": "缺少版本参数"})
        
        # 读取更新清单
        manifest_file = UPDATES_DIR / "manifest.json"
        if not manifest_file.exists():
            return JSONResponse({
                "ok": True,
                "updates": [],
                "totalSize": 0,
                "mandatory": False,
            })
        
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            
            # 查找适用的更新
            updates = []
            total_size = 0
            
            for update in manifest.get("updates", []):
                # 检查版本范围
                update_from = update.get("fromVersion", "")
                update_to = update.get("toVersion", "")
                
                # 简单匹配：from_version <= update_from < to_version
                if update_from == from_version or not update_from:
                    if update_to == to_version or not to_version:
                        updates.append({
                            "component": update.get("component", ""),
                            "fromVersion": update_from,
                            "toVersion": update_to,
                            "type": update.get("type", "full"),
                            "url": f"/api/client/download/{update.get('filename', '')}",
                            "size": update.get("size", 0),
                            "hash": update.get("hash", ""),
                        })
                        total_size += update.get("size", 0)
            
            return JSONResponse({
                "ok": True,
                "updates": updates,
                "totalSize": total_size,
                "mandatory": manifest.get("mandatory", False),
            })
        except Exception as e:
            log.error("读取更新清单失败: %s", e)
            return JSONResponse({"ok": False, "message": str(e)})
    
    @router.get("/api/client/download/{filename}")
    async def download_update(filename: str):
        """
        下载更新包
        
        Args:
            filename: 更新包文件名
        """
        # 安全检查：防止路径遍历
        if ".." in filename or "/" in filename or "\\" in filename:
            return JSONResponse({"ok": False, "message": "无效的文件名"}, status_code=400)
        
        file_path = UPDATES_DIR / filename
        if not file_path.exists():
            return JSONResponse({"ok": False, "message": "文件不存在"}, status_code=404)
        
        return FileResponse(
            file_path,
            media_type="application/zip",
            filename=filename,
        )
    
    @router.get("/api/client/components")
    async def list_components():
        """
        列出可用的组件及其版本
        """
        components = {}
        
        # 扫描更新目录
        if UPDATES_DIR.exists():
            for item in UPDATES_DIR.iterdir():
                if item.is_dir():
                    version_file = item / "version.json"
                    if version_file.exists():
                        try:
                            data = json.loads(version_file.read_text(encoding="utf-8"))
                            components[item.name] = {
                                "version": data.get("version", ""),
                                "size": sum(f.stat().st_size for f in item.rglob("*") if f.is_file()),
                            }
                        except Exception:
                            pass
        
        return JSONResponse({"ok": True, "components": components})
    
    return router
