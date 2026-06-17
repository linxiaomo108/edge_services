from __future__ import annotations

import atexit
import ctypes
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

_HAS_FORMAL_HIKSDK = False
if False:
    from edge_service.video.hik.sdk import (  # pragma: no cover
        DeviceInfo,
        DeviceInfoV40,
        FindFileData,
        HikSdk,
        LocalGeneralCfg,
        NET_DVR_FILE_NOFIND,
        NET_DVR_FILE_SUCCESS,
        NET_DVR_ISFINDING,
        NET_DVR_LOCAL_CFG_TYPE_GENERAL,
        NET_DVR_PLAYSTART,
        PlayCond,
        TimeStruct,
    )
    _HAS_FORMAL_HIKSDK = True
else:
    HikSdk = None

    class TimeStruct(ctypes.Structure):
        _fields_ = [
            ("dwYear", ctypes.c_ulong),
            ("dwMonth", ctypes.c_ulong),
            ("dwDay", ctypes.c_ulong),
            ("dwHour", ctypes.c_ulong),
            ("dwMinute", ctypes.c_ulong),
            ("dwSecond", ctypes.c_ulong),
        ]


    class DeviceInfo(ctypes.Structure):
        _fields_ = [
            ("sSerialNumber", ctypes.c_byte * 48),
            ("byAlarmInPortNum", ctypes.c_byte),
            ("byAlarmOutPortNum", ctypes.c_byte),
            ("byDiskNum", ctypes.c_byte),
            ("byDVRType", ctypes.c_byte),
            ("byChanNum", ctypes.c_byte),
            ("byStartChan", ctypes.c_byte),
            ("byAudioChanNum", ctypes.c_byte),
            ("byIPChanNum", ctypes.c_byte),
            ("byZeroChanNum", ctypes.c_byte),
            ("byMainProto", ctypes.c_byte),
            ("bySubProto", ctypes.c_byte),
            ("bySupport", ctypes.c_byte),
            ("bySupport1", ctypes.c_byte),
            ("bySupport2", ctypes.c_byte),
            ("wDevType", ctypes.c_ushort),
            ("bySupport3", ctypes.c_byte),
            ("byMultiStreamProto", ctypes.c_byte),
            ("byStartDChan", ctypes.c_byte),
            ("byStartDTalkChan", ctypes.c_byte),
            ("byHighDChanNum", ctypes.c_byte),
            ("bySupport4", ctypes.c_byte),
            ("byLanguageType", ctypes.c_byte),
            ("byVoiceInChanNum", ctypes.c_byte),
            ("byStartVoiceInChanNo", ctypes.c_byte),
            ("bySupport5", ctypes.c_byte),
            ("bySupport6", ctypes.c_byte),
            ("byMirrorChanNum", ctypes.c_byte),
            ("wStartMirrorChanNo", ctypes.c_ushort),
            ("bySupport7", ctypes.c_byte),
            ("byRes2", ctypes.c_byte),
        ]


    class DeviceInfoV40(ctypes.Structure):
        _fields_ = [
            ("struDeviceV30", DeviceInfo),
            ("bySupportLock", ctypes.c_byte),
            ("byRetryLoginTime", ctypes.c_byte),
            ("byPasswordLevel", ctypes.c_byte),
            ("byProxyType", ctypes.c_byte),
            ("dwSurplusLockTime", ctypes.c_ulong),
            ("byCharEncodeType", ctypes.c_byte),
            ("bySupportDev5", ctypes.c_byte),
            ("bySupport", ctypes.c_byte),
            ("byLoginMode", ctypes.c_byte),
            ("dwOEMCode", ctypes.c_ulong),
            ("iResidualValidity", ctypes.c_int),
            ("byResidualValidity", ctypes.c_byte),
            ("bySingleStartDTalkChan", ctypes.c_byte),
            ("bySingleDTalkChanNums", ctypes.c_byte),
            ("byPassWordResetLevel", ctypes.c_byte),
            ("bySupportStreamEncrypt", ctypes.c_byte),
            ("byMarketType", ctypes.c_byte),
            ("byRes2", ctypes.c_byte * 238),
        ]


    class PlayCond(ctypes.Structure):
        _fields_ = [
            ("dwChannel", ctypes.c_ulong),
            ("struStartTime", TimeStruct),
            ("struStopTime", TimeStruct),
            ("byDrawFrame", ctypes.c_byte),
            ("byRecordFileType", ctypes.c_byte),
            ("byRes", ctypes.c_byte * 62),
        ]


    class LocalGeneralCfg(ctypes.Structure):
        _fields_ = [
            ("byAlarmJsonPictureSeparate", ctypes.c_ubyte),
            ("byRes", ctypes.c_ubyte * 4),
            ("i64FileSize", ctypes.c_uint64),
            ("dwResumeUpgradeTimeout", ctypes.c_ulong),
            ("byAlarmReconnectMode", ctypes.c_ubyte),
            ("byStdXmlBufferSize", ctypes.c_ubyte),
            ("byMultiplexing", ctypes.c_ubyte),
            ("byFastUpgrade", ctypes.c_ubyte),
            ("byRes1", ctypes.c_ubyte * 232),
        ]


    class FindFileData(ctypes.Structure):
        _fields_ = [
            ("sFileName", ctypes.c_char * 100),
            ("struStartTime", TimeStruct),
            ("struStopTime", TimeStruct),
            ("dwFileSize", ctypes.c_ulong),
            ("sCardNum", ctypes.c_char * 32),
            ("byLocked", ctypes.c_byte),
            ("byFileType", ctypes.c_byte),
            ("byRes", ctypes.c_byte * 2),
        ]


    NET_DVR_LOCAL_CFG_TYPE_GENERAL = 0x0000
    NET_DVR_PLAYSTART = 1
    NET_DVR_FILE_SUCCESS = 1000
    NET_DVR_FILE_NOFIND = 1001
    NET_DVR_ISFINDING = 1002

