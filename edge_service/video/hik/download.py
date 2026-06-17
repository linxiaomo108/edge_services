from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import ctypes

from ...db import Db, DbConfig
from ..ffmpeg import has_audio_stream, probe_media_start_seconds, rebuild_zero_based_timeline
from .sdk import (
    HikSdk, LocalGeneralCfg, NET_DVR_LOCAL_CFG_TYPE_GENERAL, PlayCond, TimeStruct, NET_DVR_PLAYSTART,
    FindFileData, NET_DVR_FILE_SUCCESS, NET_DVR_FILE_NOFIND, NET_DVR_ISFINDING, get_device_channel_info,
)

log = logging.getLogger("edge.hik")

DEFAULT_FILE_SIZE_LIMIT_MB = int(os.getenv("HIK_SDK_FILE_LIMIT_MB", "1000"))
STALL_TIMEOUT_SECONDS = int(os.getenv("HIK_SDK_STALL_TIMEOUT", "180"))
MIN_EXPECTED_BYTES_PER_SECOND = int(os.getenv("HIK_SDK_MIN_BYTES_PER_SEC", "120000"))
MIN_VALID_VIDEO_BYTES = 1024
DOWNLOAD_PADDING_SECONDS = int(os.getenv("HIK_DOWNLOAD_PADDING_SECONDS", "0"))
_MODEL_PROFILES: tuple[dict[str, object], ...] = (
    {"canonical": "DS-7608N", "patterns": ("DS-7608N",), "hik_ip_nvr": True, "extended_scan": True, "strict_probe": True},
    {"canonical": "DS-7616N", "patterns": ("DS-7616N",), "hik_ip_nvr": True, "extended_scan": True, "strict_probe": True},
    {"canonical": "DS-7632N", "patterns": ("DS-7632N",), "hik_ip_nvr": True, "extended_scan": True, "strict_probe": True},
    {"canonical": "DS-7808N", "patterns": ("DS-7808N",), "hik_ip_nvr": True, "extended_scan": True, "strict_probe": True},
    {"canonical": "DS-7816N", "patterns": ("DS-7816N",), "hik_ip_nvr": True, "extended_scan": False, "strict_probe": False},
    {"canonical": "DS-7832N", "patterns": ("DS-7832N",), "hik_ip_nvr": True, "extended_scan": False, "strict_probe": False},
)


_BUILTIN_MODEL_OFFSETS: dict[str, tuple[int, int]] = {
    "DS-7608N": (32, 0x00),
    "DS-7808N": (32, 0x00),
}


@dataclass(frozen=True)
class DownloadResult:
    path: str
    size_bytes: int
    channel_used: int
    hint_uid: int = 0
    hint_record_type: int = 0x00
    hint_reusable: bool = False
    mapping_source: str = ""
    persisted_channel: int = 0
    persisted_failed: bool = False
    actual_duration_sec: float = 0.0
    accepted_as_tail: bool = False


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    pos: int
    size_bytes: int
    status: str
    window_label: str = "原始"
    is_main_window: bool = True


@dataclass
class HikDownloadSession:
    sdk: HikSdk
    login_uid: int
    sdk_start_channel: int | None
    sdk_start_dchan: int | None
    ip_chan_num: int | None
    device_model: str | None
    persisted_sdk_channel: int | None
    persisted_record_type: int | None
    channel_offset: int | None
    db_path: str | None
    nvr_device_id: int | None
    ip: str
    port: int
    username: str
    password: str
    channel: int
    preferred_download_uid: int | None = None


ProgressCallback = Callable[[float, int, float], None]
CancelCheck = Callable[[], str | None]


def _set_time(t: TimeStruct, dt: datetime) -> None:
    t.dwYear = dt.year
    t.dwMonth = dt.month
    t.dwDay = dt.day
    t.dwHour = dt.hour
    t.dwMinute = dt.minute
    t.dwSecond = dt.second


def _normalize_record_type(value: int | None) -> int | None:
    try:
        rt = int(value) if value is not None else 0xFF
    except Exception:
        return None
    return rt if rt in {0x00, 0x01, 0xFF} else None


def _ffmpeg_bin() -> str:
    return os.getenv("EDGE_FFMPEG_BIN") or "ffmpeg"


def _ffprobe_bin() -> str:
    b = _ffmpeg_bin()
    if b.lower().endswith("ffmpeg.exe"):
        return b[:-10] + "ffprobe.exe"
    if b.lower().endswith("ffmpeg"):
        return b[:-6] + "ffprobe"
    return "ffprobe"


def _normalize_model_text(value: str | None) -> str:
    return str(value or "").strip().upper()


def _get_model_profile(device_model: str | None) -> dict[str, object] | None:
    model = _normalize_model_text(device_model)
    if not model:
        return None
    for profile in _MODEL_PROFILES:
        patterns = tuple(str(item).upper() for item in (profile.get("patterns") or ()))
        if any(pattern and pattern in model for pattern in patterns):
            return profile
    return None


def _canonicalize_device_model(device_model: str | None) -> str | None:
    model = str(device_model or "").strip()
    profile = _get_model_profile(model)
    if profile is not None:
        return str(profile.get("canonical") or model).strip() or None
    return model or None


