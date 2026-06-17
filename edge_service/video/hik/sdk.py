from __future__ import annotations

import atexit
import ctypes
import threading
from dataclasses import dataclass
from ctypes import Structure, c_bool, c_byte, c_char, c_char_p, c_int, c_long, c_ubyte, c_uint64, c_ulong, c_ushort, c_void_p, byref

from .loader import load_hik_sdk


class TimeStruct(Structure):
    _fields_ = [
        ("dwYear", c_ulong),
        ("dwMonth", c_ulong),
        ("dwDay", c_ulong),
        ("dwHour", c_ulong),
        ("dwMinute", c_ulong),
        ("dwSecond", c_ulong),
    ]


class DeviceInfo(Structure):
    _fields_ = [
        ("sSerialNumber", c_byte * 48),
        ("byAlarmInPortNum", c_byte),
        ("byAlarmOutPortNum", c_byte),
        ("byDiskNum", c_byte),
        ("byDVRType", c_byte),
        ("byChanNum", c_byte),
        ("byStartChan", c_byte),
        ("byAudioChanNum", c_byte),
        ("byIPChanNum", c_byte),
        ("byZeroChanNum", c_byte),
        ("byMainProto", c_byte),
        ("bySubProto", c_byte),
        ("bySupport", c_byte),
        ("bySupport1", c_byte),
        ("bySupport2", c_byte),
        ("wDevType", c_ushort),
        ("bySupport3", c_byte),
        ("byMultiStreamProto", c_byte),
        ("byStartDChan", c_byte),
        ("byStartDTalkChan", c_byte),
        ("byHighDChanNum", c_byte),
        ("bySupport4", c_byte),
        ("byLanguageType", c_byte),
        ("byVoiceInChanNum", c_byte),
        ("byStartVoiceInChanNo", c_byte),
        ("bySupport5", c_byte),
        ("bySupport6", c_byte),
        ("byMirrorChanNum", c_byte),
        ("wStartMirrorChanNo", c_ushort),
        ("bySupport7", c_byte),
        ("byRes2", c_byte),
    ]


class DeviceInfoV40(Structure):
    _fields_ = [
        ("struDeviceV30", DeviceInfo),
        ("bySupportLock", c_byte),
        ("byRetryLoginTime", c_byte),
        ("byPasswordLevel", c_byte),
        ("byProxyType", c_byte),
        ("dwSurplusLockTime", c_ulong),
        ("byCharEncodeType", c_byte),
        ("bySupportDev5", c_byte),
        ("bySupport", c_byte),
        ("byLoginMode", c_byte),
        ("dwOEMCode", c_ulong),
        ("iResidualValidity", c_int),
        ("byResidualValidity", c_byte),
        ("bySingleStartDTalkChan", c_byte),
        ("bySingleDTalkChanNums", c_byte),
        ("byPassWordResetLevel", c_byte),
        ("bySupportStreamEncrypt", c_byte),
        ("byMarketType", c_byte),
        ("byRes2", c_byte * 238),
    ]


@dataclass(frozen=True)
class DeviceChannelInfo:
    start_channel: int
    analog_channel_count: int
    start_digital_channel: int
    digital_channel_count: int
    high_dchan_raw: int


def get_device_channel_info(info: DeviceInfo) -> DeviceChannelInfo:
    start_channel = int(getattr(info, "byStartChan", 0) or 0)
    analog_channel_count = int(getattr(info, "byChanNum", 0) or 0)
    start_dchan_low = int(getattr(info, "byStartDChan", 0) or 0)
    digital_channel_count_low = int(getattr(info, "byIPChanNum", 0) or 0)
    high_dchan_raw = int(getattr(info, "byHighDChanNum", 0) or 0)
    digital_channel_count = digital_channel_count_low + (((high_dchan_raw & 0x01) << 8) if high_dchan_raw else 0)
    start_digital_channel = start_dchan_low + ((((high_dchan_raw >> 1) & 0x01) << 8) if high_dchan_raw else 0)
    return DeviceChannelInfo(
        start_channel=start_channel,
        analog_channel_count=analog_channel_count,
        start_digital_channel=start_digital_channel,
        digital_channel_count=digital_channel_count,
        high_dchan_raw=high_dchan_raw,
    )


def get_start_digital_channel(info: DeviceInfo) -> int:
    return get_device_channel_info(info).start_digital_channel


def get_digital_channel_count(info: DeviceInfo) -> int:
    return get_device_channel_info(info).digital_channel_count


class UserLoginInfo(Structure):
    _fields_ = [
        ("sDeviceAddress", c_char * 129),
        ("byUseTransport", c_byte),
        ("wPort", c_ushort),
        ("sUserName", c_char * 64),
        ("sPassword", c_char * 64),
        ("cbLoginResult", c_void_p),
        ("pUser", c_void_p),
        ("bUseAsynLogin", c_bool),
        ("byProxyType", c_byte),
        ("byUseUTCTime", c_byte),
        ("byLoginMode", c_byte),
        ("byHttps", c_byte),
        ("iProxyID", c_long),
        ("byVerifyMode", c_byte),
        ("byRes2", c_byte * 119),
    ]