LOG = logging.getLogger("hik_avsync_lab.sdk")
DEFAULT_FILE_SIZE_LIMIT_MB = int(os.getenv("HIK_LAB_SDK_FILE_LIMIT_MB", "1000"))
DEFAULT_STALL_TIMEOUT_SECONDS = int(os.getenv("HIK_LAB_SDK_STALL_TIMEOUT", "180"))
DOWNLOAD_PADDING_SECONDS = int(os.getenv("HIK_DOWNLOAD_PADDING_SECONDS", "0"))


class UserLoginInfo(ctypes.Structure):
    _fields_ = [
        ("sDeviceAddress", ctypes.c_char * 129),
        ("byUseTransport", ctypes.c_byte),
        ("wPort", ctypes.c_ushort),
        ("sUserName", ctypes.c_char * 64),
        ("sPassword", ctypes.c_char * 64),
        ("cbLoginResult", ctypes.c_void_p),
        ("pUser", ctypes.c_void_p),
        ("bUseAsynLogin", ctypes.c_bool),
        ("byProxyType", ctypes.c_byte),
        ("byUseUTCTime", ctypes.c_byte),
        ("byLoginMode", ctypes.c_byte),
        ("byHttps", ctypes.c_byte),
        ("iProxyID", ctypes.c_long),
        ("byVerifyMode", ctypes.c_byte),
        ("byRes2", ctypes.c_byte * 119),
    ]


@dataclass(frozen=True)
class StandaloneDownloadResult:
    path: str
    size_bytes: int
    channel_used: int
    record_type: int
    sdk_port: int
    user_id: int
    actual_duration_sec: float
    probe_status: str


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    pos: int
    size_bytes: int
    status: str
    window_label: str = "原始"
    is_main_window: bool = True


@dataclass
class StandaloneDownloadSession:
    sdk: "StandaloneHikSdk"
    login_uid: int
    sdk_port: int
    ip: str
    port: int
    username: str
    password: str
    channel: int
    start_channel: int | None
    start_digital_channel: int | None
    digital_channel_count: int | None
    preferred_sdk_channel: int | None = None
    preferred_record_type: int | None = None
    preferred_download_uid: int | None = None


def _resolve_sdk_dir(explicit_dir: str | None = None) -> str:
    candidates: list[Path] = []
    if explicit_dir and str(explicit_dir).strip():
        candidates.append(Path(str(explicit_dir).strip()))
    env_dir = os.getenv("HIK_LAB_SDK_DIR")
    if env_dir and env_dir.strip():
        candidates.append(Path(env_dir.strip()))
    base = Path(__file__).resolve().parent
    candidates.extend([
        base / "sdk" / "download",
        base / "sdk",
    ])
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if (candidate / "HCNetSDK.dll").exists():
            return str(candidate)
    return str(candidates[0]) if candidates else ""


def _set_time(target: TimeStruct, value: datetime) -> None:
    target.dwYear = value.year
    target.dwMonth = value.month
    target.dwDay = value.day
    target.dwHour = value.hour
    target.dwMinute = value.minute
    target.dwSecond = value.second


def _login_port_candidates(port: int) -> list[int]:
    try:
        preferred = int(port)
    except Exception:
        preferred = 8000
    if preferred <= 0:
        preferred = 8000
    candidates = [preferred]
    for fallback in (8000, 8001, 8002, 8003, 18081):
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def _safe_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _as_login_bytes(value: str, max_len: int) -> bytes:
    raw = str(value or "").encode("utf-8", errors="ignore")
    return raw[: max_len - 1] if max_len > 0 else b""


