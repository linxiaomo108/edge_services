#!/usr/bin/env python3
"""
EdgeService 客户端打包脚本
用法: python build.py [--skip-model] [--skip-ffmpeg]
"""

import os
import sys
import shutil
import subprocess
import hashlib
import json
import urllib.request
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Any

# 配置
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist"

PACKAGE_PROFILES = {
    "mode1": {
        "cli_mode": "mode1",
        "package_mode": "all",
        "output_name": "EdgeServiceClientAll",
        "display_name": "EdgeServiceClientAll",
        "include_models": True,
        "capabilities": {
            "download": True,
            "transcode": True,
            "speech": True,
            "subtitle": True,
            "analysis": True,
        },
    },
    "mode2": {
        "cli_mode": "mode2",
        "package_mode": "lite",
        "output_name": "EdgeServiceClient",
        "display_name": "EdgeServiceClient",
        "include_models": False,
        "capabilities": {
            "download": True,
            "transcode": True,
            "speech": False,
            "subtitle": False,
            "analysis": True,
        },
    },
}

PACKAGE_PROFILE = dict(PACKAGE_PROFILES["mode1"])
OUTPUT_DIR = DIST_DIR / str(PACKAGE_PROFILE["output_name"])

# 本地资源缓存目录（存放预下载的资源，避免重复下载）
RESOURCE_CACHE_DIR = PROJECT_ROOT / "packaging_resources"
RESOURCE_VERSION_FILE = RESOURCE_CACHE_DIR / "versions.json"

# 模型配置
WHISPER_MODEL = "large-v3"
WHISPER_MODEL_URL = "https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main"
WHISPER_MODEL_FILES = [
    "config.json",
    "model.bin",
    "preprocessor_config.json",
    "tokenizer.json",
    "vocabulary.json",
]

