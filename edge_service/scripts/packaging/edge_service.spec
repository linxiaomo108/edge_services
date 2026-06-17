# -*- mode: python ; coding: utf-8 -*-
"""
EdgeService PyInstaller Spec文件
用于打包边缘服务客户端
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# 项目根目录
PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, '..', '..'))
PACKAGE_MODE = str(os.environ.get('EDGE_PACKAGE_MODE') or 'all').strip().lower() or 'all'
IS_LITE_MODE = PACKAGE_MODE == 'lite'

# 收集依赖
hiddenimports = [
    # FastAPI相关
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    # OpenCV
    'cv2',
    'webrtcvad',
    'numpy',
    'pydantic',
    'httpx',
    'anyio',
    'starlette',
]
if not IS_LITE_MODE:
    hiddenimports += [
        'faster_whisper',
        'ctranslate2',
        'huggingface_hub',
        'tokenizers',
        'jieba',
        'opencc',
        'zhconv',
    ]

# 收集数据文件
datas = [
    # 前端页面
    (os.path.join(PROJECT_ROOT, 'monitor_ui'), 'monitor_ui'),
    # 配置文件
    (os.path.join(PROJECT_ROOT, 'docs', 'correction_dict.json'), 'docs'),
    (os.path.join(PROJECT_ROOT, 'docs', 'hallucination_blacklist.json'), 'docs'),
    # 版本信息
    (os.path.join(PROJECT_ROOT, 'service_version.json'), '.'),
    (os.path.join(PROJECT_ROOT, 'tasks', 'templates'), 'edge_service/tasks/templates'),
]
download_sdk_dir = os.path.join(PROJECT_ROOT, 'sdk', 'download')
if os.path.exists(download_sdk_dir):
    datas.append((download_sdk_dir, 'sdk/download'))
systrans_sdk_dir = os.path.join(PROJECT_ROOT, 'sdk', 'systrans')
if os.path.exists(systrans_sdk_dir):
    datas.append((systrans_sdk_dir, 'sdk/systrans'))
if not IS_LITE_MODE:
    datas += collect_data_files('faster_whisper')

# 排除不需要的模块（减小体积）
excludes = [
    'tkinter',
    'matplotlib',
    'scipy',
    'pandas',
    'IPython',
    'jupyter',
    'notebook',
    'pytest',
    'sphinx',
]
if IS_LITE_MODE:
    excludes += [
        'edge_service.tasks.speech',
        'edge_service.tasks.subtitle',
        'edge_service.asr',
        'faster_whisper',
        'ctranslate2',
        'huggingface_hub',
        'tokenizers',
        'jieba',
        'opencc',
        'zhconv',
    ]

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'run_edge_service.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EdgeServiceCore',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # 显示控制台窗口，便于查看日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(PROJECT_ROOT, 'assets', 'icon.ico') if os.path.exists(os.path.join(PROJECT_ROOT, 'assets', 'icon.ico')) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='EdgeServiceCore',
)
