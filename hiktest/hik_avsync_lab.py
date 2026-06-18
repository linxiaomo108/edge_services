from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import shutil
import subprocess
import time
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from hik_sdk_standalone import close_download_session, download_by_time_with_session, open_download_session


# =============================================================================
# 实验参数集中配置区
# =============================================================================
# 运行模式：
#   "online" = 在线连接 NVR 下载指定时间段，再执行声画同步实验。
#   "local"  = 读取本地已有源分段，只执行修复、转封装、合并实验。
LAB_MODE = "local"

# 在线模式参数：需要临时验证下载时，优先改这里。
ONLINE_DEVICE_IP = "117.144.207.90"  # NVR 设备地址；可以是内网 IP，也可以是公网/专线映射后的 IP。
ONLINE_DEVICE_PORT = 18081  # NVR SDK 登录端口；对应海康设备的服务端口，通常内网是 8000，映射后填映射端口，不是视频通道号。
ONLINE_USERNAME = "admin"  # NVR 登录账号；用于海康 SDK 登录设备。
ONLINE_PASSWORD = "wlyn6688"  # NVR 登录密码；用于海康 SDK 登录设备。
ONLINE_WEB_CHANNEL = 1  # 业务/网页通道号；表示要下载哪个摄像头通道，例如 NVR 回放页面显示的“通道号: 1”。
ONLINE_START_TIME = "2026-06-13 08:30:00"  # 下载开始时间；格式为 YYYY-MM-DD HH:MM:SS，按 NVR 本地时间理解。
ONLINE_END_TIME = "2026-06-13 09:30:00"  # 下载结束时间；格式为 YYYY-MM-DD HH:MM:SS，必须晚于开始时间。
ONLINE_SEGMENT_MINUTES = 30  # 实验脚本切分下载的分钟数；08:30~09:30 且填 30 时，会下载 2 个 30 分钟分段。
ONLINE_LOCKED_SDK_CHANNEL = 33  # 固定使用的海康 SDK 通道号；这是摄像头通道，不是 NVR 端口。已确认映射关系时可填写，避免反复探测。
ONLINE_LOCKED_RECORD_TYPE = ""  # 固定录像类型；例如 "0xff"。为空表示按脚本/SDK逻辑自动尝试常见录像类型。
ONLINE_SDK_PROBE_SECONDS = 6  # 通道探测时每个候选下载观察的秒数；仅在未锁定或需要探测通道时使用。
ONLINE_DOWNLOAD_STALL_TIMEOUT_SEC = 180  # 下载卡住判定秒数；超过该时长没有进度则认为本段下载异常。

# 本地模式参数：已有源分段验证时，改这里。
LOCAL_EXISTING_RAW_DIR = r"E:\Videos\2026-06-06\nj"
LOCAL_RAW_GLOB = "*.mp4"
LOCAL_EXISTING_START_TIME = "2026-06-06 15:20:00"
LOCAL_SOURCE_LINK_MODE = "copy"  # hardlink 或 copy

# 实验输出和依赖目录。当前先保留 sdk/Dll 的重复文件，实验稳定后再做去重。
DEFAULT_OUTPUT_DIR = ""
DEFAULT_DOWNLOAD_SDK_DIR = str(Path(__file__).resolve().parent / "sdk" / "download")
DEFAULT_CONVERT_DLL_DIR = str(Path(__file__).resolve().parent / "Dll")
DEFAULT_CONVERSION_MODE = "format"
# 默认走浏览器轻量验证：只生成一套时间戳重置结果，并用 ffmpeg concat 做一次浏览器可播合并。
DEFAULT_FORMAT_VARIANT = "reset"
DEFAULT_MERGE_MODE = "ffmpeg_duration"
DEFAULT_MERGE_FASTSTART = False
DEFAULT_ANALYZE_DATA = False

LOG = logging.getLogger("hik_avsync_lab")
MERGE_READY_STREAM_GAP_MAX_SEC = float(os.getenv("HIK_LAB_MERGE_READY_STREAM_GAP_MAX_SEC", "0.5"))
MERGE_READY_DURATION_DELTA_MAX_SEC = float(os.getenv("HIK_LAB_MERGE_READY_DURATION_DELTA_MAX_SEC", "1.0"))


@dataclass(frozen=True)
class SegmentPlan:
    index: int
    start_time: datetime
    end_time: datetime
    raw_path: str
    converted_path: str
    converted_no_reset_path: str
    frame_log_path: str


@dataclass
class SegmentResult:
    index: int
    start_time: str
    end_time: str
    raw_path: str
    converted_path: str
    converted_no_reset_path: str
    frame_log_path: str
    download: dict[str, Any]
    raw_probe: dict[str, Any]
    converted_probe: dict[str, Any]
    converted_no_reset_probe: dict[str, Any]
    converted_method: str
    converted_no_reset_method: str
    errors: list[str]


class FCGlobalTime(ctypes.Structure):
    _fields_ = [
        ("sYear", ctypes.c_uint16),
        ("sMonth", ctypes.c_uint16),
        ("sDayOfWeek", ctypes.c_uint16),
        ("sDay", ctypes.c_uint16),
        ("sHour", ctypes.c_uint16),
        ("sMinute", ctypes.c_uint16),
        ("sSecond", ctypes.c_uint16),
        ("sMilliseconds", ctypes.c_uint16),
    ]


class FCDetailedCBInfo(ctypes.Structure):
    _fields_ = [
        ("nTrackIndex", ctypes.c_uint32),
        ("nFrameTypeOrBPS", ctypes.c_uint32),
        ("nWidthOrChannels", ctypes.c_uint32),
        ("nHeightOrSampleRate", ctypes.c_uint32),
        ("fFrameRate", ctypes.c_float),
        ("nAudioBitrate", ctypes.c_uint32),
        ("nFrameNum", ctypes.c_uint32),
        ("nTimeStamp", ctypes.c_uint32),
        ("stGlobalTime", FCGlobalTime),
        ("pData", ctypes.c_void_p),
        ("nDataLen", ctypes.c_uint32),
        ("bLastPacket", ctypes.c_bool),
        ("bFirstPacket", ctypes.c_bool),
        ("nDataType", ctypes.c_uint32),
        ("enSystemFormat", ctypes.c_int),
        ("enCodecType", ctypes.c_int),
        ("nReserved", ctypes.c_uint32 * 8),
    ]


FCDetailedCB = ctypes.CFUNCTYPE(None, ctypes.POINTER(FCDetailedCBInfo), ctypes.c_void_p)


class AnalyzePacketInfoEx(ctypes.Structure):
    _fields_ = [
        ("uWidth", ctypes.c_uint16),
        ("uHeight", ctypes.c_uint16),
        ("dwTimeStamp", ctypes.c_uint32),
        ("dwTimeStampHigh", ctypes.c_uint32),
        ("nYear", ctypes.c_uint32),
        ("nMonth", ctypes.c_uint32),
        ("nDay", ctypes.c_uint32),
        ("nHour", ctypes.c_uint32),
        ("nMinute", ctypes.c_uint32),
        ("nSecond", ctypes.c_uint32),
        ("nMillisecond", ctypes.c_uint32),
        ("dwFrameNum", ctypes.c_uint32),
        ("dwFrameRate", ctypes.c_uint32),
        ("dwFlag", ctypes.c_uint32),
        ("dwFilePos", ctypes.c_uint32),
        ("nPacketType", ctypes.c_uint32),
        ("dwPacketSize", ctypes.c_uint32),
        ("pPacketBuffer", ctypes.c_void_p),
        ("dwEncrypted", ctypes.c_uint32),
        ("dwPacketType", ctypes.c_uint32),
        ("dwEncryptArith", ctypes.c_uint32),
        ("dwEncryptRound", ctypes.c_uint32),
        ("dwKeyLen", ctypes.c_uint32),
        ("dwEncryptType", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint32 * 6),
    ]


