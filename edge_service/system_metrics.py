from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass


@dataclass
class MetricsSnapshot:
    cpu_percent: float
    ram_percent: float
    disk_percent: float
    gpu_percent: float


def _clamp_pct(v: float) -> float:
    if v != v:
        return 0.0
    if v < 0:
        return 0.0
    if v > 100:
        return 100.0
    return float(v)


def _disk_percent(path: str) -> float:
    try:
        total, used, free = shutil.disk_usage(path)
        if total <= 0:
            return 0.0
        return _clamp_pct((used / total) * 100.0)
    except Exception:
        return 0.0


def _win_total_disk_percent() -> float:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        GetLogicalDrives = kernel32.GetLogicalDrives
        GetLogicalDrives.argtypes = []
        GetLogicalDrives.restype = wintypes.DWORD

        GetDriveTypeW = kernel32.GetDriveTypeW
        GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        GetDriveTypeW.restype = wintypes.UINT

        GetDiskFreeSpaceExW = kernel32.GetDiskFreeSpaceExW
        GetDiskFreeSpaceExW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        GetDiskFreeSpaceExW.restype = wintypes.BOOL

        DRIVE_FIXED = 3
        mask = int(GetLogicalDrives())
        if mask == 0:
            return 0.0

        total_sum = 0
        used_sum = 0
        for i in range(26):
            if not (mask & (1 << i)):
                continue
            root = f"{chr(ord('A') + i)}:\\"
            if int(GetDriveTypeW(root)) != DRIVE_FIXED:
                continue
            free_avail = ctypes.c_ulonglong(0)
            total = ctypes.c_ulonglong(0)
            free_total = ctypes.c_ulonglong(0)
            ok = bool(GetDiskFreeSpaceExW(root, ctypes.byref(free_avail), ctypes.byref(total), ctypes.byref(free_total)))
            if not ok:
                continue
            t = int(total.value)
            f = int(free_total.value)
            if t <= 0 or f < 0 or f > t:
                continue
            total_sum += t
            used_sum += (t - f)
        if total_sum <= 0:
            return 0.0
        return _clamp_pct((used_sum / total_sum) * 100.0)
    except Exception:
        return 0.0


def _win_cpu_percent_wmic() -> float | None:
    try:
        windir = os.environ.get("WINDIR") or "C:\\Windows"
        candidates = [
            "wmic",
            os.path.join(windir, "System32", "wbem", "WMIC.exe"),
            os.path.join(windir, "System32", "wbem", "wmic.exe"),
        ]
        p = None
        for exe in candidates:
            try:
                p = subprocess.run(
                    [exe, "cpu", "get", "LoadPercentage", "/value"],
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                    check=False,
                )
                break
            except FileNotFoundError:
                continue
        if p is None:
            return None
        if p.returncode != 0:
            return None
        txt = (p.stdout or "").strip()
        for line in txt.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip().lower() != "loadpercentage":
                continue
            try:
                return _clamp_pct(float(v.strip()))
            except Exception:
                return None
        return None
    except Exception:
        return None


def _win_init_pdh_counter():
    return None


def _gpu_percent() -> float:
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        if p.returncode != 0:
            return 0.0
        lines = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
        vals: list[float] = []
        for ln in lines:
            try:
                vals.append(float(ln))
            except Exception:
                continue
        if not vals:
            return 0.0
        return _clamp_pct(sum(vals) / len(vals))
    except Exception:
        return 0.0


def _linux_cpu_times() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="ignore") as f:
            first = f.readline()
        parts = first.split()
        if not parts or parts[0] != "cpu":
            return None
        nums = [int(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        return total, idle
    except Exception:
        return None


def _linux_ram_percent() -> float:
    try:
        mem_total = None
        mem_avail = None
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1])
                if mem_total is not None and mem_avail is not None:
                    break
        if not mem_total or mem_total <= 0 or mem_avail is None:
            return 0.0
        used = mem_total - mem_avail
        return _clamp_pct((used / mem_total) * 100.0)
    except Exception:
        return 0.0


def _win_cpu_times() -> tuple[int, int] | None:
    try:
        import ctypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

        idle = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None
        def _to_int(ft: FILETIME) -> int:
            return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)

        idle_i = _to_int(idle)
        kernel_i = _to_int(kernel)
        user_i = _to_int(user)
        total_i = kernel_i + user_i
        return total_i, idle_i
    except Exception:
        return None


def _win_ram_percent() -> float:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint32),
                ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        if not ok or st.ullTotalPhys <= 0:
            return 0.0
        used = st.ullTotalPhys - st.ullAvailPhys
        return _clamp_pct((used / st.ullTotalPhys) * 100.0)
    except Exception:
        return 0.0


class SystemMetrics:
    def __init__(self, disk_path: str) -> None:
        p = disk_path or os.getcwd()
        try:
            p = os.path.abspath(p)
        except Exception:
            p = disk_path or os.getcwd()
        if sys.platform.startswith("win"):
            try:
                drive, _ = os.path.splitdrive(p)
                if drive:
                    p = drive + "\\"
            except Exception:
                pass
        self._disk_path = p
        self._lock = asyncio.Lock()
        self._prev_total: int | None = None
        self._prev_idle: int | None = None
        self._pdh = None
        self._last_snapshot: MetricsSnapshot | None = None
        self._last_at: float = 0.0

        total_idle = self._read_cpu_times()
        if total_idle is not None:
            self._prev_total, self._prev_idle = total_idle

    def _read_cpu_times(self) -> tuple[int, int] | None:
        if sys.platform.startswith("win"):
            return _win_cpu_times()
        return _linux_cpu_times()

    def _ram_percent(self) -> float:
        if sys.platform.startswith("win"):
            return _win_ram_percent()
        return _linux_ram_percent()

    async def snapshot(self) -> MetricsSnapshot:
        async with self._lock:
            now_m = time.monotonic()
            if self._last_snapshot is not None and (now_m - self._last_at) < 0.9:
                return self._last_snapshot

            cpu = 0.0
            if sys.platform.startswith("win"):
                now = self._read_cpu_times()
                if now is not None:
                    total, idle = now
                    if self._prev_total is not None and self._prev_idle is not None:
                        dt = total - self._prev_total
                        di = idle - self._prev_idle
                        if dt > 0:
                            cpu = _clamp_pct(((dt - di) / dt) * 100.0)
                        elif self._last_snapshot is not None:
                            cpu = float(self._last_snapshot.cpu_percent)
                    self._prev_total, self._prev_idle = total, idle
            else:
                now = self._read_cpu_times()
                if now is not None:
                    total, idle = now
                    if self._prev_total is not None and self._prev_idle is not None:
                        dt = total - self._prev_total
                        di = idle - self._prev_idle
                        if dt > 0:
                            cpu = _clamp_pct(((dt - di) / dt) * 100.0)
                    self._prev_total, self._prev_idle = total, idle

            ram = self._ram_percent()
            disk = _win_total_disk_percent() if sys.platform.startswith("win") else _disk_percent(self._disk_path)
            gpu = _gpu_percent()
            snap = MetricsSnapshot(cpu_percent=cpu, ram_percent=ram, disk_percent=disk, gpu_percent=gpu)
            self._last_snapshot = snap
            self._last_at = now_m
            return snap
