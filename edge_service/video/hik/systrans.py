from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path
from typing import Callable


SYSTRANS_OK = 0
TRANS_SYSTEM_MPEG4 = 0x5
DEFAULT_PACK_SIZE = 5 * 1024
DEFAULT_SDK_DIR = Path(__file__).resolve().parents[2] / "sdk" / "systrans"
LEGACY_DEFAULT_SDK_DIR = Path(__file__).resolve().parents[2] / "sdk" / "HCNetSDKCom"


class SYS_TRANS_PARA(ctypes.Structure):
    _fields_ = [
        ("pSrcInfo", ctypes.c_void_p),
        ("dwSrcInfoLen", ctypes.c_uint32),
        ("enTgtType", ctypes.c_uint32),
        ("dwTgtPackSize", ctypes.c_uint32),
    ]


class SystemTransformError(RuntimeError):
    pass


def resolve_systrans_sdk_dir(sdk_dir: Path | str | None = None) -> Path:
    if sdk_dir:
        candidate = Path(sdk_dir).resolve()
        if (candidate / "SystemTransform.dll").exists():
            return candidate
        nested = candidate / "HCNetSDKCom"
        if (nested / "SystemTransform.dll").exists():
            return nested
        return candidate
    env_dir = str(os.getenv("EDGE_HIK_SYSTRANS_SDK_DIR") or "").strip() or str(os.getenv("EDGE_HIK_SDK_DIR") or "").strip()
    if env_dir:
        candidate = Path(env_dir).resolve()
        if (candidate / "SystemTransform.dll").exists():
            return candidate
        systrans_nested = candidate / "systrans"
        if (systrans_nested / "SystemTransform.dll").exists():
            return systrans_nested
        nested = candidate / "HCNetSDKCom"
        if (nested / "SystemTransform.dll").exists():
            return nested
        return candidate
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            exe_dir.parent / "sdk" / "systrans",
            exe_dir.parent / "sdk" / "HCNetSDKCom",
            exe_dir / "sdk" / "systrans",
            exe_dir / "sdk" / "HCNetSDKCom",
            Path(getattr(sys, "_MEIPASS", exe_dir)) / "sdk" / "systrans",
            Path(getattr(sys, "_MEIPASS", exe_dir)) / "sdk" / "HCNetSDKCom",
            Path(getattr(sys, "_MEIPASS", exe_dir)) / "edge_service" / "sdk" / "HCNetSDKCom",
        ])
    candidates.append(DEFAULT_SDK_DIR)
    candidates.append(LEGACY_DEFAULT_SDK_DIR)
    for candidate in candidates:
        if (candidate / "SystemTransform.dll").exists():
            return candidate.resolve()
    return candidates[0].resolve()