class AnalyzeDataClient:
    PACKET_VIDEO_I = 1
    PACKET_VIDEO_B = 2
    PACKET_VIDEO_P = 3
    PACKET_AUDIO = 10

    def __init__(self, dll_dir: Path) -> None:
        self.dll_dir = Path(dll_dir).resolve()
        if hasattr(os, "add_dll_directory"):
            self._dll_dir_handle = os.add_dll_directory(str(self.dll_dir))
        else:
            self._dll_dir_handle = None
        dll_path = self.dll_dir / "AnalyzeData.dll"
        if not dll_path.exists():
            raise FileNotFoundError(f"缺少 AnalyzeData.dll: {dll_path}")
        self.dll = ctypes.WinDLL(str(dll_path))
        self._bind()

    def _bind(self) -> None:
        self.dll.HIKANA_CreateHandleByPath.argtypes = [ctypes.c_ulong, ctypes.c_char_p]
        self.dll.HIKANA_CreateHandleByPath.restype = ctypes.c_void_p
        self.dll.HIKANA_CreateStreamEx.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
        self.dll.HIKANA_CreateStreamEx.restype = ctypes.c_void_p
        self.dll.HIKANA_InputData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        self.dll.HIKANA_InputData.restype = ctypes.c_int
        self.dll.HIKANA_Destroy.argtypes = [ctypes.c_void_p]
        self.dll.HIKANA_Destroy.restype = None
        self.dll.HIKANA_GetOnePacketEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(AnalyzePacketInfoEx)]
        self.dll.HIKANA_GetOnePacketEx.restype = ctypes.c_int
        self.dll.HIKANA_GetLastErrorH.argtypes = [ctypes.c_void_p]
        self.dll.HIKANA_GetLastErrorH.restype = ctypes.c_uint32
        self.dll.HIKANA_GetVersion.argtypes = []
        self.dll.HIKANA_GetVersion.restype = ctypes.c_int

    def version(self) -> int:
        return int(self.dll.HIKANA_GetVersion())

    @staticmethod
    def _packet_time(pkt: AnalyzePacketInfoEx) -> str | None:
        if int(pkt.nYear) <= 0:
            return None
        return f"{int(pkt.nYear):04d}-{int(pkt.nMonth):02d}-{int(pkt.nDay):02d} {int(pkt.nHour):02d}:{int(pkt.nMinute):02d}:{int(pkt.nSecond):02d}.{int(pkt.nMillisecond):03d}"

    @staticmethod
    def _packet_summary(pkt: AnalyzePacketInfoEx) -> dict[str, Any]:
        return {
            "packet_type": int(pkt.nPacketType),
            "timestamp": int(pkt.dwTimeStamp) | (int(pkt.dwTimeStampHigh) << 32),
            "abs_time": AnalyzeDataClient._packet_time(pkt),
            "frame_num": int(pkt.dwFrameNum),
            "frame_rate": int(pkt.dwFrameRate),
            "width": int(pkt.uWidth),
            "height": int(pkt.uHeight),
            "file_pos": int(pkt.dwFilePos),
            "packet_size": int(pkt.dwPacketSize),
        }

    def analyze_file(self, input_path: Path, *, max_packets: int = 200000, tail_keep: int = 5) -> dict[str, Any]:
        handle = ctypes.c_void_p(self.dll.HIKANA_CreateHandleByPath(0, str(input_path).encode("mbcs")))
        stream_mode = False
        source_error = None
        file_obj = None
        if not handle.value:
            source_error = "HIKANA_CreateHandleByPath returned NULL"
            file_obj = input_path.open("rb")
            header = file_obj.read(40)
            header_buf = ctypes.create_string_buffer(header)
            handle = ctypes.c_void_p(self.dll.HIKANA_CreateStreamEx(len(header), ctypes.cast(header_buf, ctypes.c_void_p)))
            stream_mode = True
            if not handle.value:
                file_obj.close()
                return {"ok": False, "error": "HIKANA_CreateStreamEx returned NULL", "source_error": source_error, "path": str(input_path)}
        counts = {"video": 0, "audio": 0, "private": 0, "other": 0}
        first: dict[str, Any] = {}
        last: dict[str, Any] = {}
        samples: list[dict[str, Any]] = []
        last_error = 0
        input_errors: list[dict[str, Any]] = []
        input_bytes = 0
        try:
            idx = 0
            input_eof = False
            while idx < max_packets:
                pkt = AnalyzePacketInfoEx()
                ret = int(self.dll.HIKANA_GetOnePacketEx(handle, ctypes.byref(pkt)))
                if ret != 0:
                    last_error = int(self.dll.HIKANA_GetLastErrorH(handle))
                    if stream_mode and not input_eof and last_error in (8, 9):
                        data = file_obj.read(256 * 1024) if file_obj is not None else b""
                        if data:
                            data_buf = ctypes.create_string_buffer(data)
                            input_ret = int(self.dll.HIKANA_InputData(handle, ctypes.cast(data_buf, ctypes.c_void_p), len(data)))
                            input_bytes += len(data)
                            if input_ret != 0:
                                last_error = int(self.dll.HIKANA_GetLastErrorH(handle))
                                input_errors.append({"ret": input_ret, "last_error": last_error, "input_bytes": input_bytes})
                                break
                            continue
                        input_eof = True
                    break
                packet_type = int(pkt.nPacketType)
                if packet_type in (self.PACKET_VIDEO_I, self.PACKET_VIDEO_B, self.PACKET_VIDEO_P):
                    kind = "video"
                elif packet_type == self.PACKET_AUDIO:
                    kind = "audio"
                elif packet_type == 11:
                    kind = "private"
                else:
                    kind = "other"
                counts[kind] += 1
                summary = self._packet_summary(pkt)
                if kind not in first:
                    first[kind] = summary
                last[kind] = summary
                if len(samples) < tail_keep:
                    samples.append(summary)
                elif tail_keep > 0:
                    samples[idx % tail_keep] = summary
                idx += 1
            return {
                "ok": True,
                "path": str(input_path),
                "version": self.version(),
                "mode": "stream" if stream_mode else "path",
                "source_error": source_error,
                "counts": counts,
                "first": first,
                "last": last,
                "last_error": last_error,
                "input_bytes": input_bytes,
                "input_errors": input_errors,
                "max_packets": max_packets,
                "samples": samples,
            }
        finally:
            if file_obj is not None:
                file_obj.close()
            self.dll.HIKANA_Destroy(handle)


class FCVideoInfo(ctypes.Structure):
    _fields_ = [
        ("enCodec", ctypes.c_int),
        ("nTrackId", ctypes.c_uint32),
        ("nBitRate", ctypes.c_uint32),
        ("fFrameRate", ctypes.c_float),
        ("nWidth", ctypes.c_uint16),
        ("nHeight", ctypes.c_uint16),
    ]


class FCAudioInfo(ctypes.Structure):
    _fields_ = [
        ("enCodec", ctypes.c_int),
        ("nTrackId", ctypes.c_uint32),
        ("nChannels", ctypes.c_uint16),
        ("nBitsPerSample", ctypes.c_uint16),
        ("nSamplesRate", ctypes.c_uint32),
        ("nBitRate", ctypes.c_uint32),
    ]


class FCPrivtInfo(ctypes.Structure):
    _fields_ = [
        ("nType", ctypes.c_uint32),
        ("nTrackId", ctypes.c_uint32),
    ]


class FCMediaInfo(ctypes.Structure):
    _fields_ = [
        ("enSystemFormat", ctypes.c_int),
        ("nVideoStreamCount", ctypes.c_uint32),
        ("nAudioStreamCount", ctypes.c_uint32),
        ("nPrivtStreamCount", ctypes.c_uint32),
        ("stVideoInfo", FCVideoInfo * 8),
        ("stAudioInfo", FCAudioInfo * 8),
        ("stPrivtInfo", FCPrivtInfo * 8),
        ("nStreamFlag", ctypes.c_uint32),
        ("nReserved", ctypes.c_uint32 * 3),
    ]