def _probe_media_duration_seconds(path: str) -> float:
    try:
        cmd = [
            _ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore").strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def _trim_to_duration(input_path: str, target_duration_sec: float) -> bool:
    """裁剪视频到指定时长，原地替换。返回是否成功。"""
    try:
        if target_duration_sec <= 0:
            return False
        temp_output = input_path + ".trimmed.mp4"
        _safe_remove(temp_output)
        cmd = [
            _ffmpeg_bin(),
            "-y",
            "-i", str(input_path),
            "-t", str(target_duration_sec),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(temp_output),
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        if result.returncode != 0:
            log.warning("裁剪失败: %s", result.stdout[:500] if result.stdout else "unknown")
            _safe_remove(temp_output)
            return False
        if not os.path.exists(temp_output) or os.path.getsize(temp_output) < MIN_VALID_VIDEO_BYTES:
            _safe_remove(temp_output)
            return False
        _safe_remove(input_path)
        os.replace(temp_output, input_path)
        log.info("裁剪成功: %s -> %.1fs", input_path, target_duration_sec)
        return True
    except Exception as e:
        log.warning("裁剪异常: %s", e)
        return False


def _probe_media_start_time(path: str) -> float:
    """探测媒体文件的起始时间戳（秒）。返回所有流中最小的start_time。"""
    try:
        cmd = [
            _ffprobe_bin(),
            "-v", "error",
            "-show_entries", "stream=start_time",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore").strip()
        if not out:
            return 0.0
        times = []
        for line in out.split("\n"):
            line = line.strip()
            if line and line != "N/A":
                try:
                    times.append(float(line))
                except ValueError:
                    pass
        return min(times) if times else 0.0
    except Exception:
        return 0.0


def _probe_stream_start_times(path: str) -> dict[str, float]:
    if not path or not os.path.exists(path):
        return {}
    out: dict[str, float] = {}
    try:
        cmd = [
            _ffprobe_bin(),
            "-v", "error",
            "-show_entries", "stream=codec_type,start_time",
            "-of", "default=noprint_wrappers=1",
            str(path),
        ]
        raw = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        codec_type = ""
        for line_raw in (raw or "").splitlines():
            line = str(line_raw or "").strip()
            if not line:
                continue
            if line.startswith("codec_type="):
                codec_type = line.split("=", 1)[1].strip().lower()
                continue
            if line.startswith("start_time=") and codec_type and codec_type not in out:
                try:
                    out[codec_type] = float(line.split("=", 1)[1].strip() or 0.0)
                except Exception:
                    pass
    except Exception:
        return {}
    return out


def _probe_timeline_state(path: str) -> dict[str, float | bool]:
    media_start = float(probe_media_start_seconds(path) or 0.0)
    stream_starts = _probe_stream_start_times(path)
    positive_starts = [float(v) for v in stream_starts.values() if float(v) >= 0.0]
    max_stream_start = max(positive_starts) if positive_starts else media_start
    min_stream_start = min(positive_starts) if positive_starts else media_start
    stream_gap = abs(max_stream_start - min_stream_start)
    dirty = not (media_start <= 2.0 and max_stream_start <= 2.0 and stream_gap <= 1.0)
    return {
        "media_start": media_start,
        "video_start": float(stream_starts.get("video") or 0.0),
        "audio_start": float(stream_starts.get("audio") or 0.0),
        "max_start": max_stream_start,
        "min_start": min_stream_start,
        "stream_gap": stream_gap,
        "dirty": dirty,
    }


def _remux_zero_based_timeline(input_path: str, output_path: str) -> None:
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-fflags", "+genpts",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a:0?",
    ]
    cmd += [
        "-c:v", "copy",
        "-c:a", "copy",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) < MIN_VALID_VIDEO_BYTES:
        _safe_remove(output_path)
        raise RuntimeError(result.stdout[:500] if result.stdout else "remux_failed")


def _normalize_timeline_if_needed(input_path: str) -> bool:
    try:
        before = _probe_timeline_state(input_path)
        if not bool(before.get("dirty")):
            return False
        before_media = float(before.get("media_start") or 0.0)
        before_video = float(before.get("video_start") or 0.0)
        before_audio = float(before.get("audio_start") or 0.0)
        before_gap = float(before.get("stream_gap") or 0.0)
        log.warning(
            "检测到异常时间线: media=%.3fs video=%.3fs audio=%.3fs gap=%.3fs，直接执行完整重建",
            before_media,
            before_video,
            before_audio,
            before_gap,
        )

        rebuild_output = input_path + ".timeline_fix.mp4"
        _safe_remove(rebuild_output)
        rebuild_zero_based_timeline(str(input_path), str(rebuild_output))
        rebuild_state = _probe_timeline_state(rebuild_output)
        if bool(rebuild_state.get("dirty")):
            log.warning(
                "时间线完整重建后仍异常: media=%.3fs video=%.3fs audio=%.3fs gap=%.3fs",
                float(rebuild_state.get("media_start") or 0.0),
                float(rebuild_state.get("video_start") or 0.0),
                float(rebuild_state.get("audio_start") or 0.0),
                float(rebuild_state.get("stream_gap") or 0.0),
            )
            _safe_remove(rebuild_output)
            return False

        _safe_remove(input_path)
        os.replace(rebuild_output, input_path)
        log.info(
            "时间线完整重建成功: %s media=%.3fs->%.3fs video=%.3fs->%.3fs audio=%.3fs->%.3fs gap=%.3fs->%.3fs",
            input_path,
            before_media,
            float(rebuild_state.get("media_start") or 0.0),
            before_video,
            float(rebuild_state.get("video_start") or 0.0),
            before_audio,
            float(rebuild_state.get("audio_start") or 0.0),
            before_gap,
            float(rebuild_state.get("stream_gap") or 0.0),
        )
        return True
    except Exception as e:
        log.warning("时间线修复异常: %s", e)
        return False


def _dedup_channels(channels: list[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for ch in channels:
        try:
            v = int(ch)
        except (TypeError, ValueError):
            continue
        if v < 0 or v in seen:
            continue
        ordered.append(v)
        seen.add(v)
    return ordered


def _get_ds7808_channel_candidates(web_channel: int, sdk_start_channel: int | None) -> list[int]:
    base = max(1, int(web_channel or 1))
    zero = max(base - 1, 0)
    cands: list[int] = [32 + base, 32 + zero]
    if sdk_start_channel and sdk_start_channel > 0:
        cands.extend([sdk_start_channel + (base - 1), sdk_start_channel + 32 + (base - 1)])
    cands.extend([base, zero, 16 + zero, 64 + zero, 96 + zero])
    cands.extend(range(0, 16))
    cands.extend(range(32, 48))
    cands.extend(range(64, 80))
    cands.extend([100 + zero, 128 + zero, 200 + zero, base + 7])
    deduped = _dedup_channels(cands)
    log.info("DS-7808N 候选通道(web=%s): %s", base, deduped[:24])
    return deduped[:24]


def _detect_device_model(serial: str | None) -> str | None:
    """从设备序列号或型号文本自动识别归一化型号"""
    return _canonicalize_device_model(serial)


def _is_hik_ip_nvr_model(device_model: str | None) -> bool:
    profile = _get_model_profile(device_model)
    return bool(profile and profile.get("hik_ip_nvr"))


def _needs_extended_ip_nvr_scan(device_model: str | None) -> bool:
    profile = _get_model_profile(device_model)
    return bool(profile and profile.get("extended_scan"))


def _get_digital_channel_candidates(web_channel: int, sdk_start_dchan: int | None) -> list[int]:
    base = max(1, int(web_channel or 1))
    cands: list[int] = []
    if sdk_start_dchan and sdk_start_dchan > 0:
        mapped = sdk_start_dchan + (base - 1)
        cands.extend([mapped, mapped + 32, mapped - 1])
    cands.extend([32 + base, 31 + base, 64 + base, 96 + base])
    deduped = _dedup_channels(cands)
    log.info("数字通道候选(web=%s, startDChan=%s): %s", base, sdk_start_dchan, deduped[:12])
    return deduped[:12]


def _expand_ip_nvr_candidates(candidates: list[int], base: int, sdk_start_dchan: int | None, ip_chan_num: int | None) -> list[int]:
    cands: list[int] = list(candidates)
    digital_count = max(0, int(ip_chan_num or 0))
    if sdk_start_dchan and sdk_start_dchan > 0 and digital_count > 0:
        scan_limit = min(max(digital_count + 8, base + 8), 128)
        cands.extend(range(int(sdk_start_dchan), int(sdk_start_dchan) + scan_limit))
    cands.extend(range(max(0, 32 + base - 2), min(32 + max(digital_count, base + 8), 160)))
    return _dedup_channels(cands)


def _apply_channel_filters(candidates: list[int], excluded_channels: set[int] | None = None) -> list[int]:
    filtered = _dedup_channels(candidates)
    if not excluded_channels:
        return filtered
    excluded = {int(ch) for ch in excluded_channels if int(ch) >= 0}
    if not excluded:
        return filtered
    return [ch for ch in filtered if ch not in excluded]


def _load_persisted_sdk_hint(db_path: str | None, nvr_device_id: int | None, web_channel: int) -> tuple[int | None, int | None]:
    try:
        if not db_path or not nvr_device_id or int(web_channel or 0) <= 0:
            return None, None
        db = Db(DbConfig(path=str(db_path)))
        row = db.fetch_one(
            "SELECT sdk_channel, record_type, consecutive_fail_count FROM edge_nvr_channel_map WHERE nvr_device_id=? AND web_channel_num=? LIMIT 1",
            (int(nvr_device_id), int(web_channel)),
        )
        if row is None:
            return None, None
        fail_count = int(row["consecutive_fail_count"] or 0) if "consecutive_fail_count" in row.keys() else 0
        if fail_count >= 2:
            log.warning("历史可信映射已禁用: device=%s web_channel=%s fail_count=%s", nvr_device_id, web_channel, fail_count)
            return None, None
        val = int(row["sdk_channel"] or 0)
        record_type = _normalize_record_type(row["record_type"] if "record_type" in row.keys() else None)
        return (val if val > 0 else None), record_type
    except Exception:
        return None, None


def _load_channel_offset_from_nvr(db_path: str | None, nvr_device_id: int | None) -> tuple[int | None, int | None]:
    """从同NVR上任意已成功通道推算通道偏移量。
    
    如果通道1映射到SDK通道33，则偏移量=32，后续通道2可直接用32+2=34。
    返回 (offset, record_type)，offset = sdk_channel - web_channel。
    """
    try:
        if not db_path or not nvr_device_id:
            return None, None
        db = Db(DbConfig(path=str(db_path)))
        row = db.fetch_one(
            "SELECT web_channel_num, sdk_channel, record_type FROM edge_nvr_channel_map WHERE nvr_device_id=? AND sdk_channel>0 ORDER BY success_count DESC, web_channel_num ASC, last_success_time DESC LIMIT 1",
            (int(nvr_device_id),),
        )
        if row is None:
            return None, None
        web_ch = int(row["web_channel_num"] or 0)
        sdk_ch = int(row["sdk_channel"] or 0)
        if web_ch <= 0 or sdk_ch <= 0:
            return None, None
        offset = sdk_ch - web_ch
        record_type = _normalize_record_type(row["record_type"] if "record_type" in row.keys() else None)
        log.info("从同NVR已成功通道推算偏移量: nvr=%s web_ch=%s sdk_ch=%s offset=%s", nvr_device_id, web_ch, sdk_ch, offset)
        return offset, record_type
    except Exception:
        return None, None


def _load_channel_offset_from_previous_channels(db_path: str | None, nvr_device_id: int | None, web_channel: int) -> tuple[int | None, int | None]:
    try:
        if not db_path or not nvr_device_id or int(web_channel or 0) <= 1:
            return None, None
        db = Db(DbConfig(path=str(db_path)))
        row = db.fetch_one(
            "SELECT web_channel_num, sdk_channel, record_type FROM edge_nvr_channel_map WHERE nvr_device_id=? AND web_channel_num<? AND sdk_channel>0 ORDER BY web_channel_num DESC, success_count DESC, last_success_time DESC LIMIT 1",
            (int(nvr_device_id), int(web_channel)),
        )
        if row is None:
            return None, None
        prev_web_ch = int(row["web_channel_num"] or 0)
        prev_sdk_ch = int(row["sdk_channel"] or 0)
        if prev_web_ch <= 0 or prev_sdk_ch <= 0:
            return None, None
        offset = prev_sdk_ch - prev_web_ch
        record_type = _normalize_record_type(row["record_type"] if "record_type" in row.keys() else None)
        log.info("从前序通道推算偏移量: nvr=%s current_web=%s prev_web=%s prev_sdk=%s offset=%s", nvr_device_id, web_channel, prev_web_ch, prev_sdk_ch, offset)
        return offset, record_type
    except Exception:
        return None, None


def _save_persisted_sdk_hint(
    db_path: str | None,
    nvr_device_id: int | None,
    web_channel: int,
    sdk_channel: int,
    record_type: int | None,
) -> None:
    try:
        if not db_path or not nvr_device_id or int(web_channel or 0) <= 0 or int(sdk_channel or 0) <= 0:
            return
        normalized_record_type = _normalize_record_type(record_type)
        db = Db(DbConfig(path=str(db_path)))
        with db.connect() as conn:
            conn.execute(
                """
INSERT INTO edge_nvr_channel_map(
  nvr_device_id, web_channel_num, sdk_channel, record_type,
  success_count, consecutive_fail_count, last_success_time, last_fail_time, updated_time
) VALUES (?,?,?,?,1,0,strftime('%Y-%m-%dT%H:%M:%SZ','now'),NULL,strftime('%Y-%m-%dT%H:%M:%SZ','now'))
ON CONFLICT(nvr_device_id, web_channel_num) DO UPDATE SET
  sdk_channel=excluded.sdk_channel,
  record_type=excluded.record_type,
  success_count=edge_nvr_channel_map.success_count + 1,
  consecutive_fail_count=0,
  last_success_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  last_fail_time=NULL,
  updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                """.strip(),
                (int(nvr_device_id), int(web_channel), int(sdk_channel), int(normalized_record_type if normalized_record_type is not None else 0xFF)),
            )
            conn.commit()
    except Exception:
        pass


def _mark_persisted_sdk_hint_failed(
    db_path: str | None,
    nvr_device_id: int | None,
    web_channel: int,
    sdk_channel: int,
) -> None:
    try:
        if not db_path or not nvr_device_id or int(web_channel or 0) <= 0 or int(sdk_channel or 0) <= 0:
            return
        db = Db(DbConfig(path=str(db_path)))
        with db.connect() as conn:
            conn.execute(
                """
UPDATE edge_nvr_channel_map
SET consecutive_fail_count=COALESCE(consecutive_fail_count, 0) + 1,
    last_fail_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE nvr_device_id=? AND web_channel_num=? AND sdk_channel=?
                """.strip(),
                (int(nvr_device_id), int(web_channel), int(sdk_channel)),
            )
            conn.commit()
    except Exception:
        pass


def _get_channel_candidates(
    web_channel: int,
    device_model: str | None = None,
    sdk_start_channel: int | None = None,
    sdk_start_dchan: int | None = None,
    ip_chan_num: int | None = None,
    persisted_sdk_channel: int | None = None,
    channel_offset: int | None = None,
    excluded_channels: set[int] | None = None,
    emit_log: bool = True,
) -> list[int]:
    base = max(1, int(web_channel or 1))
    model = str(device_model or "").upper()
    has_ip_channels = int(ip_chan_num or 0) > 0
    is_hik_ip_nvr = _is_hik_ip_nvr_model(model)
    use_extended_ip_scan = _needs_extended_ip_nvr_scan(model)
    
    # 优先使用通道偏移量推算的候选通道
    offset_candidate = None
    if channel_offset is not None and not persisted_sdk_channel:
        offset_candidate = base + int(channel_offset)
        if offset_candidate > 0:
            log.info("使用通道偏移量推算候选: web=%s offset=%s => sdk=%s", base, channel_offset, offset_candidate)
            if emit_log and not (has_ip_channels or is_hik_ip_nvr):
                log.info("同NVR已存在稳定通道偏移量，当前通道严格使用offset候选: web=%s sdk=%s", base, offset_candidate)
            if not (has_ip_channels or is_hik_ip_nvr):
                return [int(offset_candidate)]

    if has_ip_channels and sdk_start_dchan and sdk_start_dchan > 0:
        if persisted_sdk_channel and persisted_sdk_channel > 0:
            trusted_candidates = _apply_channel_filters([int(persisted_sdk_channel)], excluded_channels)
            if emit_log:
                log.info(
                    "IP NVR 命中可信映射，仅使用该映射通道: web=%s sdk=%s",
                    base,
                    persisted_sdk_channel,
                )
            return trusted_candidates[:1]
        if offset_candidate and offset_candidate > 0:
            offset_candidates = _apply_channel_filters([int(offset_candidate)], excluded_channels)
            if emit_log:
                log.info(
                    "IP NVR 命中通道偏移量，仅使用该偏移结果: web=%s offset=%s sdk=%s",
                    base,
                    channel_offset,
                    offset_candidate,
                )
            return offset_candidates[:1]
        if not persisted_sdk_channel and not offset_candidate:
            direct_mapped = int(sdk_start_dchan) + (base - 1)
            direct_candidates = _apply_channel_filters([direct_mapped], excluded_channels)
            if emit_log:
                log.info(
                    "IP NVR 未命中可信映射，严格使用直接数字通道映射: web=%s startDChan=%s sdk=%s",
                    base,
                    sdk_start_dchan,
                    direct_mapped,
                )
            return direct_candidates[:1]
    if is_hik_ip_nvr:
        cands: list[int] = []
        if persisted_sdk_channel and persisted_sdk_channel > 0:
            cands.append(int(persisted_sdk_channel))
        elif offset_candidate and offset_candidate > 0:
            cands.append(int(offset_candidate))
        cands.extend(_get_ds7808_channel_candidates(base, sdk_start_channel))
        deduped = _expand_ip_nvr_candidates(cands, base, sdk_start_dchan, ip_chan_num) if (use_extended_ip_scan and has_ip_channels) else _dedup_channels(cands)
        deduped = _apply_channel_filters(deduped, excluded_channels)
        if emit_log:
            log.info("候选通道(web=%s, model=%s, persisted=%s, offset=%s, startChan=%s, has_ip_channels=%s): %s", base, device_model or "?", persisted_sdk_channel, channel_offset, sdk_start_channel, has_ip_channels, deduped[:32] if use_extended_ip_scan else deduped[:16])
        return deduped[:32] if use_extended_ip_scan else deduped[:16]
    cands: list[int] = []
    if persisted_sdk_channel and persisted_sdk_channel > 0:
        cands.append(int(persisted_sdk_channel))
    elif offset_candidate and offset_candidate > 0:
        cands.append(int(offset_candidate))
    if sdk_start_channel and sdk_start_channel > 0:
        mapped = sdk_start_channel + (base - 1)
        cands.append(mapped)
        if sdk_start_channel <= 16:
            cands.append(sdk_start_channel + 32 + (base - 1))
    cands.extend([32 + base, base, max(base - 1, 0), base + 16, base + 32, base + 64])
    if base > 32:
        cands.append(base - 32)
    deduped = _apply_channel_filters(cands, excluded_channels)
    if emit_log:
        log.info("候选通道(web=%s, model=%s, persisted=%s, offset=%s, startChan=%s): %s", base, device_model or "?", persisted_sdk_channel, channel_offset, sdk_start_channel, deduped[:12])
    return deduped[:12]


# ---------------------------------------------------------------------------
#  RTSP fallback
# ---------------------------------------------------------------------------

def _login_port_candidates(port: int) -> list[int]:
    try:
        p = int(port)
    except Exception:
        p = 8000
    if p <= 0:
        p = 8000
    candidates: list[int] = [p]
    for fallback in (8000, 8001, 8002, 8003):
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def _rtsp_port_candidates(port: int) -> list[int]:
    try:
        p = int(port)
    except Exception:
        p = 0
    if p == 18080:
        return [554, 18080]
    return [554, 18080]


def _set_sdk_file_size_limit(sdk: HikSdk) -> None:
    try:
        cfg = LocalGeneralCfg()
        cfg.i64FileSize = DEFAULT_FILE_SIZE_LIMIT_MB * 1024 * 1024
        sdk.dll.NET_DVR_SetSDKLocalCfg(NET_DVR_LOCAL_CFG_TYPE_GENERAL, ctypes.byref(cfg))
        log.info("SDK文件大小上限: %s MB", DEFAULT_FILE_SIZE_LIMIT_MB)
    except Exception as e:
        log.warning("设置SDK文件大小上限失败: %s", e)


def _generate_time_windows(start_dt: datetime, end_dt: datetime) -> list[tuple[str, datetime, datetime]]:
    return [("原始", start_dt, end_dt)]


def _generate_same_channel_probe_windows(start_dt: datetime, end_dt: datetime) -> list[tuple[str, datetime, datetime, bool]]:
    base_windows = [(label, win_start, win_end, label == "原始") for label, win_start, win_end in _generate_time_windows(start_dt, end_dt)]
    extras = [
        ("前扩60秒", start_dt - timedelta(seconds=60), end_dt, False),
        ("后扩60秒", start_dt, end_dt + timedelta(seconds=60), False),
        ("前后扩120秒", start_dt - timedelta(seconds=120), end_dt + timedelta(seconds=120), False),
    ]
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, datetime, datetime, bool]] = []
    for label, win_start, win_end, is_main in [*base_windows, *extras]:
        if win_start >= win_end:
            continue
        key = (win_start.isoformat(), win_end.isoformat())
        if key in seen:
            continue
        seen.add(key)
        ordered.append((label, win_start, win_end, is_main))
    return ordered


def _use_strict_sdk_probe(device_model: str | None, sdk_start_dchan: int | None, ip_chan_num: int | None) -> bool:
    if int(sdk_start_dchan or 0) <= 0 or int(ip_chan_num or 0) <= 0:
        return False
    profile = _get_model_profile(device_model)
    return bool(profile and profile.get("strict_probe"))


def _should_bypass_failed_same_channel_probe(
    probe_status: str,
    *,
    channel: int,
    hint_channel: int | None,
    persisted_sdk_channel: int | None,
) -> bool:
    status = str(probe_status or "").strip().lower()
    if not status.startswith("probe_handle_failed:") and not status.startswith("probe_play_start_failed:"):
        return False
    trusted_channel = 0
    try:
        if int(persisted_sdk_channel or 0) > 0:
            trusted_channel = int(persisted_sdk_channel or 0)
        elif int(hint_channel or 0) > 0:
            trusted_channel = int(hint_channel or 0)
    except Exception:
        trusted_channel = 0
    return bool(trusted_channel > 0 and int(channel or 0) == int(trusted_channel))


def _load_builtin_sdk_hint(device_model: str | None, web_channel: int) -> tuple[int | None, int | None]:
    try:
        canonical_model = _canonicalize_device_model(device_model)
        if not canonical_model:
            return None, None
        hint = _BUILTIN_MODEL_OFFSETS.get(str(canonical_model))
        if hint is None:
            return None, None
        offset, record_type = hint
        sdk_channel = int(web_channel or 0) + int(offset)
        if sdk_channel <= 0:
            return None, None
        normalized_record_type = _normalize_record_type(record_type)
        log.info("命中型号内置通道偏移: model=%s web_channel=%s offset=%s sdk_channel=%s record_type=%s", canonical_model, web_channel, offset, sdk_channel, normalized_record_type)
        return int(sdk_channel), normalized_record_type
    except Exception:
        return None, None


def _probe_sdk_download_candidate(
    sdk: HikSdk,
    user_id: int,
    channel: int,
    start_dt: datetime,
    end_dt: datetime,
    record_type: int,
    *,
    probe_seconds: int = 10,
) -> ProbeResult:
    pc = PlayCond()
    pc.dwChannel = int(channel)
    _set_time(pc.struStartTime, start_dt)
    _set_time(pc.struStopTime, end_dt)
    pc.byRecordFileType = int(record_type)
    pc.byDrawFrame = 0
    probe_path = os.path.join(tempfile.gettempdir(), f"hik_probe_{os.getpid()}_{int(channel)}_{int(record_type)}_{int(time.time() * 1000)}.mp4")
    c_path = ctypes.c_char_p(probe_path.encode("gbk", errors="ignore"))
    handle = sdk.dll.NET_DVR_GetFileByTime_V40(ctypes.c_long(int(user_id)), c_path, ctypes.byref(pc))
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
                last_size = os.path.getsize(probe_path) if os.path.exists(probe_path) else 0
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
        _safe_remove(probe_path)


def _run_same_channel_probe(
    sdk: HikSdk,
    user_id: int,
    channel: int,
    start_dt: datetime,
    end_dt: datetime,
    record_type: int,
    *,
    probe_seconds: int = 10,
    probe_attempts: int = 3,
    on_probe_status: Callable[[str], None] | None = None,
) -> tuple[ProbeResult, ProbeResult | None]:
    main_window_result: ProbeResult | None = None
    nearby_result: ProbeResult | None = None
    windows = _generate_same_channel_probe_windows(start_dt, end_dt)
    for label, win_start, win_end, is_main in windows:
        for attempt in range(1, max(1, int(probe_attempts)) + 1):
            result = _probe_sdk_download_candidate(
                sdk,
                user_id,
                channel,
                win_start,
                win_end,
                record_type,
                probe_seconds=max(1, int(probe_seconds)),
            )
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
                    nearby_result = ProbeResult(
                        ok=True,
                        pos=result.pos,
                        size_bytes=result.size_bytes,
                        status="probe_main_window_inconclusive",
                        window_label=label,
                        is_main_window=False,
                    )
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


def _ffmpeg_exists() -> bool:
    b = _ffmpeg_bin()
    return bool(shutil.which(b) or Path(b).exists())


def _safe_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _remove_path_strict(path: str, retries: int = 5, delay_sec: float = 0.2) -> None:
    p = Path(path)
    for idx in range(max(1, int(retries))):
        try:
            if not p.exists():
                return
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        except FileNotFoundError:
            return
        except OSError:
            if idx + 1 >= max(1, int(retries)):
                break
            time.sleep(max(0.0, float(delay_sec)))
            continue
        if not p.exists():
            return
        if idx + 1 < max(1, int(retries)):
            time.sleep(max(0.0, float(delay_sec)))
    if p.exists():
        raise RuntimeError(f"cleanup_failed:{p}")


def _replace_path_strict(src: str, dst: str, retries: int = 5, delay_sec: float = 0.2) -> None:
    src_path = Path(src)
    dst_path = Path(dst)
    for idx in range(max(1, int(retries))):
        try:
            if not src_path.exists():
                break
            if dst_path.exists():
                _remove_path_strict(str(dst_path), retries=1, delay_sec=delay_sec)
            os.replace(str(src_path), str(dst_path))
        except FileNotFoundError:
            break
        except OSError:
            if idx + 1 >= max(1, int(retries)):
                break
            time.sleep(max(0.0, float(delay_sec)))
            continue
        if dst_path.exists() and not src_path.exists():
            return
    raise RuntimeError(f"replace_failed:{src}->{dst}")


def _cleanup_attempt_files(base_output_path: str) -> None:
    base = Path(os.path.abspath(base_output_path))
    pattern = base.name + ".*.attempt"
    for attempt_path in base.parent.glob(pattern):
        _remove_path_strict(str(attempt_path))


def _rtsp_record(
    rtsp_url: str,
    duration_sec: int,
    output_path: str,
    cancel_check: CancelCheck | None = None,
    on_progress_ex: ProgressCallback | None = None,
) -> bool:
    cmd = [
        _ffmpeg_bin(), "-y",
        "-rtsp_transport", "tcp",
        "-rtsp_flags", "prefer_tcp",
        "-use_wallclock_as_timestamps", "1",
        "-timeout", str(45 * 1000000),
        "-i", rtsp_url,
        "-t", str(duration_sec),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "96k", "-ar", "8000", "-ac", "1",
        "-fflags", "+genpts",
        "-movflags", "+faststart",
        output_path,
    ]
    safe_url = rtsp_url
    if "@" in rtsp_url and "://" in rtsp_url:
        parts = rtsp_url.split("://", 1)
        if "@" in parts[1]:
            safe_url = parts[0] + "://" + parts[1].split("@", 1)[1]
    log.info("RTSP录制: %ss -> %s url=%s", duration_sec, output_path, safe_url)

    import threading as _threading

    try:
        creationflags = 0
        if os.name == "nt":
            try:
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            except Exception:
                creationflags = 0
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            universal_newlines=False,
            creationflags=creationflags,
        )
    except Exception as e:
        log.error("启动FFmpeg进程失败: %s", e)
        return False

    _stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        try:
            assert proc.stderr is not None
            for raw_line in proc.stderr:
                try:
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    line = repr(raw_line)
                _stderr_lines.append(line)
        except Exception:
            pass

    _stderr_thread = _threading.Thread(target=_drain_stderr, daemon=True)
    _stderr_thread.start()

    begin = time.time()
    last_report_pct = -1
    try:
        while proc.poll() is None:
            time.sleep(1)
            if cancel_check:
                mode = cancel_check()
                if mode in {"pause", "stop"}:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                    raise RuntimeError(f"cancelled:{mode}")
            elapsed = time.time() - begin
            if elapsed > duration_sec + 60:
                log.warning("RTSP录制超时(%.0fs > %ss+60), 强制终止", elapsed, duration_sec)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                break
            if on_progress_ex and duration_sec > 0:
                raw_pct = min(1.0, elapsed / float(duration_sec))
                pct = raw_pct * 0.95
                pct_int = int(pct * 100)
                if pct_int > last_report_pct:
                    last_report_pct = pct_int
                    sz = 0
                    try:
                        sz = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                    except Exception:
                        pass
                    speed = float(sz) / max(0.001, elapsed)
                    on_progress_ex(pct, sz, speed)
        code = proc.wait()
        _stderr_thread.join(timeout=3)
        if code != 0:
            tail = _stderr_lines[-20:] if _stderr_lines else []
            if tail:
                log.error("FFmpeg RTSP stderr:\n%s", "\n".join(tail))
            if os.path.exists(output_path) and os.path.getsize(output_path) > 5 * 1024 * 1024:
                log.warning("FFmpeg exit=%s 但部分文件可用(%s bytes)", code, os.path.getsize(output_path))
                return True
            log.error("FFmpeg RTSP 失败 exit=%s", code)
            return False
        return True
    finally:
        if proc.poll() is None:
            proc.kill()


def _sdk_download_file(
    sdk: HikSdk,
    user_id: int,
    channel: int,
    start_dt: datetime,
    end_dt: datetime,
    output_path: str,
    record_type: int = 0xFF,
    on_progress: Callable[[float, str], None] | None = None,
    on_progress_ex: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    stall_timeout: int = STALL_TIMEOUT_SECONDS,
) -> tuple[bool, str | None]:
    pc = PlayCond()
    pc.dwChannel = int(channel)
    _set_time(pc.struStartTime, start_dt)
    _set_time(pc.struStopTime, end_dt)
    pc.byRecordFileType = int(record_type)
    pc.byDrawFrame = 0

    abs_path = os.path.abspath(output_path)
    c_path = ctypes.c_char_p(abs_path.encode("gbk", errors="ignore"))
    handle = sdk.dll.NET_DVR_GetFileByTime_V40(ctypes.c_long(int(user_id)), c_path, ctypes.byref(pc))
    if handle < 0:
        err = sdk.last_error()
        _safe_remove(abs_path)
        return False, f"getfile_failed:{err}"

    log.info("SDK下载句柄=%s ch=%s uid=%s rt=%s", handle, channel, user_id, record_type)

    out_val = ctypes.c_ulong()
    if not sdk.dll.NET_DVR_PlayBackControl_V40(handle, NET_DVR_PLAYSTART, None, 0, None, ctypes.byref(out_val)):
        err = sdk.last_error()
        sdk.dll.NET_DVR_StopGetFile(handle)
        _safe_remove(abs_path)
        return False, f"playstart_failed:{err}"

    last_progress = -1
    stable = 0
    last_size = -1
    stall = 0
    last_t = time.time()
    last_speed = 0.0

    while True:
        time.sleep(1)
        if cancel_check:
            mode = cancel_check()
            if mode in {"pause", "stop"}:
                sdk.dll.NET_DVR_StopGetFile(handle)
                return False, f"cancelled:{mode}"

        try:
            cur_size = os.path.getsize(output_path) if os.path.exists(output_path) else -1
        except Exception:
            cur_size = -1

        if cur_size >= 0:
            if cur_size == last_size:
                stall += 1
            else:
                stall = 0
                now = time.time()
                dt = max(0.001, now - last_t)
                if last_size >= 0:
                    last_speed = max(0.0, float(cur_size - last_size) / dt)
                last_t = now
                last_size = cur_size
                if on_progress_ex:
                    if 0 <= last_progress < 100:
                        on_progress_ex(last_progress / 100.0, int(cur_size), float(last_speed))
                    elif cur_size > 0:
                        on_progress_ex(0.0, int(cur_size), float(last_speed))
            if stall_timeout and stall >= stall_timeout:
                log.warning("SDK下载卡顿超时 ch=%s size=%s", channel, cur_size)
                sdk.dll.NET_DVR_StopGetFile(handle)
                return False, "stall_timeout"

        p = int(sdk.dll.NET_DVR_GetDownloadPos(handle))
        if 0 <= p <= 100:
            if p == 100:
                log.info("SDK下载完成100%% ch=%s", channel)
                time.sleep(2)
                sdk.dll.NET_DVR_StopGetFile(handle)
                return True, None
            if p != last_progress:
                last_progress = p
                stable = 0
                if on_progress:
                    on_progress(p / 100.0, f"SDK {p}% ch={channel}")
                if on_progress_ex and p < 100:
                    adjusted_pct = (p / 100.0) * 0.95
                    on_progress_ex(adjusted_pct, int(cur_size if cur_size >= 0 else 0), float(last_speed))
            else:
                stable += 1
                if stable > 60:
                    log.warning("SDK下载进度停滞 ch=%s p=%s stable=%s", channel, p, stable)
                    sdk.dll.NET_DVR_StopGetFile(handle)
                    return False, "progress_stuck"
            continue
        if p == -1:
            err = sdk.last_error()
            sdk.dll.NET_DVR_StopGetFile(handle)
            if err == 0:
                log.info("SDK下载正常结束 ch=%s", channel)
                return True, None
            log.warning("SDK下载错误 ch=%s err=%s", channel, err)
            return False, f"download_error:{err}"
        sdk.dll.NET_DVR_StopGetFile(handle)
        return False, f"invalid_progress:{p}"


def _sdk_download_with_retry(
    sdk: HikSdk,
    user_id: int,
    web_channel: int,
    start_dt: datetime,
    end_dt: datetime,
    output_path: str,
    device_model: str | None = None,
    sdk_start_channel: int | None = None,
    sdk_start_dchan: int | None = None,
    ip_chan_num: int | None = None,
    persisted_sdk_channel: int | None = None,
    channel_offset: int | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    on_progress_ex: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    hint_channel: int | None = None,
    hint_record_type: int | None = None,
    hint_is_task_locked: bool = False,
    allowed_channels: set[int] | None = None,
    excluded_channels: set[int] | None = None,
    original_duration_sec: int | None = None,
    allow_tail_partial: bool = False,
) -> tuple[bool, int, int, str, bool, float, bool]:
    allowed_channel_set = {int(ch) for ch in (allowed_channels or set()) if int(ch) >= 0} or None
    excluded_channel_set = {int(ch) for ch in (excluded_channels or set()) if int(ch) >= 0}

    def _channel_allowed(channel: int | None) -> bool:
        if channel is None:
            return False
        try:
            ch = int(channel)
        except Exception:
            return False
        if allowed_channel_set is not None and ch not in allowed_channel_set:
            return False
        if ch in excluded_channel_set:
            return False
        return True

    has_hint = hint_channel is not None and hint_record_type is not None and _channel_allowed(hint_channel)
    candidates = _get_channel_candidates(
        web_channel,
        device_model,
        sdk_start_channel,
        sdk_start_dchan,
        ip_chan_num,
        persisted_sdk_channel,
        channel_offset,
        excluded_channels=excluded_channel_set,
        emit_log=not has_hint,
    )
    if allowed_channel_set is not None:
        candidates = [ch for ch in candidates if ch in allowed_channel_set]
    if original_duration_sec and original_duration_sec > 0:
        expected_duration = int(original_duration_sec)
        duration_tolerance_sec = max(30.0, expected_duration * 0.10)
    else:
        expected_duration = max(1, int((end_dt - start_dt).total_seconds()))
        duration_tolerance_sec = 0.0
    abs_output_path = os.path.abspath(output_path)
    _cleanup_attempt_files(abs_output_path)
    strict_sdk_probe = _use_strict_sdk_probe(device_model, sdk_start_dchan, ip_chan_num)
    persisted_channel = int(persisted_sdk_channel or 0) if int(persisted_sdk_channel or 0) > 0 else 0
    persisted_failed = False
    if strict_sdk_probe:
        log.info("启用严格SDK探测: model=%s sdk_start_dchan=%s ip_chan_num=%s", device_model or "?", sdk_start_dchan, ip_chan_num)
        min_valid_bytes = MIN_VALID_VIDEO_BYTES
    else:
        min_valid_bytes = max(MIN_VALID_VIDEO_BYTES, expected_duration * MIN_EXPECTED_BYTES_PER_SECOND)

    def _attempt_path(tag: str) -> str:
        return abs_output_path + f".{tag}.attempt"

    def _emit_status(progress: float, message: str) -> None:
        if on_progress:
            try:
                on_progress(progress, message)
            except Exception:
                pass

    def _tail_partial_acceptable(actual_duration: float, file_size: int) -> bool:
        if not allow_tail_partial:
            return False
        if file_size < MIN_VALID_VIDEO_BYTES:
            return False
        return float(actual_duration) > 1.0

    record_types = [
        (0x00, "定时录像"),
    ]

    if has_hint:
        log.info("使用hint快速下载: ch=%s rt=%s", hint_channel, hint_record_type)
        _emit_status(0.0, f"复用上次成功通道：SDK通道{int(hint_channel)}，开始下载")
        hint_output = _attempt_path(f"hint.{hint_channel}.{hint_record_type}")
        _remove_path_strict(hint_output)
        ok, err = _sdk_download_file(
            sdk,
            user_id,
            int(hint_channel),
            start_dt,
            end_dt,
            hint_output,
            record_type=int(hint_record_type),
            on_progress=on_progress,
            on_progress_ex=on_progress_ex,
            cancel_check=cancel_check,
        )
        if err and str(err).startswith("cancelled:"):
            _remove_path_strict(hint_output)
            raise RuntimeError(str(err))
        if ok:
            file_size = os.path.getsize(hint_output) if os.path.exists(hint_output) else 0
            if file_size >= MIN_VALID_VIDEO_BYTES:
                actual_duration = _probe_media_duration_seconds(hint_output)
                if actual_duration >= expected_duration - duration_tolerance_sec - 0.001:
                    log.info("hint快速下载成功: ch=%s rt=%s size=%s dur=%.3fs expected=%ss tol=%.1fs", hint_channel, hint_record_type, file_size, actual_duration, expected_duration, duration_tolerance_sec)
                    _remove_path_strict(abs_output_path)
                    os.replace(hint_output, abs_output_path)
                    mapping_source = "task_locked" if hint_is_task_locked else "hint"
                    if persisted_channel and int(hint_channel) == persisted_channel:
                        persisted_failed = False
                    return True, int(hint_channel), int(hint_record_type), mapping_source, persisted_failed, float(actual_duration), False
                if _tail_partial_acceptable(actual_duration, file_size):
                    log.warning("hint快速下载尾段时长不足但接受现有产物: dur=%.3fs expected=%ss tol=%.1fs size=%s", actual_duration, expected_duration, duration_tolerance_sec, file_size)
                    _remove_path_strict(abs_output_path)
                    os.replace(hint_output, abs_output_path)
                    mapping_source = "task_locked_tail" if hint_is_task_locked else "hint_tail"
                    if persisted_channel and int(hint_channel) == persisted_channel:
                        persisted_failed = False
                    return True, int(hint_channel), int(hint_record_type), mapping_source, persisted_failed, float(actual_duration), True
                log.warning("hint快速下载时长不足: dur=%.3fs expected=%ss tol=%.1fs", actual_duration, expected_duration, duration_tolerance_sec)
            _remove_path_strict(hint_output)
        if persisted_channel and int(hint_channel or 0) == persisted_channel:
            persisted_failed = True
            if strict_sdk_probe and not hint_is_task_locked:
                log.warning(
                    "可信映射下载失败，禁止跨通道扫描，继续限制在同通道补救: web_channel=%s sdk_channel=%s expected=%ss",
                    web_channel,
                    int(hint_channel or 0),
                    expected_duration,
                )
                allowed_channel_set = {int(hint_channel)}
                candidates = [int(hint_channel)]
        if allowed_channel_set is None:
            log.info("hint快速下载失败，回退候选扫描")
            _get_channel_candidates(web_channel, device_model, sdk_start_channel, sdk_start_dchan, ip_chan_num, persisted_sdk_channel, channel_offset, excluded_channels=excluded_channel_set, emit_log=True)

    for ch in candidates:
        if not _channel_allowed(ch):
            continue
        if cancel_check:
            mode = cancel_check()
            if mode in {"pause", "stop"}:
                raise RuntimeError(f"cancelled:{mode}")
        for rec_type, rec_desc in record_types:
            _emit_status(0.0, "通道计算中")
            log.info("尝试SDK下载: ch=%s 类型=%s(%s)", ch, rec_desc, rec_type)
            main_probe_result, nearby_probe_result = _run_same_channel_probe(
                sdk,
                user_id,
                ch,
                start_dt,
                end_dt,
                rec_type,
                probe_seconds=10,
                probe_attempts=3,
                on_probe_status=lambda msg: _emit_status(0.0, msg),
            )
            bypass_failed_probe = _should_bypass_failed_same_channel_probe(
                main_probe_result.status,
                channel=ch,
                hint_channel=hint_channel,
                persisted_sdk_channel=persisted_sdk_channel,
            )
            if not main_probe_result.ok:
                if nearby_probe_result is not None and nearby_probe_result.ok:
                    log.warning(
                        "同通道主窗口探测失败但辅助窗口确认附近有录像: ch=%s 类型=%s main_status=%s nearby_window=%s",
                        ch,
                        rec_desc,
                        main_probe_result.status,
                        nearby_probe_result.window_label,
                    )
                elif bypass_failed_probe:
                    log.warning(
                        "同通道探测句柄不稳定，沿用已映射通道执行正式下载补救: ch=%s 类型=%s status=%s hint_channel=%s persisted_channel=%s",
                        ch,
                        rec_desc,
                        main_probe_result.status,
                        int(hint_channel or 0) if int(hint_channel or 0) > 0 else 0,
                        int(persisted_sdk_channel or 0) if int(persisted_sdk_channel or 0) > 0 else 0,
                    )
                else:
                    log.info(
                        "同通道探测失败: ch=%s 类型=%s status=%s",
                        ch,
                        rec_desc,
                        main_probe_result.status,
                    )
                    continue
            if strict_sdk_probe:
                log.info(
                    "严格SDK探测完成: ch=%s 主窗口=%s status=%s probe_pos=%s probe_size=%s nearby_status=%s nearby_window=%s",
                    ch,
                    main_probe_result.window_label,
                    main_probe_result.status,
                    main_probe_result.pos,
                    main_probe_result.size_bytes,
                    nearby_probe_result.status if nearby_probe_result is not None else "",
                    nearby_probe_result.window_label if nearby_probe_result is not None else "",
                )
            else:
                log.info(
                    "SDK探测完成: ch=%s 主窗口=%s status=%s probe_pos=%s probe_size=%s nearby_status=%s nearby_window=%s",
                    ch,
                    main_probe_result.window_label,
                    main_probe_result.status,
                    main_probe_result.pos,
                    main_probe_result.size_bytes,
                    nearby_probe_result.status if nearby_probe_result is not None else "",
                    nearby_probe_result.window_label if nearby_probe_result is not None else "",
                )
            _emit_status(0.0, f"探索成功，开始下载：SDK通道{ch}，录像类型{rec_desc}")
            download_windows: list[tuple[str, datetime, datetime, str]] = [("原始", start_dt, end_dt, "main_window")]
            if nearby_probe_result is not None and nearby_probe_result.ok:
                for win_label, win_start, win_end, _is_main in _generate_same_channel_probe_windows(start_dt, end_dt):
                    if win_label == nearby_probe_result.window_label:
                        download_windows.append((win_label, win_start, win_end, "nearby_trim_recovery"))
                        break
            for label, win_start, win_end, download_mode in download_windows:
                tmp_output = abs_output_path if download_mode == "main_window" else _attempt_path(f"recover.{ch}.{rec_type}")
                ok2, err2 = _sdk_download_file(
                    sdk,
                    user_id,
                    ch,
                    win_start,
                    win_end,
                    tmp_output,
                    record_type=rec_type,
                    on_progress=on_progress,
                    on_progress_ex=on_progress_ex,
                    cancel_check=cancel_check,
                )
                if err2 and str(err2).startswith("cancelled:"):
                    _remove_path_strict(tmp_output)
                    raise RuntimeError(str(err2))
                if ok2:
                    final_size = os.path.getsize(tmp_output) if os.path.exists(tmp_output) else 0
                    if final_size >= min_valid_bytes:
                        final_duration = _probe_media_duration_seconds(tmp_output)
                        if download_mode == "nearby_trim_recovery":
                            _trim_to_duration(tmp_output, float(expected_duration))
                            final_duration = _probe_media_duration_seconds(tmp_output)
                        if final_duration >= expected_duration - duration_tolerance_sec - 0.001:
                            if on_progress_ex:
                                on_progress_ex(1.0, int(final_size), 0.0)
                            if download_mode == "nearby_trim_recovery":
                                _remove_path_strict(abs_output_path)
                                os.replace(tmp_output, abs_output_path)
                            log.info("SDK下载成功: ch=%s 窗口=[%s] 类型=%s mode=%s size=%s dur=%.3fs expected=%ss tol=%.1fs", ch, label, rec_desc, download_mode, final_size, final_duration, expected_duration, duration_tolerance_sec)
                            mapping_source = "persisted" if persisted_sdk_channel and int(persisted_sdk_channel) == int(ch) else ("same_channel_trimmed_recovery" if download_mode == "nearby_trim_recovery" else "scanned")
                            if persisted_channel and int(ch) == persisted_channel:
                                persisted_failed = False
                            return True, ch, rec_type, mapping_source, persisted_failed, float(final_duration), False
                        if _tail_partial_acceptable(final_duration, final_size):
                            if on_progress_ex:
                                on_progress_ex(1.0, int(final_size), 0.0)
                            if download_mode == "nearby_trim_recovery":
                                _remove_path_strict(abs_output_path)
                                os.replace(tmp_output, abs_output_path)
                            log.warning("正式SDK下载尾段时长不足但接受现有产物: ch=%s 窗口=[%s] 类型=%s actual=%.3fs expected=%ss tol=%.1fs size=%s", ch, label, rec_desc, final_duration, expected_duration, duration_tolerance_sec, final_size)
                            mapping_source = "persisted_tail" if persisted_sdk_channel and int(persisted_sdk_channel) == int(ch) else ("same_channel_trimmed_tail" if download_mode == "nearby_trim_recovery" else "scanned_tail")
                            if persisted_channel and int(ch) == persisted_channel:
                                persisted_failed = False
                            return True, ch, rec_type, mapping_source, persisted_failed, float(final_duration), True
                        log.warning("正式SDK下载时长不足: ch=%s 窗口=[%s] 类型=%s actual=%.3fs expected=%ss tol=%.1fs", ch, label, rec_desc, final_duration, expected_duration, duration_tolerance_sec)
                    else:
                        log.warning("正式SDK下载文件过小: ch=%s size=%s < %s", ch, final_size, min_valid_bytes)
                if persisted_channel and int(ch) == persisted_channel:
                    persisted_failed = True
                _remove_path_strict(tmp_output)
            continue

    log.warning("所有SDK候选通道均下载失败")
    return False, 0, 0, "", persisted_failed, 0.0, False


def _looks_like_connectivity_issue(message: str) -> bool:
    text = str(message or "").lower()
    return any(token in text for token in ("timeout", "timed out", "refused", "unreachable", "network", "connect", "socket"))


def _build_nvr_connect_failed_message(ip: str, port: int, errors: list[str]) -> str:
    uniq: list[str] = []
    for err in errors:
        text = str(err or "").strip()
        if text and text not in uniq:
            uniq.append(text)
    detail = " | ".join(uniq[:3]) if uniq else "unknown"
    return f"nvr_connect_failed:{ip}:{port}:{detail}"


def _build_rtsp_url(username: str, password: str, ip: str, port: int, channel: int, *, main_stream: bool = True) -> str:
    stream_id = 1 if main_stream else 2
    ch = int(channel)
    if ch >= 100 and (ch % 100) in (1, 2):
        base = max(1, ch // 100)
        stream_ch = base * 100 + stream_id
        path = f"/Streaming/Channels/{stream_ch}"
    else:
        path = f"/Streaming/Channels/{ch}0{stream_id}"
    rtsp_port = int(port or 554) if int(port or 0) > 0 else 554
    auth = f"{username}:{password}@" if username else ""
    return f"rtsp://{auth}{ip}:{rtsp_port}{path}"

def _rtsp_fallback(
    ip: str, port: int, username: str, password: str, channel: int,
    start_dt: datetime, end_dt: datetime, output_path: str,
    cancel_check: CancelCheck | None,
    on_progress_ex: ProgressCallback | None = None,
) -> DownloadResult:
    """RTSP回退录制"""
    if not _ffmpeg_exists():
        raise RuntimeError("sdk_download_failed_and_ffmpeg_not_available")
    
    # 使用扩大后的时间窗口录制
    download_duration = max(1, int((end_dt - start_dt).total_seconds()))
    last_error = "rtsp_fallback_failed"
    attempt_seq = 0
    success = False
    temp_output = output_path + ".rtsp_temp.mp4"
    _remove_path_strict(temp_output)
    for rtsp_port in _rtsp_port_candidates(port):
        for attempt in range(1, 4):
            attempt_seq += 1
            _safe_remove(temp_output)
            rtsp_url = _build_rtsp_url(username, password, ip, rtsp_port, channel, main_stream=True)
            log.info("RTSP回退尝试: port=%s attempt=%s/3 total_attempt=%s url=%s", rtsp_port, attempt, attempt_seq, rtsp_url)
            ok = _rtsp_record(rtsp_url, download_duration, temp_output, cancel_check, on_progress_ex)
            if ok:
                success = True
                break
            last_error = f"rtsp_fallback_failed:{ip}:{rtsp_port}:attempt={attempt}"
        if success:
            break
    if not success:
        _safe_remove(temp_output)
        raise RuntimeError(last_error)

    _remove_path_strict(output_path)
    _replace_path_strict(temp_output, output_path)
    if on_progress_ex:
        final_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        on_progress_ex(1.0, int(final_size), 0.0)
    
    size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    if size < MIN_VALID_VIDEO_BYTES:
        _safe_remove(output_path)
        raise RuntimeError(f"rtsp_output_too_small:{size}")
    
    return DownloadResult(path=output_path, size_bytes=size, channel_used=channel)


def open_download_session(
    *,
    sdk_dir: str | None,
    db_path: str | None = None,
    nvr_device_id: int | None = None,
    ip: str,
    port: int,
    username: str,
    password: str,
    channel: int,
    device_model: str | None = None,
) -> HikDownloadSession:
    sdk: HikSdk | None = None
    login_uid = 0
    login_ok = False
    login_errors: list[str] = []
    persisted_sdk_channel, persisted_record_type = _load_persisted_sdk_hint(db_path, nvr_device_id, channel)
    builtin_sdk_channel, builtin_record_type = _load_builtin_sdk_hint(device_model, int(channel))
    channel_offset: int | None = None
    if not persisted_sdk_channel and builtin_sdk_channel:
        persisted_sdk_channel = int(builtin_sdk_channel)
        if persisted_record_type is None:
            persisted_record_type = builtin_record_type
    if persisted_sdk_channel:
        log.info(
            "命中当前通道可信映射: device=%s web_channel=%s sdk_channel=%s record_type=%s",
            nvr_device_id,
            channel,
            persisted_sdk_channel,
            persisted_record_type,
        )
    else:
        log.info(
            "当前通道未命中可信映射: device=%s web_channel=%s，将进入谨慎候选扫描，不复用跨通道offset",
            nvr_device_id,
            channel,
        )
 
    try:
        sdk = HikSdk(sdk_dir=sdk_dir)
        _set_sdk_file_size_limit(sdk)
        login_ports = _login_port_candidates(port)
        info = None
        for lp in login_ports:
            try:
                log.info("SDK登录尝试 %s:%s", ip, lp)
                login_uid, info = sdk.login(ip, lp, username, password)
                login_ok = True
                log.info("SDK登录成功 %s:%s uid=%s", ip, lp, login_uid)
                break
            except Exception as e:
                login_errors.append(str(e))
                if _looks_like_connectivity_issue(str(e)):
                    log.info("SDK登录失败 %s:%s -> %s", ip, lp, e)
                else:
                    log.warning("SDK登录失败 %s:%s -> %s", ip, lp, e)
                login_uid = 0

        if not login_ok or info is None:
            raise RuntimeError(_build_nvr_connect_failed_message(ip, port, login_errors))

        channel_info = get_device_channel_info(info)
        sdk_start_channel = int(channel_info.start_channel or 0) or None
        sdk_start_dchan = int(channel_info.start_digital_channel or 0) or None
        ip_chan_num = int(channel_info.digital_channel_count or 0) or None
        try:
            serial = bytes(info.sSerialNumber).decode("utf-8", errors="ignore").strip().rstrip("\x00")
        except Exception:
            serial = ""
        detected_model = _detect_device_model(serial)
        resolved_model = _canonicalize_device_model(device_model) or detected_model
        if detected_model and not device_model:
            log.info("自动检测设备型号: %s (序列号: %s)", detected_model, serial[:60])
        log.info(
            "设备信息: startChan=%s chanNum=%s startDChan=%s ipChanNum=%s highDChanRaw=%s serial=%s model=%s",
            sdk_start_channel,
            int(channel_info.analog_channel_count or 0),
            sdk_start_dchan,
            ip_chan_num,
            int(channel_info.high_dchan_raw or 0),
            serial[:40],
            resolved_model or "?",
        )
        return HikDownloadSession(
            sdk=sdk,
            login_uid=login_uid,
            sdk_start_channel=sdk_start_channel,
            sdk_start_dchan=sdk_start_dchan,
            ip_chan_num=ip_chan_num,
            device_model=resolved_model,
            persisted_sdk_channel=persisted_sdk_channel,
            persisted_record_type=persisted_record_type,
            channel_offset=channel_offset,
            db_path=db_path,
            nvr_device_id=nvr_device_id,
            ip=ip,
            port=port,
            username=username,
            password=password,
            channel=int(channel),
        )
    except Exception:
        if login_ok and sdk:
            try:
                sdk.logout(login_uid)
            except Exception:
                pass
        if sdk:
            try:
                sdk.close()
            except Exception:
                pass
        raise


def close_download_session(session: HikDownloadSession | None) -> None:
    if session is None:
        return
    if session.login_uid is not None and session.sdk:
        try:
            session.sdk.logout(session.login_uid)
        except Exception:
            pass
    if session.sdk:
        try:
            session.sdk.close()
        except Exception:
            pass


def download_by_time_with_session(
    session: HikDownloadSession,
    *,
    start_time: datetime,
    end_time: datetime,
    output_path: str,
    on_progress: Callable[[float, str], None] | None = None,
    on_progress_ex: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    hint_uid: int | None = None,
    hint_channel: int | None = None,
    hint_record_type: int | None = None,
    locked_channel: int | None = None,
    locked_record_type: int | None = None,
    excluded_channels: list[int] | None = None,
    apply_start_padding: bool = True,
    apply_end_padding: bool = True,
    allow_tail_partial: bool = False,
    allow_rtsp_fallback: bool = False,
) -> DownloadResult:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "===== 开始下载 ip=%s port=%s ch=%s model=%s time=%s~%s out=%s =====",
        session.ip, session.port, session.channel, session.device_model or "?",
        start_time.isoformat(), end_time.isoformat(), output_path,
    )

    download_padding = timedelta(seconds=DOWNLOAD_PADDING_SECONDS)
    expanded_start = start_time - download_padding if apply_start_padding else start_time
    expanded_end = end_time + download_padding if apply_end_padding else end_time

    log.info(
        "下载缓冲窗口: %s ~ %s (目标: %s ~ %s, 缓冲=%s, start_pad=%s, end_pad=%s)",
        expanded_start.isoformat(), expanded_end.isoformat(),
        start_time.isoformat(), end_time.isoformat(),
        download_padding, apply_start_padding, apply_end_padding,
    )

    excluded_channel_set = {int(ch) for ch in (excluded_channels or []) if int(ch) >= 0}
    preferred_hint_channel = int(locked_channel) if int(locked_channel or 0) > 0 else hint_channel
    preferred_hint_record_type = locked_record_type if int(locked_channel or 0) > 0 else hint_record_type
    preferred_hint_uid = hint_uid
    hint_is_task_locked = bool(int(locked_channel or 0) > 0)
    if preferred_hint_channel is None and session.persisted_sdk_channel and session.persisted_sdk_channel > 0:
        preferred_hint_channel = int(session.persisted_sdk_channel)
    if preferred_hint_record_type is None and session.persisted_record_type is not None:
        preferred_hint_record_type = int(session.persisted_record_type)
    if preferred_hint_record_type is None and hint_is_task_locked and locked_record_type is not None:
        preferred_hint_record_type = int(locked_record_type)
    if preferred_hint_uid is None and preferred_hint_channel is not None and preferred_hint_record_type is not None:
        preferred_hint_uid = int(session.preferred_download_uid) if session.preferred_download_uid is not None else 0
    allowed_channel_set = {int(locked_channel)} if int(locked_channel or 0) > 0 else None
    user_ids: list[int] = []
    if session.preferred_download_uid is not None:
        user_ids.append(int(session.preferred_download_uid))
    if 0 not in user_ids:
        user_ids.append(0)
    if session.login_uid is not None and int(session.login_uid) not in user_ids:
        user_ids.append(int(session.login_uid))
    log.info(
        "SDK下载uid候选: device=%s ip=%s channel=%s preferred=%s login_uid=%s candidates=%s",
        session.nvr_device_id,
        session.ip,
        session.channel,
        session.preferred_download_uid,
        session.login_uid,
        user_ids,
    )

    original_duration_sec = max(1, int((end_time - start_time).total_seconds()))
    last_err: Exception | None = None
    persisted_failure_recorded = False
    for uid_candidate in user_ids:
        log.info("使用 uid=%s 尝试SDK下载", uid_candidate)
        try:
            ok, ch_used, rt_used, mapping_source, persisted_failed, actual_duration_sec, accepted_as_tail = _sdk_download_with_retry(
                session.sdk,
                uid_candidate,
                session.channel,
                expanded_start,
                expanded_end,
                output_path,
                device_model=session.device_model,
                sdk_start_channel=session.sdk_start_channel,
                sdk_start_dchan=session.sdk_start_dchan,
                ip_chan_num=session.ip_chan_num,
                persisted_sdk_channel=session.persisted_sdk_channel,
                channel_offset=session.channel_offset,
                on_progress=on_progress,
                on_progress_ex=on_progress_ex,
                cancel_check=cancel_check,
                hint_channel=preferred_hint_channel if preferred_hint_uid is not None and uid_candidate == preferred_hint_uid else None,
                hint_record_type=preferred_hint_record_type if preferred_hint_uid is not None and uid_candidate == preferred_hint_uid else None,
                hint_is_task_locked=hint_is_task_locked,
                allowed_channels=allowed_channel_set,
                excluded_channels=excluded_channel_set,
                original_duration_sec=original_duration_sec,
                allow_tail_partial=allow_tail_partial,
            )
            if persisted_failed and session.persisted_sdk_channel and not persisted_failure_recorded:
                _mark_persisted_sdk_hint_failed(session.db_path, session.nvr_device_id, session.channel, int(session.persisted_sdk_channel))
                persisted_failure_recorded = True
            if ok:
                if session.preferred_download_uid is None:
                    log.info("SDK下载uid锁定: device=%s ip=%s channel=%s uid=%s", session.nvr_device_id, session.ip, session.channel, uid_candidate)
                session.preferred_download_uid = int(uid_candidate)
                trusted_mapping = mapping_source in {"hint", "persisted"}
                if ch_used > 0:
                    if trusted_mapping and session.persisted_sdk_channel and int(session.persisted_sdk_channel) != int(ch_used):
                        log.warning(
                            "持久化通道映射已更新: device=%s web_channel=%s old_sdk=%s new_sdk=%s",
                            session.nvr_device_id,
                            session.channel,
                            session.persisted_sdk_channel,
                            ch_used,
                        )
                    if trusted_mapping:
                        _save_persisted_sdk_hint(session.db_path, session.nvr_device_id, session.channel, ch_used, rt_used)
                        session.persisted_sdk_channel = int(ch_used)
                        session.persisted_record_type = int(rt_used)
                    else:
                        log.warning(
                            "低可信候选命中，仅用于当前分段，不写入持久化映射: device=%s web_channel=%s sdk_channel=%s source=%s",
                            session.nvr_device_id,
                            session.channel,
                            ch_used,
                            mapping_source,
                        )
                size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                return DownloadResult(
                    path=output_path,
                    size_bytes=size,
                    channel_used=ch_used,
                    hint_uid=uid_candidate if trusted_mapping else 0,
                    hint_record_type=rt_used,
                    hint_reusable=trusted_mapping,
                    mapping_source=mapping_source,
                    persisted_channel=int(session.persisted_sdk_channel or 0),
                    persisted_failed=bool(persisted_failed),
                    actual_duration_sec=float(actual_duration_sec or 0.0),
                    accepted_as_tail=bool(accepted_as_tail),
                )
        except RuntimeError as e:
            if str(e).startswith("cancelled:"):
                raise
            if str(e) == "no_downloadable_video":
                msg = f"NVR设备（{session.nvr_device_id}）通道号（{session.channel}）无可下载视频！"
                log.warning(msg)
                raise RuntimeError(msg)
            last_err = e
            log.warning("uid=%s SDK下载异常: %s", uid_candidate, e)

    msg = f"sdk_candidate_probe_failed:{last_err}" if last_err else f"NVR设备（{session.nvr_device_id}）通道号（{session.channel}）无可下载视频！"
    if allow_rtsp_fallback:
        log.warning("SDK下载全部失败(last_err=%s)，RTSP回退已禁用，直接报错", last_err)
    else:
        log.warning("SDK下载全部失败(last_err=%s), 已禁用RTSP回退", last_err)
    raise RuntimeError(msg)

#  Main entry: download_by_time
# ---------------------------------------------------------------------------

def download_by_time(
    *,
    sdk_dir: str | None,
    db_path: str | None = None,
    nvr_device_id: int | None = None,
    ip: str,
    port: int,
    username: str,
    password: str,
    channel: int,
    start_time: datetime,
    end_time: datetime,
    output_path: str,
    device_model: str | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    on_progress_ex: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    stall_timeout_sec: int = STALL_TIMEOUT_SECONDS,
    hint_uid: int | None = None,
    hint_channel: int | None = None,
    hint_record_type: int | None = None,
    locked_channel: int | None = None,
    locked_record_type: int | None = None,
    excluded_channels: list[int] | None = None,
    apply_start_padding: bool = True,
    apply_end_padding: bool = True,
    allow_tail_partial: bool = False,
    allow_rtsp_fallback: bool = False,
) -> DownloadResult:
    try:
        session = open_download_session(
            sdk_dir=sdk_dir,
            db_path=db_path,
            nvr_device_id=nvr_device_id,
            ip=ip,
            port=port,
            username=username,
            password=password,
            channel=channel,
            device_model=device_model,
        )
    except Exception as e:
        msg = str(e or "").strip()
        if msg.startswith("NVR设备掉线或网络不可达") or msg.startswith("NVR设备连接失败"):
            log.warning("NVR连接失败，停止下载回退链路: %s", msg)
            raise RuntimeError(msg)
        if allow_rtsp_fallback:
            log.warning("SDK初始化或登录失败: %s, RTSP回退已禁用，直接报错", e)
        else:
            log.warning("SDK初始化或登录失败: %s, 已禁用RTSP回退", e)
        raise RuntimeError(msg or "sdk_init_failed")

    try:
        return download_by_time_with_session(
            session,
            start_time=start_time,
            end_time=end_time,
            output_path=output_path,
            on_progress=on_progress,
            on_progress_ex=on_progress_ex,
            cancel_check=cancel_check,
            hint_uid=hint_uid,
            hint_channel=hint_channel,
            hint_record_type=hint_record_type,
            locked_channel=locked_channel,
            locked_record_type=locked_record_type,
            excluded_channels=excluded_channels,
            apply_start_padding=apply_start_padding,
            apply_end_padding=apply_end_padding,
            allow_tail_partial=allow_tail_partial,
            allow_rtsp_fallback=allow_rtsp_fallback,
        )
    finally:
        close_download_session(session)