class SystemTransformClient:
    def __init__(self, sdk_dir: Path | str | None = None) -> None:
        self.sdk_dir = resolve_systrans_sdk_dir(sdk_dir)
        if not self.sdk_dir.exists():
            raise FileNotFoundError(f"SDK目录不存在: {self.sdk_dir}")
        self._dll_dir_handle = None
        if hasattr(os, "add_dll_directory"):
            self._dll_dir_handle = os.add_dll_directory(str(self.sdk_dir))
        self.dll = ctypes.WinDLL(str(self.sdk_dir / "SystemTransform.dll"))
        self._bind()

    def _bind(self) -> None:
        self.dll.SYSTRANS_Create.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(SYS_TRANS_PARA)]
        self.dll.SYSTRANS_Create.restype = ctypes.c_int
        self.dll.SYSTRANS_Start.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        self.dll.SYSTRANS_Start.restype = ctypes.c_int
        self.dll.SYSTRANS_GetTransPercent.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.dll.SYSTRANS_GetTransPercent.restype = ctypes.c_int
        self.dll.SYSTRANS_Stop.argtypes = [ctypes.c_void_p]
        self.dll.SYSTRANS_Stop.restype = ctypes.c_int
        self.dll.SYSTRANS_Release.argtypes = [ctypes.c_void_p]
        self.dll.SYSTRANS_Release.restype = ctypes.c_int
        if hasattr(self.dll, "SYSTRANS_GetVersion"):
            self.dll.SYSTRANS_GetVersion.argtypes = []
            self.dll.SYSTRANS_GetVersion.restype = ctypes.c_int

    def get_version(self) -> int | None:
        if not hasattr(self.dll, "SYSTRANS_GetVersion"):
            return None
        try:
            return int(self.dll.SYSTRANS_GetVersion())
        except Exception:
            return None

    def create(self, *, target_type: int = TRANS_SYSTEM_MPEG4, target_pack_size: int = DEFAULT_PACK_SIZE) -> ctypes.c_void_p:
        handle = ctypes.c_void_p()
        para = SYS_TRANS_PARA()
        para.pSrcInfo = None
        para.dwSrcInfoLen = 0
        para.enTgtType = int(target_type)
        para.dwTgtPackSize = int(target_pack_size)
        ret = int(self.dll.SYSTRANS_Create(ctypes.byref(handle), ctypes.byref(para)))
        if ret != SYSTRANS_OK:
            raise SystemTransformError(f"SYSTRANS_Create failed: {ret}")
        return handle

    def start(self, handle: ctypes.c_void_p, src_path: Path, dst_path: Path) -> None:
        src = str(src_path).encode("mbcs")
        dst = str(dst_path).encode("mbcs")
        ret = int(self.dll.SYSTRANS_Start(handle, src, dst))
        if ret != SYSTRANS_OK:
            raise SystemTransformError(f"SYSTRANS_Start failed: {ret}")

    def get_percent(self, handle: ctypes.c_void_p) -> int:
        percent = ctypes.c_uint32(0)
        ret = int(self.dll.SYSTRANS_GetTransPercent(handle, ctypes.byref(percent)))
        if ret != SYSTRANS_OK:
            raise SystemTransformError(f"SYSTRANS_GetTransPercent failed: {ret}")
        return int(percent.value)

    def stop(self, handle: ctypes.c_void_p) -> int:
        return int(self.dll.SYSTRANS_Stop(handle))

    def release(self, handle: ctypes.c_void_p) -> int:
        return int(self.dll.SYSTRANS_Release(handle))


def system_transform_file(
    input_path: Path | str,
    output_path: Path | str,
    *,
    sdk_dir: Path | str | None = None,
    target_type: int = TRANS_SYSTEM_MPEG4,
    target_pack_size: int = DEFAULT_PACK_SIZE,
    poll_interval_sec: float = 0.04,
    progress_callback: Callable[[int], None] | None = None,
    cancel_check: Callable[[], str | None] | None = None,
) -> dict[str, int | float | str | None]:
    src = Path(input_path).resolve()
    dst = Path(output_path).resolve()
    if not src.exists():
        raise FileNotFoundError(f"输入文件不存在: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    client = SystemTransformClient(sdk_dir)
    handle = None
    started = time.perf_counter()
    stop_ret = None
    release_ret = None
    try:
        version = client.get_version()
        handle = client.create(target_type=target_type, target_pack_size=target_pack_size)
        client.start(handle, src, dst)
        last_percent = -1
        while True:
            if cancel_check is not None:
                cancel_reason = cancel_check()
                if cancel_reason:
                    raise RuntimeError(cancel_reason)
            percent = client.get_percent(handle)
            if progress_callback is not None and percent != last_percent:
                progress_callback(percent)
            last_percent = percent
            if percent >= 100:
                break
            time.sleep(max(0.01, float(poll_interval_sec or 0.04)))
        elapsed = time.perf_counter() - started
        stop_ret = client.stop(handle)
        release_ret = client.release(handle)
        handle = None
        return {
            "sdk_version": version,
            "elapsed_sec": float(elapsed),
            "output_size": int(dst.stat().st_size if dst.exists() else 0),
            "stop_ret": int(stop_ret),
            "release_ret": int(release_ret),
            "output_path": str(dst),
        }
    finally:
        if handle is not None:
            try:
                stop_ret = client.stop(handle)
            except Exception:
                pass
            try:
                release_ret = client.release(handle)
            except Exception:
                pass