class FormatConversionClient:
    def __init__(self, dll_dir: Path) -> None:
        self.dll_dir = Path(dll_dir).resolve()
        if not self.dll_dir.exists():
            raise FileNotFoundError(f"FormatConversion DLL目录不存在: {self.dll_dir}")
        self._dll_dir_handle = None
        if hasattr(os, "add_dll_directory"):
            self._dll_dir_handle = os.add_dll_directory(str(self.dll_dir))
        for name in ("hpr.dll", "hlog.dll", "welsenc.dll", "HWEncode.dll", "HWTranscode.dll"):
            path = self.dll_dir / name
            if path.exists():
                ctypes.WinDLL(str(path))
        dll_path = self.dll_dir / "FormatConversion.dll"
        if not dll_path.exists():
            raise FileNotFoundError(f"缺少 FormatConversion.dll: {dll_path}")
        self.dll = ctypes.WinDLL(str(dll_path))
        self._bind()

    def _bind(self) -> None:
        self.dll.FC_GetSDKVersion.argtypes = []
        self.dll.FC_GetSDKVersion.restype = ctypes.c_uint32
        self.dll.FC_CreateHandle.argtypes = []
        self.dll.FC_CreateHandle.restype = ctypes.c_void_p
        self.dll.FC_DestroyHandle.argtypes = [ctypes.c_void_p]
        self.dll.FC_DestroyHandle.restype = ctypes.c_int
        self.dll.FC_GetFileInfo.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(FCMediaInfo)]
        self.dll.FC_GetFileInfo.restype = ctypes.c_int
        self.dll.FC_SetTargetMediaInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(FCMediaInfo)]
        self.dll.FC_SetTargetMediaInfo.restype = ctypes.c_int
        self.dll.FC_Start.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        self.dll.FC_Start.restype = ctypes.c_int
        self.dll.FC_Stop.argtypes = [ctypes.c_void_p]
        self.dll.FC_Stop.restype = ctypes.c_int
        self.dll.FC_GetProgress.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)]
        self.dll.FC_GetProgress.restype = ctypes.c_int
        self.dll.FC_SetGlobalTime.argtypes = [ctypes.c_void_p, ctypes.POINTER(FCGlobalTime), ctypes.c_uint32]
        self.dll.FC_SetGlobalTime.restype = ctypes.c_int
        self.dll.FC_ReSetTimeStamp.argtypes = [ctypes.c_void_p]
        self.dll.FC_ReSetTimeStamp.restype = ctypes.c_int
        self.dll.FC_RegisterDetailedCB.argtypes = [ctypes.c_void_p, FCDetailedCB, ctypes.c_void_p]
        self.dll.FC_RegisterDetailedCB.restype = ctypes.c_int

    def version(self) -> int:
        return int(self.dll.FC_GetSDKVersion())

    def get_file_info(self, input_path: Path) -> tuple[int, FCMediaInfo]:
        handle = ctypes.c_void_p(self.dll.FC_CreateHandle())
        if not handle.value:
            raise RuntimeError("FC_CreateHandle returned NULL")
        info = FCMediaInfo()
        try:
            ret = int(self.dll.FC_GetFileInfo(handle, str(input_path).encode("mbcs"), ctypes.byref(info)))
            return ret, info
        finally:
            try:
                self.dll.FC_DestroyHandle(handle)
            except Exception:
                pass

    @staticmethod
    def media_info_to_dict(info: FCMediaInfo) -> dict[str, Any]:
        return {
            "enSystemFormat": int(info.enSystemFormat),
            "nVideoStreamCount": int(info.nVideoStreamCount),
            "nAudioStreamCount": int(info.nAudioStreamCount),
            "nPrivtStreamCount": int(info.nPrivtStreamCount),
            "nStreamFlag": int(info.nStreamFlag),
            "videos": [
                {
                    "enCodec": int(info.stVideoInfo[i].enCodec),
                    "nTrackId": int(info.stVideoInfo[i].nTrackId),
                    "nBitRate": int(info.stVideoInfo[i].nBitRate),
                    "fFrameRate": float(info.stVideoInfo[i].fFrameRate),
                    "nWidth": int(info.stVideoInfo[i].nWidth),
                    "nHeight": int(info.stVideoInfo[i].nHeight),
                }
                for i in range(min(8, int(info.nVideoStreamCount)))
            ],
            "audios": [
                {
                    "enCodec": int(info.stAudioInfo[i].enCodec),
                    "nTrackId": int(info.stAudioInfo[i].nTrackId),
                    "nChannels": int(info.stAudioInfo[i].nChannels),
                    "nBitsPerSample": int(info.stAudioInfo[i].nBitsPerSample),
                    "nSamplesRate": int(info.stAudioInfo[i].nSamplesRate),
                    "nBitRate": int(info.stAudioInfo[i].nBitRate),
                }
                for i in range(min(8, int(info.nAudioStreamCount)))
            ],
        }

    def convert(
        self,
        input_path: Path,
        output_path: Path,
        segment_start: datetime,
        *,
        reset_timestamp: bool,
        timeout_sec: float,
        frame_log_path: Path | None = None,
        max_frame_log_entries: int = 2000,
        set_target_mp4: bool = False,
    ) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        handle = ctypes.c_void_p(self.dll.FC_CreateHandle())
        if not handle.value:
            raise RuntimeError("FC_CreateHandle returned NULL")
        frame_log: list[dict[str, Any]] = []

        @FCDetailedCB
        def frame_cb(info_ptr: ctypes.POINTER(FCDetailedCBInfo), user: ctypes.c_void_p) -> None:
            if len(frame_log) >= max_frame_log_entries:
                return
            try:
                info = info_ptr.contents
                gt = info.stGlobalTime
                frame_log.append(
                    {
                        "track": int(info.nTrackIndex),
                        "data_type": int(info.nDataType),
                        "frame_num": int(info.nFrameNum),
                        "pts_ms": int(info.nTimeStamp),
                        "abs_time": f"{gt.sYear:04d}-{gt.sMonth:02d}-{gt.sDay:02d} {gt.sHour:02d}:{gt.sMinute:02d}:{gt.sSecond:02d}.{gt.sMilliseconds:03d}",
                    }
                )
            except Exception:
                return

        started = False
        last_progress = 0.0
        started_at = time.monotonic()
        progress_ratio = 0.0
        try:
            ret = int(self.dll.FC_RegisterDetailedCB(handle, frame_cb, None))
            if ret != 0:
                LOG.warning("FC_RegisterDetailedCB ret=%s input=%s", ret, input_path)
            source_info = FCMediaInfo()
            get_info_ret = int(self.dll.FC_GetFileInfo(handle, str(input_path).encode("mbcs"), ctypes.byref(source_info)))
            if get_info_ret != 0:
                LOG.warning("FC_GetFileInfo ret=%s input=%s", get_info_ret, input_path)
            target_info_dict = {}
            set_target_ret = None
            if set_target_mp4 and get_info_ret == 0:
                target_info = source_info
                target_info.enSystemFormat = 5
                set_target_ret = int(self.dll.FC_SetTargetMediaInfo(handle, ctypes.byref(target_info)))
                target_info_dict = self.media_info_to_dict(target_info)
                if set_target_ret != 0:
                    LOG.warning("FC_SetTargetMediaInfo ret=%s input=%s", set_target_ret, input_path)
            global_time = FCGlobalTime(
                sYear=segment_start.year,
                sMonth=segment_start.month,
                sDayOfWeek=0,
                sDay=segment_start.day,
                sHour=segment_start.hour,
                sMinute=segment_start.minute,
                sSecond=segment_start.second,
                sMilliseconds=segment_start.microsecond // 1000,
            )
            ret = int(self.dll.FC_SetGlobalTime(handle, ctypes.byref(global_time), 1))
            if ret != 0:
                LOG.warning("FC_SetGlobalTime ret=%s input=%s", ret, input_path)
            if reset_timestamp:
                ret = int(self.dll.FC_ReSetTimeStamp(handle))
                if ret != 0:
                    LOG.warning("FC_ReSetTimeStamp ret=%s input=%s", ret, input_path)
            ret = int(self.dll.FC_Start(handle, str(input_path).encode("mbcs"), str(output_path).encode("mbcs")))
            if ret != 0:
                raise RuntimeError(f"FC_Start failed ret={ret}")
            started = True
            deadline = time.monotonic() + float(timeout_sec)
            completed_by = "progress"
            last_size = -1
            stable_count = 0
            while True:
                progress = ctypes.c_float(0.0)
                ret = int(self.dll.FC_GetProgress(handle, ctypes.byref(progress)))
                if ret != 0:
                    LOG.warning("FC_GetProgress ret=%s input=%s", ret, input_path)
                last_progress = float(progress.value)
                # Some FormatConversion builds report progress as 0.0~1.0,
                # others as 0~100. Normalize both forms so we can stop as soon
                # as conversion is genuinely complete.
                progress_ratio = last_progress / 100.0 if last_progress > 1.0 else last_progress
                if progress_ratio >= 0.999:
                    completed_by = "progress_done"
                    break
                current_size = output_path.stat().st_size if output_path.exists() else 0
                if current_size > 0 and current_size == last_size:
                    stable_count += 1
                else:
                    stable_count = 0
                    last_size = current_size
                if stable_count >= 10 and probe_media(output_path).get("format_duration"):
                    completed_by = "output_stable"
                    break
                if time.monotonic() > deadline:
                    try:
                        self.dll.FC_Stop(handle)
                        started = False
                    except Exception:
                        pass
                    if output_path.exists() and output_path.stat().st_size > 0 and probe_media(output_path).get("format_duration"):
                        completed_by = "timeout_output_probe_ok"
                        break
                    raise TimeoutError(f"FormatConversion timeout progress={last_progress:.2f} input={input_path}")
                time.sleep(0.3)
            if started:
                try:
                    self.dll.FC_Stop(handle)
                    started = False
                except Exception:
                    pass
            # FC reports completion slightly before the target file is fully
            # finalized on disk. Give it a short settle window so a valid output
            # is not misclassified as missing.
            settle_deadline = time.monotonic() + 15.0
            while time.monotonic() < settle_deadline:
                if output_path.exists() and output_path.stat().st_size > 0:
                    break
                time.sleep(0.2)
            if not output_path.exists() or output_path.stat().st_size <= 0:
                raise RuntimeError(f"FormatConversion output missing or empty: {output_path}")
            if frame_log_path is not None:
                frame_log_path.parent.mkdir(parents=True, exist_ok=True)
                frame_log_path.write_text(json.dumps(frame_log, ensure_ascii=False, indent=2), encoding="utf-8")
            return {
                "ok": True,
                "progress": last_progress,
                "progress_ratio": progress_ratio,
                "completed_by": completed_by,
                "elapsed_sec": time.monotonic() - started_at,
                "frame_log_entries": len(frame_log),
                "get_file_info_ret": get_info_ret,
                "source_info": self.media_info_to_dict(source_info),
                "set_target_ret": set_target_ret,
                "target_info": target_info_dict,
            }
        finally:
            if started:
                try:
                    self.dll.FC_Stop(handle)
                except Exception:
                    pass
            try:
                self.dll.FC_DestroyHandle(handle)
            except Exception:
                pass


class HikSdkMerger:
    def __init__(self, dll_dir: Path) -> None:
        self.dll_dir = Path(dll_dir).resolve()
        self._dll_dir_handle = None
        if hasattr(os, "add_dll_directory"):
            self._dll_dir_handle = os.add_dll_directory(str(self.dll_dir))
        for name in ("hpr.dll", "hlog.dll"):
            path = self.dll_dir / name
            if path.exists():
                ctypes.WinDLL(str(path))

    @staticmethod
    def _path_bytes(path: Path) -> bytes:
        return str(path).encode("mbcs")

    def merge_with_hmmerge(self, paths: list[Path], output_path: Path) -> dict[str, Any]:
        dll_path = self.dll_dir / "HmMerge.dll"
        if not dll_path.exists():
            return {"ok": False, "method": "hmmerge", "error": f"missing:{dll_path}"}
        existing = [p for p in paths if p.exists() and p.stat().st_size > 0]
        if not existing:
            return {"ok": False, "method": "hmmerge", "error": "no_input_files"}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        dll = ctypes.WinDLL(str(dll_path))
        dll.HM_CreateHandle.argtypes = []
        dll.HM_CreateHandle.restype = ctypes.c_void_p
        dll.HM_DestroyHandle.argtypes = [ctypes.c_void_p]
        dll.HM_DestroyHandle.restype = ctypes.c_int
        dll.HM_SetMergeStyle.argtypes = [ctypes.c_void_p, ctypes.c_int]
        dll.HM_SetMergeStyle.restype = ctypes.c_int
        dll.HM_Merge.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p]
        dll.HM_Merge.restype = ctypes.c_int
        handle = ctypes.c_void_p(dll.HM_CreateHandle())
        if not handle.value:
            return {"ok": False, "method": "hmmerge", "error": "HM_CreateHandle returned NULL"}
        started = time.monotonic()
        try:
            style_ret = int(dll.HM_SetMergeStyle(handle, 0))
            file_list = ("\0".join(str(p) for p in existing) + "\0\0").encode("mbcs")
            ret = int(dll.HM_Merge(handle, ctypes.c_char_p(file_list), ctypes.c_uint32(len(existing)), ctypes.c_char_p(self._path_bytes(output_path))))
            elapsed = time.monotonic() - started
            if ret != 0:
                return {"ok": False, "method": "hmmerge", "ret": ret, "style_ret": style_ret, "elapsed_sec": elapsed, "output": str(output_path)}
            if not output_path.exists() or output_path.stat().st_size <= 0:
                return {"ok": False, "method": "hmmerge", "ret": ret, "style_ret": style_ret, "elapsed_sec": elapsed, "error": "output_missing_or_empty", "output": str(output_path)}
            return {"ok": True, "method": "hmmerge", "ret": ret, "style_ret": style_ret, "elapsed_sec": elapsed, "output": str(output_path), "probe": probe_media(output_path)}
        except Exception as exc:
            return {"ok": False, "method": "hmmerge", "error": str(exc), "output": str(output_path)}
        finally:
            try:
                dll.HM_DestroyHandle(handle)
            except Exception:
                pass

    def merge_with_fileedit(self, paths: list[Path], output_path: Path, timeout_sec: float = 1800.0) -> dict[str, Any]:
        dll_path = self.dll_dir / "FileEdit.dll"
        if not dll_path.exists():
            return {"ok": False, "method": "fileedit", "error": f"missing:{dll_path}"}
        existing = [p for p in paths if p.exists() and p.stat().st_size > 0]
        if not existing:
            return {"ok": False, "method": "fileedit", "error": "no_input_files"}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        dll = ctypes.WinDLL(str(dll_path))
        dll.FILE_EDIT_CreateHandle.argtypes = []
        dll.FILE_EDIT_CreateHandle.restype = ctypes.c_void_p
        dll.FILE_EDIT_DestroyHandle.argtypes = [ctypes.c_void_p]
        dll.FILE_EDIT_DestroyHandle.restype = ctypes.c_int
        dll.FILE_EDIT_ClearMergeList.argtypes = [ctypes.c_void_p]
        dll.FILE_EDIT_ClearMergeList.restype = ctypes.c_int
        dll.FILE_EDIT_AddToMergeList.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.FILE_EDIT_AddToMergeList.restype = ctypes.c_int
        dll.FILE_EDIT_MergeFileInList.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.FILE_EDIT_MergeFileInList.restype = ctypes.c_int
        dll.FILE_EDIT_GetPercent.argtypes = [ctypes.c_void_p]
        dll.FILE_EDIT_GetPercent.restype = ctypes.c_int
        dll.FILE_EDIT_Stop.argtypes = [ctypes.c_void_p]
        dll.FILE_EDIT_Stop.restype = ctypes.c_int
        handle = ctypes.c_void_p(dll.FILE_EDIT_CreateHandle())
        if not handle.value:
            return {"ok": False, "method": "fileedit", "error": "FILE_EDIT_CreateHandle returned NULL"}
        started = time.monotonic()
        try:
            clear_ret = int(dll.FILE_EDIT_ClearMergeList(handle))
            add_rets = []
            for path in existing:
                add_rets.append(int(dll.FILE_EDIT_AddToMergeList(handle, ctypes.c_char_p(self._path_bytes(path)))))
            if any(ret != 0 for ret in add_rets):
                return {"ok": False, "method": "fileedit", "clear_ret": clear_ret, "add_rets": add_rets, "output": str(output_path)}
            ret = int(dll.FILE_EDIT_MergeFileInList(handle, ctypes.c_char_p(self._path_bytes(output_path))))
            deadline = time.monotonic() + float(timeout_sec)
            last_percent = -1
            while time.monotonic() < deadline:
                try:
                    last_percent = int(dll.FILE_EDIT_GetPercent(handle))
                except Exception:
                    last_percent = -1
                if output_path.exists() and output_path.stat().st_size > 0 and last_percent >= 100:
                    break
                if ret != 0:
                    break
                if output_path.exists() and output_path.stat().st_size > 0 and last_percent < 0:
                    break
                time.sleep(0.5)
            elapsed = time.monotonic() - started
            if ret != 0:
                return {"ok": False, "method": "fileedit", "ret": ret, "clear_ret": clear_ret, "add_rets": add_rets, "percent": last_percent, "elapsed_sec": elapsed, "output": str(output_path)}
            if not output_path.exists() or output_path.stat().st_size <= 0:
                return {"ok": False, "method": "fileedit", "ret": ret, "clear_ret": clear_ret, "add_rets": add_rets, "percent": last_percent, "elapsed_sec": elapsed, "error": "output_missing_or_empty", "output": str(output_path)}
            return {"ok": True, "method": "fileedit", "ret": ret, "clear_ret": clear_ret, "add_rets": add_rets, "percent": last_percent, "elapsed_sec": elapsed, "output": str(output_path), "probe": probe_media(output_path)}
        except Exception as exc:
            return {"ok": False, "method": "fileedit", "error": str(exc), "output": str(output_path)}
        finally:
            try:
                dll.FILE_EDIT_Stop(handle)
            except Exception:
                pass
            try:
                dll.FILE_EDIT_DestroyHandle(handle)
            except Exception:
                pass