class PlayCond(Structure):
    _fields_ = [
        ("dwChannel", c_ulong),
        ("struStartTime", TimeStruct),
        ("struStopTime", TimeStruct),
        ("byDrawFrame", c_byte),
        ("byRecordFileType", c_byte),
        ("byRes", c_byte * 62),
    ]


class LocalGeneralCfg(Structure):
    _fields_ = [
        ("byAlarmJsonPictureSeparate", c_ubyte),
        ("byRes", c_ubyte * 4),
        ("i64FileSize", c_uint64),
        ("dwResumeUpgradeTimeout", c_ulong),
        ("byAlarmReconnectMode", c_ubyte),
        ("byStdXmlBufferSize", c_ubyte),
        ("byMultiplexing", c_ubyte),
        ("byFastUpgrade", c_ubyte),
        ("byRes1", c_ubyte * 232),
    ]


# 录像文件查找结构体
class FileName(Structure):
    _fields_ = [
        ("sFileName", c_byte * 100),
    ]


class FindFileData(Structure):
    _fields_ = [
        ("sFileName", c_char * 100),
        ("struStartTime", TimeStruct),
        ("struStopTime", TimeStruct),
        ("dwFileSize", c_ulong),
        ("sCardNum", c_char * 32),
        ("byLocked", c_byte),
        ("byFileType", c_byte),
        ("byRes", c_byte * 2),
    ]


NET_DVR_LOCAL_CFG_TYPE_GENERAL = 0x0000
NET_DVR_PLAYSTART = 1

# 录像查找命令
NET_DVR_FILE_SUCCESS = 1000
NET_DVR_FILE_NOFIND = 1001
NET_DVR_ISFINDING = 1002


@dataclass
class HikSdkRuntime:
    dll: object
    sdk_dir: str | None
    ref_count: int = 0
    initialized: bool = False


class HikSdkManager:
    _lock = threading.RLock()
    _runtimes: dict[str, HikSdkRuntime] = {}
    _cleanup_registered = False

    @classmethod
    def _normalize_sdk_dir(cls, sdk_dir: str | None) -> str:
        text = str(sdk_dir or "").strip()
        return text.lower()

    @classmethod
    def _register_atexit_cleanup(cls) -> None:
        with cls._lock:
            if cls._cleanup_registered:
                return
            atexit.register(cls.cleanup_all)
            cls._cleanup_registered = True

    @classmethod
    def _bind_runtime(cls, dll: object) -> None:
        dll.NET_DVR_Init.restype = c_bool
        dll.NET_DVR_Cleanup.restype = c_bool
        dll.NET_DVR_GetLastError.restype = c_ulong

        dll.NET_DVR_SetConnectTime.argtypes = [c_ulong, c_ulong]
        dll.NET_DVR_SetConnectTime.restype = c_bool
        dll.NET_DVR_SetReconnect.argtypes = [c_ulong, c_bool]
        dll.NET_DVR_SetReconnect.restype = c_bool

        dll.NET_DVR_Login_V30.argtypes = [c_char_p, c_ushort, c_char_p, c_char_p, ctypes.POINTER(DeviceInfo)]
        dll.NET_DVR_Login_V30.restype = c_long
        dll.NET_DVR_Logout_V30.argtypes = [c_long]
        dll.NET_DVR_Logout_V30.restype = c_bool

        try:
            dll.NET_DVR_Login_V40.argtypes = [ctypes.POINTER(UserLoginInfo), ctypes.POINTER(DeviceInfoV40)]
            dll.NET_DVR_Login_V40.restype = c_long
        except Exception:
            pass

        dll.NET_DVR_GetFileByTime_V40.argtypes = [c_long, c_char_p, ctypes.POINTER(PlayCond)]
        dll.NET_DVR_GetFileByTime_V40.restype = c_long

        dll.NET_DVR_PlayBackControl_V40.argtypes = [c_long, c_ulong, c_void_p, c_ulong, c_void_p, ctypes.POINTER(c_ulong)]
        dll.NET_DVR_PlayBackControl_V40.restype = c_bool

        dll.NET_DVR_StopGetFile.argtypes = [c_long]
        dll.NET_DVR_StopGetFile.restype = c_bool

        dll.NET_DVR_GetDownloadPos.argtypes = [c_long]
        dll.NET_DVR_GetDownloadPos.restype = c_int

        try:
            dll.NET_DVR_SetSDKLocalCfg.argtypes = [c_ulong, ctypes.c_void_p]
            dll.NET_DVR_SetSDKLocalCfg.restype = c_bool
        except Exception:
            pass

        try:
            dll.NET_DVR_FindFile_V30.argtypes = [c_long, c_ulong, c_ulong, ctypes.POINTER(TimeStruct), ctypes.POINTER(TimeStruct)]
            dll.NET_DVR_FindFile_V30.restype = c_long
        except Exception:
            pass

        try:
            dll.NET_DVR_FindNextFile_V30.argtypes = [c_long, ctypes.POINTER(FindFileData)]
            dll.NET_DVR_FindNextFile_V30.restype = c_long
        except Exception:
            pass

        try:
            dll.NET_DVR_FindClose_V30.argtypes = [c_long]
            dll.NET_DVR_FindClose_V30.restype = c_bool
        except Exception:
            pass

    @classmethod
    def acquire(cls, sdk_dir: str | None = None) -> HikSdkRuntime:
        cls._register_atexit_cleanup()
        key = cls._normalize_sdk_dir(sdk_dir)
        with cls._lock:
            runtime = cls._runtimes.get(key)
            if runtime is None:
                dll = load_hik_sdk(sdk_dir)
                cls._bind_runtime(dll)
                runtime = HikSdkRuntime(dll=dll, sdk_dir=sdk_dir)
                cls._runtimes[key] = runtime
            if not runtime.initialized:
                if not runtime.dll.NET_DVR_Init():
                    raise RuntimeError(f"NET_DVR_Init failed: {cls.last_error(runtime)}")
                runtime.dll.NET_DVR_SetConnectTime(5000, 1)
                runtime.dll.NET_DVR_SetReconnect(10000, True)
                runtime.initialized = True
            runtime.ref_count += 1
            return runtime

    @classmethod
    def release(cls, runtime: HikSdkRuntime | None) -> None:
        if runtime is None:
            return
        key = cls._normalize_sdk_dir(runtime.sdk_dir)
        with cls._lock:
            tracked = cls._runtimes.get(key)
            if tracked is None:
                return
            if tracked.ref_count > 0:
                tracked.ref_count -= 1

    @classmethod
    def cleanup_all(cls) -> None:
        with cls._lock:
            runtimes = list(cls._runtimes.values())
            cls._runtimes.clear()
        for runtime in runtimes:
            if not runtime.initialized:
                continue
            try:
                runtime.dll.NET_DVR_Cleanup()
            except Exception:
                pass
            runtime.initialized = False
            runtime.ref_count = 0

    @classmethod
    def last_error(cls, runtime: HikSdkRuntime | None) -> int:
        if runtime is None:
            return -1
        try:
            return int(runtime.dll.NET_DVR_GetLastError())
        except Exception:
            return -1


