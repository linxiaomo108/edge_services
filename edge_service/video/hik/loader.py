from __future__ import annotations

import os
from ctypes import WinDLL
from pathlib import Path


def resolve_sdk_dir() -> str:
    v = os.getenv("EDGE_HIK_SDK_DIR")
    if v and v.strip():
        return v
    import sys
    candidates: list[Path] = []
    if getattr(sys, 'frozen', False):
        # 打包后: sys.executable = .../EdgeServiceClient/core/EdgeServiceCore.exe
        base = Path(sys.executable).resolve().parent.parent
        candidates.extend([
            base / "sdk" / "download",
            base / "sdk",
        ])
    else:
        base = Path(__file__).resolve().parents[2]
        candidates.extend([
            base / "sdk" / "download",
            base / "sdk",
        ])
    for candidate in candidates:
        if (candidate / "HCNetSDK.dll").exists():
            return str(candidate)
    return str(candidates[0])


def load_hik_sdk(sdk_dir: str | None = None) -> WinDLL:
    d = sdk_dir or resolve_sdk_dir()
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(d)
    dll_path = Path(d) / "HCNetSDK.dll"
    if not dll_path.exists():
        raise FileNotFoundError(str(dll_path))
    return WinDLL(str(dll_path))