# FFmpeg配置
FFMPEG_VERSION = "6.1"
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def run_cmd(cmd: list, cwd: Path = None, env: dict[str, str] | None = None) -> bool:
    """运行命令"""
    log(f"执行: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=False, env=env)
    return result.returncode == 0


def calc_hash(file_path: Path) -> str:
    """计算文件SHA256"""
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
            sha256.update(calc_hash(file).encode())
    return sha256.hexdigest()[:16]


def _git_output(args: list[str]) -> str:
    try:
        r = subprocess.run(args, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if r.returncode == 0:
            return str(r.stdout or "").strip()
    except Exception:
        return ""
    return ""


def _build_version_value(build_time: datetime, git_commit: str) -> str:
    stamp = build_time.strftime("%Y.%m.%d.%H%M")
    short_commit = str(git_commit or "").strip()[:7]
    return f"{stamp}.{short_commit}" if short_commit else stamp


def _base_version_info() -> dict[str, Any]:
    build_time = datetime.now()
    git_commit = _git_output(["git", "rev-parse", "--short=12", "HEAD"])
    git_branch = _git_output(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    dirty = bool(_git_output(["git", "status", "--porcelain"]))
    return {
        "version": _build_version_value(build_time, git_commit),
        "clientVersion": _build_version_value(build_time, git_commit),
        "buildTime": build_time.isoformat(),
        "gitCommit": git_commit,
        "gitBranch": git_branch,
        "dirty": dirty,
        "packageMode": str(PACKAGE_PROFILE["package_mode"]),
        "packageName": str(PACKAGE_PROFILE["display_name"]),
        "package": {
            "mode": str(PACKAGE_PROFILE["package_mode"]),
            "name": str(PACKAGE_PROFILE["display_name"]),
            "capabilities": dict(PACKAGE_PROFILE["capabilities"]),
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def step_prepare_service_version() -> dict[str, Any]:
    log("准备服务版本信息...")
    version_info = _base_version_info()
    _write_json(PROJECT_ROOT / "service_version.json", version_info)
    log(f"服务版本信息已更新: version={version_info['version']} commit={version_info['gitCommit'] or '-'} dirty={version_info['dirty']}")
    return version_info


def build_client_config_template() -> dict:
    """构建客户端默认配置模板"""
    capabilities = dict(PACKAGE_PROFILE["capabilities"])
    return {
        "server": {
            "address": "http://your-server:8080",
            "campusCode": "your-campus-code",
            "startDate": "",
            "serverId": "edge-001"
        },
        "local": {
            "bindPort": 18080,
            "downloadPath": "D:\\Videos",
            "logDir": "output/logs",
            "logRetentionDays": 30,
            "runtimeLogRetentionDays": 30,
            "errorLogRetentionDays": 30,
            "dbLogRetentionDays": 30
        },
        "speech": {
            "model": "large-v3",
            "device": "auto",
            "wordTimestamps": True,
            "promptMode": "off",
            "vadMode": "builtin",
            "promptText": "",
            "hallucinationFilter": True,
            "temperature": 0.0,
            "retryTemperature": 0.4
        },
        "taskControl": {
            "download": bool(capabilities["download"]),
            "transcode": bool(capabilities["transcode"]),
            "speech": bool(capabilities["speech"]),
            "subtitle": bool(capabilities["subtitle"]),
            "analysis": bool(capabilities["analysis"])
        },
        "package": {
            "mode": str(PACKAGE_PROFILE["package_mode"]),
            "name": str(PACKAGE_PROFILE["display_name"]),
            "capabilities": capabilities
        },
        "streamProxy": {
            "enableStreamProxy": True,
            "publicBaseUrl": "",
            "publicHost": "",
            "publicPort": "",
            "publicScheme": ""
        },
        "reportBackfill": {
            "enabled": False,
            "urlHostReplacements": [
                {
                    "oldBaseUrl": "",
                    "newBaseUrl": "",
                    "stepCodes": [
                        "DOWNLOAD",
                        "TRANSCODE",
                        "ANALYSIS"
                    ]
                }
            ]
        },
        "update": {
            "autoCheck": True,
            "autoDownload": False,
            "channel": "stable"
        },
        "executionMode": "manual"
    }


def download_file(url: str, dest: Path, desc: str = ""):
    """下载文件"""
    log(f"下载: {desc or url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)
    log(f"完成: {dest}")


def load_resource_versions() -> dict:
    """加载资源版本信息"""
    if RESOURCE_VERSION_FILE.exists():
        try:
            return json.loads(RESOURCE_VERSION_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_resource_versions(versions: dict):
    """保存资源版本信息"""
    RESOURCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RESOURCE_VERSION_FILE.write_text(json.dumps(versions, indent=2, ensure_ascii=False), encoding='utf-8')


def check_cache_valid(resource_name: str, required_version: str) -> bool:
    """检查缓存是否有效（版本匹配）"""
    versions = load_resource_versions()
    cached_version = versions.get(resource_name, {}).get("version", "")
    return cached_version == required_version


def update_cache_version(resource_name: str, version: str, path: str):
    """更新缓存版本信息"""
    versions = load_resource_versions()
    versions[resource_name] = {
        "version": version,
        "path": path,
        "updated_at": datetime.now().isoformat()
    }
    save_resource_versions(versions)


def step_clean():
    """清理构建目录"""
    global OUTPUT_DIR
    log("清理构建目录...")
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
    if OUTPUT_DIR.exists():
        try:
            shutil.rmtree(OUTPUT_DIR)
        except PermissionError as e:
            log(f"警告: 无法删除输出目录 {OUTPUT_DIR}，可能被其他程序占用")
            log(f"  错误: {e}")
            # 使用带时间戳的新目录
            import time
            OUTPUT_DIR = OUTPUT_DIR.parent / f"{PACKAGE_PROFILE['output_name']}_{int(time.time())}"
            log(f"  使用新输出目录: {OUTPUT_DIR}")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def step_pyinstaller():
    """运行PyInstaller打包"""
    log("运行PyInstaller...")
    spec_file = PROJECT_ROOT / "scripts" / "packaging" / "edge_service.spec"
    env = dict(os.environ)
    env["EDGE_PACKAGE_MODE"] = str(PACKAGE_PROFILE["package_mode"])
    if not run_cmd([
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "--workpath", str(BUILD_DIR / "pyinstaller"),
        "--distpath", str(BUILD_DIR / "dist"),
        str(spec_file)
    ], cwd=PROJECT_ROOT, env=env):
        raise RuntimeError("PyInstaller打包失败")
    
    # 移动到输出目录
    core_dir = BUILD_DIR / "dist" / "EdgeServiceCore"
    if core_dir.exists():
        shutil.copytree(core_dir, OUTPUT_DIR / "core", dirs_exist_ok=True)
    log("PyInstaller打包完成")


def step_copy_sdk():
    """复制海康SDK"""
    log("复制海康SDK...")
    sdk_src = PROJECT_ROOT / "sdk"
    sdk_dst = OUTPUT_DIR / "sdk"
    if sdk_src.exists():
        shutil.copytree(sdk_src, sdk_dst, dirs_exist_ok=True)
        # 删除不需要的文件
        for pattern in ["*.exe", "*.lib", "ClientDemoDll"]:
            for f in sdk_dst.glob(pattern):
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    shutil.rmtree(f)
        log(f"SDK复制完成: {sdk_dst}")
    else:
        log("警告: SDK目录不存在")


def step_copy_docs():
    """复制配置文件"""
    log("复制配置文件...")
    docs_dst = OUTPUT_DIR / "docs"
    docs_dst.mkdir(parents=True, exist_ok=True)
    
    for name in ["correction_dict.json", "hallucination_blacklist.json"]:
        src = PROJECT_ROOT / "docs" / name
        if src.exists():
            shutil.copy2(src, docs_dst / name)
    log("配置文件复制完成")


def step_copy_ui():
    """复制前端页面"""
    log("复制前端页面...")
    ui_src = PROJECT_ROOT / "monitor_ui"
    ui_dst = OUTPUT_DIR / "monitor_ui"
    if ui_src.exists():
        shutil.copytree(ui_src, ui_dst, dirs_exist_ok=True)
    log("前端页面复制完成")


def step_download_ffmpeg(skip: bool = False):
    """下载FFmpeg（优先从本地缓存复制）"""
    if skip:
        log("跳过FFmpeg下载")
        return
    
    ffmpeg_dir = OUTPUT_DIR / "ffmpeg"
    ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    
    # 检查输出目录是否已存在
    if (ffmpeg_dir / "ffmpeg.exe").exists():
        log("FFmpeg已存在于输出目录，跳过")
        return
    
    # 检查本地缓存
    cache_ffmpeg_dir = RESOURCE_CACHE_DIR / "ffmpeg"
    if cache_ffmpeg_dir.exists() and (cache_ffmpeg_dir / "ffmpeg.exe").exists():
        if check_cache_valid("ffmpeg", FFMPEG_VERSION):
            log(f"从本地缓存复制FFmpeg (版本: {FFMPEG_VERSION})...")
            shutil.copytree(cache_ffmpeg_dir, ffmpeg_dir, dirs_exist_ok=True)
            log("FFmpeg复制完成")
            return
        else:
            log("FFmpeg缓存版本不匹配，重新下载...")
    
    log("下载FFmpeg...")
    zip_path = BUILD_DIR / "ffmpeg.zip"
    
    try:
        download_file(FFMPEG_URL, zip_path, "FFmpeg")
        
        # 解压到缓存目录
        log("解压FFmpeg到缓存目录...")
        cache_ffmpeg_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                if member.endswith(('ffmpeg.exe', 'ffprobe.exe')):
                    filename = os.path.basename(member)
                    with zf.open(member) as src, open(cache_ffmpeg_dir / filename, 'wb') as dst:
                        dst.write(src.read())
        
        # 更新缓存版本信息
        update_cache_version("ffmpeg", FFMPEG_VERSION, str(cache_ffmpeg_dir))
        
        # 复制到输出目录
        shutil.copytree(cache_ffmpeg_dir, ffmpeg_dir, dirs_exist_ok=True)
        
        zip_path.unlink()
        log("FFmpeg下载并缓存完成")
    except Exception as e:
        log(f"FFmpeg下载失败: {e}")
        log("请手动下载FFmpeg并放入ffmpeg目录或packaging_resources/ffmpeg目录")


def step_download_model(skip: bool = False):
    """下载Whisper模型（优先从本地缓存复制）"""
    if not bool(PACKAGE_PROFILE.get("include_models", True)):
        log("当前为轻量模式，跳过语音模型打包")
        return
    if skip:
        log("跳过模型下载")
        return
    
    model_dir = OUTPUT_DIR / "models" / f"faster-whisper-{WHISPER_MODEL}"
    model_dir.mkdir(parents=True, exist_ok=True)
    
    # 检查输出目录是否已存在
    if (model_dir / "model.bin").exists():
        log("模型已存在于输出目录，跳过")
        return
    
    # 检查本地缓存
    cache_model_dir = RESOURCE_CACHE_DIR / "models" / f"faster-whisper-{WHISPER_MODEL}"
    if cache_model_dir.exists() and (cache_model_dir / "model.bin").exists():
        if check_cache_valid("whisper_model", WHISPER_MODEL):
            log(f"从本地缓存复制Whisper模型 (版本: {WHISPER_MODEL})...")
            shutil.copytree(cache_model_dir, model_dir, dirs_exist_ok=True)
            log("模型复制完成")
            return
        else:
            log("模型缓存版本不匹配，重新下载...")
    
    log(f"下载Whisper模型: {WHISPER_MODEL}")
    log("注意: 模型文件较大(约3GB)，请耐心等待...")
    
    # 下载到缓存目录
    cache_model_dir.mkdir(parents=True, exist_ok=True)
    
    for filename in WHISPER_MODEL_FILES:
        cache_dest = cache_model_dir / filename
        if not cache_dest.exists():
            url = f"{WHISPER_MODEL_URL}/{filename}"
            try:
                download_file(url, cache_dest, filename)
            except Exception as e:
                log(f"下载{filename}失败: {e}")
                log("请手动下载模型文件到packaging_resources/models目录")
                return
    
    # 更新缓存版本信息
    update_cache_version("whisper_model", WHISPER_MODEL, str(cache_model_dir))
    
    # 复制到输出目录
    shutil.copytree(cache_model_dir, model_dir, dirs_exist_ok=True)
    
    log("模型下载并缓存完成")


def step_create_launcher():
    """创建启动器"""
    log("创建启动器...")
    
    # 启动脚本
    start_bat = OUTPUT_DIR / "start.bat"
    start_bat.write_text(r'''@echo off
chcp 65001 >nul
title EdgeService 边缘服务

REM 设置环境变量
set "EDGE_ROOT=%~dp0"
set "PATH=%EDGE_ROOT%sdk\download;%EDGE_ROOT%sdk\systrans;%EDGE_ROOT%ffmpeg;%PATH%"
if exist "%EDGE_ROOT%models" set "WHISPER_MODEL_DIR=%EDGE_ROOT%models"
if exist "%EDGE_ROOT%models" set "HF_HOME=%EDGE_ROOT%models"
set "EDGE_HIK_SDK_DIR=%EDGE_ROOT%sdk\download"
set "EDGE_HIK_SYSTRANS_SDK_DIR=%EDGE_ROOT%sdk\systrans"

REM 检查配置文件
if not exist "%EDGE_ROOT%config.json" (
    echo 首次运行，请编辑 config.json 配置服务器地址
    copy "%EDGE_ROOT%config.example.json" "%EDGE_ROOT%config.json" >nul 2>&1
    notepad "%EDGE_ROOT%config.json"
)

REM 启动服务
echo 正在启动边缘服务...
cd /d "%EDGE_ROOT%core"
EdgeServiceCore.exe

pause
''', encoding='utf-8-sig')
    
    # 配置文件模板
    config_template = build_client_config_template()
    config_example = OUTPUT_DIR / "config.example.json"
    config_example.write_text(json.dumps(config_template, indent=2, ensure_ascii=False), encoding='utf-8')
    config_json = OUTPUT_DIR / "config.json"
    config_json.write_text(json.dumps(config_template, indent=2, ensure_ascii=False), encoding='utf-8')
    
    # 创建output目录
    (OUTPUT_DIR / "output").mkdir(exist_ok=True)
    
    log("启动器创建完成")


def step_create_version(base_version_info: dict[str, Any]):
    """创建版本信息文件"""
    log("创建版本信息...")
    version_info = {
        **dict(base_version_info or {}),
        "components": {
            "core": {
                "version": str((base_version_info or {}).get("version") or ""),
                "hash": calc_dir_hash(OUTPUT_DIR / "core") if (OUTPUT_DIR / "core").exists() else ""
            },
            "sdk": {
                "version": "6.1.9.4",
                "hash": calc_dir_hash(OUTPUT_DIR / "sdk") if (OUTPUT_DIR / "sdk").exists() else ""
            },
            "ffmpeg": {
                "version": FFMPEG_VERSION,
                "hash": calc_dir_hash(OUTPUT_DIR / "ffmpeg") if (OUTPUT_DIR / "ffmpeg").exists() else ""
            },
            "models": {
                "version": WHISPER_MODEL if bool(PACKAGE_PROFILE.get("include_models", True)) else "disabled",
                "hash": calc_dir_hash(OUTPUT_DIR / "models") if (OUTPUT_DIR / "models").exists() else ""
            },
            "docs": {
                "version": "1.0.0",
                "hash": calc_dir_hash(OUTPUT_DIR / "docs") if (OUTPUT_DIR / "docs").exists() else ""
            },
            "ui": {
                "version": "1.0.0",
                "hash": calc_dir_hash(OUTPUT_DIR / "monitor_ui") if (OUTPUT_DIR / "monitor_ui").exists() else ""
            }
        }
    }
    _write_json(OUTPUT_DIR / "version.json", version_info)
    _write_json(PROJECT_ROOT / "service_version.json", version_info)
    if (OUTPUT_DIR / "core").exists():
        _write_json(OUTPUT_DIR / "core" / "service_version.json", version_info)
    log("版本信息创建完成")


def step_create_readme():
    """创建使用说明"""
    log("创建使用说明...")
    
    readme = OUTPUT_DIR / "README.txt"
    feature_lines = [
        f"- 下载：{'支持' if PACKAGE_PROFILE['capabilities']['download'] else '不支持'}",
        f"- 转码：{'支持' if PACKAGE_PROFILE['capabilities']['transcode'] else '不支持'}",
        f"- 语音转写：{'支持' if PACKAGE_PROFILE['capabilities']['speech'] else '不支持'}",
        f"- 字幕挂载：{'支持' if PACKAGE_PROFILE['capabilities']['subtitle'] else '不支持'}",
        f"- 视频分析：{'支持' if PACKAGE_PROFILE['capabilities']['analysis'] else '不支持'}",
    ]
    readme.write_text(f'''EdgeService 边缘服务客户端
==========================

当前打包模式：{PACKAGE_PROFILE['cli_mode']} ({PACKAGE_PROFILE['display_name']})
功能范围：
{chr(10).join(feature_lines)}

使用说明：

1. 首次使用
   - 编辑 config.json，填写服务器地址和校区编码
   - 双击 start.bat 启动服务

2. 配置说明
   - server.address: 服务端API地址
   - server.campusCode: 校区编码
   - server.serverId: 边缘节点ID
   - local.bindPort: 本地服务端口（默认18080）
   - local.downloadPath: 视频下载目录
   - streamProxy.enableStreamProxy: 是否启用实时流代理
   - streamProxy.publicBaseUrl: 完整公网访问基础地址，优先级最高
   - streamProxy.publicHost: 公网 IP 或域名
   - streamProxy.publicPort: 公网映射端口
   - streamProxy.publicScheme: 公网访问协议（http/https）

3. 访问管理界面
   - 启动后浏览器自动打开 http://<本机真实IP>:18080
   - 或手动访问 http://<本机真实IP>:18080

4. 目录说明
   - core/: 核心程序
   - sdk/: 海康SDK
   - ffmpeg/: 视频处理工具
   - models/: AI模型（仅全量模式包含）
   - docs/: 配置文件
   - output/: 运行时数据

5. 常见问题
   - 如果GPU不可用，程序会自动使用CPU模式
   - 首次运行可能需要较长时间加载模型

技术支持：请联系系统管理员
''', encoding='utf-8')
    
    log("使用说明创建完成")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="EdgeService打包脚本")
    parser.add_argument("--mode", choices=["mode1", "mode2"], default="mode1", help="mode1=全量包(EdgeServiceClientAll)，mode2=轻量包(EdgeServiceClient)")
    parser.add_argument("--skip-model", action="store_true", help="跳过模型下载")
    parser.add_argument("--skip-ffmpeg", action="store_true", help="跳过FFmpeg下载")
    parser.add_argument("--skip-pyinstaller", action="store_true", help="跳过PyInstaller打包")
    args = parser.parse_args()
    global PACKAGE_PROFILE, OUTPUT_DIR
    PACKAGE_PROFILE = dict(PACKAGE_PROFILES[str(args.mode)])
    OUTPUT_DIR = DIST_DIR / str(PACKAGE_PROFILE["output_name"])
    
    log("=" * 50)
    log("EdgeService 客户端打包")
    log(f"打包模式: {PACKAGE_PROFILE['cli_mode']} -> {PACKAGE_PROFILE['display_name']}")
    log("=" * 50)
    
    try:
        step_clean()
        
        version_info = step_prepare_service_version()
        
        if not args.skip_pyinstaller:
            step_pyinstaller()
        
        step_copy_sdk()
        step_copy_docs()
        step_copy_ui()
        step_download_ffmpeg(args.skip_ffmpeg)
        step_download_model(args.skip_model)
        step_create_launcher()
        step_create_version(version_info)
        step_create_readme()
        
        log("=" * 50)
        log(f"打包完成！输出目录: {OUTPUT_DIR}")
        log(f"版本号: {version_info['version']}")
        log("=" * 50)
        
        # 统计大小
        total_size = sum(f.stat().st_size for f in OUTPUT_DIR.rglob("*") if f.is_file())
        log(f"总大小: {total_size / 1024 / 1024 / 1024:.2f} GB")
        
    except Exception as e:
        log(f"打包失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