def parse_dt(value: str) -> datetime:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"时间格式错误: {value}, 需要 YYYY-MM-DD HH:MM:SS")


def parse_int_csv(value: str) -> list[int]:
    text = str(value or "").strip()
    if not text:
        return []
    result: list[int] = []
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            result.append(int(item, 0))
        except Exception as exc:
            raise argparse.ArgumentTypeError(f"整数列表格式错误: {value}") from exc
    return result


def ffmpeg_bin() -> str:
    return os.getenv("EDGE_FFMPEG_BIN") or "ffmpeg"


def ffprobe_bin() -> str:
    binary = ffmpeg_bin()
    lower = binary.lower()
    if lower.endswith("ffmpeg.exe"):
        return binary[:-10] + "ffprobe.exe"
    if lower.endswith("ffmpeg"):
        return binary[:-6] + "ffprobe"
    return "ffprobe"


def run_command(cmd: list[str], *, timeout_sec: float | None = None) -> subprocess.CompletedProcess[str]:
    LOG.info("run: %s", " ".join(cmd))
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore", timeout=timeout_sec)
    if result.returncode != 0:
        raise RuntimeError(f"command failed rc={result.returncode}: {' '.join(cmd)}\n{(result.stdout or '')[-3000:]}")
    return result


def probe_media(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    cmd = [ffprobe_bin(), "-v", "quiet", "-analyzeduration", "100M", "-probesize", "100M", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        data = json.loads(out or "{}")
    except Exception as exc:
        return {"exists": True, "size_bytes": path.stat().st_size, "probe_error": str(exc)}
    streams = data.get("streams") or []
    video = next((s for s in streams if str((s or {}).get("codec_type") or "").lower() == "video"), None)
    audio = next((s for s in streams if str((s or {}).get("codec_type") or "").lower() == "audio"), None)

    def as_float(obj: Any, key: str) -> float:
        try:
            raw = (obj or {}).get(key)
            return float(raw) if raw not in (None, "", "N/A") else 0.0
        except Exception:
            return 0.0

    video_start = as_float(video, "start_time")
    audio_start = as_float(audio, "start_time")
    video_duration = as_float(video, "duration")
    audio_duration = as_float(audio, "duration")
    format_duration = as_float(data.get("format") or {}, "duration")
    starts = [x for x in (video_start if video else None, audio_start if audio else None) if x is not None]
    return {
        "exists": True,
        "size_bytes": path.stat().st_size,
        "format_duration": format_duration,
        "video": {
            "codec": (video or {}).get("codec_name"),
            "start_time": video_start,
            "duration": video_duration,
            "width": (video or {}).get("width"),
            "height": (video or {}).get("height"),
            "avg_frame_rate": (video or {}).get("avg_frame_rate"),
            "bit_rate": (video or {}).get("bit_rate"),
        } if video else None,
        "audio": {
            "codec": (audio or {}).get("codec_name"),
            "start_time": audio_start,
            "duration": audio_duration,
            "sample_rate": (audio or {}).get("sample_rate"),
            "channels": (audio or {}).get("channels"),
            "bit_rate": (audio or {}).get("bit_rate"),
        } if audio else None,
        "stream_gap": abs(max(starts) - min(starts)) if len(starts) >= 2 else 0.0,
        "duration_delta_abs": abs(video_duration - audio_duration) if video and audio else 0.0,
    }


def repair_with_ffmpeg(input_path: Path, output_path: Path, *, timeout_sec: float = 1800.0) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    cmd = [
        ffmpeg_bin(), "-y",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "copy",
        "-af", "asetpts=PTS-STARTPTS",
        "-c:a", "aac",
        "-ar", "32000",
        "-ac", "1",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(output_path),
    ]
    started = time.monotonic()
    result = run_command(cmd, timeout_sec=timeout_sec)
    elapsed = time.monotonic() - started
    probe = probe_media(output_path)
    return {
        "ok": output_path.exists() and output_path.stat().st_size > 0,
        "method": "ffmpeg_repair",
        "output": str(output_path),
        "elapsed_sec": elapsed,
        "stdout_tail": (result.stdout or "")[-4000:],
        "probe": probe,
    }


def prepare_runtime_dll_dir(base_dll_dir: Path) -> Path:
    runtime_dir = base_dll_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    for zip_path in base_dll_dir.glob("*.zip"):
        with zipfile.ZipFile(zip_path) as archive:
            for name in archive.namelist():
                if not name.lower().endswith(".dll"):
                    continue
                target = runtime_dir / Path(name).name
                if not target.exists():
                    with archive.open(name) as source, target.open("wb") as dest:
                        shutil.copyfileobj(source, dest)
    for name in ("HmMerge.dll", "FileEdit.dll", "hlog.dll", "hpr.dll"):
        source = base_dll_dir / name
        target = runtime_dir / name
        if source.exists() and not target.exists():
            shutil.copy2(source, target)
    return runtime_dir


def build_segments(start: datetime, end: datetime, minutes: int, raw_dir: Path, converted_dir: Path, no_reset_dir: Path, frame_log_dir: Path) -> list[SegmentPlan]:
    if end <= start:
        raise ValueError("end_time 必须大于 start_time")
    if minutes <= 0:
        raise ValueError("segment_minutes 必须大于 0")
    result: list[SegmentPlan] = []
    cur = start
    idx = 1
    step = timedelta(minutes=minutes)
    while cur < end:
        nxt = min(cur + step, end)
        stem = f"part{idx:03d}"
        result.append(
            SegmentPlan(
                index=idx,
                start_time=cur,
                end_time=nxt,
                raw_path=str(raw_dir / f"{stem}.mp4"),
                converted_path=str(converted_dir / f"{stem}.fc_reset.mp4"),
                converted_no_reset_path=str(no_reset_dir / f"{stem}.fc_global.mp4"),
                frame_log_path=str(frame_log_dir / f"{stem}.frames.json"),
            )
        )
        cur = nxt
        idx += 1
    return result


def load_existing_meta(meta_json: Path | None) -> dict[str, dict[str, datetime]]:
    if meta_json is None:
        return {}
    path = Path(meta_json).resolve()
    if not path.exists():
        raise FileNotFoundError(f"existing_meta_json 不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("segments"), list):
        rows = data["segments"]
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError("existing_meta_json 必须是数组，或包含 segments 数组")
    result: dict[str, dict[str, datetime]] = {}
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        name = str(row.get("file") or row.get("filename") or row.get("name") or "").strip()
        start_text = row.get("start_time") or row.get("start") or row.get("struStartTime")
        end_text = row.get("end_time") or row.get("end") or row.get("struStopTime")
        if not name:
            name = f"#{idx}"
        item: dict[str, datetime] = {}
        if start_text:
            item["start"] = parse_dt(str(start_text))
        if end_text:
            item["end"] = parse_dt(str(end_text))
        if item:
            result[name] = item
            result[f"#{idx}"] = item
    return result


def _link_or_copy_file(src: Path, dst: Path, mode: str) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    normalized_mode = str(mode or "hardlink").strip().lower()
    if normalized_mode == "copy":
        shutil.copy2(src, dst)
        return "copy"
    try:
        os.link(src, dst)
        return "hardlink"
    except Exception:
        shutil.copy2(src, dst)
        return "copy"


def build_segments_from_existing_raw(existing_raw_dir: Path, raw_glob: str, raw_dir: Path, converted_dir: Path, no_reset_dir: Path, frame_log_dir: Path, *, start_time: datetime | None = None, meta_json: Path | None = None, link_mode: str = "hardlink") -> list[SegmentPlan]:
    source_dir = Path(existing_raw_dir).resolve()
    if not source_dir.exists():
        raise FileNotFoundError(f"existing_raw_dir 不存在: {source_dir}")
    source_files = sorted([p for p in source_dir.glob(raw_glob) if p.is_file()])
    if not source_files:
        raise FileNotFoundError(f"existing_raw_dir 下没有匹配文件: dir={source_dir} glob={raw_glob}")
    meta = load_existing_meta(meta_json)
    cur = start_time or datetime.now().replace(microsecond=0)
    result: list[SegmentPlan] = []
    for idx, source in enumerate(source_files, start=1):
        stem = f"part{idx:03d}"
        raw_path = raw_dir / f"{stem}.mp4"
        used_mode = _link_or_copy_file(source, raw_path, link_mode)
        raw_probe = probe_media(raw_path)
        video_dur = float((((raw_probe or {}).get("video") or {}).get("duration") or 0.0))
        format_dur = float((raw_probe or {}).get("format_duration") or 0.0)
        duration_sec = video_dur or format_dur or 0.0
        if duration_sec <= 0:
            raise RuntimeError(f"无法获取现有 raw 分段时长: {raw_path}")
        meta_item = meta.get(source.name) or meta.get(str(source)) or meta.get(f"#{idx}") or {}
        seg_start = meta_item.get("start") or cur
        seg_end = meta_item.get("end") or (seg_start + timedelta(seconds=duration_sec))
        cur = seg_end
        LOG.info("existing raw part=%03d source=%s raw=%s mode=%s start=%s end=%s duration=%.3fs meta=%s", idx, source.name, raw_path.name, used_mode, seg_start, seg_end, duration_sec, bool(meta_item))
        result.append(
            SegmentPlan(
                index=idx,
                start_time=seg_start,
                end_time=seg_end,
                raw_path=str(raw_path),
                converted_path=str(converted_dir / f"{stem}.fc_reset.mp4"),
                converted_no_reset_path=str(no_reset_dir / f"{stem}.fc_global.mp4"),
                frame_log_path=str(frame_log_dir / f"{stem}.frames.json"),
            )
        )
    return result


def download_segments(args: argparse.Namespace, segments: list[SegmentPlan]) -> list[SegmentResult]:
    if args.skip_download:
        results: list[SegmentResult] = []
        for seg in segments:
            raw_path = Path(seg.raw_path)
            results.append(
                SegmentResult(
                    index=seg.index,
                    start_time=seg.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    end_time=seg.end_time.strftime("%Y-%m-%d %H:%M:%S"),
                    raw_path=seg.raw_path,
                    converted_path=seg.converted_path,
                    converted_no_reset_path=seg.converted_no_reset_path,
                    frame_log_path=seg.frame_log_path,
                    download={"skipped": True, "exists": raw_path.exists(), "size_bytes": raw_path.stat().st_size if raw_path.exists() else 0},
                    raw_probe=probe_media(raw_path),
                    converted_probe={},
                    converted_no_reset_probe={},
                    converted_method="",
                    converted_no_reset_method="",
                    errors=[] if raw_path.exists() else ["download_skipped:raw_missing"],
                )
            )
        return results
    session = open_download_session(
        sdk_dir=args.sdk_dir,
        ip=args.ip,
        port=args.port,
        username=args.username,
        password=args.password,
        channel=args.channel,
    )
    results: list[SegmentResult] = []
    try:
        for seg in segments:
            errors: list[str] = []
            raw_path = Path(seg.raw_path)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            download_info: dict[str, Any]
            try:
                if args.skip_download and raw_path.exists():
                    download_info = {"skipped": True, "size_bytes": raw_path.stat().st_size}
                else:
                    LOG.info("download part=%03d %s ~ %s", seg.index, seg.start_time, seg.end_time)

                    def progress(percent: float, message: str) -> None:
                        LOG.info("download part=%03d %.2f%% %s", seg.index, percent, message)

                    result = download_by_time_with_session(
                        session,
                        start_time=seg.start_time,
                        end_time=seg.end_time,
                        output_path=str(raw_path),
                        on_progress=progress,
                        hint_channel=args.sdk_channel_hint,
                        locked_record_type=args.locked_record_type,
                        sdk_channel_offsets=args.sdk_channel_offsets,
                        probe_seconds=args.sdk_probe_seconds,
                        stall_timeout_sec=args.download_stall_timeout_sec,
                    )
                    download_info = asdict(result)
            except Exception as exc:
                errors.append(f"download_failed:{exc}")
                download_info = {"ok": False, "error": str(exc)}
            results.append(
                SegmentResult(
                    index=seg.index,
                    start_time=seg.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    end_time=seg.end_time.strftime("%Y-%m-%d %H:%M:%S"),
                    raw_path=seg.raw_path,
                    converted_path=seg.converted_path,
                    converted_no_reset_path=seg.converted_no_reset_path,
                    frame_log_path=seg.frame_log_path,
                    download=download_info,
                    raw_probe=probe_media(raw_path),
                    converted_probe={},
                    converted_no_reset_probe={},
                    converted_method="",
                    converted_no_reset_method="",
                    errors=errors,
                )
            )
    finally:
        close_download_session(session)
    return results


def convert_segments(args: argparse.Namespace, runtime_dir: Path, results: list[SegmentResult]) -> dict[str, Any]:
    client: FormatConversionClient | None = None
    summary = {"version": None, "segments": []}
    for item in results:
        if item.index < args.start_part_index or (args.end_part_index and item.index > args.end_part_index):
            continue
        start_time = parse_dt(item.start_time)
        raw_path = Path(item.raw_path)
        if not raw_path.exists():
            item.errors.append("convert_skipped:raw_missing")
            continue
        variants: list[tuple[bool, Path]] = []
        if args.format_variant in ("reset", "both"):
            variants.append((True, Path(item.converted_path)))
        if args.format_variant in ("global", "both"):
            variants.append((False, Path(item.converted_no_reset_path)))
        for reset_timestamp, output_path in variants:
            try:
                method_name = ""
                if args.skip_convert and output_path.exists():
                    info = {"ok": True, "skipped": True, "size_bytes": output_path.stat().st_size}
                    method_name = "existing_output"
                elif args.conversion_mode == "ffmpeg":
                    LOG.info("repair(ffmpeg) part=%03d reset=%s", item.index, reset_timestamp)
                    info = repair_with_ffmpeg(raw_path, output_path, timeout_sec=args.convert_timeout_sec)
                    method_name = str(info.get("method") or "ffmpeg_repair")
                else:
                    if client is None:
                        client = FormatConversionClient(runtime_dir)
                        summary["version"] = client.version()
                    LOG.info("convert part=%03d reset=%s", item.index, reset_timestamp)
                    try:
                        info = client.convert(
                            raw_path,
                            output_path,
                            start_time,
                            reset_timestamp=reset_timestamp,
                            timeout_sec=args.convert_timeout_sec,
                            frame_log_path=Path(item.frame_log_path) if reset_timestamp else None,
                            max_frame_log_entries=args.max_frame_log_entries,
                            set_target_mp4=True,
                        )
                        method_name = "format_conversion"
                    except Exception as exc:
                        if args.ffmpeg_fallback_on_fc_failure:
                            LOG.warning("FC failed part=%03d reset=%s, fallback to ffmpeg: %s", item.index, reset_timestamp, exc)
                            info = repair_with_ffmpeg(raw_path, output_path, timeout_sec=args.convert_timeout_sec)
                            info["fc_error"] = str(exc)
                            method_name = str(info.get("method") or "ffmpeg_repair")
                        else:
                            raise
                if reset_timestamp:
                    item.converted_probe = probe_media(output_path)
                    item.converted_method = method_name
                else:
                    item.converted_no_reset_probe = probe_media(output_path)
                    item.converted_no_reset_method = method_name
                summary["segments"].append({"index": item.index, "reset_timestamp": reset_timestamp, "output": str(output_path), "resolved_method": method_name, **info})
            except Exception as exc:
                item.errors.append(f"convert_failed:reset={reset_timestamp}:{exc}")
                summary["segments"].append({"index": item.index, "reset_timestamp": reset_timestamp, "output": str(output_path), "ok": False, "error": str(exc)})
    return summary


def analyze_segments(args: argparse.Namespace, runtime_dir: Path, results: list[SegmentResult], output_dir: Path) -> dict[str, Any]:
    if not args.analyze_data:
        return {"enabled": False, "segments": []}
    client = AnalyzeDataClient(runtime_dir)
    analyze_dir = output_dir / "analyze_data"
    analyze_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"enabled": True, "version": client.version(), "segments": []}
    for item in results:
        if item.index < args.start_part_index or (args.end_part_index and item.index > args.end_part_index):
            continue
        raw_path = Path(item.raw_path)
        if not raw_path.exists():
            summary["segments"].append({"index": item.index, "ok": False, "error": "raw_missing", "path": str(raw_path)})
            continue
        LOG.info("AnalyzeData part=%03d", item.index)
        try:
            info = client.analyze_file(raw_path, max_packets=args.analyze_max_packets)
            info["index"] = item.index
            info["start_time"] = item.start_time
            info["end_time"] = item.end_time
            (analyze_dir / f"part{item.index:03d}.analyze.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            summary["segments"].append(info)
        except Exception as exc:
            summary["segments"].append({"index": item.index, "ok": False, "error": str(exc), "path": str(raw_path)})
    return summary


def check_runtime_dll_dependencies(runtime_dir: Path) -> dict[str, Any]:
    names = [
        "AnalyzeData.dll",
        "FormatConversion.dll",
        "HmMerge.dll",
        "FileEdit.dll",
        "hpr.dll",
        "hlog.dll",
        "HWEncode.dll",
        "HWTranscode.dll",
        "welsenc.dll",
        "nvcuda.dll",
        "nvcuvid.dll",
        "nvEncodeAPI64.dll",
    ]
    items = []
    for name in names:
        path = runtime_dir / name
        exists = path.exists()
        load_ok = False
        error = None
        if exists:
            try:
                ctypes.WinDLL(str(path))
                load_ok = True
            except Exception as exc:
                error = str(exc)
        items.append({"name": name, "path": str(path), "exists": exists, "load_ok": load_ok, "error": error})
    return {
        "runtime_dir": str(runtime_dir),
        "items": items,
        "missing": [item["name"] for item in items if not item["exists"]],
        "failed_to_load": [item for item in items if item["exists"] and not item["load_ok"]],
    }


def diagnose_format_conversion(args: argparse.Namespace, runtime_dir: Path, results: list[SegmentResult], output_dir: Path) -> dict[str, Any]:
    candidates = [item for item in results if Path(item.raw_path).exists()]
    if not candidates:
        return {"ok": False, "error": "no_raw_segments"}
    item = candidates[max(0, min(int(args.diagnose_part_index or 1), len(candidates))) - 1]
    raw_path = Path(item.raw_path)
    client = FormatConversionClient(runtime_dir)
    diag_dir = output_dir / "diagnostics" / f"part{item.index:03d}"
    diag_dir.mkdir(parents=True, exist_ok=True)
    base_start = parse_dt(item.start_time)
    starts = [
        ("segment_start", base_start),
        ("zero_epoch", datetime(1970, 1, 1)),
        ("file_mtime", datetime.fromtimestamp(raw_path.stat().st_mtime).replace(microsecond=0)),
    ]
    path_variants: list[tuple[str, Path]] = [("staged", raw_path)]
    ascii_input = diag_dir / raw_path.name
    if ascii_input != raw_path:
        try:
            if ascii_input.exists():
                ascii_input.unlink()
            os.link(raw_path, ascii_input)
        except Exception:
            shutil.copy2(raw_path, ascii_input)
        path_variants.append(("ascii_path", ascii_input))
    attempts: list[dict[str, Any]] = []
    for path_label, input_path in path_variants:
        try:
            file_info_ret, file_info = client.get_file_info(input_path)
            attempts.append({
                "path_label": path_label,
                "input": str(input_path),
                "probe_only": True,
                "get_file_info_ret": file_info_ret,
                "source_info": client.media_info_to_dict(file_info),
            })
        except Exception as exc:
            attempts.append({"path_label": path_label, "input": str(input_path), "probe_only": True, "ok": False, "error": str(exc)})
        for start_label, start_time in starts:
            for reset in (False, True):
                for set_target_mp4 in (False, True):
                    output_path = diag_dir / f"{path_label}.{start_label}.reset_{int(reset)}.target_{int(set_target_mp4)}.mp4"
                    frame_log_path = diag_dir / f"{path_label}.{start_label}.reset_{int(reset)}.target_{int(set_target_mp4)}.frames.json"
                    attempt = {
                        "path_label": path_label,
                        "input": str(input_path),
                        "start_label": start_label,
                        "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "reset_timestamp": reset,
                        "set_target_mp4": set_target_mp4,
                        "output": str(output_path),
                    }
                    try:
                        started = time.monotonic()
                        info = client.convert(
                            input_path,
                            output_path,
                            start_time,
                            reset_timestamp=reset,
                            timeout_sec=float(args.diagnose_timeout_sec),
                            frame_log_path=frame_log_path,
                            max_frame_log_entries=args.max_frame_log_entries,
                            set_target_mp4=set_target_mp4,
                        )
                        attempt.update(info)
                        attempt["elapsed_sec"] = time.monotonic() - started
                        attempt["probe"] = probe_media(output_path)
                    except Exception as exc:
                        attempt.update({"ok": False, "error": str(exc)})
                    attempts.append(attempt)
    return {"ok": any(bool(item.get("ok")) for item in attempts), "part_index": item.index, "raw_path": str(raw_path), "version": client.version(), "attempts": attempts}


def write_concat_file(paths: list[Path], concat_file: Path) -> None:
    concat_file.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for path in paths:
        normalized = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{normalized}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def probe_pts_large_gap(path: Path, stream_selector: str = "v:0", gap_threshold_sec: float = 2.0) -> dict[str, Any]:
    cmd = [
        ffprobe_bin(), "-v", "error",
        "-select_streams", stream_selector,
        "-show_packets",
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="ignore")
    except Exception as exc:
        return {"large_gap_count": 0, "max_gap_sec": 0.0, "error": str(exc)}
    large_gap_count = 0
    max_gap_sec = 0.0
    prev_pts: float | None = None
    prev_dur = 0.033
    for line in (out or "").splitlines():
        parts = line.split(",")
        if not parts:
            continue
        try:
            pts = float(parts[0])
            if len(parts) > 1 and parts[1] not in ("", "N/A"):
                prev_dur = float(parts[1])
        except Exception:
            continue
        if prev_pts is not None:
            gap = pts - prev_pts
            if gap > max(gap_threshold_sec, prev_dur * 4):
                large_gap_count += 1
                max_gap_sec = max(max_gap_sec, gap)
        prev_pts = pts
    return {"large_gap_count": int(large_gap_count), "max_gap_sec": float(max_gap_sec)}


def probe_segment_meta(path: Path) -> dict[str, Any]:
    probe = probe_media(path)
    video = probe.get("video") or {}
    audio = probe.get("audio") or {}
    return {
        "video_codec": video.get("codec"),
        "audio_codec": audio.get("codec"),
        "sample_rate": audio.get("sample_rate"),
    }


def validate_segments_before_merge(paths: list[Path]) -> dict[str, Any]:
    existing = [p for p in paths if p.exists() and p.stat().st_size > 0]
    metas = [{"path": str(p), **probe_segment_meta(p)} for p in existing]
    if not metas:
        return {"ok": False, "error": "no_input_files", "metas": []}
    ref = metas[0]
    incompatible = [
        item for item in metas[1:]
        if item.get("video_codec") != ref.get("video_codec")
        or item.get("audio_codec") != ref.get("audio_codec")
        or item.get("sample_rate") != ref.get("sample_rate")
    ]
    return {"ok": not incompatible, "reference": ref, "incompatible": incompatible, "metas": metas}


def build_silent_audio_variant(
    input_path: Path,
    output_path: Path,
    *,
    sample_rate: int,
    channels: int,
    bit_rate: str = "64k",
    timeout_sec: float = 1800.0,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    channel_layout = "mono" if channels <= 1 else "stereo"
    cmd = [
        ffmpeg_bin(), "-y",
        "-i", str(input_path),
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl={channel_layout}",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", bit_rate,
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-shortest",
        str(output_path),
    ]
    started = time.monotonic()
    run_command(cmd, timeout_sec=timeout_sec)
    return {
        "ok": True,
        "output": str(output_path),
        "elapsed_sec": time.monotonic() - started,
        "probe": probe_media(output_path),
        "method": "silent_audio_fill",
    }


def build_padded_audio_variant(
    input_path: Path,
    output_path: Path,
    *,
    sample_rate: int,
    channels: int,
    bit_rate: str = "64k",
    timeout_sec: float = 1800.0,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    cmd = [
        ffmpeg_bin(), "-y",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", bit_rate,
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-af", "apad",
        "-shortest",
        str(output_path),
    ]
    started = time.monotonic()
    run_command(cmd, timeout_sec=timeout_sec)
    return {
        "ok": True,
        "output": str(output_path),
        "elapsed_sec": time.monotonic() - started,
        "probe": probe_media(output_path),
        "method": "audio_tail_silence_fill",
    }


def prepare_paths_for_merge(paths: list[Path], output_path: Path) -> dict[str, Any]:
    existing = [p for p in paths if p.exists() and p.stat().st_size > 0]
    probes = {p: probe_media(p) for p in existing}
    metas = [{"path": p, **probe_segment_meta(p)} for p in existing]
    audio_present = [item for item in metas if item.get("audio_codec")]
    audio_missing = [item for item in metas if not item.get("audio_codec")]
    audio_tail_short: set[Path] = set()
    for p, probe in probes.items():
        video = probe.get("video") or {}
        audio = probe.get("audio") or {}
        if not video or not audio:
            continue
        video_duration = float(video.get("duration") or probe.get("format_duration") or 0.0)
        audio_duration = float(audio.get("duration") or 0.0)
        if video_duration > 0 and audio_duration > 0 and video_duration - audio_duration > 1.0:
            audio_tail_short.add(p)
    if not audio_missing and not audio_tail_short:
        return {"paths": existing, "prepared": [], "metas": metas}

    ref = audio_present[0]
    sample_rate = int(ref.get("sample_rate") or 32000)
    prepared_dir = output_path.parent / "_prepared_for_merge"
    prepared_paths: list[Path] = []
    prepared_items: list[dict[str, Any]] = []
    for item in metas:
        src = Path(item["path"])
        if item.get("audio_codec") and src not in audio_tail_short:
            prepared_paths.append(src)
            continue
        if item.get("audio_codec"):
            probe = probes.get(src) or {}
            audio = probe.get("audio") or {}
            prepared_path = prepared_dir / f"{src.stem}.audio_tail_silence.mp4"
            prepared = build_padded_audio_variant(
                src,
                prepared_path,
                sample_rate=int(audio.get("sample_rate") or sample_rate),
                channels=int(audio.get("channels") or 1),
            )
        else:
            prepared_path = prepared_dir / f"{src.stem}.with_silence.mp4"
            prepared = build_silent_audio_variant(
                src,
                prepared_path,
                sample_rate=sample_rate,
                channels=1,
            )
        prepared_paths.append(prepared_path)
        prepared_items.append({
            "source": str(src),
            "prepared": str(prepared_path),
            "sample_rate": sample_rate,
            **prepared,
        })
    return {"paths": prepared_paths, "prepared": prepared_items, "metas": metas}


def segment_duration_for_concat(path: Path) -> float:
    probe = probe_media(path)
    video = probe.get("video") or {}
    fmt = probe.get("format_duration") or 0.0
    return float(video.get("duration") or fmt or 0.0)


def write_concat_file_with_durations(paths: list[Path], concat_file: Path, durations_sec: list[float] | None = None) -> None:
    concat_file.parent.mkdir(parents=True, exist_ok=True)
    durations = durations_sec or [segment_duration_for_concat(p) for p in paths]
    lines = []
    for path, dur in zip(paths, durations):
        normalized = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{normalized}'")
        if dur > 0:
            lines.append(f"duration {dur:.6f}")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def video_bsf_args_for(path: Path) -> list[str]:
    codec = str((probe_media(path).get("video") or {}).get("codec") or "").lower()
    if codec == "h264":
        return ["-bsf:v", "h264_mp4toannexb"]
    if codec in {"hevc", "h265"}:
        return ["-bsf:v", "hevc_mp4toannexb"]
    return []


def merge_with_ts_bridge(
    paths: list[Path],
    output_path: Path,
    *,
    prepared_inputs: list[dict[str, Any]] | None = None,
    validation: dict[str, Any] | None = None,
    faststart: bool = False,
) -> dict[str, Any]:
    existing = [p for p in paths if p.exists() and p.stat().st_size > 0]
    if not existing:
        return {"ok": False, "method": "ffmpeg_ts_bridge", "error": "no_input_files"}
    ts_dir = output_path.parent / "_ts_bridge"
    ts_dir.mkdir(parents=True, exist_ok=True)
    ts_paths: list[Path] = []
    started = time.monotonic()
    try:
        for idx, part in enumerate(existing, start=1):
            ts_path = ts_dir / f"part{idx:03d}.ts"
            if ts_path.exists():
                ts_path.unlink()
            cmd = [
                ffmpeg_bin(), "-y",
                "-fflags", "+genpts",
                "-i", str(part),
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                *video_bsf_args_for(part),
                "-f", "mpegts",
                str(ts_path),
            ]
            run_command(cmd)
            if not ts_path.exists() or ts_path.stat().st_size <= 0:
                raise RuntimeError(f"ts_bridge_part_missing:{idx}")
            ts_paths.append(ts_path)
        concat_arg = "concat:" + "|".join(str(p) for p in ts_paths)
        cmd = [
            ffmpeg_bin(), "-y",
            "-fflags", "+genpts",
            "-i", concat_arg,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-avoid_negative_ts", "make_zero",
        ]
        if faststart:
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(output_path))
        run_command(cmd)
        return {
            "ok": True,
            "method": "ffmpeg_ts_bridge",
            "output": str(output_path),
            "elapsed_sec": time.monotonic() - started,
            "faststart": faststart,
            "prepared_inputs": prepared_inputs or [],
            "validation": validation or validate_segments_before_merge(existing),
            "ts_parts": [str(p) for p in ts_paths],
            "probe": probe_media(output_path),
        }
    except Exception as exc:
        return {
            "ok": False,
            "method": "ffmpeg_ts_bridge",
            "output": str(output_path),
            "elapsed_sec": time.monotonic() - started,
            "faststart": faststart,
            "prepared_inputs": prepared_inputs or [],
            "validation": validation or {},
            "ts_parts": [str(p) for p in ts_paths],
            "error": str(exc),
        }


def merge_with_ffmpeg_duration(
    paths: list[Path],
    output_path: Path,
    durations_sec: list[float] | None = None,
    faststart: bool = False,
) -> dict[str, Any]:
    prepared = prepare_paths_for_merge(paths, output_path)
    existing = [p for p in (prepared.get("paths") or []) if p.exists() and p.stat().st_size > 0]
    if not existing:
        return {"ok": False, "method": "ffmpeg_concat_duration", "error": "no_input_files"}
    validation = validate_segments_before_merge(existing)
    if not validation.get("ok"):
        return {
            "ok": False,
            "method": "ffmpeg_concat_duration",
            "error": "incompatible_segments",
            "validation": validation,
            "prepared_inputs": prepared.get("prepared") or [],
        }
    pts_gaps = [
        {
            "path": str(p),
            "video": probe_pts_large_gap(p, "v:0"),
            "audio": probe_pts_large_gap(p, "a:0"),
        }
        for p in existing
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    concat_file = output_path.with_suffix(".concat.txt")
    concat_durations = bool(durations_sec)
    if concat_durations:
        write_concat_file_with_durations(existing, concat_file, durations_sec)
    else:
        write_concat_file(existing, concat_file)
    ref_meta = (validation.get("reference") or {})
    audio_codec = str(ref_meta.get("audio_codec") or "").lower()
    # MP4 cannot mux pcm_alaw by copy. Keep video copy and only normalize the
    # audio codec at merge time for browser-playable output.
    needs_audio_reencode = audio_codec in {"pcm_alaw", "pcm_mulaw"}
    try:
        cmd = [ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", "-avoid_negative_ts", "make_zero"]
        if needs_audio_reencode:
            cmd = [ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", "-ar", "8000", "-ac", "1", "-avoid_negative_ts", "make_zero"]
        if faststart:
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(output_path))
        started = time.monotonic()
        run_command(cmd)
        elapsed = time.monotonic() - started
        method = "ffmpeg_concat_duration_audio_aac" if needs_audio_reencode else "ffmpeg_concat_duration"
        probe = probe_media(output_path)
        ready_issue = merge_ready_issue(probe)
        if ready_issue:
            ts_output = output_path.with_suffix(".ts_bridge.mp4")
            ts_result = merge_with_ts_bridge(
                existing,
                ts_output,
                prepared_inputs=prepared.get("prepared") or [],
                validation=validation,
                faststart=faststart,
            )
            ts_ready_issue = merge_ready_issue(ts_result.get("probe") or {}) if ts_result.get("ok") else ""
            if ts_result.get("ok") and not ts_ready_issue:
                ts_result["mp4_concat_result"] = {
                    "ok": False,
                    "method": method,
                    "output": str(output_path),
                    "elapsed_sec": elapsed,
                    "faststart": faststart,
                    "concat_durations": concat_durations,
                    "audio_reencoded": needs_audio_reencode,
                    "probe": probe,
                    "error": f"merge_ready_invalid:{ready_issue}",
                }
                return ts_result
            if ts_ready_issue:
                ts_result["ok"] = False
                ts_result["error"] = f"merge_ready_invalid:{ts_ready_issue}"
            return {
                "ok": False,
                "method": method,
                "output": str(output_path),
                "elapsed_sec": elapsed,
                "faststart": faststart,
                "concat_durations": concat_durations,
                "audio_reencoded": needs_audio_reencode,
                "prepared_inputs": prepared.get("prepared") or [],
                "validation": validation,
                "pts_large_gaps": pts_gaps,
                "probe": probe,
                "error": f"merge_ready_invalid:{ready_issue}",
                "ts_bridge_result": ts_result,
            }
        return {
            "ok": True,
            "method": method,
            "output": str(output_path),
            "elapsed_sec": elapsed,
            "faststart": faststart,
            "concat_durations": concat_durations,
            "audio_reencoded": needs_audio_reencode,
            "prepared_inputs": prepared.get("prepared") or [],
            "validation": validation,
            "pts_large_gaps": pts_gaps,
            "probe": probe,
        }
    except Exception as exc:
        method = "ffmpeg_concat_duration_audio_aac" if needs_audio_reencode else "ffmpeg_concat_duration"
        return {
            "ok": False,
            "method": method,
            "output": str(output_path),
            "faststart": faststart,
            "concat_durations": concat_durations,
            "audio_reencoded": needs_audio_reencode,
            "prepared_inputs": prepared.get("prepared") or [],
            "validation": validation,
            "pts_large_gaps": pts_gaps,
            "error": str(exc),
        }
    finally:
        try:
            concat_file.unlink()
        except FileNotFoundError:
            pass


def merge_ready_issue(probe: dict[str, Any]) -> str:
    if not probe.get("exists"):
        return "output_missing"
    if not probe.get("video"):
        return "video_missing"
    if not probe.get("audio"):
        return "audio_missing"
    stream_gap = float(probe.get("stream_gap") or 0.0)
    if stream_gap > MERGE_READY_STREAM_GAP_MAX_SEC:
        return f"stream_gap:{stream_gap:.3f}"
    duration_delta_abs = float(probe.get("duration_delta_abs") or 0.0)
    if duration_delta_abs > MERGE_READY_DURATION_DELTA_MAX_SEC:
        return f"duration_delta_abs:{duration_delta_abs:.3f}"
    return ""


def collect_variant_outputs(results: list[SegmentResult], variant: str, start_index: int, end_index: int) -> dict[str, Any]:
    selected = [
        item for item in results
        if item.index >= start_index and (end_index <= 0 or item.index <= end_index)
    ]
    if variant == "reset":
        paths = [Path(item.converted_path) for item in selected if Path(item.converted_path).exists() and Path(item.converted_path).stat().st_size > 0]
        methods = [item.converted_method for item in selected if item.converted_method]
        missing = [item.index for item in selected if not Path(item.converted_path).exists() or Path(item.converted_path).stat().st_size <= 0]
    else:
        paths = [Path(item.converted_no_reset_path) for item in selected if Path(item.converted_no_reset_path).exists() and Path(item.converted_no_reset_path).stat().st_size > 0]
        methods = [item.converted_no_reset_method for item in selected if item.converted_no_reset_method]
        missing = [item.index for item in selected if not Path(item.converted_no_reset_path).exists() or Path(item.converted_no_reset_path).stat().st_size <= 0]
    return {
        "variant": variant,
        "selected_indexes": [item.index for item in selected],
        "paths": paths,
        "missing_indexes": missing,
        "methods": sorted(set(methods)),
    }


def merge_variant_outputs(args: argparse.Namespace, runtime_dir: Path, results: list[SegmentResult], merged_dir: Path, variant: str) -> list[dict[str, Any]]:
    collected = collect_variant_outputs(results, variant, args.start_part_index, args.end_part_index)
    selected_indexes = collected.get("selected_indexes") or []
    if not selected_indexes:
        return []
    if collected.get("missing_indexes"):
        return [{
            "ok": False,
            "variant": variant,
            "method": "merge_plan",
            "error": "missing_converted_segments",
            "selected_indexes": selected_indexes,
            "missing_indexes": collected["missing_indexes"],
        }]
    methods = collected.get("methods") or []
    if len(methods) != 1:
        return [{
            "ok": False,
            "variant": variant,
            "method": "merge_plan",
            "error": "mixed_conversion_methods",
            "selected_indexes": selected_indexes,
            "conversion_methods": methods,
        }]
    paths = list(collected.get("paths") or [])
    if not paths:
        return [{
            "ok": False,
            "variant": variant,
            "method": "merge_plan",
            "error": "no_input_files",
            "selected_indexes": selected_indexes,
        }]
    primary_method = str(methods[0])
    prefix = f"merged_{variant}_{primary_method}"
    results_list: list[dict[str, Any]] = []
    if args.merge_mode == "none":
        return [{
            "ok": True,
            "variant": variant,
            "method": "merge_skipped",
            "selected_indexes": selected_indexes,
            "conversion_method": primary_method,
        }]
    if args.merge_mode == "all" and primary_method == "format_conversion":
        merger = HikSdkMerger(runtime_dir)
        results_list.append(merger.merge_with_hmmerge(paths, merged_dir / f"{prefix}_hmmerge.mp4"))
        results_list.append(merger.merge_with_fileedit(paths, merged_dir / f"{prefix}_fileedit.mp4"))
    merge_result = merge_with_ffmpeg_duration(
        paths,
        merged_dir / f"{prefix}_ffmpeg_duration.mp4",
        faststart=bool(args.merge_faststart),
    )
    merge_result["variant"] = variant
    merge_result["conversion_method"] = primary_method
    merge_result["selected_indexes"] = selected_indexes
    ready_issue = merge_ready_issue(merge_result.get("probe") or {}) if merge_result.get("ok") else ""
    if ready_issue:
        merge_result["ok"] = False
        merge_result["error"] = f"merge_ready_invalid:{ready_issue}"
    results_list.append(merge_result)
    return results_list


def generate_player(report: dict[str, Any], output_dir: Path) -> None:
    videos: list[dict[str, Any]] = []
    for merge in report.get("merges") or []:
        if merge.get("ok") and merge.get("output"):
            path = Path(merge["output"])
            videos.append({"title": path.name, "url": path.relative_to(output_dir).as_posix(), "probe": merge.get("probe")})
    for seg in report.get("segments") or []:
        for key, title, probe_key, method_key in (
            ("raw_path", "raw", "raw_probe", None),
            ("converted_no_reset_path", "converted_global", "converted_no_reset_probe", "converted_no_reset_method"),
            ("converted_path", "converted_reset", "converted_probe", "converted_method"),
        ):
            path = Path(seg.get(key) or "")
            if path.exists():
                method_label = f" [{seg.get(method_key)}]" if method_key and seg.get(method_key) else ""
                videos.append({"title": f"part{seg.get('index'):03d} {title}{method_label}", "url": path.relative_to(output_dir).as_posix(), "probe": seg.get(probe_key, {})})
    html = f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>Hikvision AV Sync Lab</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
.card {{ background: #111827; border: 1px solid #334155; border-radius: 14px; padding: 18px; margin: 18px 0; box-shadow: 0 10px 30px rgba(0,0,0,.25); }}
video {{ width: 100%; max-height: 70vh; background: #000; border-radius: 10px; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #020617; padding: 12px; border-radius: 10px; color: #cbd5e1; }}
a {{ color: #38bdf8; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; }}
</style>
</head>
<body>
<main>
<h1>Hikvision AV Sync Lab</h1>
<p>报告文件：<a href=\"report.json\">report.json</a></p>
<div class=\"grid\">
{''.join(f'<section class="card"><h2>{item["title"]}</h2><video controls preload="metadata" src="{item["url"]}"></video><pre>{json.dumps(item.get("probe") or {}, ensure_ascii=False, indent=2)}</pre></section>' for item in videos)}
</div>
</main>
</body>
</html>"""
    (output_dir / "index.html").write_text(html, encoding="utf-8")


def serve_directory(directory: Path, host: str, port: int) -> None:
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    httpd = ThreadingHTTPServer((host, port), handler)
    LOG.info("player: http://%s:%s/", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("server stopped")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hikvision NVR AV sync independent validation lab")
    parser.add_argument("--mode", choices=("online", "local"), default=os.getenv("HIK_LAB_MODE", LAB_MODE))
    parser.add_argument("--ip", default=os.getenv("HIK_LAB_IP", ONLINE_DEVICE_IP))
    parser.add_argument("--port", type=int, default=int(os.getenv("HIK_LAB_PORT", str(ONLINE_DEVICE_PORT))))
    parser.add_argument("--username", default=os.getenv("HIK_LAB_USERNAME", ONLINE_USERNAME))
    parser.add_argument("--password", default=os.getenv("HIK_LAB_PASSWORD", ONLINE_PASSWORD))
    parser.add_argument("--channel", type=int, default=int(os.getenv("HIK_LAB_CHANNEL", str(ONLINE_WEB_CHANNEL))))
    parser.add_argument("--device-model", default=os.getenv("HIK_LAB_DEVICE_MODEL") or None)
    parser.add_argument("--nvr-device-id", type=int, default=int(os.getenv("HIK_LAB_NVR_DEVICE_ID", "0")) or None)
    parser.add_argument("--sdk-dir", default=os.getenv("HIK_LAB_SDK_DIR", DEFAULT_DOWNLOAD_SDK_DIR))
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--locked-channel", type=int, default=int(os.getenv("HIK_LAB_LOCKED_CHANNEL", str(ONLINE_LOCKED_SDK_CHANNEL))) or None)
    parser.add_argument("--locked-record-type", type=lambda x: int(x, 0), default=(int(ONLINE_LOCKED_RECORD_TYPE, 0) if str(ONLINE_LOCKED_RECORD_TYPE or "").strip() else None))
    parser.add_argument("--sdk-channel-hint", type=int, default=None)
    parser.add_argument("--sdk-channel-offsets", type=parse_int_csv, default=[])
    parser.add_argument("--sdk-probe-seconds", type=int, default=int(os.getenv("HIK_LAB_SDK_PROBE_SECONDS", str(ONLINE_SDK_PROBE_SECONDS))))
    parser.add_argument("--download-stall-timeout-sec", type=int, default=int(os.getenv("HIK_LAB_DOWNLOAD_STALL_TIMEOUT_SEC", str(ONLINE_DOWNLOAD_STALL_TIMEOUT_SEC))))
    parser.add_argument("--start-time", type=parse_dt, default=parse_dt(os.getenv("HIK_LAB_START_TIME", ONLINE_START_TIME)) if (os.getenv("HIK_LAB_START_TIME", ONLINE_START_TIME) or "").strip() else None)
    parser.add_argument("--end-time", type=parse_dt, default=parse_dt(os.getenv("HIK_LAB_END_TIME", ONLINE_END_TIME)) if (os.getenv("HIK_LAB_END_TIME", ONLINE_END_TIME) or "").strip() else None)
    parser.add_argument("--segment-minutes", type=int, default=int(os.getenv("HIK_LAB_SEGMENT_MINUTES", str(ONLINE_SEGMENT_MINUTES))))
    parser.add_argument("--output-dir", default=os.getenv("HIK_LAB_OUTPUT_DIR", DEFAULT_OUTPUT_DIR) or None)
    parser.add_argument("--dll-dir", default=os.getenv("HIK_LAB_DLL_DIR", DEFAULT_CONVERT_DLL_DIR))
    parser.add_argument("--convert-timeout-sec", type=float, default=900.0)
    parser.add_argument("--max-frame-log-entries", type=int, default=2000)
    parser.add_argument("--existing-raw-dir", default=os.getenv("HIK_LAB_EXISTING_RAW_DIR", LOCAL_EXISTING_RAW_DIR) or None)
    parser.add_argument("--raw-glob", default=os.getenv("HIK_LAB_RAW_GLOB", LOCAL_RAW_GLOB))
    parser.add_argument("--existing-start-time", type=parse_dt, default=parse_dt(os.getenv("HIK_LAB_EXISTING_START_TIME", LOCAL_EXISTING_START_TIME)) if (os.getenv("HIK_LAB_EXISTING_START_TIME", LOCAL_EXISTING_START_TIME) or "").strip() else None)
    parser.add_argument("--existing-meta-json", default=None)
    parser.add_argument("--source-link-mode", choices=("hardlink", "copy"), default=os.getenv("HIK_LAB_SOURCE_LINK_MODE", LOCAL_SOURCE_LINK_MODE))
    parser.add_argument("--conversion-mode", choices=("format", "ffmpeg"), default=os.getenv("HIK_LAB_CONVERSION_MODE", DEFAULT_CONVERSION_MODE))
    parser.add_argument("--format-variant", choices=("reset", "global", "both"), default=os.getenv("HIK_LAB_FORMAT_VARIANT", DEFAULT_FORMAT_VARIANT))
    parser.add_argument("--ffmpeg-fallback-on-fc-failure", action="store_true")
    parser.add_argument("--start-part-index", type=int, default=1)
    parser.add_argument("--end-part-index", type=int, default=0)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-convert", action="store_true")
    parser.add_argument("--diagnose-format", action="store_true")
    parser.add_argument("--diagnose-part-index", type=int, default=1)
    parser.add_argument("--diagnose-timeout-sec", type=float, default=120.0)
    parser.add_argument("--analyze-data", action="store_true", default=DEFAULT_ANALYZE_DATA)
    parser.add_argument("--analyze-max-packets", type=int, default=200000)
    parser.add_argument("--merge-mode", choices=("ffmpeg_duration", "all", "none"), default=os.getenv("HIK_LAB_MERGE_MODE", DEFAULT_MERGE_MODE))
    parser.add_argument("--merge-faststart", action="store_true", default=DEFAULT_MERGE_FASTSTART)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--serve-host", default="127.0.0.1")
    parser.add_argument("--serve-port", type=int, default=18080)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.locked_channel is not None and int(args.locked_channel or 0) <= 0:
        args.locked_channel = None
    if args.locked_channel is not None and args.sdk_channel_hint is None:
        args.sdk_channel_hint = int(args.locked_channel)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (Path(__file__).resolve().parent / "output" / run_id).resolve()
    raw_dir = output_dir / "raw"
    converted_dir = output_dir / "converted_reset"
    no_reset_dir = output_dir / "converted_global"
    frame_log_dir = output_dir / "frame_logs"
    merged_dir = output_dir / "merged"
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = prepare_runtime_dll_dir(Path(args.dll_dir).resolve())
    if str(args.mode or "online").lower() == "local":
        args.skip_download = True
        if not args.existing_raw_dir:
            parser.error("local 模式下请在脚本顶部设置 LOCAL_EXISTING_RAW_DIR，或传入 --existing-raw-dir")
        segments = build_segments_from_existing_raw(Path(args.existing_raw_dir), args.raw_glob, raw_dir, converted_dir, no_reset_dir, frame_log_dir, start_time=args.existing_start_time, meta_json=Path(args.existing_meta_json) if args.existing_meta_json else None, link_mode=args.source_link_mode)
    elif args.existing_raw_dir:
        args.skip_download = True
        segments = build_segments_from_existing_raw(Path(args.existing_raw_dir), args.raw_glob, raw_dir, converted_dir, no_reset_dir, frame_log_dir, start_time=args.existing_start_time, meta_json=Path(args.existing_meta_json) if args.existing_meta_json else None, link_mode=args.source_link_mode)
    else:
        if not args.ip:
            parser.error("--ip 或 HIK_LAB_IP 必填")
        if not args.password:
            parser.error("--password 或 HIK_LAB_PASSWORD 必填")
        if args.start_time is None or args.end_time is None:
            parser.error("下载模式下 --start-time 和 --end-time 必填")
        segments = build_segments(args.start_time, args.end_time, args.segment_minutes, raw_dir, converted_dir, no_reset_dir, frame_log_dir)
    LOG.info("output_dir=%s segments=%s runtime_dll_dir=%s", output_dir, len(segments), runtime_dir)
    report: dict[str, Any] = {
        "created_at": datetime.now().isoformat(sep=" "),
        "args": {k: (v.isoformat(sep=" ") if isinstance(v, datetime) else v) for k, v in vars(args).items() if k != "password"},
        "runtime_dll_dir": str(runtime_dir),
        "dll_dependencies": check_runtime_dll_dependencies(runtime_dir),
        "segments": [],
        "format_conversion": {},
        "format_diagnostics": {},
        "analyze_data": {},
        "merges": [],
    }
    results = download_segments(args, segments)
    report["analyze_data"] = analyze_segments(args, runtime_dir, results, output_dir)
    format_summary: dict[str, Any] = {"version": None, "segments": []}
    if args.diagnose_format:
        report["format_diagnostics"] = diagnose_format_conversion(args, runtime_dir, results, output_dir)
    elif args.skip_convert:
        format_summary = {"version": None, "segments": [], "skipped": True}
    else:
        format_summary = convert_segments(args, runtime_dir, results)
    report["format_conversion"] = format_summary
    if not args.skip_convert:
        if args.format_variant in ("reset", "both"):
            report["merges"].extend(merge_variant_outputs(args, runtime_dir, results, merged_dir, "reset"))
        if args.format_variant in ("global", "both"):
            report["merges"].extend(merge_variant_outputs(args, runtime_dir, results, merged_dir, "global"))
    report["segments"] = [asdict(item) for item in results]
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    generate_player(report, output_dir)
    LOG.info("report=%s", report_path)
    LOG.info("player_file=%s", output_dir / "index.html")
    if args.serve:
        serve_directory(output_dir, args.serve_host, args.serve_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