class StandaloneHikSdk:
    def __init__(self, sdk_dir: str | None = None) -> None:
        self.sdk_dir = _resolve_sdk_dir(sdk_dir)
        self._client = HikSdk(self.sdk_dir) if _HAS_FORMAL_HIKSDK and HikSdk is not None else None
        if self._client is not None:
            self.dll = self._client.dll
        else:
            dll_dirs = [Path(self.sdk_dir), Path(self.sdk_dir) / "ClientDemoDll"]
            if hasattr(os, "add_dll_directory"):
                self._dll_dir_handles = []
                for dll_dir in dll_dirs:
                    if dll_dir.exists():
                        self._dll_dir_handles.append(os.add_dll_directory(str(dll_dir)))
            else:
                self._dll_dir_handles = []
            path_parts = [str(path) for path in dll_dirs if path.exists()]
            if path_parts:
                os.environ["PATH"] = os.pathsep.join(path_parts + [os.environ.get("PATH", "")])
            dll_path = Path(self.sdk_dir) / "HCNetSDK.dll"
            if not dll_path.exists():
                raise FileNotFoundError(str(dll_path))
            self.dll = ctypes.WinDLL(str(dll_path))
            self._bind_local()
            if not self.dll.NET_DVR_Init():
                raise RuntimeError(f"NET_DVR_Init failed:{self.last_error()}")
            self.dll.NET_DVR_SetConnectTime(5000, 1)
            self.dll.NET_DVR_SetReconnect(10000, True)
        self._closed = False
        atexit.register(self.close)

    def _bind_local(self) -> None:
        self.dll.NET_DVR_Init.restype = ctypes.c_bool
        self.dll.NET_DVR_Cleanup.restype = ctypes.c_bool
        self.dll.NET_DVR_GetLastError.restype = ctypes.c_ulong
        self.dll.NET_DVR_SetConnectTime.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        self.dll.NET_DVR_SetConnectTime.restype = ctypes.c_bool
        self.dll.NET_DVR_SetReconnect.argtypes = [ctypes.c_ulong, ctypes.c_bool]
        self.dll.NET_DVR_SetReconnect.restype = ctypes.c_bool
        self.dll.NET_DVR_Login_V30.argtypes = [ctypes.c_char_p, ctypes.c_ushort, ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(DeviceInfo)]
        self.dll.NET_DVR_Login_V30.restype = ctypes.c_long
        self.dll.NET_DVR_Logout_V30.argtypes = [ctypes.c_long]
        self.dll.NET_DVR_Logout_V30.restype = ctypes.c_bool
        try:
            self.dll.NET_DVR_Login_V40.argtypes = [ctypes.POINTER(UserLoginInfo), ctypes.POINTER(DeviceInfoV40)]
            self.dll.NET_DVR_Login_V40.restype = ctypes.c_long
        except Exception:
            pass
        self.dll.NET_DVR_GetFileByTime_V40.argtypes = [ctypes.c_long, ctypes.c_char_p, ctypes.POINTER(PlayCond)]
        self.dll.NET_DVR_GetFileByTime_V40.restype = ctypes.c_long
        self.dll.NET_DVR_PlayBackControl_V40.argtypes = [ctypes.c_long, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        self.dll.NET_DVR_PlayBackControl_V40.restype = ctypes.c_bool
        self.dll.NET_DVR_StopGetFile.argtypes = [ctypes.c_long]
        self.dll.NET_DVR_StopGetFile.restype = ctypes.c_bool
        self.dll.NET_DVR_GetDownloadPos.argtypes = [ctypes.c_long]
        self.dll.NET_DVR_GetDownloadPos.restype = ctypes.c_int
        try:
            self.dll.NET_DVR_SetSDKLocalCfg.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
            self.dll.NET_DVR_SetSDKLocalCfg.restype = ctypes.c_bool
        except Exception:
            pass
        try:
            self.dll.NET_DVR_FindFile_V30.argtypes = [ctypes.c_long, ctypes.c_ulong, ctypes.c_ulong, ctypes.POINTER(TimeStruct), ctypes.POINTER(TimeStruct)]
            self.dll.NET_DVR_FindFile_V30.restype = ctypes.c_long
        except Exception:
            pass
        try:
            self.dll.NET_DVR_FindNextFile_V30.argtypes = [ctypes.c_long, ctypes.POINTER(FindFileData)]
            self.dll.NET_DVR_FindNextFile_V30.restype = ctypes.c_long
        except Exception:
            pass
        try:
            self.dll.NET_DVR_FindClose_V30.argtypes = [ctypes.c_long]
            self.dll.NET_DVR_FindClose_V30.restype = ctypes.c_bool
        except Exception:
            pass

    def last_error(self) -> int:
        if self._client is not None:
            return int(self._client.last_error())
        return int(self.dll.NET_DVR_GetLastError())

    def close(self) -> None:
        if self._closed:
            return
        if self._client is not None:
            self._client.close()
        else:
            try:
                self.dll.NET_DVR_Cleanup()
            except Exception:
                pass
        self._closed = True

    def set_file_size_limit(self, limit_mb: int = DEFAULT_FILE_SIZE_LIMIT_MB) -> None:
        try:
            cfg = LocalGeneralCfg()
            cfg.i64FileSize = int(limit_mb) * 1024 * 1024
            self.dll.NET_DVR_SetSDKLocalCfg(NET_DVR_LOCAL_CFG_TYPE_GENERAL, ctypes.byref(cfg))
        except Exception as exc:
            LOG.warning("set file size limit failed: %s", exc)

    def login(self, ip: str, port: int, username: str, password: str) -> tuple[int, DeviceInfo]:
        if self._client is not None:
            return self._client.login(ip, port, username, password)
        errors: list[str] = []
        if hasattr(self.dll, "NET_DVR_Login_V40"):
            try:
                login = UserLoginInfo()
                login.sDeviceAddress = _as_login_bytes(ip, 129)
                login.wPort = int(port)
                login.sUserName = _as_login_bytes(username, 64)
                login.sPassword = _as_login_bytes(password, 64)
                login.bUseAsynLogin = False
                info_v40 = DeviceInfoV40()
                user_id = int(self.dll.NET_DVR_Login_V40(ctypes.byref(login), ctypes.byref(info_v40)))
                if user_id >= 0:
                    return user_id, info_v40.struDeviceV30
                errors.append(f"Login_V40:{self.last_error()}")
            except Exception as exc:
                errors.append(f"Login_V40:{exc}")
        try:
            info = DeviceInfo()
            user_id = int(self.dll.NET_DVR_Login_V30(ip.encode("utf-8"), int(port), username.encode("utf-8"), password.encode("utf-8"), ctypes.byref(info)))
            if user_id >= 0:
                return user_id, info
            errors.append(f"Login_V30:{self.last_error()}")
        except Exception as exc:
            errors.append(f"Login_V30:{exc}")
        raise RuntimeError("; ".join(errors) if errors else f"login failed:{self.last_error()}")

    def logout(self, user_id: int) -> None:
        if self._client is not None:
            self._client.logout(user_id)
            return
        try:
            self.dll.NET_DVR_Logout_V30(int(user_id))
        except Exception:
            pass


def _device_channel_info(info: DeviceInfo) -> tuple[int | None, int | None, int | None]:
    start_channel = int(getattr(info, "byStartChan", 0) or 0) or None
    start_dchan_low = int(getattr(info, "byStartDChan", 0) or 0)
    ip_chan_num_low = int(getattr(info, "byIPChanNum", 0) or 0)
    high_dchan_raw = int(getattr(info, "byHighDChanNum", 0) or 0)
    digital_channel_count = ip_chan_num_low + (((high_dchan_raw & 0x01) << 8) if high_dchan_raw else 0)
    start_digital_channel = start_dchan_low + ((((high_dchan_raw >> 1) & 0x01) << 8) if high_dchan_raw else 0)
    return start_channel, (start_digital_channel or None), (digital_channel_count or None)


def open_download_session(*, sdk_dir: str | None, ip: str, port: int, username: str, password: str, channel: int) -> StandaloneDownloadSession:
    sdk = StandaloneHikSdk(sdk_dir=sdk_dir)
    sdk.set_file_size_limit()
    login_errors: list[str] = []
    for candidate_port in _login_port_candidates(port):
        try:
            login_uid, info = sdk.login(ip, candidate_port, username, password)
            start_channel, start_digital_channel, digital_channel_count = _device_channel_info(info)
            LOG.info(
                "login ok ip=%s port=%s uid=%s start_channel=%s start_digital_channel=%s digital_channel_count=%s",
                ip,
                candidate_port,
                login_uid,
                start_channel,
                start_digital_channel,
                digital_channel_count,
            )
            return StandaloneDownloadSession(
                sdk=sdk,
                login_uid=login_uid,
                sdk_port=int(candidate_port),
                ip=ip,
                port=port,
                username=username,
                password=password,
                channel=int(channel),
                start_channel=start_channel,
                start_digital_channel=start_digital_channel,
                digital_channel_count=digital_channel_count,
            )
        except Exception as exc:
            login_errors.append(str(exc))
    sdk.close()
    detail = " | ".join(login_errors[:3]) if login_errors else "unknown"
    raise RuntimeError(f"nvr_connect_failed:{ip}:{port}:{detail}")


def close_download_session(session: StandaloneDownloadSession | None) -> None:
    if session is None:
        return
    try:
        session.sdk.logout(session.login_uid)
    except Exception:
        pass
    session.sdk.close()


def _channel_candidates(session: StandaloneDownloadSession, *, hint_channel: int | None = None, sdk_channel_offsets: list[int] | None = None) -> list[int]:
    candidates: list[int] = []

    def add(value: int | None) -> None:
        if value is None:
            return
        ivalue = int(value)
        if ivalue > 0 and ivalue not in candidates:
            candidates.append(ivalue)

    web_channel = int(session.channel)
    if hint_channel is not None:
        add(hint_channel)
        LOG.info(
            "channel candidates locked web=%s hint=%s preferred=%s candidates=%s",
            web_channel,
            hint_channel,
            session.preferred_sdk_channel,
            candidates,
        )
        return candidates
    add(hint_channel)
    add(session.preferred_sdk_channel)
    add(web_channel)
    add(web_channel + 32)
    add(web_channel + 64)
    add(web_channel + 16)
    if web_channel > 32:
        add(web_channel - 32)
    if session.start_digital_channel:
        mapped_digital = int(session.start_digital_channel) + max(0, web_channel - 1)
        add(mapped_digital)
        for delta in (1, -1, 2, -2, 4, -4, 8, -8):
            add(mapped_digital + delta)
    if session.start_channel:
        mapped_analog = int(session.start_channel) + max(0, web_channel - 1)
        add(mapped_analog)
        add(mapped_analog + 32)
    for offset in (sdk_channel_offsets or []):
        add(web_channel + int(offset))
    for guessed_offset in (31, 32, 33, 63, 64, 65):
        add(web_channel + guessed_offset)
    LOG.info(
        "channel candidates web=%s hint=%s preferred=%s start_channel=%s start_digital_channel=%s digital_count=%s candidates=%s",
        web_channel,
        hint_channel,
        session.preferred_sdk_channel,
        session.start_channel,
        session.start_digital_channel,
        session.digital_channel_count,
        candidates[:24],
    )
    return candidates


def _record_type_candidates(session: StandaloneDownloadSession, locked_record_type: int | None = None) -> list[int]:
    candidates: list[int] = []
    for value in (locked_record_type, session.preferred_record_type, 0xFF, 0x00):
        if value is None:
            continue
        ivalue = int(value)
        if ivalue not in candidates:
            candidates.append(ivalue)
    return candidates or [0xFF]


def _probe_candidate(sdk: StandaloneHikSdk, user_id: int, channel: int, record_type: int, start_time: datetime, end_time: datetime, probe_seconds: int) -> ProbeResult:
    temp_path = os.path.join(tempfile.gettempdir(), f"hik_lab_probe_{os.getpid()}_{channel}_{record_type}_{int(time.time() * 1000)}.mp4")
    play = PlayCond()
    play.dwChannel = int(channel)
    _set_time(play.struStartTime, start_time)
    _set_time(play.struStopTime, end_time)
    play.byDrawFrame = 0
    play.byRecordFileType = int(record_type)
    c_path = ctypes.c_char_p(temp_path.encode("gbk", errors="ignore"))
    handle = int(sdk.dll.NET_DVR_GetFileByTime_V40(ctypes.c_long(int(user_id)), c_path, ctypes.byref(play)))
    if handle < 0:
        return ProbeResult(False, -1, 0, f"probe_handle_failed:{sdk.last_error()}")
    started = False
    last_pos = -999
    last_size = 0
    try:
        out_val = ctypes.c_ulong()
        if not sdk.dll.NET_DVR_PlayBackControl_V40(handle, NET_DVR_PLAYSTART, None, 0, None, ctypes.byref(out_val)):
            return ProbeResult(False, -1, 0, f"probe_play_start_failed:{sdk.last_error()}")
        started = True
        for _ in range(max(1, int(probe_seconds))):
            time.sleep(1)
            try:
                last_size = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
            except Exception:
                last_size = 0
            try:
                last_pos = int(sdk.dll.NET_DVR_GetDownloadPos(handle))
            except Exception:
                last_pos = -999
            if last_size > 0:
                if 0 <= last_pos <= 100:
                    return ProbeResult(True, int(last_pos), int(last_size), "probe_confirmed_ok")
                return ProbeResult(True, int(last_pos), int(last_size), "probe_wrote_bytes_no_pos")
            if last_pos == 100:
                return ProbeResult(False, int(last_pos), int(last_size), "probe_finished_zero_bytes")
        if started and last_size > 0:
            return ProbeResult(True, int(last_pos), int(last_size), "probe_wrote_bytes_timeout")
        return ProbeResult(False, int(last_pos if started else -1), int(last_size), "probe_no_progress_timeout")
    finally:
        try:
            sdk.dll.NET_DVR_StopGetFile(handle)
        except Exception:
            pass
        _safe_remove(temp_path)


def _run_same_channel_probe(sdk: StandaloneHikSdk, user_id: int, channel: int, start_time: datetime, end_time: datetime, record_type: int, *, probe_seconds: int = 10, probe_attempts: int = 3, on_probe_status: Callable[[str], None] | None = None) -> tuple[ProbeResult, ProbeResult | None]:
    main_window_result: ProbeResult | None = None
    nearby_result: ProbeResult | None = None
    windows = [
        ("原始", start_time, end_time, True),
        ("前30分钟", start_time - timedelta(minutes=30), end_time - timedelta(minutes=30), False),
        ("后30分钟", start_time + timedelta(minutes=30), end_time + timedelta(minutes=30), False),
    ]
    for label, win_start, win_end, is_main in windows:
        for attempt in range(1, max(1, int(probe_attempts)) + 1):
            result = _probe_candidate(sdk, user_id, channel, record_type, win_start, win_end, max(1, int(probe_seconds)))
            result = ProbeResult(
                ok=bool(result.ok),
                pos=int(result.pos),
                size_bytes=int(result.size_bytes),
                status=str(result.status or "probe_unknown"),
                window_label=label,
                is_main_window=bool(is_main),
            )
            if on_probe_status is not None:
                try:
                    on_probe_status(f"同通道探测[{label}] 第{attempt}/{max(1, int(probe_attempts))}次：{result.status}")
                except Exception:
                    pass
            if is_main and main_window_result is None:
                main_window_result = result
            if result.ok:
                if is_main:
                    return result, nearby_result
                if nearby_result is None:
                    nearby_result = ProbeResult(True, result.pos, result.size_bytes, "probe_main_window_inconclusive", label, False)
                break
            if attempt < max(1, int(probe_attempts)):
                time.sleep(1)
        if is_main and main_window_result and main_window_result.ok:
            return main_window_result, nearby_result
    if main_window_result is None:
        main_window_result = ProbeResult(False, -1, 0, "probe_no_progress_timeout")
    if nearby_result is not None:
        return main_window_result, nearby_result
    return main_window_result, None


def _search_recordings(sdk: StandaloneHikSdk, user_id: int, channel: int, start_time: datetime, end_time: datetime, *, max_results: int = 3) -> dict[str, object]:
    if not hasattr(sdk.dll, "NET_DVR_FindFile_V30"):
        return {"ok": False, "error": "findfile_unavailable", "matches": []}
    start_struct = TimeStruct()
    end_struct = TimeStruct()
    _set_time(start_struct, start_time)
    _set_time(end_struct, end_time)
    handle = int(sdk.dll.NET_DVR_FindFile_V30(ctypes.c_long(int(user_id)), ctypes.c_ulong(int(channel)), ctypes.c_ulong(0xFF), ctypes.byref(start_struct), ctypes.byref(end_struct)))
    if handle < 0:
        return {"ok": False, "error": f"findfile_failed:{sdk.last_error()}", "matches": []}
    matches: list[dict[str, object]] = []
    try:
        while len(matches) < int(max_results):
            data = FindFileData()
            status = int(sdk.dll.NET_DVR_FindNextFile_V30(handle, ctypes.byref(data)))
            if status == NET_DVR_ISFINDING:
                time.sleep(0.2)
                continue
            if status == NET_DVR_FILE_SUCCESS:
                filename = bytes(data.sFileName).split(b"\x00", 1)[0].decode("gbk", errors="ignore")
                matches.append({
                    "file_name": filename,
                    "size_bytes": int(data.dwFileSize),
                    "start_time": f"{int(data.struStartTime.dwYear):04d}-{int(data.struStartTime.dwMonth):02d}-{int(data.struStartTime.dwDay):02d} {int(data.struStartTime.dwHour):02d}:{int(data.struStartTime.dwMinute):02d}:{int(data.struStartTime.dwSecond):02d}",
                    "end_time": f"{int(data.struStopTime.dwYear):04d}-{int(data.struStopTime.dwMonth):02d}-{int(data.struStopTime.dwDay):02d} {int(data.struStopTime.dwHour):02d}:{int(data.struStopTime.dwMinute):02d}:{int(data.struStopTime.dwSecond):02d}",
                    "file_type": int(data.byFileType),
                    "locked": int(data.byLocked),
                })
                continue
            if status == NET_DVR_FILE_NOFIND:
                break
            return {"ok": False, "error": f"findnext_failed:{status}:{sdk.last_error()}", "matches": matches}
        return {"ok": True, "matches": matches, "found": bool(matches)}
    finally:
        try:
            sdk.dll.NET_DVR_FindClose_V30(handle)
        except Exception:
            pass


def _search_recordings_nearby(sdk: StandaloneHikSdk, user_id: int, channel: int, start_time: datetime, end_time: datetime) -> dict[str, object]:
    windows = [
        ("main", start_time, end_time),
        ("minus_30m", start_time - timedelta(minutes=30), end_time - timedelta(minutes=30)),
        ("plus_30m", start_time + timedelta(minutes=30), end_time + timedelta(minutes=30)),
        ("minus_2h", start_time - timedelta(hours=2), end_time - timedelta(hours=2)),
        ("plus_2h", start_time + timedelta(hours=2), end_time + timedelta(hours=2)),
    ]
    scanned: list[dict[str, object]] = []
    for label, win_start, win_end in windows:
        result = _search_recordings(sdk, user_id, channel, win_start, win_end)
        scanned.append({"label": label, "start": win_start.strftime("%Y-%m-%d %H:%M:%S"), "end": win_end.strftime("%Y-%m-%d %H:%M:%S"), "result": result})
        if result.get("ok") and result.get("found"):
            return {"ok": True, "found": True, "matched_window": label, "scanned": scanned, "matches": result.get("matches") or []}
    return {"ok": True, "found": False, "scanned": scanned, "matches": []}


def _download_file(sdk: StandaloneHikSdk, user_id: int, channel: int, record_type: int, start_time: datetime, end_time: datetime, output_path: str, on_progress: Callable[[float, str], None] | None, stall_timeout_sec: int) -> tuple[bool, str | None]:
    abs_path = os.path.abspath(output_path)
    _safe_remove(abs_path)
    play = PlayCond()
    play.dwChannel = int(channel)
    _set_time(play.struStartTime, start_time)
    _set_time(play.struStopTime, end_time)
    play.byDrawFrame = 0
    play.byRecordFileType = int(record_type)
    c_path = ctypes.c_char_p(abs_path.encode("gbk", errors="ignore"))
    handle = int(sdk.dll.NET_DVR_GetFileByTime_V40(ctypes.c_long(int(user_id)), c_path, ctypes.byref(play)))
    if handle < 0:
        _safe_remove(abs_path)
        return False, f"getfile_failed:{sdk.last_error()}"
    LOG.info("SDK下载句柄=%s ch=%s uid=%s rt=%s", handle, channel, user_id, record_type)
    started = False
    try:
        out_val = ctypes.c_ulong()
        if not sdk.dll.NET_DVR_PlayBackControl_V40(handle, NET_DVR_PLAYSTART, None, 0, None, ctypes.byref(out_val)):
            sdk.dll.NET_DVR_StopGetFile(handle)
            return False, f"playstart_failed:{sdk.last_error()}"
        started = True
        last_progress = -1
        stable = 0
        last_size = -1
        stall = 0
        while True:
            time.sleep(1)
            cur_size = os.path.getsize(abs_path) if os.path.exists(abs_path) else -1
            if cur_size >= 0:
                if cur_size == last_size:
                    stall += 1
                else:
                    stall = 0
                    last_size = cur_size
                if stall_timeout_sec > 0 and stall >= int(stall_timeout_sec):
                    sdk.dll.NET_DVR_StopGetFile(handle)
                    return False, "stall_timeout"
            progress = int(sdk.dll.NET_DVR_GetDownloadPos(handle))
            if 0 <= progress <= 100:
                if progress == 100:
                    time.sleep(2)
                    sdk.dll.NET_DVR_StopGetFile(handle)
                    return True, None
                if progress != last_progress:
                    last_progress = progress
                    stable = 0
                    if on_progress is not None:
                        on_progress(progress / 100.0, f"SDK {progress}% ch={channel} rt={record_type}")
                else:
                    stable += 1
                    if stable > 60:
                        sdk.dll.NET_DVR_StopGetFile(handle)
                        return False, "progress_stuck"
                continue
            if progress == -1:
                err = sdk.last_error()
                sdk.dll.NET_DVR_StopGetFile(handle)
                if err == 0:
                    return True, None
                return False, f"download_error:{err}"
            sdk.dll.NET_DVR_StopGetFile(handle)
            return False, f"invalid_progress:{progress}"
    finally:
        if started:
            try:
                sdk.dll.NET_DVR_StopGetFile(handle)
            except Exception:
                pass


def download_by_time_with_session(session: StandaloneDownloadSession, *, start_time: datetime, end_time: datetime, output_path: str, on_progress: Callable[[float, str], None] | None = None, hint_channel: int | None = None, locked_record_type: int | None = None, sdk_channel_offsets: list[int] | None = None, probe_seconds: int = 6, stall_timeout_sec: int = DEFAULT_STALL_TIMEOUT_SECONDS) -> StandaloneDownloadResult:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    padding = timedelta(seconds=DOWNLOAD_PADDING_SECONDS)
    expanded_start = start_time - padding
    expanded_end = end_time + padding
    last_error = "no_candidate"
    user_ids: list[int] = []
    if session.preferred_download_uid is not None:
        user_ids.append(int(session.preferred_download_uid))
    if 0 not in user_ids:
        user_ids.append(0)
    if int(session.login_uid) not in user_ids:
        user_ids.append(int(session.login_uid))
    for channel in _channel_candidates(session, hint_channel=hint_channel, sdk_channel_offsets=sdk_channel_offsets):
        if hint_channel is not None and locked_record_type is not None:
            for uid_candidate in user_ids:
                main_probe_result, nearby_probe_result = _run_same_channel_probe(
                    session.sdk,
                    uid_candidate,
                    channel,
                    expanded_start,
                    expanded_end,
                    int(locked_record_type),
                    probe_seconds=max(1, int(probe_seconds or 10)),
                    probe_attempts=3,
                    on_probe_status=lambda message: LOG.info(message),
                )
                if not main_probe_result.ok:
                    LOG.info(
                        "locked same-channel probe failed uid=%s channel=%s record_type=%s status=%s nearby=%s",
                        uid_candidate,
                        channel,
                        locked_record_type,
                        main_probe_result.status,
                        nearby_probe_result.status if nearby_probe_result is not None else "",
                    )
                    last_error = str(main_probe_result.status or f"probe_failed:{uid_candidate}:{channel}:{locked_record_type}")
                    continue
                LOG.info(
                    "locked same-channel probe ok uid=%s channel=%s record_type=%s main_status=%s probe_pos=%s probe_size=%s",
                    uid_candidate,
                    channel,
                    locked_record_type,
                    main_probe_result.status,
                    main_probe_result.pos,
                    main_probe_result.size_bytes,
                )
                download_windows: list[tuple[str, datetime, datetime]] = [("原始", expanded_start, expanded_end)]
                if nearby_probe_result is not None and nearby_probe_result.ok:
                    if nearby_probe_result.window_label == "前30分钟":
                        download_windows.append((nearby_probe_result.window_label, expanded_start - timedelta(minutes=30), expanded_end - timedelta(minutes=30)))
                    elif nearby_probe_result.window_label == "后30分钟":
                        download_windows.append((nearby_probe_result.window_label, expanded_start + timedelta(minutes=30), expanded_end + timedelta(minutes=30)))
                for window_label, win_start, win_end in download_windows:
                    ok, error = _download_file(session.sdk, uid_candidate, channel, int(locked_record_type), win_start, win_end, str(output), on_progress, stall_timeout_sec)
                    LOG.info("locked probed download uid=%s channel=%s record_type=%s window=%s ok=%s error=%s window_range=%s~%s", uid_candidate, channel, locked_record_type, window_label, ok, error, win_start, win_end)
                    if ok and output.exists() and output.stat().st_size > 0:
                        session.preferred_sdk_channel = int(channel)
                        session.preferred_record_type = int(locked_record_type)
                        session.preferred_download_uid = int(uid_candidate)
                        return StandaloneDownloadResult(
                            path=str(output),
                            size_bytes=output.stat().st_size,
                            channel_used=int(channel),
                            record_type=int(locked_record_type),
                            sdk_port=int(session.sdk_port),
                            user_id=int(uid_candidate),
                            actual_duration_sec=max(0.0, (win_end - win_start).total_seconds()),
                            probe_status=str(main_probe_result.status),
                        )
                    last_error = str(error or f"locked_probed_download_failed:{window_label}:{uid_candidate}:{channel}:{locked_record_type}")
            continue
        search = _search_recordings(session.sdk, session.login_uid, channel, start_time, end_time)
        LOG.info("findfile channel=%s result=%s", channel, search)
        if search.get("ok") and not search.get("found"):
            nearby = _search_recordings_nearby(session.sdk, session.login_uid, channel, start_time, end_time)
            LOG.info("findfile_nearby channel=%s result=%s", channel, nearby)
            if nearby.get("found"):
                last_error = f"recording_exists_nearby:{nearby.get('matched_window')}"
            else:
                last_error = "findfile_no_recording"
            continue
        for record_type in _record_type_candidates(session, locked_record_type=locked_record_type):
            probe_ok = True
            probe_status = "probe_skipped"
            if probe_seconds > 0:
                probe_result = _probe_candidate(session.sdk, session.login_uid, channel, record_type, start_time, end_time, probe_seconds)
                probe_ok = bool(probe_result.ok)
                probe_status = str(probe_result.status)
                LOG.info("probe channel=%s record_type=%s status=%s", channel, record_type, probe_status)
            if not probe_ok:
                last_error = probe_status
                continue
            ok, error = _download_file(session.sdk, session.login_uid, channel, record_type, start_time, end_time, str(output), on_progress, stall_timeout_sec)
            if ok and output.exists() and output.stat().st_size > 0:
                session.preferred_sdk_channel = int(channel)
                session.preferred_record_type = int(record_type)
                return StandaloneDownloadResult(
                    path=str(output),
                    size_bytes=output.stat().st_size,
                    channel_used=int(channel),
                    record_type=int(record_type),
                    sdk_port=int(session.sdk_port),
                    user_id=int(session.login_uid),
                    actual_duration_sec=max(0.0, (end_time - start_time).total_seconds()),
                    probe_status=probe_status,
                )
            last_error = str(error or "download_failed")
            _safe_remove(str(output))
    raise RuntimeError(f"sdk_download_failed:{last_error}")