class HikSdk:
    def __init__(self, sdk_dir: str | None = None) -> None:
        self._runtime = HikSdkManager.acquire(sdk_dir)
        self.dll = self._runtime.dll
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        HikSdkManager.release(self._runtime)
        self._closed = True

    def _bind(self) -> None:
        HikSdkManager._bind_runtime(self.dll)

    def last_error(self) -> int:
        return HikSdkManager.last_error(self._runtime)

    @staticmethod
    def _as_login_bytes(value: str, max_len: int) -> bytes:
        raw = str(value or "").encode("utf-8", errors="ignore")
        if max_len <= 0:
            return b""
        return raw[: max_len - 1]

    def _login_v40(self, ip: str, port: int, username: str, password: str) -> tuple[int, DeviceInfo]:
        if not hasattr(self.dll, "NET_DVR_Login_V40"):
            raise RuntimeError("login v40 unsupported")
        login = UserLoginInfo()
        login.sDeviceAddress = self._as_login_bytes(ip, 129)
        login.wPort = int(port)
        login.sUserName = self._as_login_bytes(username, 64)
        login.sPassword = self._as_login_bytes(password, 64)
        login.bUseAsynLogin = False
        info_v40 = DeviceInfoV40()
        user_id = self.dll.NET_DVR_Login_V40(byref(login), byref(info_v40))
        if user_id < 0:
            raise RuntimeError(f"login failed: {self.last_error()}")
        return int(user_id), info_v40.struDeviceV30

    def _login_v30(self, ip: str, port: int, username: str, password: str) -> tuple[int, DeviceInfo]:
        info = DeviceInfo()
        user_id = self.dll.NET_DVR_Login_V30(ip.encode("utf-8"), int(port), username.encode("utf-8"), password.encode("utf-8"), byref(info))
        if user_id < 0:
            raise RuntimeError(f"login failed: {self.last_error()}")
        return int(user_id), info

    def login(self, ip: str, port: int, username: str, password: str) -> tuple[int, DeviceInfo]:
        errors: list[str] = []
        try:
            return self._login_v40(ip, port, username, password)
        except Exception as exc:
            errors.append(f"Login_V40: {exc}")
        try:
            return self._login_v30(ip, port, username, password)
        except Exception as exc:
            errors.append(f"Login_V30: {exc}")
        raise RuntimeError("; ".join(errors) if errors else f"login failed: {self.last_error()}")

    def logout(self, user_id: int) -> None:
        try:
            self.dll.NET_DVR_Logout_V30(int(user_id))
        except Exception:
            pass
