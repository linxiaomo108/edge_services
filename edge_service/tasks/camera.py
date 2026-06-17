from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..db import Db, DbConfig
from ..utils import safe_name as _safe_name, load_download_path as _load_download_path, get_lesson_dir as _get_lesson_dir, resolve_lesson_date as _resolve_lesson_date, task_type_prefix as _task_type_prefix, is_task_step_enabled
from ..video.ffmpeg import ffmpeg_exists, remux_faststart, probe_audio_content_offset, probe_audio_stream_info, probe_authoritative_duration_seconds, probe_bitrate_bps, probe_duration_seconds, probe_max_packet_duration, probe_media_start_seconds, probe_packet_timeline_anomaly, probe_stream_timings, rebuild_zero_based_timeline, transcode_crf_progress, generate_hls_crf_progress
from ..video.hik.systrans import system_transform_file
from ..video.nvr import close_download_session, download_by_time, download_by_time_with_session, open_download_session

LESSON_DURATION_TOLERANCE_SECONDS = 60.0
TRANSCODE_COMPLETE_TOLERANCE_SECONDS = 30.0
TIMELINE_START_TOLERANCE_SECONDS = 2.0
TIMELINE_GAP_TOLERANCE_SECONDS = 1.0
AV_SYNC_START_TOLERANCE_SECONDS = 0.05
AUDIO_EARLY_ALIGN_THRESHOLD_SECONDS = 0.2
AUDIO_LATE_REBUILD_THRESHOLD_SECONDS = 0.2
SYSTRANS_MERGE_TOLERABLE_DURATION_DELTA_SECONDS = 1.0
ABSOLUTE_TIMELINE_START_REBUILD_THRESHOLD_SECONDS = 60.0
ABSOLUTE_TIMELINE_TS_BRIDGE_POLICY = "absolute_timeline_same_origin_ts_bridge"
STRUCTURAL_PACKET_DURATION_ABNORMAL_SECONDS = 60.0
STRUCTURAL_AUDIO_PACKET_DURATION_ABNORMAL_SECONDS = 1.0
STRUCTURAL_AUDIO_PACKET_DURATION_WARNING_SECONDS = 0.5
STRUCTURAL_STREAM_END_GAP_ABNORMAL_SECONDS = 60.0
DOWNLOAD_PART_REBUILD_START_GAP_TOLERANCE_SECONDS = 0.3
DOWNLOAD_PART_REBUILD_END_GAP_TOLERANCE_SECONDS = 0.8
DOWNLOAD_PART_AUDIO_TAIL_TRIM_MAX_SECONDS = 3.0
DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_MIN_SECONDS = 30.0
DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_MIN_AUDIO_SECONDS = 30.0
DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_CLASSIFICATION = "expected_tail_silence_from_source"
DOWNLOAD_PART_SOURCE_NO_AUDIO_CLASSIFICATION = "source_no_audio_video_only"
TS_AUDIO_FORCE_AAC_CODECS = {"pcm_alaw", "pcm_mulaw", "mp2"}
# raw 与 systrans 的 start_gap 差额相对比例：systrans 将两条流时间戳各自归零会丢失 raw 的真实音视频起始差，
# 当差异超过 raw 自身 gap 的此比例（且超过 SYSTRANS_LOST_RAW_START_GAP_FLOOR_SECONDS 兜底下限）时，
# 认为 systrans 抹平了 raw 的关键特征，触发 systrans 准入拒收。该判定不再依赖具体 gap 秒数。
SYSTRANS_LOST_RAW_START_GAP_RATIO = 0.5
SYSTRANS_LOST_RAW_START_GAP_FLOOR_SECONDS = 1.0
MERGE_BOUNDARY_DECODE_CHECK_WINDOW_SECONDS = 15.0
MERGE_BOUNDARY_VIDEO_GAP_PACKET_ABNORMAL_SECONDS = 1.0
MERGE_BOUNDARY_VIDEO_GAP_AUDIO_PACKET_MAX_SECONDS = 0.5
MERGE_BOUNDARY_VIDEO_GAP_AV_TOLERANCE_SECONDS = 0.3
PER_NVR_SDK_DOWNLOAD_LIMIT = 2
_nvr_sdk_download_limit_lock = threading.Lock()
_nvr_sdk_download_limits: dict[str, asyncio.Semaphore] = {}


def _nvr_download_limit_key(nvr_device_id: int | None, ip: str, port: int) -> str:
    if int(nvr_device_id or 0) > 0:
        return f"device:{int(nvr_device_id)}"
    return f"ip:{str(ip or '').strip()}:{int(port or 0)}"


def _get_nvr_sdk_download_semaphore(nvr_device_id: int | None, ip: str, port: int) -> asyncio.Semaphore:
    key = _nvr_download_limit_key(nvr_device_id, ip, port)
    with _nvr_sdk_download_limit_lock:
        sem = _nvr_sdk_download_limits.get(key)
        if sem is None:
            sem = asyncio.Semaphore(PER_NVR_SDK_DOWNLOAD_LIMIT)
            _nvr_sdk_download_limits[key] = sem
        return sem

# CAM-DL-NORM-000: 分段时间线正常，跳过下载阶段音视频修复。
CAM_DL_NORM_000 = "CAM-DL-NORM-000"
# CAM-DL-NORM-010: 分段命中强异常时间线，执行完整重建，并要求后续走 canonical merge。
CAM_DL_NORM_010 = "CAM-DL-NORM-010"
# CAM-DL-NORM-020: 分段仅有轻微音频晚到或轻微脏时间线，风险可接受，跳过修复。
CAM_DL_NORM_020 = "CAM-DL-NORM-020"
# CAM-DL-NORM-030: 分段存在明显 stream gap，执行完整重建以恢复时间线。
CAM_DL_NORM_030 = "CAM-DL-NORM-030"
# CAM-DL-NORM-040: 分段音频略早于视频但在阈值内，跳过对齐。
CAM_DL_NORM_040 = "CAM-DL-NORM-040"
# CAM-DL-NORM-050: 分段音频早于视频且超过阈值，执行 itsoffset 无损对齐。
CAM_DL_NORM_050 = "CAM-DL-NORM-050"
# CAM-DL-NORM-051: itsoffset 对齐失败，回退到完整重建。
CAM_DL_NORM_051 = "CAM-DL-NORM-051"
# CAM-DL-NORM-052: itsoffset 对齐异常，回退到完整重建。
CAM_DL_NORM_052 = "CAM-DL-NORM-052"
# CAM-DL-NORM-060: 分段优先通过海康 SystemTransform 修复时间轴。
CAM_DL_NORM_060 = "CAM-DL-NORM-060"
# CAM-DL-NORM-061: 海康 SystemTransform 输出未通过验收，回退修复。
CAM_DL_NORM_061 = "CAM-DL-NORM-061"
# CAM-DL-NORM-070: 分段相对对齐健康但绝对起点异常，对 raw 与 systrans 候选做通用对比，选优后轻量归零。
CAM_DL_NORM_070 = "CAM-DL-NORM-070"
# CAM-DL-NORM-071: zero_based 选优后轻量归零失败，直接采用所选候选作为 merge 输入。
CAM_DL_NORM_071 = "CAM-DL-NORM-071"
# CAM-DL-NORM-080: 强异常入口内的 audio_early 主导场景，通过 itsoffset+零基化 stream copy 完成轻量修复。
CAM_DL_NORM_080 = "CAM-DL-NORM-080"
# CAM-DL-NORM-081: L2 轻量修复未通过验收，回退到 010 完整重建路径。
CAM_DL_NORM_081 = "CAM-DL-NORM-081"
# CAM-DL-NORM-082: audio-early 主导场景轻量修复。
CAM_DL_NORM_082 = "CAM-DL-NORM-082"
# CAM-DL-NORM-083: PCM 音频早到且绝对时间轴异常，优先尝试 copy video + 重建音频时间轴。
CAM_DL_NORM_083 = "CAM-DL-NORM-083"
# CAM-DL-NORM-084: 断电/中断类 audio_content_late，按真实视频时长 copy video + 修音频，避免按异常长时间轴重建。
CAM_DL_NORM_084 = "CAM-DL-NORM-084"

# CAM-DL-SRC-010: 源视频时间线异常或强制校准，执行源视频时间线重建。
CAM_DL_SRC_010 = "CAM-DL-SRC-010"
# CAM-DL-SRC-011: 源视频时间线重建失败。
CAM_DL_SRC_011 = "CAM-DL-SRC-011"
# CAM-DL-SRC-020: 合并后的源视频结构异常，先执行完整重建。
CAM_DL_SRC_020 = "CAM-DL-SRC-020"
# CAM-DL-SRC-021: 源视频结构异常重建失败，但继续后续流程。
CAM_DL_SRC_021 = "CAM-DL-SRC-021"
# CAM-DL-SRC-030: 源视频预校准时间轴失败，但继续后续流程。
CAM_DL_SRC_030 = "CAM-DL-SRC-030"
# CAM-DL-SRC-040: 源视频裁剪到课次基准时长。
CAM_DL_SRC_040 = "CAM-DL-SRC-040"
# CAM-DL-SRC-041: 源视频补齐到课次基准时长。
CAM_DL_SRC_041 = "CAM-DL-SRC-041"
# CAM-DL-SRC-050: 源视频执行 faststart 重封装。
CAM_DL_SRC_050 = "CAM-DL-SRC-050"
# CAM-DL-SRC-051: 源视频 faststart 重封装失败。
CAM_DL_SRC_051 = "CAM-DL-SRC-051"

# CAM-MRG-010: 强异常任务对单段做 canonical A/V 重编码，产出 merge-ready MP4。
CAM_MRG_010 = "CAM-MRG-010"
# CAM-MRG-011: 强异常任务使用 canonical MP4 列表执行最终 concat copy。
CAM_MRG_011 = "CAM-MRG-011"
# CAM-MRG-020: 普通任务优先尝试 direct concat copy。
CAM_MRG_020 = "CAM-MRG-020"
# CAM-MRG-030: 普通任务先对单段做 normalized remux，再参与 concat。
CAM_MRG_030 = "CAM-MRG-030"
# CAM-MRG-031: normalized MP4 列表执行最终 concat copy。
CAM_MRG_031 = "CAM-MRG-031"
# CAM-MRG-040: TS fallback 使用 copy video + audio profile 生成单段 TS。
CAM_MRG_040 = "CAM-MRG-040"
# CAM-MRG-041: TS fallback 单段 copy 失败后，执行视频重编码兜底。
CAM_MRG_041 = "CAM-MRG-041"
# CAM-MRG-050: TS fallback 最终合并因音频包异常改为 audio reencode。
CAM_MRG_050 = "CAM-MRG-050"
# CAM-MRG-051: TS fallback 最终合并直接 copy A/V。
CAM_MRG_051 = "CAM-MRG-051"
# CAM-MRG-060: merged 输出因音频包异常执行最终修复。
CAM_MRG_060 = "CAM-MRG-060"
CAM_MRG_061 = "CAM-MRG-061"


def _branch_code_tag(code: str) -> str:
    return f"[{str(code or '').strip()}]"

@dataclass(frozen=True)
class CameraTaskPlan:
    download_seconds: int
    transcode_seconds: int


@dataclass(frozen=True)
class DownloadPartNormalizeResult:
    duration_sec: float
    force_canonical_merge: bool = False
    canonicalize_merge_part: bool = False
    merge_risk_level: int = 0
    normalize_reason: str = ""
    merge_part_path: str = ""
    classification: str = ""


@dataclass(frozen=True)
class DownloadBatchAvPolicy:
    name: str = "default"
    start_gap_median: float = 0.0
    start_gap_range: float = 0.0
    part_count: int = 0
    reason: str = ""
    target_video_codec: str = ""
    video_codec_mix: bool = False
    video_codec_reason: str = ""


@dataclass(frozen=True)
class DownloadResumeReconcileResult:
    part_idx: int
    current_start: datetime
    current_seg_sec: int
    completed_duration_sec: float
    reused_parts: int = 0
    reprocess_parts: int = 0
    deleted_partial_parts: int = 0
    pending_reprocess: tuple[int, ...] = ()
    pending_reprocess_meta: dict[int, dict[str, Any]] | None = None
    reused_part_meta: tuple[dict[str, Any], ...] = ()
    status_message: str = ""


class FinalizePendingError(RuntimeError):
    def __init__(self, *, step_code: str, src_path: str, dst_path: str, action: str, reason: str, user_message: str = "文件暂时被占用，系统将自动重试最终提交") -> None:
        super().__init__(str(user_message or "文件暂时被占用，系统将自动重试最终提交"))
        self.step_code = str(step_code or "")
        self.src_path = str(src_path or "")
        self.dst_path = str(dst_path or "")
        self.action = str(action or "")
        self.reason = str(reason or "")
        self.user_message = str(user_message or "文件暂时被占用，系统将自动重试最终提交")


def _resolve_download_merge_policy(classification: str) -> tuple[bool, bool, int]:
    normalized = str(classification or "").strip().lower()
    if normalized in {"timestamp_only_audio_late", "duration_delta_audio_late"}:
        return False, False, 0
    if normalized == "timestamp_only_audio_late_packet_disorder":
        return False, True, 1
    if normalized in {"audio_late_uncertain", "audio_content_late"}:
        return True, True, 2
    return True, True, 2


def _format_process_stage_status_message(status_message: str, classification: str = "", normalize_reason: str = "") -> str:
    msg = str(status_message or "").strip()
    cls = str(classification or normalize_reason or "").strip().lower()

    if "回退完整重建当前分段" in msg:
        if "无损对齐失败" in msg:
            return "命中音频早到对齐失败，正在回退完整重建当前分段"
        if "无损对齐异常" in msg:
            return "命中音频早到对齐异常，正在回退完整重建当前分段"
        return "命中分段修复回退场景，正在回退完整重建当前分段"

    if "正在完整重建当前分段" in msg:
        if cls in {"audio_content_late", "audio_late_uncertain", "strong_abnormal_timeline"}:
            return "命中强异常时间线修复，正在完整重建当前分段"
        if cls in {"timestamp_only_audio_late", "duration_delta_audio_late"}:
            return "命中音频晚到修复，正在完整重建当前分段"
        if cls == "timestamp_only_audio_late_packet_disorder":
            return "命中音频晚到且音频包乱序修复，正在完整重建当前分段"
        if msg.startswith("检测到分段绝对时间轴异常"):
            return "命中强异常时间线修复，正在完整重建当前分段"
        if msg.startswith("检测到分段时间线异常"):
            return "命中时间线异常修复，正在完整重建当前分段"
        if msg.startswith("检测到音频晚于视频"):
            return "命中音频晚到修复，正在完整重建当前分段"
        return "命中分段时间线修复，正在完整重建当前分段"

    if "正在无损对齐" in msg:
        return "命中音频早到无损对齐，正在无损对齐当前分段"

    if "命中音频早到轻量修复" in msg or "itsoffset 推迟音频" in msg:
        return "命中音频早到轻量修复，正在轻量归零分段时间线"

    if msg.startswith("检测到分段绝对时间轴异常但相对对齐健康"):
        return "命中绝对时间轴异常修复，正在对比 raw/systrans 候选"
    if "轻量归零分段时间线" in msg or "分段时间线已完成轻量归零" in msg:
        return "命中绝对时间轴异常修复，正在轻量归零分段时间线"

    if msg:
        return msg
    return "正在分析当前分段"


def build_camera_plan(raw: dict[str, Any], simulate: bool) -> CameraTaskPlan:
    if not simulate:
        return CameraTaskPlan(download_seconds=1, transcode_seconds=1)
    return CameraTaskPlan(download_seconds=25, transcode_seconds=25)


def _parse_dt(v: str) -> datetime:
    s = str(v or "").strip()
    if not s:
        raise RuntimeError("missing datetime")
    s = s.replace("Z", "")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception as e:
            raise RuntimeError("invalid datetime") from e


def _resolve_nvr_provider(raw: dict[str, Any], nvr: dict[str, Any]) -> str | None:
    return str(
        raw.get("nvrProvider")
        or raw.get("deviceVendor")
        or raw.get("vendor")
        or nvr.get("nvrProvider")
        or nvr.get("deviceVendor")
        or nvr.get("vendor")
        or ""
    ).strip() or None


def _load_lesson_sync_info(db_path: str | None, lesson_id: str) -> dict[str, Any] | None:
    try:
        if not db_path:
            return None
        db = Db(DbConfig(path=str(db_path)))
        row = db.fetch_one(
            "SELECT t.download_start, t.download_end, s.output_file_path "
            "FROM edge_stream_task t "
            "JOIN edge_stream_task_step s ON s.task_id=t.id AND s.step_code='DOWNLOAD' "
            "WHERE t.lesson_id=? AND t.task_kind='CameraTask' AND t.task_type=1 "
            "AND s.step_status=2 AND s.output_file_path IS NOT NULL AND s.output_file_path<>'' "
            "ORDER BY t.id DESC LIMIT 1",
            (int(lesson_id),),
        )
        if row is None:
            return None
        output_file_path = str(row["output_file_path"] or "").strip()
        if not output_file_path:
            return None
        p = Path(output_file_path)
        if not p.exists():
            return None
        duration_sec = float(probe_duration_seconds(str(p)) or 0.0)
        start_raw = str(row["download_start"] or "").strip()
        end_raw = str(row["download_end"] or "").strip()
        if not start_raw:
            return None
        start_at = _parse_dt(start_raw)
        end_at = _parse_dt(end_raw) if end_raw else start_at
        return {
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "duration_sec": duration_sec,
            "task_type_1_duration": duration_sec,
            "output_file_path": str(p),
        }
    except Exception:
        return None


def _probe_video_fps(video_path: Path) -> float:
    if not video_path.exists():
        return 25.0
    try:
        cmd = [
            _ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        vals = [str(x or "").strip() for x in (r.stdout or "").splitlines() if str(x or "").strip()]
        for raw in vals:
            if "/" in raw:
                a, b = raw.split("/", 1)
                fa = float(a or 0)
                fb = float(b or 0)
                if fa > 0 and fb > 0:
                    fps = fa / fb
                    if fps > 0:
                        return fps
            else:
                fps = float(raw)
                if fps > 0:
                    return fps
    except Exception:
        log.debug("_probe_video_fps failed, using default 25.0", exc_info=True)
    return 25.0


def _canonicalize_duration_sec(duration_sec: float, video_path: Path | None = None) -> float:
    dur = float(duration_sec or 0.0)
    if dur <= 0:
        return 0.0
    fps = _probe_video_fps(video_path) if video_path is not None else 25.0
    fps = fps if fps > 0 else 25.0
    return math.ceil(dur * fps) / fps


def _duration_adjustment_needed(current_duration: float, target_duration: float, video_path: Path | None = None) -> bool:
    cur = float(current_duration or 0.0)
    tgt = float(target_duration or 0.0)
    if cur <= 0 or tgt <= 0:
        return False
    fps = _probe_video_fps(video_path) if video_path is not None else 25.0
    fps = fps if fps > 0 else 25.0
    tolerance = max(LESSON_DURATION_TOLERANCE_SECONDS, 2.0 / fps)
    return abs(cur - tgt) > tolerance


def _probe_stream_start_times(video_path: Path) -> dict[str, float]:
    if not video_path.exists():
        return {}
    out: dict[str, float] = {}
    try:
        cmd = [
            _ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,start_time",
            "-of",
            "default=noprint_wrappers=1",
            str(video_path),
        ]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        codec_type = ""
        for raw in (r.stdout or "").splitlines():
            line = str(raw or "").strip()
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
        logging.getLogger("edge.runner").debug("probe stream start times failed: %s", str(video_path), exc_info=True)
    return out


def _probe_timeline_state(video_path: Path) -> dict[str, float | bool]:
    if not video_path.exists():
        return {
            "media_start": 0.0,
            "video_start": 0.0,
            "audio_start": 0.0,
            "max_start": 0.0,
            "min_start": 0.0,
            "stream_gap": 0.0,
            "dirty": False,
        }
    start_sec = float(probe_media_start_seconds(str(video_path)) or 0.0)
    stream_starts = _probe_stream_start_times(video_path)
    positive_starts = [float(v) for v in stream_starts.values() if float(v) >= 0.0]
    max_stream_start = max(positive_starts) if positive_starts else start_sec
    min_stream_start = min(positive_starts) if positive_starts else start_sec
    video_start = float(stream_starts.get("video") or 0.0)
    audio_start = float(stream_starts.get("audio") or 0.0)
    stream_gap = abs(max_stream_start - min_stream_start)
    dirty = not (
        start_sec <= TIMELINE_START_TOLERANCE_SECONDS
        and max_stream_start <= TIMELINE_START_TOLERANCE_SECONDS
        and stream_gap <= TIMELINE_GAP_TOLERANCE_SECONDS
    )
    return {
        "media_start": start_sec,
        "video_start": video_start,
        "audio_start": audio_start,
        "max_start": max_stream_start,
        "min_start": min_stream_start,
        "stream_gap": stream_gap,
        "dirty": dirty,
    }


def _probe_precise_av_sync(video_path: Path, *, include_packet_timeline: bool = True) -> dict[str, float | bool | str]:
    timings = probe_stream_timings(str(video_path)) if video_path.exists() else {}
    video_timing = timings.get("video") or {}
    audio_timing = timings.get("audio") or {}
    video_start = float(video_timing.get("start_time") or 0.0)
    audio_start = float(audio_timing.get("start_time") or 0.0)
    video_duration = float(video_timing.get("duration") or 0.0)
    audio_duration = float(audio_timing.get("duration") or 0.0)
    coarse_gap = video_start - audio_start
    duration_delta = audio_duration - video_duration if video_duration > 0 and audio_duration > 0 else 0.0
    precise_trim = 0.0
    reason = ""
    audio_content_offset = 0.0
    classification = "aligned"
    timestamp_gap = max(0.0, audio_start - video_start)
    packet_timeline = (
        probe_packet_timeline_anomaly(str(video_path), stream_selector="a:0")
        if (
            video_path.exists()
            and include_packet_timeline
            and video_duration > 0.0
            and audio_duration > 0.0
        )
        else {}
    )
    pts_backward_count = int(packet_timeline.get("pts_backward_count") or 0)
    dts_backward_count = int(packet_timeline.get("dts_backward_count") or 0)
    max_pts_backward_sec = float(packet_timeline.get("max_pts_backward_sec") or 0.0)
    max_dts_backward_sec = float(packet_timeline.get("max_dts_backward_sec") or 0.0)
    audio_packet_timeline_disorder = bool(
        (pts_backward_count > 0 or dts_backward_count > 0)
        and max(max_pts_backward_sec, max_dts_backward_sec) >= 0.005
    )
    if (
        video_path.exists()
        and video_duration > 0.0
        and audio_duration > 0.0
        and (timestamp_gap > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS or duration_delta > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS)
    ):
        try:
            probe_window = duration_delta if duration_delta > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS and timestamp_gap <= AUDIO_LATE_REBUILD_THRESHOLD_SECONDS else timestamp_gap
            audio_content_offset = float(probe_audio_content_offset(str(video_path), probe_sec=min(20.0, max(8.0, probe_window + 2.0))) or 0.0)
        except Exception:
            audio_content_offset = 0.0
    timestamp_dominant_audio_late = bool(
        timestamp_gap > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS
        and abs(duration_delta) <= max(0.6, min(3.0, timestamp_gap * 0.08))
        and (pts_backward_count + dts_backward_count) <= 2
        and max(max_pts_backward_sec, max_dts_backward_sec) <= 0.05
        and audio_content_offset <= min(4.0, max(1.0, timestamp_gap * 0.25))
    )
    timestamp_only_audio_late = bool(
        timestamp_gap > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS
        and (
            audio_content_offset <= min(0.8, max(0.2, timestamp_gap * 0.25))
            or timestamp_dominant_audio_late
        )
    )
    content_late_likely = bool(
        timestamp_gap > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS
        and audio_content_offset >= max(0.15, min(max(0.2, timestamp_gap - 0.15), timestamp_gap * 0.6))
    )
    if timestamp_gap > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS:
        precise_trim = timestamp_gap
        reason = "stream_start"
        if timestamp_only_audio_late:
            classification = "timestamp_only_audio_late_packet_disorder" if audio_packet_timeline_disorder else "timestamp_only_audio_late"
        elif content_late_likely:
            classification = "audio_content_late"
        else:
            classification = "audio_late_uncertain"
    elif duration_delta > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS:
        precise_trim = duration_delta
        reason = "duration_delta"
        classification = "duration_delta_audio_late"
    elif coarse_gap > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS:
        classification = "audio_early"
    return {
        "video_start": video_start,
        "audio_start": audio_start,
        "coarse_gap": coarse_gap,
        "video_duration": video_duration,
        "audio_duration": audio_duration,
        "duration_delta": duration_delta,
        "audio_content_offset": audio_content_offset,
        "audio_late_trim": max(0.0, precise_trim),
        "reason": reason,
        "audio_late": precise_trim > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS,
        "classification": classification,
        "timestamp_only_audio_late": timestamp_only_audio_late,
        "audio_packet_timeline_disorder": audio_packet_timeline_disorder,
        "audio_pts_backward_count": pts_backward_count,
        "audio_dts_backward_count": dts_backward_count,
        "audio_max_pts_backward_sec": max_pts_backward_sec,
        "audio_max_dts_backward_sec": max_dts_backward_sec,
    }


def _normalized_part_output_path(part_path: Path) -> Path:
    return part_path.with_name(part_path.stem + ".fixed.mp4")


def _systrans_part_output_path(part_path: Path) -> Path:
    return part_path.with_name(part_path.stem + ".systrans.mp4")


def _download_part_tmp_paths(part_path: Path) -> list[Path]:
    normalized_part = _normalized_part_output_path(part_path)
    systrans_part = _systrans_part_output_path(part_path)
    return [
        normalized_part,
        systrans_part,
        normalized_part.with_name(normalized_part.stem + ".timeline.tmp.mp4"),
        normalized_part.with_name(normalized_part.stem + ".avsync.tmp.mp4"),
        systrans_part.with_name(systrans_part.stem + ".timeline.tmp.mp4"),
        part_path.with_name(part_path.stem + ".timeline.tmp.mp4"),
    ]


def _cleanup_download_part_derivatives(part_path: Path, done_path: Path | None = None, *, remove_done: bool, keep_merge_part: str = "") -> list[str]:
    removed: list[str] = []
    keep_target = str(keep_merge_part or "").strip()
    for path in _download_part_tmp_paths(part_path):
        if keep_target and str(path) == keep_target:
            continue
        try:
            if path.exists() and path != part_path:
                path.unlink(missing_ok=True)
                removed.append(path.name)
        except Exception:
            pass
    if remove_done and done_path is not None:
        try:
            if done_path.exists():
                done_path.unlink(missing_ok=True)
                removed.append(done_path.name)
        except Exception:
            pass
    return removed


def _is_download_part_process_completed(meta: dict[str, Any] | None) -> bool:
    if not meta:
        return False
    if "process_completed" in meta:
        return bool(meta.get("process_completed"))
    return any(
        key in meta
        for key in (
            "merge_part_path",
            "normalize_reason",
            "normalize_classification",
            "force_canonical_merge",
            "canonicalize_merge_part",
            "merge_risk_level",
        )
    )


def _download_source_identity(
    *,
    nvr_device_id: int | None,
    ip: str,
    port: int,
    web_channel: int,
    provider: str,
) -> dict[str, Any]:
    return {
        "nvr_device_id": int(nvr_device_id or 0),
        "nvr_ip": str(ip or "").strip(),
        "nvr_port": int(port or 0),
        "web_channel": int(web_channel or 0),
        "nvr_provider": str(provider or "").strip().lower(),
    }


def _download_source_identity_mismatch(meta: dict[str, Any], expected: dict[str, Any] | None) -> str:
    if not expected:
        return ""
    checks = (
        ("nvr_device_id", int),
        ("nvr_ip", str),
        ("nvr_port", int),
        ("web_channel", int),
        ("nvr_provider", str),
    )
    for key, caster in checks:
        if key not in meta:
            return f"done_meta_missing_source_identity:{key}"
        try:
            actual = caster(meta.get(key) or 0) if caster is int else str(meta.get(key) or "").strip().lower()
            want = caster(expected.get(key) or 0) if caster is int else str(expected.get(key) or "").strip().lower()
        except Exception:
            return f"done_meta_invalid_source_identity:{key}"
        if actual != want:
            return f"done_meta_source_mismatch:{key}:{actual}!={want}"
    return ""


def _is_download_part_ready_for_reuse(part_path: Path, done_path: Path, *, done_meta: dict[str, Any] | None = None, expected_identity: dict[str, Any] | None = None) -> tuple[bool, str, str]:
    meta = done_meta or _load_part_done_meta(done_path)
    if not part_path.exists() or part_path.stat().st_size <= 0:
        return False, "raw_part_missing", ""
    identity_mismatch = _download_source_identity_mismatch(meta, expected_identity)
    if identity_mismatch:
        return False, identity_mismatch, ""
    if not _is_download_part_process_completed(meta):
        return False, "part_process_incomplete", ""
    range_end_raw = str(meta.get("range_end") or "").strip()
    if not range_end_raw:
        return False, "done_meta_missing_range_end", ""
    merge_part_path = str(meta.get("merge_part_path") or "").strip() or str(part_path)
    merge_part = Path(merge_part_path)
    if not merge_part.exists() or merge_part.stat().st_size <= 0:
        return False, "merge_part_missing", merge_part_path
    issue = _probe_structural_media_issue(merge_part)
    if issue and str(meta.get("normalize_classification") or "").strip() == DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_CLASSIFICATION and _is_expected_tail_silence_output(merge_part):
        issue = ""
    if issue and str(meta.get("normalize_classification") or "").strip() == DOWNLOAD_PART_SOURCE_NO_AUDIO_CLASSIFICATION and _source_part_has_no_audio_stream(merge_part):
        issue = ""
    if issue:
        return False, f"merge_part_invalid:{issue}", merge_part_path
    if merge_part != part_path and merge_part_path == str(part_path):
        return False, "merge_part_path_incomplete", merge_part_path
    return True, "ok", merge_part_path


def _reconcile_download_resume_parts(
    *,
    out_dir: Path,
    prefix: str,
    server_task_id: str,
    download_start: datetime,
    download_end: datetime,
    initial_seg_sec: int,
    adaptive_segmenting: bool,
    expected_identity: dict[str, Any] | None,
    log: logging.Logger,
) -> DownloadResumeReconcileResult:
    current_start = download_start
    current_seg_sec = int(initial_seg_sec)
    completed_duration_sec = 0.0
    part_idx = 1
    reused_parts = 0
    reprocess_parts = 0
    deleted_partial_parts = 0
    pending_reprocess: list[int] = []
    pending_reprocess_meta: dict[int, dict[str, Any]] = {}
    reused_part_meta: list[dict[str, Any]] = []
    while current_start < download_end:
        rs = current_start
        re = rs + timedelta(seconds=int(current_seg_sec))
        if re > download_end:
            re = download_end
        if re <= rs:
            break
        idx = int(part_idx)
        part = out_dir / f"{prefix}_{server_task_id}.part{idx:03d}.mp4"
        done = out_dir / f"{prefix}_{server_task_id}.part{idx:03d}.done"
        part_duration_sec = max(1.0, float((re - rs).total_seconds()))
        if done.exists() and part.exists() and part.stat().st_size > 0:
            done_meta = _load_part_done_meta(done)
            reusable, reason, merge_part_path = _is_download_part_ready_for_reuse(part, done, done_meta=done_meta, expected_identity=expected_identity)
            if reusable:
                reused_parts += 1
                reused_part_meta.append({
                    "part_idx": idx,
                    "part_path": str(part),
                    "merge_part_path": merge_part_path or str(part),
                    "channel_used": int(done_meta.get("channel_used") or 0),
                    "record_type_used": int(done_meta.get("record_type_used") or 0),
                    "normalize_reason": str(done_meta.get("normalize_reason") or ""),
                    "normalize_classification": str(done_meta.get("normalize_classification") or ""),
                    "force_canonical_merge": bool(done_meta.get("force_canonical_merge", False)),
                    "canonicalize_merge_part": bool(done_meta.get("canonicalize_merge_part", False)),
                    "merge_risk_level": int(done_meta.get("merge_risk_level") or 0),
                    "part_duration_sec": part_duration_sec,
                })
                completed_duration_sec = min(float((download_end - download_start).total_seconds()), completed_duration_sec + part_duration_sec)
                range_end_raw = str(done_meta.get("range_end") or "").strip()
                if range_end_raw:
                    try:
                        current_start = _parse_dt(range_end_raw)
                    except Exception:
                        current_start = re
                else:
                    current_start = re
                next_seg_sec = int(done_meta.get("next_seg_sec") or current_seg_sec or initial_seg_sec)
                current_seg_sec = max(30, int(next_seg_sec)) if adaptive_segmenting else int(initial_seg_sec)
                part_idx += 1
                continue
            if reason.startswith("done_meta_missing_source_identity:") or reason.startswith("done_meta_invalid_source_identity:") or reason.startswith("done_meta_source_mismatch:"):
                removed = _cleanup_download_part_derivatives(part, done, remove_done=True, keep_merge_part="")
                try:
                    part.unlink(missing_ok=True)
                    removed.append(part.name)
                except Exception:
                    pass
                deleted_partial_parts += 1
                log.warning(
                    "download resume delete untrusted part task=%s part=%s reason=%s raw=%s removed=%s",
                    server_task_id,
                    idx,
                    reason,
                    str(part),
                    ",".join(removed) if removed else "",
                )
                break
            removed = _cleanup_download_part_derivatives(part, done, remove_done=False, keep_merge_part="")
            pending_reprocess.append(idx)
            pending_reprocess_meta[idx] = done_meta
            reprocess_parts += 1
            log.info(
                "download resume reprocess queued task=%s part=%s reason=%s kept_raw=%s removed=%s",
                server_task_id,
                idx,
                reason,
                str(part),
                ",".join(removed) if removed else "",
            )
            if reason == "part_process_incomplete":
                completed_duration_sec = min(float((download_end - download_start).total_seconds()), completed_duration_sec + part_duration_sec)
                range_end_raw = str(done_meta.get("range_end") or "").strip()
                if range_end_raw:
                    try:
                        current_start = _parse_dt(range_end_raw)
                    except Exception:
                        current_start = re
                else:
                    current_start = re
                next_seg_sec = int(done_meta.get("next_seg_sec") or current_seg_sec or initial_seg_sec)
                current_seg_sec = max(30, int(next_seg_sec)) if adaptive_segmenting else int(initial_seg_sec)
                part_idx += 1
                continue
            break
        if part.exists() and part.stat().st_size > 0:
            removed = _cleanup_download_part_derivatives(part, done, remove_done=True, keep_merge_part="")
            try:
                part.unlink(missing_ok=True)
                removed.append(part.name)
            except Exception:
                pass
            deleted_partial_parts += 1
            log.info(
                "download resume delete partial part task=%s part=%s raw=%s removed=%s",
                server_task_id,
                idx,
                str(part),
                ",".join(removed) if removed else "",
            )
            break
        break
    parts: list[str] = []
    if reused_parts > 0:
        parts.append(f"复用已完成分段{reused_parts}个")
    if reprocess_parts > 0:
        parts.append(f"重建处理中断分段{reprocess_parts}个（保留源分段）")
    if deleted_partial_parts > 0:
        parts.append(f"删除未完成分段{deleted_partial_parts}个")
    status_message = "；".join(parts)
    return DownloadResumeReconcileResult(
        part_idx=part_idx,
        current_start=current_start,
        current_seg_sec=current_seg_sec,
        completed_duration_sec=completed_duration_sec,
        reused_parts=reused_parts,
        reprocess_parts=reprocess_parts,
        deleted_partial_parts=deleted_partial_parts,
        pending_reprocess=tuple(pending_reprocess),
        pending_reprocess_meta=pending_reprocess_meta,
        reused_part_meta=tuple(reused_part_meta),
        status_message=status_message,
    )


def _probe_structural_media_issue(video_path: Path, *, include_packet_metrics: bool = True) -> str:
    if not video_path.exists():
        return "missing_output"
    metrics = _collect_structural_media_metrics(video_path, include_packet_metrics=include_packet_metrics)
    return _structural_media_issue_from_metrics(metrics)


def _detect_absolute_timeline_ts_bridge_policy(parts: list[Path] | tuple[Path, ...]) -> DownloadBatchAvPolicy | None:
    valid: list[dict[str, float]] = []
    for part in parts:
        try:
            if not part.exists() or part.stat().st_size <= 0:
                continue
            metrics = _collect_structural_media_metrics(part, include_packet_metrics=False)
            video_start = float(metrics.get("video_start") or 0.0)
            audio_start = float(metrics.get("audio_start") or 0.0)
            video_duration = float(metrics.get("video_duration") or 0.0)
            audio_duration = float(metrics.get("audio_duration") or 0.0)
            if video_duration <= 0.0 or audio_duration <= 0.0:
                continue
            if not _part_relative_alignment_healthy(metrics):
                continue
            max_start = max(video_start, audio_start)
            if max_start <= ABSOLUTE_TIMELINE_START_REBUILD_THRESHOLD_SECONDS:
                continue
            valid.append({
                "video_start": video_start,
                "audio_start": audio_start,
                "video_end": float(metrics.get("video_end") or 0.0),
                "audio_end": float(metrics.get("audio_end") or 0.0),
                "start_gap": audio_start - video_start,
                "duration_delta_abs": abs(audio_duration - video_duration),
            })
        except Exception:
            logging.getLogger("edge.runner").debug("absolute timeline batch policy probe failed: %s", str(part), exc_info=True)
    if len(valid) < 2 or len(valid) != len(parts):
        return None
    boundary_gaps: list[float] = []
    for left, right in zip(valid, valid[1:]):
        boundary_gaps.append(abs(float(right["video_start"]) - float(left["video_end"])))
    max_boundary_gap = max(boundary_gaps, default=0.0)
    gaps = sorted(float(item["start_gap"]) for item in valid)
    mid = len(gaps) // 2
    median_gap = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
    gap_range = max(gaps) - min(gaps)
    start_gap_small = max(abs(float(item["start_gap"])) for item in valid) <= DOWNLOAD_PART_REBUILD_START_GAP_TOLERANCE_SECONDS
    duration_close = all(float(item["duration_delta_abs"]) <= SYSTRANS_MERGE_TOLERABLE_DURATION_DELTA_SECONDS for item in valid)
    if max_boundary_gap <= 2.0 and start_gap_small and duration_close:
        return DownloadBatchAvPolicy(
            name=ABSOLUTE_TIMELINE_TS_BRIDGE_POLICY,
            start_gap_median=float(median_gap),
            start_gap_range=float(gap_range),
            part_count=len(valid),
            reason=(
                "absolute_timeline_continuous_relative_healthy:"
                f"max_boundary_gap={max_boundary_gap:.3f}:"
                f"duration_close={duration_close}:"
                f"start_gap_small={start_gap_small}"
            ),
        )
    return None


def _normalize_video_codec_name_for_policy(codec_name: str) -> str:
    normalized = str(codec_name or "").strip().lower()
    if normalized in {"h265", "hevc"}:
        return "hevc"
    if normalized in {"x264", "h264"}:
        return "h264"
    return normalized


def _probe_video_codec_name_for_policy(video_path: Path) -> str:
    try:
        timings = probe_stream_timings(str(video_path))
        video_timing = timings.get("video") or {}
        return _normalize_video_codec_name_for_policy(str(video_timing.get("codec_name") or ""))
    except Exception:
        return ""


def _detect_batch_video_codec_policy(parts: list[Path] | tuple[Path, ...]) -> tuple[str, bool, str]:
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for idx, part in enumerate(parts, start=1):
        try:
            if not part.exists() or part.stat().st_size <= 0:
                continue
            codec = _probe_video_codec_name_for_policy(part)
        except Exception:
            codec = ""
        if not codec:
            continue
        counts[codec] = counts.get(codec, 0) + 1
        first_seen.setdefault(codec, int(idx))
    if not counts:
        return "", False, "video_codec_unknown"
    target = sorted(counts.keys(), key=lambda item: (-counts[item], first_seen.get(item, 999999), item))[0]
    mix = len(counts) > 1
    counts_text = ",".join(f"{codec}={count}" for codec, count in sorted(counts.items()))
    return target, mix, f"target={target}:counts={counts_text}"


def _detect_download_batch_av_policy(parts: list[Path] | tuple[Path, ...]) -> DownloadBatchAvPolicy:
    target_video_codec, video_codec_mix, video_codec_reason = _detect_batch_video_codec_policy(parts)
    absolute_timeline_policy = _detect_absolute_timeline_ts_bridge_policy(parts)
    if absolute_timeline_policy is not None:
        return DownloadBatchAvPolicy(
            name=absolute_timeline_policy.name,
            start_gap_median=absolute_timeline_policy.start_gap_median,
            start_gap_range=absolute_timeline_policy.start_gap_range,
            part_count=absolute_timeline_policy.part_count,
            reason=absolute_timeline_policy.reason,
            target_video_codec=target_video_codec,
            video_codec_mix=video_codec_mix,
            video_codec_reason=video_codec_reason,
        )
    valid: list[dict[str, float]] = []
    for part in parts:
        try:
            if not part.exists() or part.stat().st_size <= 0:
                continue
            precise = _probe_precise_av_sync(part)
            video_start = float(precise.get("video_start") or 0.0)
            audio_start = float(precise.get("audio_start") or 0.0)
            video_duration = float(precise.get("video_duration") or 0.0)
            audio_duration = float(precise.get("audio_duration") or 0.0)
            audio_content_offset = float(precise.get("audio_content_offset") or 0.0)
            if video_duration <= 0.0 or audio_duration <= 0.0:
                continue
            valid.append({
                "start_gap": audio_start - video_start,
                "video_duration": video_duration,
                "audio_duration": audio_duration,
                "duration_delta_abs": abs(audio_duration - video_duration),
                "audio_content_offset": audio_content_offset,
            })
        except Exception:
            logging.getLogger("edge.runner").debug("download batch av policy probe failed: %s", str(part), exc_info=True)
    if len(valid) < 2:
        return DownloadBatchAvPolicy(
            name="default",
            part_count=len(valid),
            reason="insufficient_parts",
            target_video_codec=target_video_codec,
            video_codec_mix=video_codec_mix,
            video_codec_reason=video_codec_reason,
        )
    gaps = sorted(float(item["start_gap"]) for item in valid)
    mid = len(gaps) // 2
    median_gap = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
    gap_range = max(gaps) - min(gaps)
    large_stable_gap = all(abs(float(item["start_gap"])) >= 5.0 for item in valid) and gap_range <= 2.0
    duration_close = all(
        float(item["duration_delta_abs"]) <= max(1.5, float(item["video_duration"]) * 0.005)
        for item in valid
    )
    content_checks: list[tuple[bool, float]] = []
    for item in valid:
        start_gap_abs = abs(float(item["start_gap"]))
        content_limit = min(20.0, start_gap_abs * 0.5)
        content_overage = float(item["audio_content_offset"]) - content_limit
        content_checks.append((content_overage <= 0.0, content_overage))
    content_pass_count = sum(1 for ok, _overage in content_checks if ok)
    content_fail_count = len(content_checks) - content_pass_count
    max_content_overage = max((float(overage) for ok, overage in content_checks if not ok), default=0.0)
    content_not_matching_stream_gap = content_fail_count == 0
    content_not_matching_stream_gap_relaxed = (
        len(valid) >= 3
        and content_pass_count >= max(2, len(valid) - 1)
        and max_content_overage <= max(2.0, abs(float(median_gap)) * 0.08)
    )
    if large_stable_gap and duration_close and (content_not_matching_stream_gap or content_not_matching_stream_gap_relaxed):
        reason = "large_stable_stream_start_gap_with_matched_durations"
        if content_not_matching_stream_gap_relaxed and not content_not_matching_stream_gap:
            reason = (
                "large_stable_stream_start_gap_with_matched_durations:"
                f"content_relaxed_pass={content_pass_count}/{len(valid)}:"
                f"max_overage={max_content_overage:.3f}"
            )
        return DownloadBatchAvPolicy(
            name="nvr_pts_skew_same_origin",
            start_gap_median=float(median_gap),
            start_gap_range=float(gap_range),
            part_count=len(valid),
            reason=reason,
            target_video_codec=target_video_codec,
            video_codec_mix=video_codec_mix,
            video_codec_reason=video_codec_reason,
        )
    return DownloadBatchAvPolicy(
        name="default",
        start_gap_median=float(median_gap),
        start_gap_range=float(gap_range),
        part_count=len(valid),
        reason=f"large_stable_gap={large_stable_gap}:duration_close={duration_close}:content_not_matching_stream_gap={content_not_matching_stream_gap}:content_pass={content_pass_count}/{len(valid)}:max_content_overage={max_content_overage:.3f}",
        target_video_codec=target_video_codec,
        video_codec_mix=video_codec_mix,
        video_codec_reason=video_codec_reason,
    )


def _collect_structural_media_metrics(video_path: Path, *, include_packet_metrics: bool = True) -> dict[str, float]:
    timings = probe_stream_timings(str(video_path))
    video_timing = timings.get("video") or {}
    audio_timing = timings.get("audio") or {}
    video_start = max(0.0, float(video_timing.get("start_time") or 0.0))
    audio_start = max(0.0, float(audio_timing.get("start_time") or 0.0))
    video_duration = max(0.0, float(video_timing.get("duration") or 0.0))
    audio_duration = max(0.0, float(audio_timing.get("duration") or 0.0))
    video_end = video_start + video_duration
    audio_end = audio_start + audio_duration
    positive_ends = [float(v) for v in (video_end, audio_end) if float(v) > 0.0]
    min_end = min(positive_ends) if positive_ends else 0.0
    max_end = max(positive_ends) if positive_ends else 0.0
    format_duration = float(probe_duration_seconds(str(video_path)) or 0.0)
    authoritative_duration = float(probe_authoritative_duration_seconds(str(video_path)) or 0.0)
    max_video_packet = 0.0
    max_audio_packet = 0.0
    if include_packet_metrics:
        max_video_packet = float(probe_max_packet_duration(str(video_path), stream_selector="v:0") or 0.0)
        max_audio_packet = float(probe_max_packet_duration(str(video_path), stream_selector="a:0") or 0.0)
    return {
        "video_start": video_start,
        "audio_start": audio_start,
        "video_duration": video_duration,
        "audio_duration": audio_duration,
        "video_end": video_end,
        "audio_end": audio_end,
        "min_end": min_end,
        "max_end": max_end,
        "format_duration": format_duration,
        "authoritative_duration": authoritative_duration,
        "max_video_packet": max_video_packet,
        "max_audio_packet": max_audio_packet,
    }


def _structural_media_issue_from_metrics(metrics: dict[str, float]) -> str:
    video_end = float(metrics.get("video_end") or 0.0)
    audio_end = float(metrics.get("audio_end") or 0.0)
    min_end = float(metrics.get("min_end") or 0.0)
    max_end = float(metrics.get("max_end") or 0.0)
    format_duration = float(metrics.get("format_duration") or 0.0)
    authoritative_duration = float(metrics.get("authoritative_duration") or 0.0)
    max_video_packet = float(metrics.get("max_video_packet") or 0.0)
    max_audio_packet = float(metrics.get("max_audio_packet") or 0.0)
    if max_video_packet > STRUCTURAL_PACKET_DURATION_ABNORMAL_SECONDS and max_end > 0.0 and max_video_packet > max_end * 0.1:
        return f"video_packet_duration_abnormal:max={max_video_packet:.3f}:video_end={video_end:.3f}:audio_end={audio_end:.3f}"
    if max_audio_packet > STRUCTURAL_AUDIO_PACKET_DURATION_ABNORMAL_SECONDS and audio_end > 0.0:
        return f"audio_packet_duration_abnormal:max={max_audio_packet:.3f}:video_end={video_end:.3f}:audio_end={audio_end:.3f}"
    if min_end > 0.0 and (max_end - min_end) > STRUCTURAL_STREAM_END_GAP_ABNORMAL_SECONDS and max_end > min_end * 1.2:
        return f"stream_end_gap_abnormal:video_end={video_end:.3f}:audio_end={audio_end:.3f}"
    if authoritative_duration > 0.0 and format_duration > authoritative_duration + STRUCTURAL_STREAM_END_GAP_ABNORMAL_SECONDS and format_duration > authoritative_duration * 1.2:
        return f"duration_mismatch_abnormal:format={format_duration:.3f}:authoritative={authoritative_duration:.3f}"
    return ""


def _is_expected_tail_silence_source(
    source_metrics: dict[str, float],
    precise_state: dict[str, Any] | None,
) -> bool:
    """Detect raw parts whose later audio is genuinely missing, not repair-lost."""
    if not source_metrics:
        return False
    precise = precise_state or {}
    video_start = float(source_metrics.get("video_start") or 0.0)
    audio_start = float(source_metrics.get("audio_start") or 0.0)
    video_duration = float(source_metrics.get("video_duration") or 0.0)
    audio_duration = float(source_metrics.get("audio_duration") or 0.0)
    format_duration = float(source_metrics.get("format_duration") or 0.0)
    authoritative_duration = float(source_metrics.get("authoritative_duration") or 0.0)
    if video_start <= ABSOLUTE_TIMELINE_START_REBUILD_THRESHOLD_SECONDS or audio_start <= ABSOLUTE_TIMELINE_START_REBUILD_THRESHOLD_SECONDS:
        return False
    if abs(audio_start - video_start) > 2.0:
        return False
    if video_duration <= DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_MIN_AUDIO_SECONDS:
        return False
    if audio_duration > 0.05:
        return False
    if format_duration <= DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_MIN_AUDIO_SECONDS:
        return False
    if authoritative_duration > 0.0 and abs(authoritative_duration - audio_start) > 2.0:
        return False
    return True


def _is_expected_tail_silence_output(video_path: Path, *, include_packet_metrics: bool = True) -> bool:
    if not video_path.exists():
        return False
    metrics = _collect_structural_media_metrics(video_path, include_packet_metrics=include_packet_metrics)
    video_start = float(metrics.get("video_start") or 0.0)
    audio_start = float(metrics.get("audio_start") or 0.0)
    video_end = float(metrics.get("video_end") or 0.0)
    audio_end = float(metrics.get("audio_end") or 0.0)
    max_video_packet = float(metrics.get("max_video_packet") or 0.0)
    max_audio_packet = float(metrics.get("max_audio_packet") or 0.0)
    if video_end <= 0.0 or audio_end <= 0.0:
        return False
    if video_start > DOWNLOAD_PART_REBUILD_START_GAP_TOLERANCE_SECONDS or audio_start > DOWNLOAD_PART_REBUILD_START_GAP_TOLERANCE_SECONDS:
        return False
    if abs(audio_start - video_start) > DOWNLOAD_PART_REBUILD_START_GAP_TOLERANCE_SECONDS:
        return False
    if (video_end - audio_end) < DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_MIN_SECONDS:
        return False
    if audio_end < DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_MIN_AUDIO_SECONDS:
        return False
    if max_audio_packet > STRUCTURAL_AUDIO_PACKET_DURATION_ABNORMAL_SECONDS and audio_end > 0.0:
        return False
    if max_video_packet > STRUCTURAL_PACKET_DURATION_ABNORMAL_SECONDS and video_end > 0.0 and max_video_packet > video_end * 0.1:
        return False
    return True


def _structural_media_warning_from_metrics(metrics: dict[str, float]) -> str:
    max_audio_packet = float(metrics.get("max_audio_packet") or 0.0)
    audio_end = float(metrics.get("audio_end") or 0.0)
    if max_audio_packet > STRUCTURAL_AUDIO_PACKET_DURATION_WARNING_SECONDS and audio_end > 0.0:
        return f"audio_packet_duration_warning:max={max_audio_packet:.3f}:threshold={STRUCTURAL_AUDIO_PACKET_DURATION_WARNING_SECONDS:.3f}"
    return ""


def _format_structural_media_metrics(metrics: dict[str, float]) -> str:
    return (
        f"video_start={float(metrics.get('video_start') or 0.0):.3f} "
        f"audio_start={float(metrics.get('audio_start') or 0.0):.3f} "
        f"video_end={float(metrics.get('video_end') or 0.0):.3f} "
        f"audio_end={float(metrics.get('audio_end') or 0.0):.3f} "
        f"format={float(metrics.get('format_duration') or 0.0):.3f} "
        f"authoritative={float(metrics.get('authoritative_duration') or 0.0):.3f} "
        f"max_v_pkt={float(metrics.get('max_video_packet') or 0.0):.3f} "
        f"max_a_pkt={float(metrics.get('max_audio_packet') or 0.0):.3f}"
    )


def _probe_merge_boundary_decode_issue(video_path: Path, boundary_points_sec: list[float]) -> str:
    if not video_path.exists():
        return "missing_output"
    checkpoints: list[float] = []
    for raw in boundary_points_sec:
        try:
            sec = float(raw or 0.0)
        except Exception:
            continue
        if sec > 1.0:
            checkpoints.append(sec)
    if not checkpoints:
        return ""
    total_duration = float(probe_authoritative_duration_seconds(str(video_path)) or 0.0)
    seen: set[int] = set()
    for sec in checkpoints:
        rounded_key = int(round(sec * 1000.0))
        if rounded_key in seen:
            continue
        seen.add(rounded_key)
        start_sec = max(0.0, sec - 1.0)
        if total_duration > 0.0 and start_sec >= total_duration:
            continue
        cmd = [
            _ffmpeg_bin(),
            "-v",
            "warning",
            "-ss",
            f"{start_sec:.3f}",
            "-t",
            f"{MERGE_BOUNDARY_DECODE_CHECK_WINDOW_SECONDS:.3f}",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ]
        try:
            r = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=8,
            )
            output = str(r.stdout or "")
        except Exception as e:
            return f"decode_probe_failed:boundary={sec:.3f}:err={e}"
        lowered = output.lower()
        if (
            "invalid data found when processing input" in lowered
            or "error submitting packet to decoder" in lowered
            or "missing picture in access unit" in lowered
            or "no frame!" in lowered
            or "decoding error" in lowered
        ):
            return f"boundary_decode_abnormal:boundary={sec:.3f}"
    return ""


def _detect_post_merge_freeze(video_path: Path, boundary_points_sec: list[float]) -> str:

    log = logging.getLogger("edge.runner")

    if not video_path.exists():

        return "missing_output"

    if not boundary_points_sec:

        return ""

    

    last_boundary = boundary_points_sec[-1]

    start_sec = max(0.0, last_boundary - 2.0)

    cmd = [

        _ffmpeg_bin(),

        "-v", "info",

        "-ss", f"{start_sec:.3f}",

        "-t", "15.0",

        "-i", str(video_path),

        "-vf", "freezedetect=noise=-55dB:duration=8",

        "-map", "0:v:0",

        "-f", "null",

        "-",

    ]

    try:

        r = subprocess.run(

            cmd,

            stdout=subprocess.PIPE,

            stderr=subprocess.STDOUT,

            text=True,

            encoding="utf-8",

            errors="ignore",

        )

        output = str(r.stdout or "")

        lowered = output.lower()

        if "lavfi.freezedetect.freeze_start" in lowered or "freeze_start" in lowered:

            log.warning("Post-merge freeze detected near boundary %.3f in %s", last_boundary, video_path.name)

            return f"post_merge_freeze_detected:boundary={last_boundary:.3f}"

    except Exception as e:

        log.warning("Failed to run freeze detection: %s", e)

    return ""


def _classify_merge_boundary_cfr_repair(video_path: Path, issue: str) -> str:
    normalized_issue = str(issue or "")
    if not video_path.exists():
        return ""
    if not (
        normalized_issue.startswith("merge_boundary_video_gap:")
        or normalized_issue.startswith("audio_packet_duration_abnormal:")
        or normalized_issue.startswith("video_packet_duration_abnormal:")
    ):
        return ""
    try:
        timings = probe_stream_timings(str(video_path))
        video_timing = timings.get("video") or {}
        audio_timing = timings.get("audio") or {}
        video_start = float(video_timing.get("start_time") or 0.0)
        video_duration = float(video_timing.get("duration") or 0.0)
        audio_start = float(audio_timing.get("start_time") or 0.0)
        audio_duration = float(audio_timing.get("duration") or 0.0)
        start_gap = audio_start - video_start
        end_gap = (audio_start + audio_duration) - (video_start + video_duration)
        max_v = float(probe_max_packet_duration(str(video_path), stream_selector="v:0") or 0.0)
        max_a = float(probe_max_packet_duration(str(video_path), stream_selector="a:0") or 0.0)
        video_anomaly = probe_packet_timeline_anomaly(str(video_path), stream_selector="v:0")
        audio_anomaly = probe_packet_timeline_anomaly(str(video_path), stream_selector="a:0")
        v_dts_back = int(video_anomaly.get("dts_backward_count") or 0)
        a_dts_back = int(audio_anomaly.get("dts_backward_count") or 0)
        if v_dts_back != 0 or a_dts_back != 0:
            return ""
        v_pts_back = int(video_anomaly.get("pts_backward_count") or 0)
        v_max_pts_back = float(video_anomaly.get("max_pts_backward_sec") or 0.0)
        if max_v <= MERGE_BOUNDARY_VIDEO_GAP_PACKET_ABNORMAL_SECONDS and max_a <= STRUCTURAL_AUDIO_PACKET_DURATION_ABNORMAL_SECONDS:
            return ""
        return (
            "merge_boundary_video_gap:"
            f"source_issue={normalized_issue}:"
            f"max_v={max_v:.3f}:"
            f"max_a={max_a:.3f}:"
            f"start_gap={start_gap:.3f}:"
            f"end_gap={end_gap:.3f}:"
            f"v_pts_back={v_pts_back}:"
            f"v_dts_back={v_dts_back}:"
            f"a_dts_back={a_dts_back}:"
            f"v_max_pts_back={v_max_pts_back:.3f}"
        )
    except Exception:
        return ""


def _normalize_source_timeline(downloaded: Path, on_status: Callable[[str], None] | None = None, status_label: str = "视频下载完成，正在校准时间轴", force: bool = False) -> bool:
    if not downloaded.exists() or not ffmpeg_exists():
        return False
    log = logging.getLogger("edge.runner")
    timeline_state = _probe_timeline_state(downloaded)
    if not force and not bool(timeline_state.get("dirty")):
        return False
    start_sec = float(probe_media_start_seconds(str(downloaded)) or 0.0)
    tmp_out = downloaded.with_name(downloaded.stem + ".timeline.mp4")
    try:
        tmp_out.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        _run_with_status_heartbeat(
            status_label,
            lambda: rebuild_zero_based_timeline(str(downloaded), str(tmp_out)),
            on_status,
        )
        _replace_file_or_raise_finalize_pending(
            tmp_out,
            downloaded,
            log=log,
            action=f"source timeline normalize {downloaded.name}",
            user_message="源视频时间轴已校准，正在等待最终提交",
        )
        log.info("%s 源视频时间轴已归零: %s start_time=%.3fs", _branch_code_tag(CAM_DL_SRC_010), str(downloaded), start_sec)
        return True
    except Exception as e:
        if isinstance(e, FinalizePendingError):
            raise
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        log.warning("%s 源视频时间轴校准失败: %s", _branch_code_tag(CAM_DL_SRC_011), e)
        return False


def _has_abnormal_absolute_timeline_start(*starts: float) -> bool:
    relevant: list[float] = []
    for raw in starts:
        try:
            val = float(raw or 0.0)
        except Exception:
            continue
        if val >= 0.0:
            relevant.append(val)
    if not relevant:
        return False
    return max(relevant) > ABSOLUTE_TIMELINE_START_REBUILD_THRESHOLD_SECONDS


def _rebuild_download_part_timeline(
    part_path: Path,
    *,
    precise_trim: float,
    target_duration_sec: float = 0.0,
    force_full_rebuild: bool = False,
    target_video_codec: str = "",
    output_path: Path | None = None,
    branch_code: str = CAM_DL_NORM_030,
    on_status: Callable[[str], None] | None = None,
    total_elapsed_supplier: Callable[[], float] | None = None,
    cancel_check: Callable[[], str | None] | None = None,
    user_status: str = "正在完整重建当前分段",
    log_context: str = "分段时间线异常",
    allow_audio_tail_trim: bool = False,
    audio_tail_trim_max_seconds: float = DOWNLOAD_PART_AUDIO_TAIL_TRIM_MAX_SECONDS,
    allow_expected_tail_silence: bool = False,
    allow_missing_audio_timing: bool = False,
) -> bool:
    log = logging.getLogger("edge.runner")
    final_out = output_path or part_path
    tmp_out = final_out.with_name(final_out.stem + ".timeline.tmp.mp4")
    try:
        tmp_out.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        _run_with_status_heartbeat(
            "正在完整重建当前分段",
            lambda: rebuild_zero_based_timeline(
                str(part_path),
                str(tmp_out),
                precise_audio_trim_sec=float(precise_trim or 0.0),
                target_duration_sec=float(target_duration_sec or 0.0),
                force_full_rebuild=bool(force_full_rebuild),
                target_video_codec=str(target_video_codec or ""),
                cancel_check=cancel_check,
            ),
            on_status,
            total_elapsed_supplier,
            cancel_check=cancel_check,
        )
        validation_issue = _validate_download_part_rebuild_output(
            tmp_out,
            allow_missing_audio_timing=allow_missing_audio_timing,
        )
        if (
            validation_issue.startswith("stream_end_gap_after_rebuild")
            and allow_audio_tail_trim
            and _trim_audio_tail_to_video(tmp_out, max_trim_seconds=audio_tail_trim_max_seconds)
        ):
            log.info(
                "%s 分段 %s rebuild output hit %s, applied audio tail trim and retrying validation",
                _branch_code_tag(branch_code),
                part_path.name,
                validation_issue,
            )
            validation_issue = _validate_download_part_rebuild_output(
                tmp_out,
                allow_missing_audio_timing=allow_missing_audio_timing,
            )
        if (
            validation_issue.startswith("stream_end_gap_after_rebuild")
            and allow_expected_tail_silence
            and _pad_audio_tail_with_silence_to_video(tmp_out)
        ):
            log.info(
                "%s 分段 %s rebuild output hit %s, appended silent audio tail and retrying validation",
                _branch_code_tag(branch_code),
                part_path.name,
                validation_issue,
            )
            validation_issue = _validate_download_part_rebuild_output(
                tmp_out,
                allow_missing_audio_timing=allow_missing_audio_timing,
            )
        if (
            validation_issue.startswith("stream_end_gap_after_rebuild")
            and allow_expected_tail_silence
            and _is_expected_tail_silence_output(tmp_out)
        ):
            log.info(
                "%s 分段 %s rebuild output hit %s, accepted as expected tail silence from source",
                _branch_code_tag(branch_code),
                part_path.name,
                validation_issue,
            )
            validation_issue = ""
        if validation_issue:
            raise RuntimeError(f"download_part_rebuild_validation_failed:{validation_issue}")
        _replace_file_or_raise_finalize_pending(
            tmp_out,
            final_out,
            log=log,
            action=f"download part timeline normalize {part_path.name} -> {final_out.name}",
            user_message="分段重建完成，正在等待最终提交",
        )
        log.info("%s 分段 %s 时间线重建完成: %s precise_trim=%.3fs target_video_codec=%s out=%s", _branch_code_tag(branch_code), part_path.name, log_context, float(precise_trim or 0.0), str(target_video_codec or "default"), str(final_out))
        return True
    except Exception as e:
        if isinstance(e, FinalizePendingError):
            raise
        if str(e) in {"cancelled:pause", "cancelled:stop"}:
            raise
        log.warning("%s 分段 %s 时间轴归零失败，保留原始文件: %s context=%s precise_trim=%.3f out=%s", _branch_code_tag(branch_code), part_path.name, e, log_context, float(precise_trim or 0.0), str(final_out))
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _validate_download_part_rebuild_output(
    video_path: Path,
    *,
    include_packet_metrics: bool = True,
    allow_missing_audio_timing: bool = False,
    extra_start_tolerance: float = 0.0,
) -> str:
    if not video_path.exists():
        return "missing_output"
    metrics = _collect_structural_media_metrics(video_path, include_packet_metrics=include_packet_metrics)
    video_start = float(metrics.get("video_start") or 0.0)
    audio_start = float(metrics.get("audio_start") or 0.0)
    video_end = float(metrics.get("video_end") or 0.0)
    audio_end = float(metrics.get("audio_end") or 0.0)
    max_video_packet = float(metrics.get("max_video_packet") or 0.0)
    max_audio_packet = float(metrics.get("max_audio_packet") or 0.0)
    start_tolerance = DOWNLOAD_PART_REBUILD_START_GAP_TOLERANCE_SECONDS + max(0.0, float(extra_start_tolerance or 0.0))
    if video_end <= 0.0:
        return "missing_video_timing"
    if audio_end <= 0.0:
        if allow_missing_audio_timing:
            audio_info = probe_audio_stream_info(str(video_path))
            if not str(audio_info.get("codec_name") or "").strip().lower():
                return ""
        return "missing_audio_timing"
    start_gap = abs(audio_start - video_start)
    end_gap = abs(audio_end - video_end)
    if video_start > start_tolerance or audio_start > start_tolerance:
        return f"stream_start_not_zero:video_start={video_start:.3f}:audio_start={audio_start:.3f}"
    if start_gap > start_tolerance:
        return f"stream_start_gap_abnormal:gap={start_gap:.3f}:video_start={video_start:.3f}:audio_start={audio_start:.3f}"
    if end_gap > DOWNLOAD_PART_REBUILD_END_GAP_TOLERANCE_SECONDS:
        return f"stream_end_gap_after_rebuild:gap={end_gap:.3f}:video_end={video_end:.3f}:audio_end={audio_end:.3f}"
    if max_audio_packet > STRUCTURAL_AUDIO_PACKET_DURATION_ABNORMAL_SECONDS and audio_end > 0.0:
        return f"audio_packet_duration_abnormal:max={max_audio_packet:.3f}:video_end={video_end:.3f}:audio_end={audio_end:.3f}"
    if max_video_packet > STRUCTURAL_PACKET_DURATION_ABNORMAL_SECONDS and video_end > 0.0 and max_video_packet > video_end * 0.1:
        return f"video_packet_duration_abnormal:max={max_video_packet:.3f}:video_end={video_end:.3f}:audio_end={audio_end:.3f}"
    return ""


def _download_part_merge_ready_issue(video_path: Path, *, include_packet_metrics: bool = True) -> str:
    return _download_part_merge_ready_issue_with_options(
        video_path,
        include_packet_metrics=include_packet_metrics,
        defer_precise_probe_until_suspicious=False,
    )


def _download_part_merge_ready_issue_for_batch_policy(
    video_path: Path,
    batch_policy_name: str,
    *,
    include_packet_metrics: bool = True,
) -> str:
    policy_name = str(batch_policy_name or "").strip()
    if policy_name == DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_CLASSIFICATION and _is_expected_tail_silence_output(video_path, include_packet_metrics=include_packet_metrics):
        return ""
    if policy_name == DOWNLOAD_PART_SOURCE_NO_AUDIO_CLASSIFICATION and _source_part_has_no_audio_stream(video_path):
        structural_issue = _probe_structural_media_issue(video_path, include_packet_metrics=include_packet_metrics)
        return "" if not structural_issue else structural_issue
    if policy_name != ABSOLUTE_TIMELINE_TS_BRIDGE_POLICY:
        return _download_part_merge_ready_issue(
            video_path,
            include_packet_metrics=include_packet_metrics,
        )
    structural_issue = _probe_structural_media_issue(video_path, include_packet_metrics=include_packet_metrics)
    if structural_issue:
        return structural_issue
    lightweight_metrics = _collect_structural_media_metrics(video_path, include_packet_metrics=False)
    if _part_relative_alignment_healthy(lightweight_metrics):
        return ""
    return _download_part_merge_ready_issue_with_options(
        video_path,
        include_packet_metrics=include_packet_metrics,
        defer_precise_probe_until_suspicious=False,
    )


def _download_part_merge_ready_issue_with_options(
    video_path: Path,
    *,
    include_packet_metrics: bool = True,
    defer_precise_probe_until_suspicious: bool = False,
    deferred_start_gap_tolerance_seconds: float = AUDIO_LATE_REBUILD_THRESHOLD_SECONDS,
) -> str:
    structural_issue = _probe_structural_media_issue(video_path, include_packet_metrics=include_packet_metrics)
    if structural_issue:
        return structural_issue
    rebuild_issue = _validate_download_part_rebuild_output(video_path, include_packet_metrics=include_packet_metrics)
    if rebuild_issue:
        return rebuild_issue
    lightweight_metrics = _collect_structural_media_metrics(video_path, include_packet_metrics=False)
    if defer_precise_probe_until_suspicious:
        start_gap = abs(float(lightweight_metrics.get("audio_start") or 0.0) - float(lightweight_metrics.get("video_start") or 0.0))
        duration_delta = abs(float(lightweight_metrics.get("audio_duration") or 0.0) - float(lightweight_metrics.get("video_duration") or 0.0))
        audio_end = float(lightweight_metrics.get("audio_end") or 0.0)
        video_end = float(lightweight_metrics.get("video_end") or 0.0)
        end_gap = abs(audio_end - video_end) if audio_end > 0.0 and video_end > 0.0 else 0.0
        if (
            start_gap <= max(AUDIO_LATE_REBUILD_THRESHOLD_SECONDS, float(deferred_start_gap_tolerance_seconds or 0.0))
            and duration_delta <= AUDIO_LATE_REBUILD_THRESHOLD_SECONDS
            and end_gap <= DOWNLOAD_PART_REBUILD_END_GAP_TOLERANCE_SECONDS
        ):
            return ""
    precise_state = _probe_precise_av_sync(video_path, include_packet_timeline=include_packet_metrics)
    classification = str(precise_state.get("classification") or "").strip()
    precise_trim = float(precise_state.get("audio_late_trim") or 0.0)
    if classification and classification != "aligned" and precise_trim > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS:
        audio_content_offset = float(precise_state.get("audio_content_offset") or 0.0)
        if (
            defer_precise_probe_until_suspicious
            and classification in {"timestamp_only_audio_late", "audio_late_uncertain"}
            and audio_content_offset <= AUDIO_LATE_REBUILD_THRESHOLD_SECONDS
            and precise_trim <= max(0.35, AUDIO_LATE_REBUILD_THRESHOLD_SECONDS)
        ):
            return ""
        if classification == "duration_delta_audio_late" and precise_trim <= SYSTRANS_MERGE_TOLERABLE_DURATION_DELTA_SECONDS:
            return ""
        return f"av_sync_not_aligned:classification={classification}:trim={precise_trim:.3f}"
    return ""


def _av_sync_badness(metrics: dict[str, float], precise_state: dict[str, float | bool | str]) -> float:
    video_start = float(metrics.get("video_start") or 0.0)
    audio_start = float(metrics.get("audio_start") or 0.0)
    video_end = float(metrics.get("video_end") or 0.0)
    audio_end = float(metrics.get("audio_end") or 0.0)
    max_video_packet = float(metrics.get("max_video_packet") or 0.0)
    max_audio_packet = float(metrics.get("max_audio_packet") or 0.0)
    precise_trim = float(precise_state.get("audio_late_trim") or 0.0)
    return (
        abs(audio_start - video_start) * 4.0
        + abs(audio_end - video_end) * 2.0
        + max(0.0, video_start)
        + max(0.0, audio_start)
        + max(0.0, precise_trim) * 4.0
        + max(0.0, max_audio_packet - STRUCTURAL_AUDIO_PACKET_DURATION_WARNING_SECONDS) * 10.0
        + max(0.0, max_video_packet - 1.0)
    )


def _systrans_candidate_is_better_than_raw(raw_path: Path, systrans_path: Path) -> tuple[bool, str]:
    if not systrans_path.exists() or systrans_path.stat().st_size <= 0:
        return False, "systrans_missing_or_empty"
    raw_metrics = _collect_structural_media_metrics(raw_path, include_packet_metrics=False)
    systrans_metrics = _collect_structural_media_metrics(systrans_path, include_packet_metrics=False)
    raw_precise = _probe_precise_av_sync(raw_path, include_packet_timeline=False)
    systrans_precise = _probe_precise_av_sync(systrans_path, include_packet_timeline=False)
    raw_video_duration = float(raw_metrics.get("video_duration") or 0.0)
    raw_audio_duration = float(raw_metrics.get("audio_duration") or 0.0)
    raw_content_duration = max(raw_video_duration, raw_audio_duration)
    systrans_video_duration = float(systrans_metrics.get("video_duration") or 0.0)
    systrans_audio_duration = float(systrans_metrics.get("audio_duration") or 0.0)
    systrans_content_duration = max(systrans_video_duration, systrans_audio_duration)
    if raw_video_duration > 0.0 and systrans_video_duration <= 0.0:
        return False, "systrans_video_lost"
    if raw_audio_duration > 0.0 and systrans_audio_duration <= 0.0:
        return False, "systrans_audio_lost"
    if raw_content_duration > 0.0 and systrans_content_duration < max(3.0, raw_content_duration * 0.97):
        return False, f"systrans_duration_shrunk:raw={raw_content_duration:.3f}:systrans={systrans_content_duration:.3f}"
    raw_badness = _av_sync_badness(raw_metrics, raw_precise)
    systrans_badness = _av_sync_badness(systrans_metrics, systrans_precise)
    if systrans_badness + 1.0 < raw_badness * 0.5:
        return True, f"systrans_better:raw_badness={raw_badness:.3f}:systrans_badness={systrans_badness:.3f}"
    return False, f"systrans_not_significantly_better:raw_badness={raw_badness:.3f}:systrans_badness={systrans_badness:.3f}"


def _relative_alignment_badness(metrics: dict[str, float]) -> float:
    """对齐健康度评分（越小越好），仅基于相对指标，不含绝对起点。"""
    if not metrics:
        return float("inf")
    video_start = float(metrics.get("video_start") or 0.0)
    audio_start = float(metrics.get("audio_start") or 0.0)
    video_end = float(metrics.get("video_end") or 0.0)
    audio_end = float(metrics.get("audio_end") or 0.0)
    video_duration = float(metrics.get("video_duration") or 0.0)
    audio_duration = float(metrics.get("audio_duration") or 0.0)
    max_video_packet = float(metrics.get("max_video_packet") or 0.0)
    max_audio_packet = float(metrics.get("max_audio_packet") or 0.0)
    if video_duration <= 0.0 or audio_duration <= 0.0:
        return float("inf")
    return (
        abs(audio_start - video_start) * 4.0
        + abs(audio_end - video_end) * 2.0
        + abs(audio_duration - video_duration) * 4.0
        + max(0.0, max_audio_packet - STRUCTURAL_AUDIO_PACKET_DURATION_WARNING_SECONDS) * 10.0
        + max(0.0, max_video_packet - 1.0) * 1.0
    )


def _part_relative_alignment_healthy(metrics: dict[str, float]) -> bool:
    """判断分段自身相对对齐是否健康（不考虑绝对起点）：start_gap / end_gap / duration_delta 均在容差内。"""
    if not metrics:
        return False
    video_duration = float(metrics.get("video_duration") or 0.0)
    audio_duration = float(metrics.get("audio_duration") or 0.0)
    if video_duration <= 0.0 or audio_duration <= 0.0:
        return False
    video_start = float(metrics.get("video_start") or 0.0)
    audio_start = float(metrics.get("audio_start") or 0.0)
    video_end = float(metrics.get("video_end") or 0.0)
    audio_end = float(metrics.get("audio_end") or 0.0)
    start_gap = abs(audio_start - video_start)
    end_gap = abs(audio_end - video_end)
    duration_delta = abs(audio_duration - video_duration)
    return (
        start_gap <= DOWNLOAD_PART_REBUILD_START_GAP_TOLERANCE_SECONDS
        and end_gap <= DOWNLOAD_PART_REBUILD_END_GAP_TOLERANCE_SECONDS
        and duration_delta <= SYSTRANS_MERGE_TOLERABLE_DURATION_DELTA_SECONDS
    )


def _choose_better_for_zero_based_remux(
    raw_path: Path, systrans_path: Path
) -> tuple[str, str, dict[str, float], dict[str, float]]:
    """通用 raw vs systrans 对比（仅用于 CAM-DL-NORM-070 分支）。

    返回 (winner ∈ {"raw","systrans"}, reason, raw_metrics, systrans_metrics)。

    决策顺序：
      1) systrans 缺文件 / 流丢失 → raw
      2) raw 自身相对对齐健康，且 systrans 任一关键指标比 raw 显著变差 → raw
      3) 公平评分（不含绝对起点偏置）：systrans 显著更优才选 systrans，否则保守选 raw
    """
    raw_metrics = _collect_structural_media_metrics(raw_path) if raw_path.exists() else {}
    sys_metrics: dict[str, float] = {}
    if systrans_path.exists() and systrans_path.stat().st_size > 0:
        sys_metrics = _collect_structural_media_metrics(systrans_path)
    if not sys_metrics:
        return "raw", "systrans_missing_or_empty", raw_metrics, sys_metrics
    raw_has_video = float(raw_metrics.get("video_duration") or 0.0) > 0.0
    raw_has_audio = float(raw_metrics.get("audio_duration") or 0.0) > 0.0
    sys_has_video = float(sys_metrics.get("video_duration") or 0.0) > 0.0
    sys_has_audio = float(sys_metrics.get("audio_duration") or 0.0) > 0.0
    if raw_has_video and not sys_has_video:
        return "raw", "systrans_video_lost", raw_metrics, sys_metrics
    if raw_has_audio and not sys_has_audio:
        return "raw", "systrans_audio_lost", raw_metrics, sys_metrics
    if _part_relative_alignment_healthy(raw_metrics):
        raw_end_gap = abs(float(raw_metrics["audio_end"]) - float(raw_metrics["video_end"]))
        sys_end_gap = abs(float(sys_metrics["audio_end"]) - float(sys_metrics["video_end"]))
        raw_dur_delta = abs(float(raw_metrics["audio_duration"]) - float(raw_metrics["video_duration"]))
        sys_dur_delta = abs(float(sys_metrics["audio_duration"]) - float(sys_metrics["video_duration"]))
        raw_start_gap = abs(float(raw_metrics["audio_start"]) - float(raw_metrics["video_start"]))
        sys_start_gap = abs(float(sys_metrics["audio_start"]) - float(sys_metrics["video_start"]))
        worsened: list[str] = []
        if sys_end_gap > raw_end_gap + DOWNLOAD_PART_REBUILD_END_GAP_TOLERANCE_SECONDS:
            worsened.append(f"end_gap raw={raw_end_gap:.3f}->sys={sys_end_gap:.3f}")
        if sys_dur_delta > raw_dur_delta + SYSTRANS_MERGE_TOLERABLE_DURATION_DELTA_SECONDS:
            worsened.append(f"dur_delta raw={raw_dur_delta:.3f}->sys={sys_dur_delta:.3f}")
        if sys_start_gap > raw_start_gap + DOWNLOAD_PART_REBUILD_START_GAP_TOLERANCE_SECONDS:
            worsened.append(f"start_gap raw={raw_start_gap:.3f}->sys={sys_start_gap:.3f}")
        if worsened:
            return "raw", "raw_healthy_systrans_worsened:" + ";".join(worsened), raw_metrics, sys_metrics
    raw_b = _relative_alignment_badness(raw_metrics)
    sys_b = _relative_alignment_badness(sys_metrics)
    if sys_b + 0.05 < raw_b:
        return "systrans", f"systrans_better:raw_b={raw_b:.3f}:sys_b={sys_b:.3f}", raw_metrics, sys_metrics
    return "raw", f"raw_preferred:raw_b={raw_b:.3f}:sys_b={sys_b:.3f}", raw_metrics, sys_metrics


def _zero_based_remux_part(src: Path, dst: Path) -> bool:
    """轻量归零：copy 流，重置 PTS 起点；保持 mp4 风格比特流以便后续 merge 阶段处理。"""
    log = logging.getLogger("edge.runner")
    try:
        dst.unlink(missing_ok=True)
    except Exception:
        pass
    cmd = [
        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-reset_timestamps", "1",
        "-fflags", "+genpts",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    except subprocess.CalledProcessError as e:
        log.warning("zero_based_remux failed src=%s dst=%s rc=%s err=%s", src.name, dst.name, e.returncode, (e.stderr or "")[-400:])
        return False
    except Exception as e:
        log.warning("zero_based_remux exception src=%s dst=%s err=%s", src.name, dst.name, e)
        return False
    return dst.exists() and dst.stat().st_size > 0


def _audio_copy_to_mp4_allowed(codec_name: str) -> bool:
    return str(codec_name or "").strip().lower() in {"aac", "alac", "ac3", "eac3", "mp3", "mp2"}


def _can_zero_based_copy_to_mp4(src: Path) -> bool:
    audio_info = probe_audio_stream_info(str(src))
    audio_codec = str(audio_info.get("codec_name") or "").strip().lower()
    return not audio_codec or _audio_copy_to_mp4_allowed(audio_codec)


def _copy_video_audio_early_pcm_repair_part(
    raw_part: Path,
    output_path: Path,
    *,
    precise_state: dict[str, float | bool | str],
    source_metrics: dict[str, float],
    target_video_codec: str = "",
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], str | None] | None = None,
) -> str:
    """PCM audio_early fast path: zero-base copied video, then rebuild only audio timeline."""
    log = logging.getLogger("edge.runner")
    if not raw_part.exists() or raw_part.stat().st_size <= 0:
        return "raw_missing_or_empty"
    if not ffmpeg_exists():
        return "ffmpeg_missing"
    classification = str(precise_state.get("classification") or "").strip()
    coarse_gap = float(precise_state.get("coarse_gap") or 0.0)
    content_offset = float(precise_state.get("audio_content_offset") or 0.0)
    audio_packet_timeline_disorder = bool(precise_state.get("audio_packet_timeline_disorder"))
    video_start = float(precise_state.get("video_start") or 0.0)
    audio_start = float(precise_state.get("audio_start") or 0.0)
    if classification != "audio_early":
        return f"classification_not_audio_early:{classification or 'unknown'}"
    if coarse_gap <= 0.5:
        return f"coarse_gap_too_small:{coarse_gap:.3f}"
    if content_offset > 0.2:
        return f"content_offset_too_large:{content_offset:.3f}"
    if audio_packet_timeline_disorder:
        return "audio_packet_timeline_disorder"
    if not _has_abnormal_absolute_timeline_start(video_start, audio_start):
        return f"absolute_start_not_abnormal:video={video_start:.3f}:audio={audio_start:.3f}"
    audio_info = probe_audio_stream_info(str(raw_part))
    audio_codec = str(audio_info.get("codec_name") or "").strip().lower()
    if audio_codec not in {"pcm_alaw", "pcm_mulaw"}:
        return f"audio_codec_not_pcm:{audio_codec or 'unknown'}"
    source_timings = probe_stream_timings(str(raw_part))
    source_video_codec = str((source_timings.get("video") or {}).get("codec_name") or "").strip().lower()
    target_codec = str(target_video_codec or "").strip().lower()
    if target_codec and source_video_codec and source_video_codec != target_codec:
        return f"target_video_codec_mismatch:source={source_video_codec}:target={target_codec}"
    target_duration = float(source_metrics.get("video_duration") or 0.0)
    if target_duration <= 0.0:
        return "missing_video_duration"
    tmp_video = output_path.with_name(output_path.stem + ".copyvideo.videozero.tmp.mp4")
    tmp_out = output_path.with_name(output_path.stem + ".copyvideo.tmp.mp4")
    for tmp in (tmp_video, tmp_out):
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    audio_filter = (
        f"aresample=async=1:first_pts=0,"
        f"asetpts=PTS-STARTPTS,"
        f"atrim=end={target_duration:.3f},"
        f"apad=whole_dur={target_duration:.3f},"
        f"atrim=end={target_duration:.3f},"
        f"asetpts=PTS-STARTPTS"
    )

    def _run_ffmpeg_step(cmd: list[str], step_name: str) -> tuple[bool, str, float]:
        started = time.perf_counter()
        if cancel_check:
            cancel_mode = cancel_check()
            if cancel_mode in {"pause", "stop"}:
                raise RuntimeError(f"cancelled:{cancel_mode}")
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
        except Exception as e:
            return False, f"{step_name}_exception:{e}", time.perf_counter() - started
        elapsed = time.perf_counter() - started
        if cancel_check:
            cancel_mode = cancel_check()
            if cancel_mode in {"pause", "stop"}:
                raise RuntimeError(f"cancelled:{cancel_mode}")
        if proc.returncode != 0:
            tail = (proc.stdout or "")[-1200:]
            return False, f"{step_name}_ffmpeg_failed:rc={proc.returncode}:tail={tail}", elapsed
        return True, "", elapsed

    if on_status:
        on_status("命中 PCM 音频早到快路径，正在 copy 视频并重建音频时间轴")
    video_cmd = [
        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-i", str(raw_part),
        "-map", "0:v:0",
        "-c:v", "copy",
        "-an",
        "-avoid_negative_ts", "make_zero",
        "-reset_timestamps", "1",
        "-movflags", "+faststart",
        str(tmp_video),
    ]
    ok, reason, video_elapsed = _run_ffmpeg_step(video_cmd, "video_zero_copy")
    if not ok:
        log.warning(
            "%s 分段 %s PCM 音频早到快路径 video_zero_copy 失败 reason=%s",
            _branch_code_tag(CAM_DL_NORM_083),
            raw_part.name,
            reason,
        )
        for tmp in (tmp_video, tmp_out):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return reason
    video_zero_metrics = _collect_structural_media_metrics(tmp_video, include_packet_metrics=False)
    video_zero_start = float(video_zero_metrics.get("video_start") or 0.0)
    video_zero_end = float(video_zero_metrics.get("video_end") or 0.0)
    if video_zero_end <= 0.0 or video_zero_start > DOWNLOAD_PART_REBUILD_START_GAP_TOLERANCE_SECONDS:
        reason = f"video_zero_invalid:video_start={video_zero_start:.3f}:video_end={video_zero_end:.3f}"
        log.warning("%s 分段 %s PCM 音频早到快路径 %s", _branch_code_tag(CAM_DL_NORM_083), raw_part.name, reason)
        try:
            tmp_video.unlink(missing_ok=True)
        except Exception:
            pass
        return reason
    mux_cmd = [
        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-analyzeduration", "100M",
        "-probesize", "100M",
        "-i", str(tmp_video),
        "-i", str(raw_part),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-af", audio_filter,
        "-c:a", "aac",
        "-b:a", "128k",
        "-avoid_negative_ts", "make_zero",
        "-reset_timestamps", "1",
        "-movflags", "+faststart",
        str(tmp_out),
    ]
    ok, reason, mux_elapsed = _run_ffmpeg_step(mux_cmd, "final_mux_copy_video")
    if not ok:
        log.warning(
            "%s 分段 %s PCM 音频早到快路径 final_mux_copy_video 失败 reason=%s",
            _branch_code_tag(CAM_DL_NORM_083),
            raw_part.name,
            reason,
        )
        for tmp in (tmp_video, tmp_out):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return reason
    validation_issue = _validate_download_part_rebuild_output(tmp_out, include_packet_metrics=False)
    merge_issue = ""
    if not validation_issue:
        merge_issue = _download_part_merge_ready_issue_with_options(
            tmp_out,
            include_packet_metrics=False,
            defer_precise_probe_until_suspicious=True,
            deferred_start_gap_tolerance_seconds=0.2,
        )
    if validation_issue or merge_issue:
        reason = validation_issue or merge_issue
        log.warning(
            "%s 分段 %s PCM 音频早到快路径验收失败 reason=%s video_zero_elapsed=%.3fs mux_elapsed=%.3fs",
            _branch_code_tag(CAM_DL_NORM_083),
            raw_part.name,
            reason,
            video_elapsed,
            mux_elapsed,
        )
        for tmp in (tmp_video, tmp_out):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return f"validation_failed:{reason}"
    try:
        _replace_file_or_raise_finalize_pending(
            tmp_out,
            output_path,
            log=log,
            action=f"download part audio early pcm copy-video repair {raw_part.name} -> {output_path.name}",
            user_message="PCM 音频早到快路径修复完成，正在等待最终提交",
        )
    except FinalizePendingError:
        raise
    except Exception as e:
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return f"finalize_failed:{e}"
    finally:
        try:
            tmp_video.unlink(missing_ok=True)
        except Exception:
            pass
    log.info(
        "%s 分段 %s PCM 音频早到 copy-video 快路径完成 coarse_gap=%.3fs codec=%s target_duration=%.3fs video_zero_elapsed=%.3fs mux_elapsed=%.3fs audio_filter=%s out=%s",
        _branch_code_tag(CAM_DL_NORM_083),
        raw_part.name,
        coarse_gap,
        audio_codec,
        target_duration,
        video_elapsed,
        mux_elapsed,
        audio_filter,
        str(output_path),
    )
    return ""


def _copy_video_audio_content_late_repair_part(
    raw_part: Path,
    output_path: Path,
    *,
    precise_state: dict[str, float | bool | str],
    source_metrics: dict[str, float],
    target_video_codec: str = "",
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], str | None] | None = None,
) -> str:
    log = logging.getLogger("edge.runner")
    if not raw_part.exists() or raw_part.stat().st_size <= 0:
        return "raw_missing_or_empty"
    if not ffmpeg_exists():
        return "ffmpeg_missing"
    classification = str(precise_state.get("classification") or "").strip()
    if classification != "audio_content_late":
        return f"classification_not_audio_content_late:{classification or 'unknown'}"
    video_start = float(precise_state.get("video_start") or 0.0)
    audio_start = float(precise_state.get("audio_start") or 0.0)
    if not _has_abnormal_absolute_timeline_start(video_start, audio_start):
        return f"absolute_start_not_abnormal:video={video_start:.3f}:audio={audio_start:.3f}"
    audio_info = probe_audio_stream_info(str(raw_part))
    audio_codec = str(audio_info.get("codec_name") or "").strip().lower()
    if not audio_codec:
        return "missing_audio_stream"
    source_timings = probe_stream_timings(str(raw_part))
    source_video_codec = str((source_timings.get("video") or {}).get("codec_name") or "").strip().lower()
    target_codec = str(target_video_codec or "").strip().lower()
    if target_codec and source_video_codec and source_video_codec != target_codec:
        return f"target_video_codec_mismatch:source={source_video_codec}:target={target_codec}"

    raw_video_duration = float(source_metrics.get("video_duration") or 0.0)
    raw_audio_duration = float(source_metrics.get("audio_duration") or 0.0)
    raw_format_duration = float(source_metrics.get("format_duration") or 0.0)
    raw_authoritative_duration = float(source_metrics.get("authoritative_duration") or 0.0)
    if raw_video_duration <= 0.0 or raw_audio_duration <= 0.0:
        return f"missing_duration:video={raw_video_duration:.3f}:audio={raw_audio_duration:.3f}"
    # 只处理停电/中断类的异常长时间轴：raw 容器声画时长被拉到几十分钟/数小时，
    # 但实际 copy 出来的视频能落到正常短段时长。普通 audio_content_late 仍走原 010。
    abnormal_long_timeline = bool(
        raw_audio_duration > raw_video_duration + 60.0
        or raw_format_duration > raw_video_duration + 60.0
        or raw_authoritative_duration > raw_video_duration + 60.0
    )
    if not abnormal_long_timeline:
        return (
            "duration_not_abnormal_long:"
            f"video={raw_video_duration:.3f}:audio={raw_audio_duration:.3f}:"
            f"format={raw_format_duration:.3f}:authoritative={raw_authoritative_duration:.3f}"
        )

    tmp_video = output_path.with_name(output_path.stem + ".contentlate.videozero.tmp.mp4")
    tmp_out = output_path.with_name(output_path.stem + ".contentlate.tmp.mp4")
    for tmp in (tmp_video, tmp_out):
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    def _run_ffmpeg_step(cmd: list[str], step_name: str) -> tuple[bool, str, float]:
        started = time.perf_counter()
        if cancel_check:
            cancel_mode = cancel_check()
            if cancel_mode in {"pause", "stop"}:
                raise RuntimeError(f"cancelled:{cancel_mode}")
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
        except Exception as e:
            return False, f"{step_name}_exception:{e}", time.perf_counter() - started
        elapsed = time.perf_counter() - started
        if cancel_check:
            cancel_mode = cancel_check()
            if cancel_mode in {"pause", "stop"}:
                raise RuntimeError(f"cancelled:{cancel_mode}")
        if proc.returncode != 0:
            tail = (proc.stdout or "")[-1200:]
            return False, f"{step_name}_ffmpeg_failed:rc={proc.returncode}:tail={tail}", elapsed
        return True, "", elapsed

    if on_status:
        on_status("命中异常长时间轴音频晚到快路径，正在按真实视频时长修复当前分段")
    video_cmd = [
        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-i", str(raw_part),
        "-map", "0:v:0",
        "-c:v", "copy",
        "-an",
        "-avoid_negative_ts", "make_zero",
        "-reset_timestamps", "1",
        "-movflags", "+faststart",
        str(tmp_video),
    ]
    ok, reason, video_elapsed = _run_ffmpeg_step(video_cmd, "video_zero_copy")
    if not ok:
        for tmp in (tmp_video, tmp_out):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return reason
    video_zero_metrics = _collect_structural_media_metrics(tmp_video, include_packet_metrics=False)
    video_zero_start = float(video_zero_metrics.get("video_start") or 0.0)
    video_zero_duration = float(video_zero_metrics.get("video_duration") or 0.0)
    if video_zero_duration <= 0.0:
        return "video_zero_missing_duration"
    if video_zero_start > DOWNLOAD_PART_REBUILD_START_GAP_TOLERANCE_SECONDS:
        return f"video_zero_start_not_zero:{video_zero_start:.3f}"
    if video_zero_duration >= min(raw_audio_duration, raw_format_duration or raw_audio_duration) - 60.0:
        return f"video_zero_still_abnormal_long:{video_zero_duration:.3f}"

    audio_filter = (
        "asetpts=PTS-STARTPTS,"
        f"atrim=end={video_zero_duration:.3f},"
        f"apad=whole_dur={video_zero_duration:.3f},"
        f"atrim=end={video_zero_duration:.3f},"
        "asetpts=PTS-STARTPTS"
    )
    mux_cmd = [
        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-analyzeduration", "100M",
        "-probesize", "100M",
        "-i", str(tmp_video),
        "-i", str(raw_part),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-af", audio_filter,
        "-c:a", "aac",
        "-b:a", "128k",
        "-avoid_negative_ts", "make_zero",
        "-reset_timestamps", "1",
        "-movflags", "+faststart",
        str(tmp_out),
    ]
    ok, reason, mux_elapsed = _run_ffmpeg_step(mux_cmd, "final_mux_copy_video")
    if not ok:
        for tmp in (tmp_video, tmp_out):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return reason
    validation_issue = _validate_download_part_rebuild_output(tmp_out, include_packet_metrics=False)
    merge_issue = ""
    if not validation_issue:
        merge_issue = _download_part_merge_ready_issue_with_options(
            tmp_out,
            include_packet_metrics=False,
            defer_precise_probe_until_suspicious=True,
            deferred_start_gap_tolerance_seconds=0.2,
        )
    if validation_issue or merge_issue:
        reason = validation_issue or merge_issue
        log.warning(
            "%s 分段 %s audio_content_late copy-video 快路径验收失败 reason=%s video_zero_elapsed=%.3fs mux_elapsed=%.3fs",
            _branch_code_tag(CAM_DL_NORM_084),
            raw_part.name,
            reason,
            video_elapsed,
            mux_elapsed,
        )
        for tmp in (tmp_video, tmp_out):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return f"validation_failed:{reason}"
    try:
        _replace_file_or_raise_finalize_pending(
            tmp_out,
            output_path,
            log=log,
            action=f"download part audio content late copy-video repair {raw_part.name} -> {output_path.name}",
            user_message="异常长时间轴音频晚到快路径修复完成，正在等待最终提交",
        )
    except FinalizePendingError:
        raise
    except Exception as e:
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return f"finalize_failed:{e}"
    finally:
        try:
            tmp_video.unlink(missing_ok=True)
        except Exception:
            pass
    log.info(
        "%s 分段 %s audio_content_late copy-video 快路径完成 raw_video=%.3fs raw_audio=%.3fs videozero=%.3fs video_zero_elapsed=%.3fs mux_elapsed=%.3fs audio_filter=%s out=%s",
        _branch_code_tag(CAM_DL_NORM_084),
        raw_part.name,
        raw_video_duration,
        raw_audio_duration,
        video_zero_duration,
        video_elapsed,
        mux_elapsed,
        audio_filter,
        str(output_path),
    )
    return ""


def _lightweight_audio_early_repair_part(
    raw_part: Path,
    audio_early_by: float,
    output_path: Path,
    *,
    need_zero_based: bool,
    on_status: Callable[[str], None] | None = None,
    cancel_check: Callable[[], str | None] | None = None,
) -> str:
    """对 audio_early 主导场景做轻量修复（仅 stream copy）：
    - 一次 ffmpeg 调用：对 audio 流应用 itsoffset 推迟 audio_early_by 秒，使其与 video 内容时间对齐；
    - 若同时 absolute_start_abnormal=True，则在同次调用中归零 PTS、重置时间戳；
    - 输出强制经过 _probe_structural_media_issue + _probe_precise_av_sync 双重验收。

    返回空字符串表示成功（产物落在 output_path）；非空字符串为失败原因，调用方应回退到完整重建。
    所有判定均使用现有检测下限/对齐容忍度，不引入与 gap 量级相关的死值。
    """
    log = logging.getLogger("edge.runner")
    if not raw_part.exists() or raw_part.stat().st_size <= 0:
        return "raw_missing_or_empty"
    if audio_early_by <= 0:
        return "audio_early_by_not_positive"
    if not ffmpeg_exists():
        return "ffmpeg_missing"
    tmp_out = output_path.with_name(output_path.stem + ".lightaudioearly.tmp.mp4")
    try:
        tmp_out.unlink(missing_ok=True)
    except Exception:
        pass
    cmd = [
        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(raw_part),
        "-itsoffset", f"{float(audio_early_by):.3f}",
        "-i", str(raw_part),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c", "copy",
    ]
    if need_zero_based:
        cmd.extend([
            "-avoid_negative_ts", "make_zero",
            "-reset_timestamps", "1",
            "-fflags", "+genpts",
        ])
    cmd.extend([
        "-movflags", "+faststart",
        str(tmp_out),
    ])
    if on_status:
        on_status("命中音频早到轻量修复，正在 itsoffset 推迟音频并归零分段时间线")
    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except subprocess.CalledProcessError as e:
        tail = (e.stdout or "")[-1200:]
        log.warning(
            "%s 分段 %s 轻量音频早到修复 ffmpeg 失败 audio_early_by=%.3fs need_zero_based=%s rc=%s tail=%s",
            _branch_code_tag(CAM_DL_NORM_080),
            raw_part.name,
            audio_early_by,
            need_zero_based,
            e.returncode,
            tail,
        )
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return f"ffmpeg_failed:rc={e.returncode}"
    except Exception as e:
        log.warning(
            "%s 分段 %s 轻量音频早到修复 ffmpeg 异常 audio_early_by=%.3fs need_zero_based=%s err=%s",
            _branch_code_tag(CAM_DL_NORM_080),
            raw_part.name,
            audio_early_by,
            need_zero_based,
            e,
        )
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return f"ffmpeg_exception:{e}"
    if cancel_check:
        cancel_mode = cancel_check()
        if cancel_mode in {"pause", "stop"}:
            try:
                tmp_out.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"cancelled:{cancel_mode}")
    if not tmp_out.exists() or tmp_out.stat().st_size <= 0:
        return "output_missing"
    structural_issue = _probe_structural_media_issue(tmp_out)
    if structural_issue:
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return f"structural_invalid:{structural_issue}"
    post_precise = _probe_precise_av_sync(tmp_out)
    post_classification = str(post_precise.get("classification") or "").strip()
    post_audio_late_trim = float(post_precise.get("audio_late_trim") or 0.0)
    post_video_start = float(post_precise.get("video_start") or 0.0)
    post_audio_start = float(post_precise.get("audio_start") or 0.0)
    post_start_gap = post_audio_start - post_video_start
    if post_audio_late_trim > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS:
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return f"post_repair_audio_late:trim={post_audio_late_trim:.3f}:classification={post_classification}"
    if abs(post_start_gap) > AUDIO_EARLY_ALIGN_THRESHOLD_SECONDS:
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return f"post_repair_misaligned:start_gap={post_start_gap:.3f}:classification={post_classification}"
    if need_zero_based:
        rebuild_issue = _validate_download_part_rebuild_output(tmp_out)
        if rebuild_issue:
            try:
                tmp_out.unlink(missing_ok=True)
            except Exception:
                pass
            return f"rebuild_validation_failed:{rebuild_issue}"
    try:
        _replace_file_or_raise_finalize_pending(
            tmp_out,
            output_path,
            log=log,
            action=f"download part lightweight audio_early repair {raw_part.name} -> {output_path.name}",
            user_message="音频早到轻量修复完成，正在等待最终提交",
        )
    except FinalizePendingError:
        raise
    except Exception as e:
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return f"finalize_failed:{e}"
    log.info(
        "%s 分段 %s 音频早到轻量修复完成 audio_early_by=%.3fs need_zero_based=%s out=%s post_classification=%s post_start_gap=%.3fs",
        _branch_code_tag(CAM_DL_NORM_080),
        raw_part.name,
        audio_early_by,
        need_zero_based,
        str(output_path),
        post_classification or "aligned",
        post_start_gap,
    )
    return ""


def _normalize_download_part(
    part_path: Path,
    task_type: int = 0,
    on_status: Callable[[str], None] | None = None,
    total_elapsed_supplier: Callable[[], float] | None = None,
    cancel_check: Callable[[], str | None] | None = None,
    batch_av_policy: DownloadBatchAvPolicy | None = None,
) -> DownloadPartNormalizeResult:
    """修复分段视频音视频同步：student/ppt 分段重建 0 基时间线，teacher 保持原有无损对齐。"""
    if not part_path.exists():
        return DownloadPartNormalizeResult(duration_sec=0.0)
    if not ffmpeg_exists():
        return DownloadPartNormalizeResult(duration_sec=float(probe_duration_seconds(str(part_path)) or 0.0))
    log = logging.getLogger("edge.runner")
    timeline_state = _probe_timeline_state(part_path)
    media_start = float(timeline_state.get("media_start") or 0.0)
    video_start = float(timeline_state.get("video_start") or 0.0)
    audio_start = float(timeline_state.get("audio_start") or 0.0)
    stream_gap = float(timeline_state.get("stream_gap") or 0.0)
    start_gap = audio_start - video_start
    max_start = float(timeline_state.get("max_start") or 0.0)
    timeline_dirty = bool(timeline_state.get("dirty"))
    audio_late_by = max(0.0, start_gap)
    audio_early_by = max(0.0, -start_gap)
    aligned_start = abs(start_gap) <= AV_SYNC_START_TOLERANCE_SECONDS
    absolute_start_abnormal = _has_abnormal_absolute_timeline_start(media_start, video_start, audio_start, max_start)
    batch_policy_name = str(batch_av_policy.name if batch_av_policy else "default")
    nvr_pts_skew_same_origin = batch_policy_name == "nvr_pts_skew_same_origin"
    batch_target_video_codec = _normalize_video_codec_name_for_policy(str(batch_av_policy.target_video_codec if batch_av_policy else ""))
    if batch_target_video_codec not in {"h264", "hevc"}:
        batch_target_video_codec = ""
    if batch_policy_name == ABSOLUTE_TIMELINE_TS_BRIDGE_POLICY:
        fast_metrics = _collect_structural_media_metrics(part_path, include_packet_metrics=False)
        if absolute_start_abnormal and _part_relative_alignment_healthy(fast_metrics):
            if on_status:
                on_status("检测到整批绝对时间轴连续异常，当前分段保留源时间线交给TS合并")
            log.info(
                "%s 分段 %s 命中整批绝对时间轴连续场景，跳过单段重建 merge_part=%s metrics=%s policy=%s",
                _branch_code_tag(CAM_DL_NORM_070),
                part_path.name,
                str(part_path),
                _format_structural_media_metrics(fast_metrics),
                batch_av_policy.reason if batch_av_policy else "",
            )
            return DownloadPartNormalizeResult(
                duration_sec=float(fast_metrics.get("video_duration") or probe_duration_seconds(str(part_path)) or 0.0),
                force_canonical_merge=False,
                canonicalize_merge_part=False,
                merge_risk_level=0,
                normalize_reason=f"{ABSOLUTE_TIMELINE_TS_BRIDGE_POLICY}:raw_passthrough",
                merge_part_path=str(part_path),
                classification=ABSOLUTE_TIMELINE_TS_BRIDGE_POLICY,
            )
    precise_state = _probe_precise_av_sync(part_path)
    precise_trim = float(precise_state.get("audio_late_trim") or 0.0)
    precise_reason = str(precise_state.get("reason") or "").strip()
    video_duration = float(precise_state.get("video_duration") or 0.0)
    audio_duration = float(precise_state.get("audio_duration") or 0.0)
    duration_delta = float(precise_state.get("duration_delta") or 0.0)
    audio_content_offset = float(precise_state.get("audio_content_offset") or 0.0)
    precise_classification = str(precise_state.get("classification") or "").strip()
    raw_structural_metrics = _collect_structural_media_metrics(part_path, include_packet_metrics=False)
    expected_tail_silence_source = _is_expected_tail_silence_source(raw_structural_metrics, precise_state)
    source_has_no_audio_stream = _source_part_has_no_audio_stream(part_path, precise_state=precise_state)
    raw_video_metric_duration = float(raw_structural_metrics.get("video_duration") or 0.0)
    raw_audio_metric_duration = float(raw_structural_metrics.get("audio_duration") or 0.0)
    raw_format_metric_duration = float(raw_structural_metrics.get("format_duration") or 0.0)
    # 只把 7451/7449 这类断电后原始容器时长被拉到异常大值的分段提前送入 084。
    # 普通 30 分钟左右的 audio_content_late 分段仍交给原 010，避免 part006 这类误试快路径。
    raw_audio_content_late_abnormal_long = bool(
        precise_classification == "audio_content_late"
        and absolute_start_abnormal
        and raw_video_metric_duration > 0.0
        and max(raw_video_metric_duration, raw_audio_metric_duration, raw_format_metric_duration) > 3600.0
    )
    significant_audio_late = precise_trim > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS
    minor_audio_late = AV_SYNC_START_TOLERANCE_SECONDS < audio_late_by <= AUDIO_LATE_REBUILD_THRESHOLD_SECONDS
    rebuild_input_path = part_path
    rebuild_input_context = "raw"
    # 源分段后半段真实失声时，优先保留原视频轨，只修正音频时间轴并补静音尾巴；
    # 避免在单段修复阶段把画质先压坏，再在合并阶段做第二次视频处理。
    expected_tail_silence_force_full_rebuild = False
    rebuild_target_video_codec = "" if expected_tail_silence_source else batch_target_video_codec
    if source_has_no_audio_stream:
        log.info(
            "%s 分段 %s 源分段未检测到音频流，后续按 video-only 时间线修复处理，不进入音频轻量修复分支",
            _branch_code_tag(CAM_DL_NORM_010),
            part_path.name,
        )
    if nvr_pts_skew_same_origin:
        systrans_part = _systrans_part_output_path(part_path)
        video_scaffold_part = part_path.with_name(part_path.stem + ".nvrskew.scaffold.mp4")
        try:
            if on_status:
                on_status("检测到NVR固定PTS偏移，正在生成当前分段的稳定视频轨道")
            if not systrans_part.exists():
                system_transform_file(part_path, systrans_part, cancel_check=cancel_check)
            video_source = systrans_part if systrans_part.exists() else part_path
            if _build_nvr_skew_video_scaffold_part(video_source, video_scaffold_part):
                return DownloadPartNormalizeResult(
                    duration_sec=float(probe_duration_seconds(str(video_scaffold_part)) or 0.0),
                    normalize_reason="nvr_pts_skew_video_scaffold_for_merged_raw_audio",
                    merge_part_path=str(video_scaffold_part),
                    classification="nvr_pts_skew_same_origin_video_scaffold",
                )
        except Exception as e:
            log.warning("%s 分段 %s NVR固定PTS偏移视频脚手架构造失败，回退常规归一化: %s", _branch_code_tag(CAM_DL_NORM_061), part_path.name, e)
    if not timeline_dirty and precise_trim <= AUDIO_LATE_REBUILD_THRESHOLD_SECONDS and aligned_start:
        log.info("%s 分段 %s 时间线正常，跳过下载修复 media_start=%.3fs video=%.3fs audio=%.3fs start_gap=%.3fs stream_gap=%.3fs classification=%s", _branch_code_tag(CAM_DL_NORM_000), part_path.name, media_start, video_start, audio_start, start_gap, stream_gap, precise_classification or "aligned")
        return DownloadPartNormalizeResult(duration_sec=float(probe_duration_seconds(str(part_path)) or 0.0), merge_part_path=str(part_path), classification=precise_classification or "aligned")
    # CAM-DL-NORM-070：raw 自身相对对齐健康，仅绝对起点异常时，对 raw 与 systrans 做通用对比，
    # 选优后只做轻量归零。避免在该场景误进入"systrans 拉长音频→全量重建失败"的强异常路径。
    if (
        absolute_start_abnormal
        and not significant_audio_late
        and not nvr_pts_skew_same_origin
        and not (timeline_dirty and stream_gap > TIMELINE_GAP_TOLERANCE_SECONDS)
    ):
        raw_metrics_pre = _collect_structural_media_metrics(part_path)
        raw_relative_healthy = _part_relative_alignment_healthy(raw_metrics_pre)
        if raw_relative_healthy:
            systrans_part = _systrans_part_output_path(part_path)
            normalized_part = _normalized_part_output_path(part_path)
            zero_based_done = False
            try:
                if on_status:
                    on_status("检测到分段绝对时间轴异常但相对对齐健康，正在对比修复方案")
                if not (systrans_part.exists() and systrans_part.stat().st_size > 0):
                    try:
                        system_transform_file(part_path, systrans_part, cancel_check=cancel_check)
                    except Exception as sys_err:
                        if isinstance(sys_err, FinalizePendingError):
                            raise
                        if str(sys_err) in {"cancelled:pause", "cancelled:stop"}:
                            raise
                        log.warning("%s 分段 %s 海康SDK转换失败，按 raw 候选继续: %s", _branch_code_tag(CAM_DL_NORM_070), part_path.name, sys_err)
                winner, reason, raw_m, sys_m = _choose_better_for_zero_based_remux(part_path, systrans_part)
                chosen_path = part_path if winner == "raw" else systrans_part
                chosen_metrics = raw_m if winner == "raw" else sys_m
                zero_based_method = "selected"
                chosen_abs_start = max(
                    float(chosen_metrics.get("video_start") or 0.0),
                    float(chosen_metrics.get("audio_start") or 0.0),
                )
                log.info(
                    "%s 分段 %s zero_based 选优 winner=%s reason=%s abs_start=%.3fs raw_metrics=[%s] systrans_metrics=[%s]",
                    _branch_code_tag(CAM_DL_NORM_070),
                    part_path.name,
                    winner,
                    reason,
                    chosen_abs_start,
                    _format_structural_media_metrics(raw_m) if raw_m else "missing",
                    _format_structural_media_metrics(sys_m) if sys_m else "missing",
                )
                merge_part: Path
                if chosen_abs_start > TIMELINE_START_TOLERANCE_SECONDS:
                    if on_status:
                        on_status(f"已选定 {winner} 候选，正在轻量归零分段时间线")
                    zero_based_copy_allowed = _can_zero_based_copy_to_mp4(chosen_path)
                    if zero_based_copy_allowed and _zero_based_remux_part(chosen_path, normalized_part):
                        zero_based_method = "remux"
                        merge_part = normalized_part
                    else:
                        if on_status:
                            on_status("轻量归零不可用，正在回退完整重建当前分段")
                        log.warning(
                            "%s 分段 %s 轻量归零不可用，回退完整重建 winner=%s copy_allowed=%s",
                            _branch_code_tag(CAM_DL_NORM_071),
                            part_path.name,
                            winner,
                            zero_based_copy_allowed,
                        )
                        rebuilt = _rebuild_download_part_timeline(
                            chosen_path,
                            precise_trim=0.0,
                            target_duration_sec=float(chosen_metrics.get("video_duration") or 0.0),
                            force_full_rebuild=True,
                            target_video_codec=batch_target_video_codec,
                            output_path=normalized_part,
                            branch_code=CAM_DL_NORM_071,
                            on_status=on_status,
                            total_elapsed_supplier=total_elapsed_supplier,
                            cancel_check=cancel_check,
                            user_status="正在完整重建当前分段",
                            log_context=f"fallback_from_zero_based_remux_failed winner={winner} copy_allowed={zero_based_copy_allowed}",
                        )
                        if not rebuilt:
                            raise RuntimeError(f"zero_based_remux_rebuild_failed:winner={winner}:copy_allowed={zero_based_copy_allowed}")
                        zero_based_method = "rebuild"
                        merge_part = normalized_part
                else:
                    merge_part = chosen_path
                post_issue = _probe_structural_media_issue(merge_part)
                if post_issue:
                    log.warning(
                        "%s 分段 %s zero_based 输出结构性校验失败 issue=%s，回退至原有强异常修复路径",
                        _branch_code_tag(CAM_DL_NORM_070),
                        part_path.name,
                        post_issue,
                    )
                    if merge_part != part_path and merge_part != systrans_part:
                        try:
                            merge_part.unlink(missing_ok=True)
                        except Exception:
                            pass
                else:
                    if winner == "raw":
                        try:
                            systrans_part.unlink(missing_ok=True)
                        except Exception:
                            pass
                    elif winner == "systrans" and merge_part != systrans_part:
                        try:
                            systrans_part.unlink(missing_ok=True)
                        except Exception:
                            pass
                    if on_status:
                        if zero_based_method == "rebuild":
                            on_status("分段时间线已完成完整重建")
                        elif zero_based_method == "remux":
                            on_status("分段时间线已完成轻量归零")
                        else:
                            on_status("分段时间线候选已确认，可直接参与合并")
                    zero_based_done = True
                    return DownloadPartNormalizeResult(
                        duration_sec=float(probe_duration_seconds(str(merge_part)) or 0.0),
                        normalize_reason=f"zero_based_{zero_based_method}_{winner}:{reason}",
                        merge_part_path=str(merge_part),
                        classification="aligned",
                    )
            except FinalizePendingError:
                raise
            except Exception as e:
                if str(e) in {"cancelled:pause", "cancelled:stop"}:
                    raise
                log.warning("%s 分段 %s zero_based 选优分支异常，回退原有强异常修复路径: %s", _branch_code_tag(CAM_DL_NORM_070), part_path.name, e)
            if not zero_based_done:
                # 清理本分支的中间产物（systrans 复用给下游路径，但 normalized_part 必须清理）
                try:
                    if normalized_part.exists():
                        normalized_part.unlink(missing_ok=True)
                except Exception:
                    pass
    if expected_tail_silence_source:
        log.info(
            "%s 分段 %s 命中源分段尾部真实失声特征，跳过海康SDK转换，直接基于 raw 修复 source_metrics=%s classification=%s",
            _branch_code_tag(CAM_DL_NORM_010),
            part_path.name,
            _format_structural_media_metrics(raw_structural_metrics),
            precise_classification or "unknown",
        )
    if raw_audio_content_late_abnormal_long:
        log.info(
            "%s 分段 %s 命中 audio_content_late 异常长时间轴特征，跳过海康SDK转换，直接按真实视频时长修复 source_metrics=%s",
            _branch_code_tag(CAM_DL_NORM_084),
            part_path.name,
            _format_structural_media_metrics(raw_structural_metrics),
        )
    if (
        (significant_audio_late or absolute_start_abnormal or (timeline_dirty and stream_gap > TIMELINE_GAP_TOLERANCE_SECONDS))
        and not expected_tail_silence_source
        and not source_has_no_audio_stream
        and not raw_audio_content_late_abnormal_long
    ):
        # raw 是否存在显著的音视频起始差：以现有检测下限驱动，不再用具体秒数门限。
        # 任一方向（早/晚）的 gap 超过对应检测下限即认为显著，需要在 systrans 准入时校验是否被抹平。
        raw_significant_start_gap = (
            audio_early_by > AUDIO_EARLY_ALIGN_THRESHOLD_SECONDS
            or audio_late_by > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS
        )
        systrans_part = _systrans_part_output_path(part_path)
        try:
            if on_status:
                on_status("检测到分段时间线异常，正在优先尝试海康SDK转换")
            systrans_result = system_transform_file(part_path, systrans_part, cancel_check=cancel_check)
            systrans_light_metrics = _collect_structural_media_metrics(systrans_part, include_packet_metrics=False) if systrans_part.exists() else {}
            systrans_issue = ""
            systrans_start_gap = 0.0
            systrans_lost_raw_start_gap = False
            if raw_significant_start_gap and systrans_part.exists():
                systrans_timeline = _probe_timeline_state(systrans_part)
                systrans_video_start = float(systrans_timeline.get("video_start") or 0.0)
                systrans_audio_start = float(systrans_timeline.get("audio_start") or 0.0)
                systrans_start_gap = systrans_audio_start - systrans_video_start
                systrans_lost_raw_start_gap = abs(systrans_start_gap - start_gap) >= max(
                    SYSTRANS_LOST_RAW_START_GAP_FLOOR_SECONDS,
                    abs(start_gap) * SYSTRANS_LOST_RAW_START_GAP_RATIO,
                )
            reject_systrans_admission = systrans_lost_raw_start_gap and not nvr_pts_skew_same_origin
            if not reject_systrans_admission and systrans_part.exists():
                systrans_issue = _download_part_merge_ready_issue_with_options(
                    systrans_part,
                    include_packet_metrics=False,
                    defer_precise_probe_until_suspicious=True,
                    deferred_start_gap_tolerance_seconds=AUDIO_LATE_REBUILD_THRESHOLD_SECONDS,
                )
            log.info(
                "%s 分段 %s 海康SDK转换完成 issue=%s result=%s metrics=%s",
                _branch_code_tag(CAM_DL_NORM_060),
                part_path.name,
                systrans_issue or "ok",
                systrans_result,
                _format_structural_media_metrics(systrans_light_metrics) if systrans_light_metrics else "",
            )
            if not systrans_issue and not reject_systrans_admission:
                systrans_classification = str(_probe_precise_av_sync(systrans_part).get("classification") or "aligned")
                return DownloadPartNormalizeResult(
                    duration_sec=float(probe_duration_seconds(str(systrans_part)) or 0.0),
                    normalize_reason="hik_systrans_nvr_pts_skew_same_origin_ok" if nvr_pts_skew_same_origin and systrans_lost_raw_start_gap else "hik_systrans_ok",
                    merge_part_path=str(systrans_part),
                    classification=systrans_classification,
                )
            if not systrans_issue and reject_systrans_admission:
                gap_detail = ""
                if systrans_lost_raw_start_gap:
                    gap_detail = f" raw_start_gap={start_gap:.3f}s systrans_start_gap={systrans_start_gap:.3f}s"
                log.warning(
                    "%s 分段 %s SDK 转换丢失 raw 显著音视频起始差 precise_trim=%.3fs audio_early_by=%.3fs%s，不直接准入，回退基于 raw 的精确重建",
                    _branch_code_tag(CAM_DL_NORM_061),
                    part_path.name,
                    precise_trim,
                    audio_early_by,
                    gap_detail,
                )
                try:
                    systrans_part.unlink(missing_ok=True)
                except Exception:
                    pass
                # 保持 rebuild_input_path=part_path（raw），后续 significant_audio_late 分支会按 precise_trim 精确重建
                raise _SkipSystransAdmission()
            systrans_better, systrans_better_reason = _systrans_candidate_is_better_than_raw(part_path, systrans_part)
            if systrans_lost_raw_start_gap and not nvr_pts_skew_same_origin:
                systrans_better = False
                systrans_better_reason = f"systrans_lost_raw_start_gap:raw={start_gap:.3f}:systrans={systrans_start_gap:.3f}:{systrans_better_reason}"
            log.warning(
                "%s 分段 %s 海康SDK转换未通过验收 issue=%s better_than_raw=%s reason=%s",
                _branch_code_tag(CAM_DL_NORM_061),
                part_path.name,
                systrans_issue,
                systrans_better,
                systrans_better_reason,
            )
            if systrans_better:
                rebuild_input_path = systrans_part
                rebuild_input_context = f"systrans_candidate:{systrans_issue}:{systrans_better_reason}"
                systrans_precise = _probe_precise_av_sync(systrans_part)
                precise_trim = float(systrans_precise.get("audio_late_trim") or 0.0)
                precise_reason = str(systrans_precise.get("reason") or "").strip()
                video_duration = float(systrans_precise.get("video_duration") or 0.0)
                audio_duration = float(systrans_precise.get("audio_duration") or 0.0)
                duration_delta = float(systrans_precise.get("duration_delta") or 0.0)
                audio_content_offset = float(systrans_precise.get("audio_content_offset") or 0.0)
                precise_classification = str(systrans_precise.get("classification") or "").strip()
            else:
                try:
                    systrans_part.unlink(missing_ok=True)
                except Exception:
                    pass
        except _SkipSystransAdmission:
            # 已在上方记录 warning，并清理 systrans 输出；继续走 raw 精确重建路径
            pass
        except Exception as e:
            if isinstance(e, FinalizePendingError):
                raise
            if str(e) in {"cancelled:pause", "cancelled:stop"}:
                raise
            log.warning("%s 分段 %s 海康SDK转换失败，回退原始分段修复: %s", _branch_code_tag(CAM_DL_NORM_061), part_path.name, e)
    if significant_audio_late or absolute_start_abnormal:
        normalized_part = _normalized_part_output_path(part_path)
        log.info(
            "%s 分段 %s 命中强异常时间线修复 media_start=%.3fs video=%.3fs audio=%.3fs max_start=%.3fs start_gap=%.3fs stream_gap=%.3fs precise_trim=%.3fs reason=%s classification=%s absolute_start_abnormal=%s video_dur=%.3fs audio_dur=%.3fs delta=%.3fs content_offset=%.3fs rebuild_input=%s，执行完整重建并保留原始分段",
            _branch_code_tag(CAM_DL_NORM_010),
            part_path.name,
            media_start,
            video_start,
            audio_start,
            max_start,
            start_gap,
            stream_gap,
            precise_trim,
            precise_reason,
            precise_classification,
            absolute_start_abnormal,
            video_duration,
            audio_duration,
            duration_delta,
            audio_content_offset,
            rebuild_input_context,
        )
        if on_status:
            if significant_audio_late:
                on_status(f"检测到音频晚于视频{precise_trim:.1f}s，正在精确重建当前分段")
            else:
                on_status(f"检测到分段绝对时间轴异常{max_start:.1f}s，正在完整重建当前分段")
        content_late_copy_reason = _copy_video_audio_content_late_repair_part(
            rebuild_input_path,
            normalized_part,
            precise_state=precise_state,
            source_metrics={
                "video_duration": video_duration,
                "audio_duration": audio_duration,
                "video_start": video_start,
                "audio_start": audio_start,
                "format_duration": float(raw_structural_metrics.get("format_duration") or 0.0),
                "authoritative_duration": float(raw_structural_metrics.get("authoritative_duration") or 0.0),
            },
            target_video_codec=rebuild_target_video_codec,
            on_status=on_status,
            cancel_check=cancel_check,
        )
        if not content_late_copy_reason:
            log.info(
                "%s 分段 %s audio_content_late 异常长时间轴 copy-video 快路径验收通过，跳过 010 重建 rebuild_input=%s",
                _branch_code_tag(CAM_DL_NORM_084),
                part_path.name,
                rebuild_input_context,
            )
            return DownloadPartNormalizeResult(
                duration_sec=float(probe_duration_seconds(str(normalized_part)) or 0.0),
                force_canonical_merge=False,
                canonicalize_merge_part=False,
                merge_risk_level=0,
                normalize_reason="audio_content_late_abnormal_long_copy_video",
                merge_part_path=str(normalized_part),
                classification="aligned",
            )
        if content_late_copy_reason.startswith("cancelled:"):
            raise RuntimeError(content_late_copy_reason)
        log.info(
            "%s 分段 %s audio_content_late copy-video 快路径未命中或未通过，继续原 010 流程 reason=%s classification=%s",
            _branch_code_tag(CAM_DL_NORM_084),
            part_path.name,
            content_late_copy_reason,
            precise_classification or "unknown",
        )
        copy_video_reason = _copy_video_audio_early_pcm_repair_part(
            rebuild_input_path,
            normalized_part,
            precise_state=precise_state,
            source_metrics={
                "video_duration": video_duration,
                "audio_duration": audio_duration,
                "video_start": video_start,
                "audio_start": audio_start,
            },
            target_video_codec=rebuild_target_video_codec,
            on_status=on_status,
            cancel_check=cancel_check,
        )
        if not copy_video_reason:
            log.info(
                "%s 分段 %s PCM 音频早到 copy-video 快路径验收通过，跳过 080/081 和 010 重建 coarse_gap=%.3fs classification=%s rebuild_input=%s",
                _branch_code_tag(CAM_DL_NORM_083),
                part_path.name,
                audio_early_by,
                precise_classification or "unknown",
                rebuild_input_context,
            )
            return DownloadPartNormalizeResult(
                duration_sec=float(probe_duration_seconds(str(normalized_part)) or 0.0),
                force_canonical_merge=False,
                canonicalize_merge_part=False,
                merge_risk_level=0,
                normalize_reason=f"audio_early_pcm_copy_video:gap={audio_early_by:.3f}",
                merge_part_path=str(normalized_part),
                classification="aligned",
            )
        if copy_video_reason.startswith("cancelled:"):
            raise RuntimeError(copy_video_reason)
        log.info(
            "%s 分段 %s PCM 音频早到 copy-video 快路径未命中或未通过，继续原 010 流程 reason=%s classification=%s codec-aware=true",
            _branch_code_tag(CAM_DL_NORM_083),
            part_path.name,
            copy_video_reason,
            precise_classification or "unknown",
        )
        # L2 轻量逃生口（CAM-DL-NORM-080）：在进入 010 完整重建前，针对 audio_early 主导
        # 且无音频包级 PTS 乱序的场景，先尝试一次 itsoffset+零基化的 stream copy 修复。
        # 注意：_probe_timeline_state.stream_gap 在该项目里就是 |audio_start - video_start|，
        # 对 audio_early 场景它必然 == audio_early_by，不能用于判定"流内 PTS 跳变"。
        # 真正的流内乱序由精确对齐探测中的 audio_packet_timeline_disorder 标志承载，
        # 当该标志出现时 classification 会带 *_packet_disorder 后缀，此时直接放行 010。
        light_audio_early_eligible = (
            not significant_audio_late
            and audio_early_by > AUDIO_EARLY_ALIGN_THRESHOLD_SECONDS
            and not source_has_no_audio_stream
            and "packet_disorder" not in (precise_classification or "").lower()
        )
        if light_audio_early_eligible:
            light_reason = _lightweight_audio_early_repair_part(
                rebuild_input_path,
                audio_early_by,
                normalized_part,
                need_zero_based=absolute_start_abnormal,
                on_status=on_status,
                cancel_check=cancel_check,
            )
            if not light_reason:
                log.info(
                    "%s 分段 %s L2 轻量音频早到修复成功，跳过 010 完整重建 audio_early_by=%.3fs absolute_start_abnormal=%s rebuild_input=%s",
                    _branch_code_tag(CAM_DL_NORM_080),
                    part_path.name,
                    audio_early_by,
                    absolute_start_abnormal,
                    rebuild_input_context,
                )
                return DownloadPartNormalizeResult(
                    duration_sec=float(probe_duration_seconds(str(normalized_part)) or 0.0),
                    force_canonical_merge=False,
                    canonicalize_merge_part=False,
                    merge_risk_level=0,
                    normalize_reason=f"lightweight_audio_early:itsoffset={audio_early_by:.3f}:zero_based={absolute_start_abnormal}",
                    merge_part_path=str(normalized_part),
                    classification="aligned",
                )
            log.warning(
                "%s 分段 %s L2 轻量音频早到修复未通过验收，回退 010 完整重建 reason=%s audio_early_by=%.3fs absolute_start_abnormal=%s",
                _branch_code_tag(CAM_DL_NORM_081),
                part_path.name,
                light_reason,
                audio_early_by,
                absolute_start_abnormal,
            )
            if on_status:
                on_status("L2 轻量音频早到修复未通过，正在回退完整重建当前分段")
        rebuilt = _rebuild_download_part_timeline(
            rebuild_input_path,
            precise_trim=precise_trim,
            target_duration_sec=video_duration,
            force_full_rebuild=expected_tail_silence_force_full_rebuild,
            target_video_codec=rebuild_target_video_codec,
            output_path=normalized_part,
            branch_code=CAM_DL_NORM_010,
            on_status=on_status,
            total_elapsed_supplier=total_elapsed_supplier,
            cancel_check=cancel_check,
            user_status="正在完整重建当前分段",
            log_context=f"input={rebuild_input_context} media_start={media_start:.3f}s video={video_start:.3f}s audio={audio_start:.3f}s max_start={max_start:.3f}s start_gap={start_gap:.3f}s stream_gap={stream_gap:.3f}s reason={precise_reason or 'absolute_start'} classification={precise_classification or 'unknown'} absolute_start_abnormal={absolute_start_abnormal}",
        )
        if not rebuilt:
            log.warning(
                "%s 分段 %s 细分重建未通过，回退全量重建兜底 classification=%s video_dur=%.3fs",
                _branch_code_tag(CAM_DL_NORM_010),
                part_path.name,
                precise_classification or "unknown",
                video_duration,
            )
            if on_status:
                on_status("分段细分修复验收未通过，正在执行全量重建兜底")
            rebuilt = _rebuild_download_part_timeline(
                rebuild_input_path,
                precise_trim=precise_trim,
                target_duration_sec=video_duration,
                force_full_rebuild=True,
                target_video_codec=rebuild_target_video_codec,
                output_path=normalized_part,
                branch_code=CAM_DL_NORM_010,
                on_status=on_status,
                total_elapsed_supplier=total_elapsed_supplier,
                cancel_check=cancel_check,
                user_status="正在全量重建当前分段",
                log_context=f"full_rebuild_fallback input={rebuild_input_context} media_start={media_start:.3f}s video={video_start:.3f}s audio={audio_start:.3f}s max_start={max_start:.3f}s start_gap={start_gap:.3f}s stream_gap={stream_gap:.3f}s reason={precise_reason or 'absolute_start'} classification={precise_classification or 'unknown'} absolute_start_abnormal={absolute_start_abnormal}",
            )
        if not rebuilt:
            raise RuntimeError(f"download_part_normalize_failed:{CAM_DL_NORM_010}:{part_path.name}:strong_abnormal_timeline")
        post_issue = _probe_structural_media_issue(normalized_part)
        if post_issue and expected_tail_silence_source and rebuild_input_path == part_path and _is_expected_tail_silence_output(normalized_part):
            log.info(
                "%s 分段 %s 输出存在尾部无声结构 issue=%s，但源分段已确认尾部真实失声，允许参与合并",
                _branch_code_tag(CAM_DL_NORM_010),
                part_path.name,
                post_issue,
            )
            post_issue = ""
        if post_issue:
            raise RuntimeError(f"download_part_structural_invalid:{CAM_DL_NORM_010}:{part_path.name}:{post_issue}")
        force_canonical_merge, canonicalize_merge_part, merge_risk_level = _resolve_download_merge_policy(precise_classification or "strong_abnormal_timeline")
        result_classification = precise_classification
        result_reason = precise_classification or "strong_abnormal_timeline"
        if expected_tail_silence_source and rebuild_input_path == part_path:
            force_canonical_merge = False
            canonicalize_merge_part = False
            merge_risk_level = 0
            result_classification = DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_CLASSIFICATION
            result_reason = DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_CLASSIFICATION
        elif source_has_no_audio_stream and rebuild_input_path == part_path and _source_part_has_no_audio_stream(normalized_part):
            force_canonical_merge = False
            canonicalize_merge_part = False
            merge_risk_level = 0
            result_classification = DOWNLOAD_PART_SOURCE_NO_AUDIO_CLASSIFICATION
            result_reason = DOWNLOAD_PART_SOURCE_NO_AUDIO_CLASSIFICATION
        return DownloadPartNormalizeResult(
            duration_sec=float(probe_duration_seconds(str(normalized_part)) or 0.0),
            force_canonical_merge=force_canonical_merge,
            canonicalize_merge_part=canonicalize_merge_part,
            merge_risk_level=merge_risk_level,
            normalize_reason=result_reason,
            merge_part_path=str(normalized_part),
            classification=result_classification,
        )
    if minor_audio_late or (timeline_dirty and aligned_start and not absolute_start_abnormal):
        log.info(
            "%s 分段 %s 音视频起始偏差可忽略，跳过修复 media_start=%.3fs video=%.3fs audio=%.3fs start_gap=%.3fs stream_gap=%.3fs precise_trim=%.3fs reason=%s classification=%s video_dur=%.3fs audio_dur=%.3fs delta=%.3fs content_offset=%.3fs",
            _branch_code_tag(CAM_DL_NORM_020),
            part_path.name,
            media_start,
            video_start,
            audio_start,
            start_gap,
            stream_gap,
            precise_trim,
            precise_reason,
            precise_classification,
            video_duration,
            audio_duration,
            duration_delta,
            audio_content_offset,
        )
        return DownloadPartNormalizeResult(duration_sec=float(probe_duration_seconds(str(part_path)) or 0.0), merge_part_path=str(part_path), classification=precise_classification)
    if timeline_dirty and stream_gap > TIMELINE_GAP_TOLERANCE_SECONDS and audio_early_by <= AV_SYNC_START_TOLERANCE_SECONDS:
        normalized_part = _normalized_part_output_path(part_path)
        log.info(
            "%s 分段 %s 命中时间线异常修复 media_start=%.3fs video=%.3fs audio=%.3fs start_gap=%.3fs stream_gap=%.3fs precise_trim=%.3fs reason=%s classification=%s video_dur=%.3fs audio_dur=%.3fs delta=%.3fs content_offset=%.3fs，执行完整重建并保留原始分段",
            _branch_code_tag(CAM_DL_NORM_030),
            part_path.name,
            media_start,
            video_start,
            audio_start,
            start_gap,
            stream_gap,
            precise_trim,
            precise_reason,
            precise_classification,
            video_duration,
            audio_duration,
            duration_delta,
            audio_content_offset,
        )
        if on_status:
            on_status(f"检测到分段时间线异常{stream_gap:.1f}s，正在完整重建当前分段")
        rebuilt = _rebuild_download_part_timeline(
            rebuild_input_path,
            precise_trim=precise_trim,
            target_duration_sec=video_duration,
            target_video_codec=batch_target_video_codec,
            output_path=normalized_part,
            branch_code=CAM_DL_NORM_030,
            on_status=on_status,
            total_elapsed_supplier=total_elapsed_supplier,
            cancel_check=cancel_check,
            user_status="正在完整重建当前分段",
            log_context=f"input={rebuild_input_context} media_start={media_start:.3f}s video={video_start:.3f}s audio={audio_start:.3f}s start_gap={start_gap:.3f}s stream_gap={stream_gap:.3f}s reason={precise_reason or 'timeline_gap'} classification={precise_classification or 'unknown'}",
        )
        if not rebuilt:
            log.warning(
                "%s 分段 %s 细分重建未通过，回退全量重建兜底 classification=%s video_dur=%.3fs",
                _branch_code_tag(CAM_DL_NORM_030),
                part_path.name,
                precise_classification or "unknown",
                video_duration,
            )
            if on_status:
                on_status("分段细分修复验收未通过，正在执行全量重建兜底")
            rebuilt = _rebuild_download_part_timeline(
                rebuild_input_path,
                precise_trim=precise_trim,
                target_duration_sec=video_duration,
                force_full_rebuild=True,
                target_video_codec=batch_target_video_codec,
                output_path=normalized_part,
                branch_code=CAM_DL_NORM_030,
                on_status=on_status,
                total_elapsed_supplier=total_elapsed_supplier,
                cancel_check=cancel_check,
                user_status="正在全量重建当前分段",
                log_context=f"full_rebuild_fallback input={rebuild_input_context} media_start={media_start:.3f}s video={video_start:.3f}s audio={audio_start:.3f}s start_gap={start_gap:.3f}s stream_gap={stream_gap:.3f}s reason={precise_reason or 'timeline_gap'} classification={precise_classification or 'unknown'}",
            )
        if not rebuilt:
            raise RuntimeError(f"download_part_normalize_failed:{CAM_DL_NORM_030}:{part_path.name}:timeline_gap_rebuild")
        return DownloadPartNormalizeResult(duration_sec=float(probe_duration_seconds(str(normalized_part)) or 0.0), merge_part_path=str(normalized_part), classification=precise_classification)
    if 0.0 < audio_early_by <= AUDIO_EARLY_ALIGN_THRESHOLD_SECONDS:
        log.info(
            "%s 分段 %s 音频早于视频但偏差可忽略，跳过无损对齐 media_start=%.3fs video=%.3fs audio=%.3fs start_gap=%.3fs stream_gap=%.3fs early_threshold=%.3fs",
            _branch_code_tag(CAM_DL_NORM_040),
            part_path.name,
            media_start,
            video_start,
            audio_start,
            start_gap,
            stream_gap,
            AUDIO_EARLY_ALIGN_THRESHOLD_SECONDS,
        )
        return DownloadPartNormalizeResult(duration_sec=float(probe_duration_seconds(str(part_path)) or 0.0), merge_part_path=str(part_path), classification=precise_classification or "audio_early")
    log.info("%s 分段 %s 音频早于视频 %.3fs (video=%.3fs audio=%.3fs start_gap=%.3fs stream_gap=%.3fs)，执行 itsoffset 无损对齐",
             _branch_code_tag(CAM_DL_NORM_050), part_path.name, audio_early_by, video_start, audio_start, start_gap, stream_gap)
    if on_status:
        on_status(f"检测到音视频偏差{audio_early_by:.1f}s，正在无损对齐")
    normalized_part = _normalized_part_output_path(part_path)
    tmp_out = normalized_part.with_name(normalized_part.stem + ".avsync.tmp.mp4")
    try:
        tmp_out.unlink(missing_ok=True)
    except Exception:
        pass
    abs_gap = audio_early_by
    if audio_early_by > 0:
        cmd = [
            _ffmpeg_bin(), "-y",
            "-i", str(part_path),
            "-itsoffset", f"{abs_gap:.3f}",
            "-i", str(part_path),
            "-map", "0:v:0", "-map", "1:a:0?",
            "-c", "copy",
            "-movflags", "+faststart",
            str(tmp_out),
        ]
    try:
        r = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="ignore")
        _replace_file_or_raise_finalize_pending(
            tmp_out,
            normalized_part,
            log=log,
            action=f"download part avsync normalize {part_path.name} -> {normalized_part.name}",
            user_message="分段对齐完成，正在等待最终提交",
        )
        log.info("%s 分段 %s 音视频已无损对齐 (audio_early_by=%.3fs start_gap=%.3fs stream_gap=%.3fs out=%s)", _branch_code_tag(CAM_DL_NORM_050), part_path.name, audio_early_by, start_gap, stream_gap, str(normalized_part))
        return DownloadPartNormalizeResult(duration_sec=float(probe_duration_seconds(str(normalized_part)) or 0.0), merge_part_path=str(normalized_part), classification=precise_classification or "audio_early", normalize_reason="audio_early_itsoffset")
    except subprocess.CalledProcessError as e:
        tail = (e.stdout or "")[-1200:]
        log.warning(
            "%s 分段 %s 音视频无损对齐失败，回退完整重建: audio_early_by=%.3fs media_start=%.3fs video=%.3fs audio=%.3fs start_gap=%.3fs stream_gap=%.3fs absolute_start_abnormal=%s exit=%s cmd=%s ffmpeg_tail=%s",
            _branch_code_tag(CAM_DL_NORM_051),
            part_path.name,
            audio_early_by,
            media_start,
            video_start,
            audio_start,
            start_gap,
            stream_gap,
            absolute_start_abnormal,
            e.returncode,
            " ".join(cmd),
            tail,
        )
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        if on_status:
            on_status("分段无损对齐失败，正在回退完整重建当前分段")
        rebuilt = _rebuild_download_part_timeline(
            part_path,
            precise_trim=0.0,
            target_video_codec=batch_target_video_codec,
            output_path=normalized_part,
            branch_code=CAM_DL_NORM_051,
            on_status=on_status,
            total_elapsed_supplier=total_elapsed_supplier,
            user_status="正在完整重建当前分段",
            log_context=f"fallback_from_itsoffset media_start={media_start:.3f}s video={video_start:.3f}s audio={audio_start:.3f}s start_gap={start_gap:.3f}s stream_gap={stream_gap:.3f}s absolute_start_abnormal={absolute_start_abnormal}",
        )
        if not rebuilt:
            raise RuntimeError(f"download_part_normalize_failed:{CAM_DL_NORM_051}:{part_path.name}:itsoffset_fallback_rebuild")
        return DownloadPartNormalizeResult(duration_sec=float(probe_duration_seconds(str(normalized_part)) or 0.0), merge_part_path=str(normalized_part), classification=precise_classification or "audio_early", normalize_reason="audio_early_rebuild_fallback")
    except Exception as e:
        if isinstance(e, FinalizePendingError):
            raise
        log.warning("%s 分段 %s 音视频无损对齐异常，回退完整重建: %s", _branch_code_tag(CAM_DL_NORM_052), part_path.name, e)
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        if on_status:
            on_status("分段无损对齐异常，正在回退完整重建当前分段")
        rebuilt = _rebuild_download_part_timeline(
            part_path,
            precise_trim=0.0,
            target_video_codec=batch_target_video_codec,
            output_path=normalized_part,
            branch_code=CAM_DL_NORM_052,
            on_status=on_status,
            total_elapsed_supplier=total_elapsed_supplier,
            user_status="正在完整重建当前分段",
            log_context=f"fallback_from_itsoffset_exception media_start={media_start:.3f}s video={video_start:.3f}s audio={audio_start:.3f}s start_gap={start_gap:.3f}s stream_gap={stream_gap:.3f}s absolute_start_abnormal={absolute_start_abnormal}",
        )
        if not rebuilt:
            raise RuntimeError(f"download_part_normalize_failed:{CAM_DL_NORM_052}:{part_path.name}:itsoffset_exception_rebuild")
        return DownloadPartNormalizeResult(duration_sec=float(probe_duration_seconds(str(normalized_part)) or 0.0), merge_part_path=str(normalized_part), classification=precise_classification or "audio_early", normalize_reason="audio_early_exception_rebuild")
    return DownloadPartNormalizeResult(duration_sec=float(probe_duration_seconds(str(part_path)) or 0.0), merge_part_path=str(part_path), classification=precise_classification)


def _trim_source_video_to_duration(video_path: Path, duration_sec: float) -> bool:
    if float(duration_sec or 0.0) <= 0:
        return False
    if not ffmpeg_exists():
        return False
    tmp_out = video_path.with_name(video_path.stem + ".aligned.mp4")
    try:
        tmp_out.unlink(missing_ok=True)
    except Exception:
        pass
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-i",
        str(video_path),
        "-t",
        f"{float(duration_sec):.3f}",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(tmp_out),
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0 or not tmp_out.exists():
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    _replace_file_or_raise_finalize_pending(
        tmp_out,
        video_path,
        log=logging.getLogger("edge.runner"),
        action=f"trim source video {video_path.name}",
        user_message="视频时长校准完成，正在等待最终提交",
    )
    return True


def _trim_video_head(input_path: Path, trim_sec: float, output_path: Path) -> bool:
    if float(trim_sec or 0.0) <= 0:
        return False
    if not input_path.exists() or not ffmpeg_exists():
        return False
    try:
        output_path.unlink(missing_ok=True)
    except Exception:
        pass
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-i",
        str(input_path),
        "-ss",
        f"{float(trim_sec):.3f}",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0 or not output_path.exists():
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    return True


def _extend_source_video_to_duration(video_path: Path, duration_sec: float, current_duration: float) -> bool:
    if float(duration_sec or 0.0) <= 0 or float(current_duration or 0.0) <= 0:
        return False
    if not ffmpeg_exists():
        return False
    gap = float(duration_sec) - float(current_duration)
    if gap <= 0:
        return False
    tmp_out = video_path.with_name(video_path.stem + ".aligned.mp4")
    try:
        tmp_out.unlink(missing_ok=True)
    except Exception:
        pass
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"tpad=stop_mode=clone:stop_duration={gap:.3f}",
        "-af",
        f"apad=pad_dur={gap:.3f},aformat=sample_rates=48000:channel_layouts=stereo",
        "-t",
        f"{float(duration_sec):.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "256k",
        "-movflags",
        "+faststart",
        str(tmp_out),
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0 or not tmp_out.exists():
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    _replace_file_or_raise_finalize_pending(
        tmp_out,
        video_path,
        log=logging.getLogger("edge.runner"),
        action=f"extend source video {video_path.name}",
        user_message="视频补齐完成，正在等待最终提交",
    )
    return True


def _fmt_elapsed_short(elapsed: float) -> str:
    total = max(0, int(elapsed))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}时{m}分{s}秒"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _is_retryable_file_busy_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        winerror = int(getattr(exc, "winerror", 0) or 0)
        return winerror in {5, 32}
    return False


def _trim_audio_tail_to_video(

    video_path: Path,

    *,

    max_trim_seconds: float = DOWNLOAD_PART_AUDIO_TAIL_TRIM_MAX_SECONDS,

) -> bool:

    metrics = _collect_structural_media_metrics(video_path)

    video_end = float(metrics.get("video_end") or 0.0)

    audio_end = float(metrics.get("audio_end") or 0.0)

    if video_end <= 0.0 or audio_end <= 0.0:

        return False

    tail_gap = audio_end - video_end

    if tail_gap <= 0.0 or tail_gap > float(max_trim_seconds or 0.0) + 0.01:

        return False

    log = logging.getLogger("edge.runner")

    tmp_out = video_path.with_name(video_path.stem + ".tailtrim.tmp.mp4")

    try:

        tmp_out.unlink(missing_ok=True)

    except Exception:

        pass

    cmd = [

        _ffmpeg_bin(),

        "-y",

        "-hide_banner",

        "-loglevel",

        "error",

        "-i",

        str(video_path),

        "-map",

        "0:v:0",

        "-c:v",

        "copy",

        "-map",

        "0:a:0?",

        "-af",

        f"atrim=end={video_end:.3f},asetpts=PTS-STARTPTS",

        "-movflags",

        "+faststart",

        str(tmp_out),

    ]

    try:

        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")

        try:

            video_path.unlink(missing_ok=True)

        except Exception:

            pass

        tmp_out.replace(video_path)

        log.info(

            "download_part_audio_tail_trim_applied video_end=%.3f audio_end=%.3f gap=%.3f out=%s",

            video_end,

            audio_end,

            tail_gap,

            str(video_path),

        )

        return True

    except subprocess.CalledProcessError as e:

        tail = (e.stdout or "")[-800:]

        log.warning(

            "download_part_audio_tail_trim_failed gap=%.3f out=%s cmd=%s ffmpeg_tail=%s",

            tail_gap,

            str(video_path),

            " ".join(cmd),

            tail,

        )

    except Exception as e:

        log.warning("download_part_audio_tail_trim_error gap=%.3f out=%s err=%s", tail_gap, str(video_path), e)

    finally:

        try:

            tmp_out.unlink(missing_ok=True)

        except Exception:

            pass

    return False


def _pad_audio_tail_with_silence_to_video(video_path: Path) -> bool:
    metrics = _collect_structural_media_metrics(video_path, include_packet_metrics=False)
    video_end = float(metrics.get("video_end") or 0.0)
    audio_end = float(metrics.get("audio_end") or 0.0)
    if video_end <= 0.0 or audio_end <= 0.0:
        return False
    gap = video_end - audio_end
    if gap < DOWNLOAD_PART_EXPECTED_TAIL_SILENCE_MIN_SECONDS:
        return False
    audio_info = probe_audio_stream_info(str(video_path))
    audio_codec = str(audio_info.get("codec_name") or "").strip().lower()
    if not audio_codec:
        return False
    try:
        sample_rate = int(audio_info.get("sample_rate") or 32000)
    except Exception:
        sample_rate = 32000
    try:
        channels = int(audio_info.get("channels") or 1)
    except Exception:
        channels = 1
    try:
        bit_rate = int(audio_info.get("bit_rate") or 0)
    except Exception:
        bit_rate = 0
    if sample_rate <= 0:
        sample_rate = 32000
    if channels <= 0:
        channels = 1
    channel_layout = "mono" if channels == 1 else "stereo"
    bitrate = max(64000, min(256000, int(math.ceil(max(bit_rate, 64000) / 32000.0) * 32000)))
    tmp_out = video_path.with_name(video_path.stem + ".tailsilence.tmp.mp4")
    try:
        tmp_out.unlink(missing_ok=True)
    except Exception:
        pass
    log = logging.getLogger("edge.runner")
    filter_complex = (
        f"[0:a:0]aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS[a0];"
        f"[1:a:0]atrim=0:{gap:.3f},asetpts=PTS-STARTPTS[a1];"
        f"[a0][a1]concat=n=2:v=0:a=1,atrim=0:{video_end:.3f},asetpts=PTS-STARTPTS[aout]"
    )
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=channel_layout={channel_layout}:sample_rate={sample_rate}",
        "-filter_complex",
        filter_complex,
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-map",
        "[aout]",
        "-c:a",
        "aac",
        "-b:a",
        f"{int(max(64, round(bitrate / 1000.0)))}k",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-movflags",
        "+faststart",
        str(tmp_out),
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        try:
            video_path.unlink(missing_ok=True)
        except Exception:
            pass
        tmp_out.replace(video_path)
        log.info(
            "download_part_expected_tail_silence_audio_pad_applied video_end=%.3f audio_end=%.3f gap=%.3f out=%s",
            video_end,
            audio_end,
            gap,
            str(video_path),
        )
        return True
    except subprocess.CalledProcessError as e:
        tail = (e.stdout or "")[-800:]
        log.warning(
            "download_part_expected_tail_silence_audio_pad_failed gap=%.3f out=%s cmd=%s ffmpeg_tail=%s",
            gap,
            str(video_path),
            " ".join(cmd),
            tail,
        )
    except Exception as e:
        log.warning(
            "download_part_expected_tail_silence_audio_pad_error gap=%.3f out=%s err=%s",
            gap,
            str(video_path),
            e,
        )
    finally:
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
    return False


def _lightweight_audio_late_repair_part(

    raw_part: Path,

    audio_late_by: float,

    output_path: Path,

    *,

    need_zero_based: bool,

    on_status: Callable[[str], None] | None = None,

    cancel_check: Callable[[], str | None] | None = None,

) -> str:

    """Stream-copy repair for timestamp-only audio late cases."""

    log = logging.getLogger("edge.runner")

    if not raw_part.exists() or raw_part.stat().st_size <= 0:

        return "raw_missing_or_empty"

    if audio_late_by <= 0:

        return "audio_late_by_not_positive"

    if not ffmpeg_exists():

        return "ffmpeg_missing"

    aligned_tmp = output_path.with_name(output_path.stem + ".lightaudiolate.tmp.mp4")

    try:

        aligned_tmp.unlink(missing_ok=True)

    except Exception:

        pass

    offset = -float(audio_late_by)

    cmd = [

        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",

        "-fflags", "+genpts",

        "-i", str(raw_part),

        "-itsoffset", f"{offset:.3f}",

        "-i", str(raw_part),

        "-map", "0:v:0", "-map", "1:a:0?",

        "-c", "copy",

        "-avoid_negative_ts", "make_zero",

        "-reset_timestamps", "1",

        "-movflags", "+faststart",

        str(aligned_tmp),

    ]

    if on_status:

        on_status("命中音频晚到轻量修复，正在提前音轨并归零分段时间线")

    try:

        subprocess.run(

            cmd,

            check=True,

            stdout=subprocess.PIPE,

            stderr=subprocess.STDOUT,

            text=True,

            encoding="utf-8",

            errors="ignore",

        )

    except subprocess.CalledProcessError as e:

        tail = (e.stdout or "")[-1200:]

        log.warning(

            "%s 分段 %s 轻量音频晚到修复 ffmpeg 失败 audio_late_by=%.3fs need_zero_based=%s rc=%s tail=%s",

            _branch_code_tag(CAM_DL_NORM_082),

            raw_part.name,

            audio_late_by,

            need_zero_based,

            e.returncode,

            tail,

        )

        try:

            aligned_tmp.unlink(missing_ok=True)

        except Exception:

            pass

        return f"ffmpeg_failed:rc={e.returncode}"

    except Exception as e:

        log.warning(

            "%s 分段 %s 轻量音频晚到修复 ffmpeg 异常 audio_late_by=%.3fs need_zero_based=%s err=%s",

            _branch_code_tag(CAM_DL_NORM_082),

            raw_part.name,

            audio_late_by,

            need_zero_based,

            e,

        )

        try:

            aligned_tmp.unlink(missing_ok=True)

        except Exception:

            pass

        return f"ffmpeg_exception:{e}"

    if cancel_check:

        cancel_mode = cancel_check()

        if cancel_mode in {"pause", "stop"}:

            try:

                aligned_tmp.unlink(missing_ok=True)

            except Exception:

                pass

            raise RuntimeError(f"cancelled:{cancel_mode}")

    if not aligned_tmp.exists() or aligned_tmp.stat().st_size <= 0:

        return "output_missing"

    structural_issue = _probe_structural_media_issue(aligned_tmp)

    if structural_issue:

        try:

            aligned_tmp.unlink(missing_ok=True)

        except Exception:

            pass

        return f"structural_invalid:{structural_issue}"

    def _second_pass_residual_fix(current_path: Path, residual_gap: float) -> str:

        adjust_tmp = output_path.with_name(output_path.stem + ".lightaudiolate.secondpass.tmp.mp4")

        try:

            adjust_tmp.unlink(missing_ok=True)

        except Exception:

            pass

        offset = -float(residual_gap)

        cmd = [

            _ffmpeg_bin(),

            "-y",

            "-hide_banner",

            "-loglevel",

            "error",

            "-fflags",

            "+genpts",

            "-i",

            str(current_path),

            "-itsoffset",

            f"{offset:.3f}",

            "-i",

            str(current_path),

            "-map",

            "0:v:0",

            "-map",

            "1:a:0?",

            "-c",

            "copy",

            "-avoid_negative_ts",

            "make_zero",

            "-reset_timestamps",

            "1",

            "-movflags",

            "+faststart",

            str(adjust_tmp),

        ]

        try:

            subprocess.run(

                cmd,

                check=True,

                stdout=subprocess.PIPE,

                stderr=subprocess.STDOUT,

                text=True,

                encoding="utf-8",

                errors="ignore",

            )

        except subprocess.CalledProcessError as e:

            tail = (e.stdout or "")[-1200:]

            log.warning(

                "%s 分段 %s 轻量音频晚到二次校准失败 residual_gap=%.3fs rc=%s tail=%s",

                _branch_code_tag(CAM_DL_NORM_082),

                raw_part.name,

                residual_gap,

                e.returncode,

                tail,

            )

            try:

                adjust_tmp.unlink(missing_ok=True)

            except Exception:

                pass

            return f"second_pass_ffmpeg_failed:rc={e.returncode}"

        except Exception as e:

            log.warning(

                "%s 分段 %s 轻量音频晚到二次校准异常 residual_gap=%.3fs err=%s",

                _branch_code_tag(CAM_DL_NORM_082),

                raw_part.name,

                residual_gap,

                e,

            )

            try:

                adjust_tmp.unlink(missing_ok=True)

            except Exception:

                pass

            return f"second_pass_exception:{e}"

        try:

            current_path.unlink(missing_ok=True)

        except Exception:

            pass

        adjust_tmp.replace(current_path)

        return ""



    post_precise = _probe_precise_av_sync(aligned_tmp)

    post_classification = str(post_precise.get("classification") or "").strip()

    post_audio_late_trim = float(post_precise.get("audio_late_trim") or 0.0)

    post_video_start = float(post_precise.get("video_start") or 0.0)

    post_audio_start = float(post_precise.get("audio_start") or 0.0)

    post_start_gap = post_audio_start - post_video_start

    if post_audio_late_trim > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS:

        try:

            aligned_tmp.unlink(missing_ok=True)

        except Exception:

            pass

        return f"post_repair_audio_late:trim={post_audio_late_trim:.3f}:classification={post_classification}"

    if abs(post_start_gap) > AUDIO_EARLY_ALIGN_THRESHOLD_SECONDS:

        if abs(post_start_gap) <= AUDIO_LATE_LIGHT_SECONDARY_CORRECTION_MAX_SECONDS:

            log.info(

                "%s 分段 %s 轻量音频晚到残余起点偏差 %.3fs，执行二次 itsoffset 校准",

                _branch_code_tag(CAM_DL_NORM_082),

                raw_part.name,

                post_start_gap,

            )

            if on_status:

                on_status("轻量修复残余偏差已检测到，正在追加一次 itsoffset 校准")

            residual_issue = _second_pass_residual_fix(aligned_tmp, post_start_gap)

            if residual_issue:

                try:

                    aligned_tmp.unlink(missing_ok=True)

                except Exception:

                    pass

                return f"post_repair_secondary_failed:{residual_issue}"

            post_precise = _probe_precise_av_sync(aligned_tmp)

            post_classification = str(post_precise.get("classification") or "").strip()

            post_audio_late_trim = float(post_precise.get("audio_late_trim") or 0.0)

            post_video_start = float(post_precise.get("video_start") or 0.0)

            post_audio_start = float(post_precise.get("audio_start") or 0.0)

            post_start_gap = post_audio_start - post_video_start

        if post_audio_late_trim > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS or abs(post_start_gap) > AUDIO_EARLY_ALIGN_THRESHOLD_SECONDS:

            try:

                aligned_tmp.unlink(missing_ok=True)

            except Exception:

                pass

            if post_audio_late_trim > AUDIO_LATE_REBUILD_THRESHOLD_SECONDS:

                return f"post_repair_audio_late:trim={post_audio_late_trim:.3f}:classification={post_classification}"

            return f"post_repair_misaligned:start_gap={post_start_gap:.3f}:classification={post_classification}"

    candidate = aligned_tmp

    zero_tmp = None

    if need_zero_based:

        zero_tmp = output_path.with_name(output_path.stem + ".lightaudiolate.zerobased.tmp.mp4")

        try:

            zero_tmp.unlink(missing_ok=True)

        except Exception:

            pass

        zero_allowed = _can_zero_based_copy_to_mp4(candidate)

        if zero_allowed and _zero_based_remux_part(candidate, zero_tmp):

            candidate = zero_tmp

        else:

            log.warning(

                "%s 分段 %s 轻量音频晚到修复无法零基化 zero_allowed=%s",

                _branch_code_tag(CAM_DL_NORM_082),

                raw_part.name,

                zero_allowed,

            )

    rebuild_issue = _validate_download_part_rebuild_output(

        candidate,

        extra_start_tolerance=AUDIO_LATE_LIGHT_EXTRA_START_TOLERANCE_SECONDS,

    )

    if rebuild_issue:

        try:

            candidate.unlink(missing_ok=True)

        except Exception:

            pass

        if zero_tmp and zero_tmp.exists():

            zero_tmp.unlink(missing_ok=True)

        try:

            aligned_tmp.unlink(missing_ok=True)

        except Exception:

            pass

        return f"rebuild_validation_failed:{rebuild_issue}"

    try:

        _replace_file_or_raise_finalize_pending(

            candidate,

            output_path,

            log=log,

            action=f"download part lightweight audio_late repair {raw_part.name} -> {output_path.name}",

            user_message="音频晚到轻量修复完成，正在等待最终提交",

        )

    except FinalizePendingError:

        raise

    except Exception as e:

        try:

            candidate.unlink(missing_ok=True)

        except Exception:

            pass

        return f"finalize_failed:{e}"

    finally:

        if aligned_tmp.exists() and aligned_tmp != output_path:

            aligned_tmp.unlink(missing_ok=True)

        if zero_tmp and zero_tmp.exists() and zero_tmp != output_path:

            zero_tmp.unlink(missing_ok=True)

    log.info(

        "%s 分段 %s 音频晚到轻量修复完成 audio_late_by=%.3fs need_zero_based=%s out=%s post_classification=%s post_start_gap=%.3fs",

        _branch_code_tag(CAM_DL_NORM_082),

        raw_part.name,

        audio_late_by,

        need_zero_based,

        str(output_path),

        post_classification or "aligned",

        post_start_gap,

    )

    return ""


def _source_part_has_no_audio_stream(
    part_path: Path,
    *,
    precise_state: dict[str, float | bool | str] | None = None,
) -> bool:
    audio_info = probe_audio_stream_info(str(part_path))
    if str(audio_info.get("codec_name") or "").strip().lower():
        return False
    if precise_state is None:
        return True
    try:
        return float(precise_state.get("audio_duration") or 0.0) <= 0.0
    except Exception:
        return True


def _replace_file_with_retry(src_path: Path, dst_path: Path, *, log: logging.Logger, action: str, attempts: int = 12, delay_sec: float = 0.5) -> None:
    src = Path(src_path)
    dst = Path(dst_path)
    last_error: BaseException | None = None
    for idx in range(max(1, int(attempts))):
        target_exists = dst.exists()
        try:
            if target_exists:
                dst.unlink(missing_ok=True)
            src.replace(dst)
            if idx > 0:
                log.info("%s file replace succeeded after retry=%s src=%s dst=%s", action, idx + 1, str(src), str(dst))
            return
        except Exception as e:
            last_error = e
            if not _is_retryable_file_busy_error(e) or idx >= int(attempts) - 1:
                raise
            log.warning(
                "%s file replace busy retry=%s/%s src=%s dst=%s dst_exists=%s err=%s",
                action,
                idx + 1,
                int(attempts),
                str(src),
                str(dst),
                target_exists,
                e,
            )
            time.sleep(max(0.1, float(delay_sec)))
    if last_error is not None:
        raise last_error


def _replace_file_or_raise_finalize_pending(
    src_path: Path,
    dst_path: Path,
    *,
    log: logging.Logger,
    action: str,
    step_code: str = "DOWNLOAD",
    user_message: str = "文件暂时被占用，系统将自动重试最终提交",
) -> None:
    try:
        _replace_file_with_retry(src_path, dst_path, log=log, action=action)
    except Exception as e:
        if _is_retryable_file_busy_error(e):
            raise FinalizePendingError(
                step_code=step_code,
                src_path=str(src_path),
                dst_path=str(dst_path),
                action=action,
                reason=str(e),
                user_message=user_message,
            ) from e
        raise


def _run_with_status_heartbeat(
    label: str,
    action: Callable[[], Any],
    on_status: Callable[[str], None] | None,
    total_elapsed_supplier: Callable[[], float] | None = None,
    cancel_check: Callable[[], str | None] | None = None,
) -> Any:
    if on_status is None:
        return action()
    box: dict[str, Any] = {"done": False, "result": None, "error": None}

    def _worker() -> None:
        try:
            box["result"] = action()
        except Exception as e:
            box["error"] = e
        finally:
            box["done"] = True

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    started = time.monotonic()
    while True:
        th.join(timeout=1.0)
        elapsed_str = _fmt_elapsed_short(time.monotonic() - started)
        status_label = label
        if cancel_check is not None and not box["done"]:
            try:
                cancel_mode = str(cancel_check() or "").strip().lower()
            except Exception:
                cancel_mode = ""
            if cancel_mode in {"pause", "stop"}:
                status_label = f"{label}，正在响应{('暂停' if cancel_mode == 'pause' else '终止')}请求"
        if total_elapsed_supplier is not None:
            try:
                total_elapsed = float(total_elapsed_supplier() or 0.0)
            except Exception:
                total_elapsed = 0.0
            total_elapsed_str = _fmt_elapsed_short(total_elapsed)
            on_status(f"{status_label}，已耗时{elapsed_str}，共耗时{total_elapsed_str}")
        else:
            on_status(f"{status_label}，已耗时{elapsed_str}")
        if box["done"]:
            break
    if box["error"] is not None:
        raise box["error"]
    return box["result"]


def _rebuild_nvr_skew_merged_audio_from_raw_parts(clean_video_path: Path, raw_part_paths: list[Path], output_path: Path) -> bool:
    if not clean_video_path.exists() or not raw_part_paths or not ffmpeg_exists():
        return False
    target_duration = float(probe_duration_seconds(str(clean_video_path)) or 0.0)
    if target_duration <= 0.0:
        target_duration = float(probe_authoritative_duration_seconds(str(clean_video_path)) or 0.0)
    if target_duration <= 0.0:
        return False
    existing_parts = [p for p in raw_part_paths if p.exists() and p.stat().st_size > 0]
    if not existing_parts:
        return False
    tmp_out = output_path.with_name(output_path.stem + ".nvrskew_audio.tmp.mp4")
    try:
        tmp_out.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
    except Exception:
        pass
    cmd: list[str] = [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "warning", "-i", str(clean_video_path)]
    for part in existing_parts:
        cmd.extend(["-i", str(part)])
    filter_parts: list[str] = []
    labels: list[str] = []
    for idx in range(1, len(existing_parts) + 1):
        label = f"a{idx}"
        filter_parts.append(
            f"[{idx}:a:0]aresample=async=1:first_pts=0,"
            f"aformat=sample_fmts=fltp:channel_layouts=mono:sample_rates=32000,"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
    filter_parts.append(
        "".join(labels)
        + f"concat=n={len(labels)}:v=0:a=1,atrim=0:{target_duration:.6f},asetpts=PTS-STARTPTS[aout]"
    )
    cmd.extend([
        "-filter_complex", ";".join(filter_parts),
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-ar", "32000",
        "-ac", "1",
        "-b:a", "96k",
        "-movflags", "+faststart",
        str(tmp_out),
    ])
    log = logging.getLogger("edge.runner")
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        issue = _probe_structural_media_issue(tmp_out)
        if issue:
            raise RuntimeError(f"nvr_skew_audio_rebuild_structural_issue:{issue}")
        _replace_file_or_raise_finalize_pending(
            tmp_out,
            output_path,
            log=log,
            action=f"nvr skew merged audio rebuild {clean_video_path.name} -> {output_path.name}",
            user_message="NVR音频时间线修复完成，正在等待最终提交",
        )
        return True
    except Exception as e:
        if isinstance(e, FinalizePendingError):
            raise
        log.warning("nvr skew merged audio rebuild failed video=%s out=%s err=%s", str(clean_video_path), str(output_path), e)
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _nvr_skew_expected_video_duration(raw_part_paths: list[Path]) -> float:
    total = 0.0
    for part in raw_part_paths:
        if not part.exists() or part.stat().st_size <= 0:
            continue
        timings = probe_stream_timings(str(part))
        video_timing = timings.get("video") or {}
        video_duration = float(video_timing.get("duration") or 0.0)
        if video_duration <= 0.0:
            video_duration = float(probe_authoritative_duration_seconds(str(part)) or 0.0)
        total += max(0.0, video_duration)
    return total


def _nvr_skew_output_high_risk(output_path: Path, raw_part_paths: list[Path]) -> str:
    if not output_path.exists():
        return "missing_output"
    expected_video_duration = _nvr_skew_expected_video_duration(raw_part_paths)
    timings = probe_stream_timings(str(output_path))
    video_timing = timings.get("video") or {}
    audio_timing = timings.get("audio") or {}
    output_video_duration = float(video_timing.get("duration") or 0.0)
    output_audio_duration = float(audio_timing.get("duration") or 0.0)
    if expected_video_duration > 0.0 and output_video_duration > 0.0:
        delta = output_video_duration - expected_video_duration
        if abs(delta) > max(5.0, expected_video_duration * 0.005):
            return f"video_duration_deviates_from_raw_parts:output={output_video_duration:.3f}:expected={expected_video_duration:.3f}:delta={delta:.3f}"
    if output_video_duration > 0.0 and output_audio_duration > 0.0:
        av_delta = output_audio_duration - output_video_duration
        if abs(av_delta) > max(5.0, output_video_duration * 0.005):
            return f"audio_video_duration_delta_abnormal:video={output_video_duration:.3f}:audio={output_audio_duration:.3f}:delta={av_delta:.3f}"
    return ""


def _build_nvr_skew_video_scaffold_part_for_duration(input_path: Path, output_path: Path, duration_sec: float) -> bool:
    if not input_path.exists() or not ffmpeg_exists() or duration_sec <= 0.0:
        return False
    try:
        output_path.unlink(missing_ok=True)
    except Exception:
        pass
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts",
        "-i",
        str(input_path),
        "-f",
        "lavfi",
        "-t",
        f"{duration_sec:.6f}",
        "-i",
        "anullsrc=r=32000:cl=mono",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-ar",
        "32000",
        "-ac",
        "1",
        "-b:a",
        "64k",
        "-shortest",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        return output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _extract_nvr_skew_audio_part_for_duration(input_path: Path, output_path: Path, duration_sec: float) -> bool:
    if not input_path.exists() or not ffmpeg_exists() or duration_sec <= 0.0:
        return False
    try:
        output_path.unlink(missing_ok=True)
    except Exception:
        pass
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(input_path),
        "-vn",
        "-af",
        f"aresample=async=1:first_pts=0,aformat=sample_fmts=fltp:channel_layouts=mono:sample_rates=32000,atrim=0:{duration_sec:.6f},asetpts=PTS-STARTPTS",
        "-c:a",
        "aac",
        "-ar",
        "32000",
        "-ac",
        "1",
        "-b:a",
        "96k",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        return output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _concat_copy_media_files(inputs: list[Path], output_path: Path) -> bool:
    if not inputs:
        return False
    list_path = output_path.with_name(output_path.stem + ".concat.txt")
    try:
        list_path.write_text("\n".join(["file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'" for p in inputs]), encoding="utf-8", errors="ignore")
        subprocess.run(
            [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "warning", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", "-movflags", "+faststart", str(output_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return output_path.exists() and output_path.stat().st_size > 0
    finally:
        try:
            list_path.unlink(missing_ok=True)
        except Exception:
            pass


def _rebuild_nvr_skew_merged_by_raw_video_duration(raw_part_paths: list[Path], output_path: Path) -> tuple[bool, list[Path]]:
    if not raw_part_paths or not ffmpeg_exists():
        return False, []
    log = logging.getLogger("edge.runner")
    temp_files: list[Path] = []
    existing_parts = [p for p in raw_part_paths if p.exists() and p.stat().st_size > 0]
    if not existing_parts:
        return False, temp_files
    work_prefix = output_path.with_suffix("")
    video_parts: list[Path] = []
    audio_parts: list[Path] = []
    try:
        output_path.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        for idx, part in enumerate(existing_parts, start=1):
            timings = probe_stream_timings(str(part))
            video_timing = timings.get("video") or {}
            duration_sec = float(video_timing.get("duration") or 0.0)
            if duration_sec <= 0.0:
                duration_sec = float(probe_authoritative_duration_seconds(str(part)) or 0.0)
            if duration_sec <= 0.0:
                raise RuntimeError(f"bad_part_video_duration:{part.name}")
            video_part = output_path.with_name(f"{work_prefix.name}.part{idx:03d}.video_scaffold.mp4")
            audio_part = output_path.with_name(f"{work_prefix.name}.part{idx:03d}.audio_norm.m4a")
            if not _build_nvr_skew_video_scaffold_part_for_duration(part, video_part, duration_sec):
                raise RuntimeError(f"video_scaffold_failed:{part.name}")
            if not _extract_nvr_skew_audio_part_for_duration(part, audio_part, duration_sec):
                raise RuntimeError(f"audio_extract_failed:{part.name}")
            video_parts.append(video_part)
            audio_parts.append(audio_part)
            temp_files.extend([video_part, audio_part])
        merged_video = output_path.with_name(f"{work_prefix.name}.video_merged.mp4")
        merged_audio = output_path.with_name(f"{work_prefix.name}.audio_merged.m4a")
        temp_files.extend([merged_video, merged_audio])
        if not _concat_copy_media_files(video_parts, merged_video):
            raise RuntimeError("video_concat_failed")
        if not _concat_copy_media_files(audio_parts, merged_audio):
            raise RuntimeError("audio_concat_failed")
        tmp_out = output_path.with_name(output_path.stem + ".tmp.mp4")
        temp_files.append(tmp_out)
        subprocess.run(
            [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "warning", "-i", str(merged_video), "-i", str(merged_audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(tmp_out)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        issue = _probe_structural_media_issue(tmp_out)
        if issue:
            raise RuntimeError(f"nvr_skew_video_duration_rebuild_structural_issue:{issue}")
        _replace_file_or_raise_finalize_pending(
            tmp_out,
            output_path,
            log=log,
            action=f"nvr skew video-duration rebuild -> {output_path.name}",
            user_message="NVR高风险音视频时间线修复完成，正在等待最终提交",
        )
        return output_path.exists() and output_path.stat().st_size > 0, temp_files
    except Exception as e:
        if isinstance(e, FinalizePendingError):
            raise
        log.warning("nvr skew video-duration rebuild failed out=%s err=%s", str(output_path), e)
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False, temp_files


def _build_nvr_skew_video_scaffold_part(input_path: Path, output_path: Path) -> bool:
    if not input_path.exists() or not ffmpeg_exists():
        return False
    duration_sec = float(probe_duration_seconds(str(input_path)) or 0.0)
    if duration_sec <= 0.0:
        duration_sec = float(probe_authoritative_duration_seconds(str(input_path)) or 0.0)
    if duration_sec <= 0.0:
        return False
    try:
        output_path.unlink(missing_ok=True)
    except Exception:
        pass
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts",
        "-i",
        str(input_path),
        "-f",
        "lavfi",
        "-t",
        f"{duration_sec:.6f}",
        "-i",
        "anullsrc=r=32000:cl=mono",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-ar",
        "32000",
        "-ac",
        "1",
        "-b:a",
        "64k",
        "-shortest",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        return output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _normalize_source_video(
    downloaded: Path, target_duration_sec: float | None = None, on_status: Callable[[str], None] | None = None, on_observation: Callable[[dict[str, Any]], None] | None = None) -> float:
    if not downloaded.exists():
        return 0.0
    log = logging.getLogger("edge.runner")
    duration_reencoded = False
    structure_issue = _probe_structural_media_issue(downloaded)
    if structure_issue and ffmpeg_exists():
        log.warning("%s 源视频结构异常，执行完整重建: %s file=%s", _branch_code_tag(CAM_DL_SRC_020), structure_issue, str(downloaded))
        _emit_phase5_observation(on_observation, phase="source_finalize", event="fallback_entered", branch_code=CAM_DL_SRC_020, reason=structure_issue, source=str(downloaded))
        try:
            _normalize_source_timeline(downloaded, on_status, status_label="视频下载完成，正在重建异常源视频", force=True)
        except FinalizePendingError:
            raise
        except Exception as e:
            log.warning("%s 源视频结构异常重建失败，继续后续流程: %s issue=%s", _branch_code_tag(CAM_DL_SRC_021), e, structure_issue)
            _emit_phase5_observation(on_observation, phase="source_finalize", event="fallback_failed", branch_code=CAM_DL_SRC_021, reason=structure_issue, error=str(e), source=str(downloaded))
    try:
        _normalize_source_timeline(downloaded, on_status)
    except FinalizePendingError:
        raise
    except Exception as e:
        log.warning("%s 源视频预校准时间轴失败，继续后续流程: %s", _branch_code_tag(CAM_DL_SRC_030), e)
        _emit_phase5_observation(on_observation, phase="source_finalize", event="fallback_failed", branch_code=CAM_DL_SRC_030, reason="timeline_pre_normalize_failed", error=str(e), source=str(downloaded))
    current_duration = float(probe_duration_seconds(str(downloaded)) or 0.0)
    canonical_target = _canonicalize_duration_sec(float(target_duration_sec), downloaded) if target_duration_sec else current_duration
    if canonical_target and _duration_adjustment_needed(current_duration, canonical_target, downloaded) and current_duration > canonical_target:
        ok = bool(_run_with_status_heartbeat(
            "分段合并完成，正在校准视频时长",
            lambda: _trim_source_video_to_duration(downloaded, float(canonical_target)),
            on_status,
        ))
        if ok:
            duration_reencoded = True
            current_duration = float(probe_duration_seconds(str(downloaded)) or 0.0)
            log.info("%s 源视频已按课次基准时长对齐: %s -> %.3fs", _branch_code_tag(CAM_DL_SRC_040), str(downloaded), current_duration)
            _emit_phase5_observation(on_observation, phase="source_finalize", event="duration_adjusted", branch_code=CAM_DL_SRC_040, reason="trim_to_canonical_target", current_duration=current_duration, target_duration=float(canonical_target), source=str(downloaded))
    elif canonical_target and _duration_adjustment_needed(current_duration, canonical_target, downloaded) and current_duration < canonical_target:
        ok = bool(_run_with_status_heartbeat(
            "分段合并完成，正在校准视频时长",
            lambda: _extend_source_video_to_duration(downloaded, float(canonical_target), current_duration),
            on_status,
        ))
        if ok:
            duration_reencoded = True
            current_duration = float(probe_duration_seconds(str(downloaded)) or 0.0)
            log.info("%s 源视频已补齐到课次基准时长: %s -> %.3fs", _branch_code_tag(CAM_DL_SRC_041), str(downloaded), current_duration)
            _emit_phase5_observation(on_observation, phase="source_finalize", event="duration_adjusted", branch_code=CAM_DL_SRC_041, reason="extend_to_canonical_target", current_duration=current_duration, target_duration=float(canonical_target), source=str(downloaded))
    faststart_already_applied = bool(duration_reencoded)
    if ffmpeg_exists() and downloaded.exists() and not faststart_already_applied and canonical_target and _duration_adjustment_needed(current_duration, canonical_target, downloaded):
        tmp_out = downloaded.with_name(downloaded.stem + ".faststart.mp4")
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            _run_with_status_heartbeat(
                "视频时长校准完成，正在优化封装",
                lambda: remux_faststart(str(downloaded), str(tmp_out)),
                on_status,
            )
            _replace_file_or_raise_finalize_pending(
                tmp_out,
                downloaded,
                log=log,
                action=f"source faststart finalize {downloaded.name}",
                user_message="视频封装优化完成，正在等待最终提交",
            )
            current_duration = float(probe_duration_seconds(str(downloaded)) or current_duration)
            log.info("%s 源视频已重封装为faststart: %s", _branch_code_tag(CAM_DL_SRC_050), str(downloaded))
            _emit_phase5_observation(on_observation, phase="source_finalize", event="faststart_applied", branch_code=CAM_DL_SRC_050, reason="duration_adjustment_remaining", source=str(downloaded))
        except Exception as e:
            if isinstance(e, FinalizePendingError):
                raise
            try:
                tmp_out.unlink(missing_ok=True)
            except Exception:
                pass
            log.warning("%s 源视频faststart重封装失败: %s", _branch_code_tag(CAM_DL_SRC_051), e)
            _emit_phase5_observation(on_observation, phase="source_finalize", event="fallback_failed", branch_code=CAM_DL_SRC_051, reason="faststart_failed", error=str(e), source=str(downloaded))
    final_duration = float(probe_duration_seconds(str(downloaded)) or current_duration)
    if canonical_target and _duration_adjustment_needed(final_duration, canonical_target, downloaded) and final_duration > canonical_target:
        if _trim_source_video_to_duration(downloaded, float(canonical_target)):
            final_duration = float(probe_duration_seconds(str(downloaded)) or canonical_target)
    elif canonical_target and _duration_adjustment_needed(final_duration, canonical_target, downloaded) and final_duration < canonical_target:
        if _extend_source_video_to_duration(downloaded, float(canonical_target), final_duration):
            final_duration = float(probe_duration_seconds(str(downloaded)) or canonical_target)
    return final_duration


def _align_to_lesson_sync(
    db_path: str | None,
    lesson_id: str,
    task_type: int,
    requested_start: datetime,
    requested_end: datetime,
) -> tuple[datetime, datetime]:
    """
    对齐下载时间范围到课次同步信息。
    如果是taskType=1，返回原始请求时间。
    如果是taskType=2/3，尝试对齐到taskType=1的时间范围。
    """
    if task_type == 1:
        return requested_start, requested_end

    sync_info = _load_lesson_sync_info(db_path, lesson_id)
    if sync_info is None:
        logging.getLogger("edge.runner").info(
            "课次%s无taskType=1同步信息，使用原始请求时间", lesson_id
        )
        return requested_start, requested_end

    sync_start_str = sync_info.get("start_at")
    sync_end_str = sync_info.get("end_at")

    if not sync_start_str or not sync_end_str:
        return requested_start, requested_end

    try:
        sync_start = datetime.fromisoformat(sync_start_str)
        sync_end = datetime.fromisoformat(sync_end_str)
    except Exception:
        return requested_start, requested_end

    log = logging.getLogger("edge.runner")
    log.info(
        "课次%s taskType=%s 对齐到taskType=1时间范围: %s ~ %s (原始请求: %s ~ %s)",
        lesson_id,
        task_type,
        sync_start.isoformat(),
        sync_end.isoformat(),
        requested_start.isoformat(),
        requested_end.isoformat(),
    )

    return sync_start, sync_end


class _SkipSystransAdmission(Exception):
    """内部信号：systrans 输出虽然时间戳合法，但 raw 存在内容级偏移，跳过准入直接进入精确重建。"""
    pass


class PauseRequested(Exception):
    pass


class StopRequested(Exception):
    pass


def _is_recoverable_download_interruption(message: str) -> bool:
    text = str(message or "").strip()
    lowered = text.lower()
    if text.startswith("NVR设备掉线或网络不可达") or text.startswith("NVR设备连接失败"):
        return True
    if lowered.startswith("nvr_connect_failed:"):
        return True
    return any(token in lowered for token in ("timeout", "timed out", "refused", "unreachable", "network", "connect", "socket"))


def _transcode_state_path(hls_dir: Path) -> Path:
    return hls_dir / ".transcode_state.json"


def _count_hls_segments(hls_dir: Path) -> int:
    try:
        return sum(1 for f in hls_dir.iterdir() if f.suffix == ".ts")
    except Exception:
        return 0


def _load_transcode_state(hls_dir: Path) -> dict[str, Any]:
    p = _transcode_state_path(hls_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="ignore") or "{}") or {}
    except Exception:
        return {}


def _save_transcode_state(hls_dir: Path, st: dict[str, Any]) -> None:
    try:
        p = _transcode_state_path(hls_dir)
        p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8", errors="ignore")
    except Exception:
        pass


def _source_signature(video_path: Path) -> dict[str, Any]:
    try:
        st = video_path.stat()
        mtime_ns = int(getattr(st, "st_mtime_ns", int(float(st.st_mtime) * 1_000_000_000)))
        return {"source_size": int(st.st_size), "source_mtime_ns": mtime_ns}
    except Exception:
        return {}


def _clear_dir_contents(dir_path: Path) -> None:
    if not dir_path.exists() or not dir_path.is_dir():
        return
    for child in dir_path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _prepare_transcode_dir(hls_dir: Path, video_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source_sig = _source_signature(video_path)
    hls_dir.mkdir(parents=True, exist_ok=True)
    state = _load_transcode_state(hls_dir)
    same_source = bool(state) and all(state.get(k) == v for k, v in source_sig.items())
    has_payload = any(True for _ in hls_dir.iterdir())
    if has_payload and not same_source:
        _clear_dir_contents(hls_dir)
        state = {}
    return state, source_sig


def _load_hls_segment_durations(playlist: Path) -> dict[str, float]:
    if not playlist.exists():
        return {}
    durations: dict[str, float] = {}
    pending: float | None = None
    for raw in playlist.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = str(raw or "").strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending = float(line.split(":", 1)[1].split(",", 1)[0].strip())
            except Exception:
                pending = None
            continue
        if line.startswith("#"):
            continue
        if pending is not None and pending > 0:
            durations[line] = float(pending)
        pending = None
    return durations


def _rewrite_hls_vod_playlist(hls_dir: Path, expected_total_sec: float | None = None, finalize: bool = True) -> Path:
    playlist = hls_dir / "index.m3u8"
    seg_files = sorted(hls_dir.glob("seg_*.ts"))
    if not seg_files:
        raise RuntimeError(f"hls_segments_missing:{hls_dir}")
    existing_durations = _load_hls_segment_durations(playlist)
    durations: list[float] = []
    target_duration = 1
    for seg in seg_files:
        dur = float(existing_durations.get(seg.name) or 0.0)
        if dur <= 0 or float(dur).is_integer():
            probed = float(probe_duration_seconds(str(seg)) or 0.0)
            if probed > 0:
                dur = probed
        if dur <= 0:
            dur = 10.0
        durations.append(dur)
        target_duration = max(target_duration, int(math.ceil(dur)))
    total_duration = sum(durations)
    try:
        expected = float(expected_total_sec or 0.0)
    except Exception:
        expected = 0.0
    if expected > 0 and total_duration > expected and durations:
        scale = max(0.0, float(expected) / float(total_duration))
        durations = [max(0.001, float(d) * scale) for d in durations]
        total_duration = sum(durations)
        target_duration = max(1, max(int(math.ceil(d)) for d in durations))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD" if finalize else "#EXT-X-PLAYLIST-TYPE:EVENT",
    ]
    for seg, dur in zip(seg_files, durations):
        lines.append(f"#EXTINF:{dur:.6f},")
        lines.append(seg.name)
    if finalize:
        lines.append("#EXT-X-ENDLIST")
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8", errors="ignore")
    return playlist


def _probe_hls_resume_state(
    hls_dir: Path,
    fallback_last_sec: float = 0.0,
    fallback_start_number: int = 0,
    expected_total_sec: float | None = None,
) -> tuple[float, int, int]:
    seg_files = sorted(hls_dir.glob("seg_*.ts"))
    if not seg_files:
        return float(max(0.0, fallback_last_sec or 0.0)), int(max(0, fallback_start_number or 0)), 0
    existing_durations = _load_hls_segment_durations(hls_dir / "index.m3u8")
    total_sec = 0.0
    last_seq = -1
    count = 0
    for seg in seg_files:
        try:
            seq = int(seg.stem.split("_")[-1])
        except Exception:
            seq = last_seq + 1
        dur = float(existing_durations.get(seg.name) or 0.0)
        if dur <= 0:
            dur = float(probe_duration_seconds(str(seg)) or 0.0)
        if dur <= 0:
            dur = 10.0
        total_sec += dur
        last_seq = max(last_seq, seq)
        count += 1
    try:
        expected = float(expected_total_sec or 0.0)
    except Exception:
        expected = 0.0
    if expected > 0:
        total_sec = min(total_sec, expected)
    return total_sec, max(last_seq + 1, count), count


def _summarize_hls_output(hls_dir: Path) -> tuple[int, float]:
    seg_files = sorted(hls_dir.glob("seg_*.ts"))
    if not seg_files:
        return 0, 0.0
    existing_durations = _load_hls_segment_durations(hls_dir / "index.m3u8")
    total_sec = 0.0
    count = 0
    for seg in seg_files:
        dur = float(existing_durations.get(seg.name) or 0.0)
        if dur <= 0:
            dur = float(probe_duration_seconds(str(seg)) or 0.0)
        if dur <= 0:
            dur = 10.0
        total_sec += dur
        count += 1
    return count, float(total_sec)


def _ensure_hls_output_complete(hls_dir: Path, expected_total_sec: float) -> tuple[int, float]:
    seg_count, total_sec = _summarize_hls_output(hls_dir)
    expected = max(0.0, float(expected_total_sec or 0.0))
    if seg_count <= 0:
        raise RuntimeError(f"hls_transcode_incomplete:no_segments:{hls_dir}")
    if expected > 0 and total_sec + TRANSCODE_COMPLETE_TOLERANCE_SECONDS < expected:
        raise RuntimeError(
            f"hls_transcode_incomplete:segments={seg_count}:duration={total_sec:.3f}:expected={expected:.3f}"
        )
    return seg_count, total_sec


async def _do_hls_transcode(
    downloaded: Path,
    out_dir: Path,
    raw: dict[str, Any],
    on_progress: Callable,
    server_task_id: str,
    log_prefix: str = "transcode",
) -> tuple[str, int, int]:
    """执行 HLS 转码，返回 (m3u8_path, dl_size, tc_size)。
    抛出 PauseRequested / StopRequested / RuntimeError。
    """
    log = logging.getLogger("edge.runner")
    src_bps = probe_bitrate_bps(str(downloaded))
    source_dur = float(probe_authoritative_duration_seconds(str(downloaded)) or 0.0)
    target_dur = float(source_dur)
    task_type = int(raw.get("taskType") or 0)
    aligned_hls = abs(float(target_dur) - float(source_dur)) > 0.001
    align_mode = "none"
    if aligned_hls:
        align_mode = "trim" if target_dur < source_dur else "pad"
    t_bps = int(src_bps * 0.3) if src_bps > 0 else 1_800_000

    def _fmt_bps(bps: int) -> str:
        return f"{int(bps/1000)}k" if bps < 1_000_000 else f"{round(bps/1_000_000,2)}M"

    maxrate = _fmt_bps(t_bps)
    bufsize = _fmt_bps(t_bps * 2)
    video_stem = downloaded.stem
    audio_info = probe_audio_stream_info(str(downloaded))
    audio_codec = str(audio_info.get("codec_name") or "").strip().lower()
    try:
        audio_sample_rate = int(audio_info.get("sample_rate") or 0)
    except Exception:
        audio_sample_rate = 0
    try:
        audio_channels = int(audio_info.get("channels") or 0)
    except Exception:
        audio_channels = 0
    hls_dir = out_dir / f"{video_stem}_1080P"
    state, source_sig = _prepare_transcode_dir(hls_dir, downloaded)
    state_align_mode = str(state.get("align_mode") or "none").strip().lower() if isinstance(state, dict) else "none"
    try:
        state_output_dur = float(state.get("output_duration_sec") or 0.0) if isinstance(state, dict) else 0.0
    except Exception:
        state_output_dur = 0.0
    state_duration_mismatch = (
        state_output_dur > 0.0
        and target_dur > 0.0
        and abs(float(state_output_dur) - float(target_dur)) > TRANSCODE_COMPLETE_TOLERANCE_SECONDS
    )
    reusable_trim_resume = align_mode == "trim" and state_align_mode == "trim" and abs(float(state_output_dur) - float(target_dur)) <= 0.001
    if any(True for _ in hls_dir.iterdir()) and (state_duration_mismatch or align_mode == "pad" or (aligned_hls and not reusable_trim_resume)):
        if state_duration_mismatch:
            log.info(
                "%s clear stale hls state task=%s prev_output_dur=%.3f new_target_dur=%.3f out_dir=%s",
                log_prefix,
                server_task_id,
                state_output_dur,
                target_dur,
                str(hls_dir),
            )
        _clear_dir_contents(hls_dir)
        state = {}
    seg_time = 10
    expected_segs = max(1, int(target_dur / seg_time) + (1 if target_dur % seg_time > 0 else 0)) if target_dur > 0 else 0
    log.info("%s start task=%s src_bps=%s target=%s source_dur=%.0f output_dur=%.0f segs=%s out_dir=%s align_hls=%s align_mode=%s",
             log_prefix, server_task_id, src_bps, maxrate, source_dur, target_dur, expected_segs, str(hls_dir), aligned_hls, align_mode)

    _tc_start_mono = time.monotonic()

    def _fmt_elapsed(elapsed: float) -> str:
        h = int(elapsed) // 3600
        m = (int(elapsed) % 3600) // 60
        s = int(elapsed) % 60
        return f"{h}时{m}分{s}秒" if h > 0 else (f"{m}分{s}秒" if m > 0 else f"{s}秒")

    state_last_sec = float(state.get("last_sec") or 0.0) if isinstance(state, dict) else 0.0
    state_start_no = int(state.get("start_number") or 0) if isinstance(state, dict) else 0
    if align_mode == "pad":
        resume_sec, start_no, exist_ts = 0.0, 0, 0
    else:
        resume_sec, start_no, exist_ts = await asyncio.to_thread(
            _probe_hls_resume_state, hls_dir, state_last_sec, state_start_no, target_dur
        )
        if target_dur > 0:
            resume_sec = max(0.0, min(float(target_dur), float(resume_sec)))
    if 0.0 < resume_sec < target_dur and exist_ts > 0:
        try:
            await asyncio.to_thread(_rewrite_hls_vod_playlist, hls_dir, None, False)
        except Exception:
            pass
    progress_scan_interval_sec = 300.0
    progress_state_save_interval_sec = 10.0
    last_seg_scan_ts = 0.0
    last_state_save_ts = 0.0
    cached_cur_segs = int(exist_ts)

    def _cancel_check() -> str | None:
        m = str(raw.get("__cancel") or "")
        return m if m in {"pause", "stop"} else None

    def _on_tc_progress(p: float, speed_x: float) -> None:
        nonlocal last_seg_scan_ts, last_state_save_ts, cached_cur_segs
        now = time.monotonic()
        if target_dur > 0:
            last_sec_est = float(resume_sec) + (float(p) * max(0.0, float(target_dur) - float(resume_sec)))
        else:
            last_sec_est = float(resume_sec)
        if seg_time > 0 and last_sec_est > 0:
            estimated_cur_segs = max(int(exist_ts), int(math.ceil(last_sec_est / float(seg_time))))
        else:
            estimated_cur_segs = int(exist_ts)
        if estimated_cur_segs > cached_cur_segs:
            cached_cur_segs = estimated_cur_segs
        if cached_cur_segs <= 0 or now - last_seg_scan_ts >= progress_scan_interval_sec:
            cached_cur_segs = _count_hls_segments(hls_dir)
            last_seg_scan_ts = now
        cur_segs = int(cached_cur_segs)
        if speed_x > 0:
            speed_str = f"{speed_x:.1f}x"
        else:
            speed_str = "计算中"
        seg_str = f"{cur_segs}/{expected_segs}" if expected_segs > 0 else f"{cur_segs}"
        tc_elapsed_str = _fmt_elapsed(time.monotonic() - _tc_start_mono)
        if target_dur > 0 and resume_sec > 0:
            base = max(0.0, min(1.0, float(resume_sec) / float(target_dur)))
            overall_p = base + (1.0 - base) * float(p)
        else:
            overall_p = float(p)
        overall_pct = int(max(0.0, min(1.0, overall_p)) * 100.0)
        if overall_p > 0.0 and overall_pct == 0:
            overall_pct = 1
        done_sec = max(0.0, min(float(target_dur), float(target_dur) * float(overall_p))) if target_dur > 0 else 0.0
        done_h = int(done_sec) // 3600
        done_m = (int(done_sec) % 3600) // 60
        done_s = int(done_sec) % 60
        done_str = f"{done_h}:{done_m:02d}:{done_s:02d}" if done_h > 0 else f"{done_m}:{done_s:02d}"
        total_h = int(target_dur) // 3600
        total_m = (int(target_dur) % 3600) // 60
        total_s = int(target_dur) % 60
        total_dur_str = f"{total_h}:{total_m:02d}:{total_s:02d}" if total_h > 0 else f"{total_m}:{total_s:02d}"
        detail = f"进度{overall_pct}%，{speed_str}，已转码{done_str}/{total_dur_str}，分片{seg_str}，耗时{tc_elapsed_str}"
        on_progress("TRANSCODE", overall_p, detail)
        try:
            last_sec = float(last_sec_est)
            force_state_save = float(p) >= 0.999
            if force_state_save or now - last_state_save_ts >= progress_state_save_interval_sec:
                _save_transcode_state(
                    hls_dir,
                    {
                        "last_sec": last_sec,
                        "start_number": int(cur_segs),
                        "align_mode": align_mode,
                        "output_duration_sec": float(target_dur),
                        **source_sig,
                    },
                )
                last_state_save_ts = now
        except Exception:
            pass

    try:
        m3u8_path = await asyncio.to_thread(
            generate_hls_crf_progress,
            str(downloaded),
            str(hls_dir),
            duration_sec=source_dur,
            target_duration_sec=target_dur,
            segment_time=seg_time,
            crf=28,
            preset="fast",
            height=1080,
            maxrate=maxrate,
            bufsize=bufsize,
            on_progress=_on_tc_progress,
            cancel_check=_cancel_check,
            resume_start_sec=resume_sec,
            start_number=start_no,
            append=bool((align_mode in {"none", "trim"}) and (resume_sec > 0 or start_no > 0)),
            audio_copy=bool(audio_codec == "aac" and align_mode == "none" and resume_sec <= 0.0 and start_no <= 0),
            audio_bitrate="256k",
            audio_sample_rate=(audio_sample_rate if audio_sample_rate > 0 else 48000),
            audio_channels=(audio_channels if audio_channels > 0 else 2),
        )
    except RuntimeError as e:
        msg = str(e)
        if msg == "cancelled:pause":
            try:
                await asyncio.to_thread(_rewrite_hls_vod_playlist, hls_dir, target_dur, False)
            except Exception:
                pass
            log.info("%s paused task=%s", log_prefix, server_task_id)
            raise PauseRequested()
        if msg == "cancelled:stop":
            log.info("%s stopped task=%s", log_prefix, server_task_id)
            raise StopRequested()
        raise

    m3u8_path = await asyncio.to_thread(_rewrite_hls_vod_playlist, hls_dir, target_dur)
    dl_size = int(downloaded.stat().st_size) if downloaded.exists() else 0
    tc_size = sum(f.stat().st_size for f in hls_dir.iterdir() if f.is_file())
    tc_segs_final, tc_total_sec = await asyncio.to_thread(_ensure_hls_output_complete, hls_dir, target_dur)
    tc_size_str = f"{tc_size / 1048576.0:.0f}MB" if tc_size < 1073741824 else f"{tc_size / 1073741824.0:.2f}GB"
    tc_elapsed_final = _fmt_elapsed(time.monotonic() - _tc_start_mono)
    on_progress("TRANSCODE", 1.0, f"转码完成，共{tc_segs_final}个分片，总大小{tc_size_str}，共耗时{tc_elapsed_final}")
    try:
        _transcode_state_path(hls_dir).unlink(missing_ok=True)
    except Exception:
        pass
    log.info("%s done task=%s src=%sMB out=%sMB ratio=%.0f%% hls=%s total_sec=%.3f",
             log_prefix, server_task_id, dl_size // 1048576, tc_size // 1048576,
             (tc_size / max(1, dl_size)) * 100, str(m3u8_path), tc_total_sec)
    return str(m3u8_path), dl_size, tc_size


def _cleanup_merge_artifacts(out_dir: Path, prefix: str, server_task_id: str) -> None:
    if not out_dir.exists():
        return
    for path in out_dir.glob(f"{prefix}_{server_task_id}.merged*"):
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except Exception:
            pass


def _ffmpeg_bin() -> str:
    return os.getenv("EDGE_FFMPEG_BIN") or "ffmpeg"


def _ffprobe_bin() -> str:
    b = _ffmpeg_bin()
    if b.lower().endswith("ffmpeg.exe"):
        return b[:-10] + "ffprobe.exe"
    if b.lower().endswith("ffmpeg"):
        return b[:-6] + "ffprobe"
    return "ffprobe"


def _segment_target_mb() -> int:
    try:
        v = int(os.getenv("EDGE_HIK_SEGMENT_TARGET_MB") or "850")
        return max(128, v)
    except Exception:
        return 850


def _segment_target_bytes() -> float:
    return float(_segment_target_mb()) * 1024.0 * 1024.0


def _segment_estimated_bps() -> int:
    try:
        v = int(os.getenv("EDGE_HIK_SEGMENT_EST_BPS") or "4500000")
        return max(500_000, v)
    except Exception:
        return 4_500_000


def _segment_estimated_bytes_per_sec() -> float:
    try:
        return max(1.0, float(_segment_estimated_bps()) / 8.0)
    except Exception:
        return 562_500.0


def _segment_seconds() -> int:
    explicit = str(os.getenv("EDGE_HIK_SEGMENT_SEC") or "").strip()
    if explicit:
        try:
            v = int(explicit)
            return max(30, v)
        except Exception:
            pass
    try:
        est_bps = _segment_estimated_bps()
        padding_sec = 120
        target_bytes = _segment_target_bytes()
        total_window_sec = max(30.0, (target_bytes * 8.0) / float(est_bps))
        raw_sec = max(30.0, total_window_sec - float(padding_sec))
        return max(30, int(raw_sec))
    except Exception:
        return 1744


def _segment_ranges(start_at: datetime, end_at: datetime, seg_sec: int) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    cur = start_at
    while cur < end_at:
        end = cur + timedelta(seconds=int(seg_sec))
        if end > end_at:
            end = end_at
        if end <= cur:
            break
        out.append((cur, end))
        cur = end
    return out if out else [(start_at, end_at)]


def _load_part_done_meta(done_path: Path) -> dict[str, Any]:
    try:
        return json.loads(done_path.read_text(encoding="utf-8", errors="ignore") or "{}") or {}
    except Exception:
        return {}


def _save_part_done_meta(done_path: Path, meta: dict[str, Any]) -> None:
    try:
        done_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8", errors="ignore")
    except Exception:
        done_path.write_text("ok", encoding="utf-8", errors="ignore")


def _estimate_next_segment_seconds(size_bytes: int, requested_sec: float, fallback_sec: int) -> int:
    fb = max(30, int(fallback_sec or 30))
    try:
        if int(size_bytes or 0) <= 0 or float(requested_sec or 0.0) <= 0:
            return fb
        target_bytes = _segment_target_bytes()
        observed_bytes_per_sec = float(size_bytes) / max(1.0, float(requested_sec))
        if observed_bytes_per_sec <= 0:
            return fb
        estimated = target_bytes / observed_bytes_per_sec
        blended = (float(fb) * 0.35) + (float(estimated) * 0.65)
        lower = 30.0
        upper = max(300.0, float(fb) * 2.5)
        return max(30, int(min(max(lower, blended), upper)))
    except Exception:
        return fb


def _download_resume_state_path(out_dir: Path, prefix: str, server_task_id: str) -> Path:
    return out_dir / f"{prefix}_{server_task_id}.download_state.json"


def _process_resume_state_path(out_dir: Path, prefix: str, server_task_id: str) -> Path:
    return out_dir / f"{prefix}_{server_task_id}.process_state.json"


def _load_download_resume_state(state_path: Path) -> dict[str, Any]:
    try:
        return json.loads(state_path.read_text(encoding="utf-8", errors="ignore") or "{}") or {}
    except Exception:
        return {}


def _load_process_resume_state(state_path: Path) -> dict[str, Any]:
    try:
        return json.loads(state_path.read_text(encoding="utf-8", errors="ignore") or "{}") or {}
    except Exception:
        return {}


def _save_download_resume_state(state_path: Path, started_at_epoch: float, **updates: Any) -> None:
    try:
        state = _load_download_resume_state(state_path)
        state["started_at_epoch"] = float(started_at_epoch)
        for key, value in updates.items():
            state[str(key)] = value
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8", errors="ignore")
    except Exception:
        pass


def _save_process_resume_state(state_path: Path, started_at_epoch: float, **updates: Any) -> None:
    try:
        state = _load_process_resume_state(state_path)
        state["started_at_epoch"] = float(started_at_epoch)
        for key, value in updates.items():
            state[str(key)] = value
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8", errors="ignore")
    except Exception:
        pass


def _clear_download_resume_state(state_path: Path) -> None:
    try:
        state_path.unlink(missing_ok=True)
    except Exception:
        pass


def _clear_process_resume_state(state_path: Path) -> None:
    try:
        state_path.unlink(missing_ok=True)
    except Exception:
        pass


def _emit_phase5_observation(callback: Callable[[dict[str, Any]], None] | None, **payload: Any) -> None:
    if callback is None:
        return
    try:
        callback({str(k): v for k, v in payload.items() if v is not None})
    except Exception:
        pass


def _concat_parts(parts: list[Path], out_path: Path, *, force_canonical_merge: bool = False, canonicalize_part_indexes: set[int] | None = None, on_observation: Callable[[dict[str, Any]], None] | None = None, skip_adjacent_preflight: bool = False, boundary_video_gap_action: str = "repair", force_ts_bridge: bool = False, cancel_check: Callable[[], str | None] | None = None) -> list[Path]:
    """Concat video parts into out_path. Returns list of intermediate temp files to clean up."""
    log = logging.getLogger("edge.runner")
    temp_files: list[Path] = []
    last_merge_issue = ""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_path.unlink(missing_ok=True)
    except Exception:
        pass

    def _raise_if_cancelled() -> None:
        if cancel_check is None:
            return
        try:
            mode = str(cancel_check() or "").strip().lower()
        except Exception:
            mode = ""
        if mode in {"pause", "stop"}:
            raise RuntimeError(f"cancelled:{mode}")

    def _run_capture(cmd: list[str]) -> tuple[bool, str, int]:
        _raise_if_cancelled()
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            output = ""
            while True:
                try:
                    output, _ = proc.communicate(timeout=0.5)
                    output = str(output or "")
                    break
                except subprocess.TimeoutExpired:
                    try:
                        _raise_if_cancelled()
                    except RuntimeError:
                        with contextlib.suppress(Exception):
                            proc.terminate()
                        try:
                            output, _ = proc.communicate(timeout=3)
                        except Exception:
                            with contextlib.suppress(Exception):
                                proc.kill()
                            with contextlib.suppress(Exception):
                                output, _ = proc.communicate(timeout=3)
                        raise
            code = int(proc.returncode or 0)
            if code == 0:
                return True, output, 0
            log.warning("ffmpeg merge step failed (exit=%s): %s", code, output[-400:])
            return False, output, code
        finally:
            if proc is not None and proc.stdout is not None:
                with contextlib.suppress(Exception):
                    proc.stdout.close()

    def _run(cmd: list[str]) -> bool:
        ok, _, _ = _run_capture(cmd)
        return ok

    cumulative_boundaries_sec_cache: list[float] | None = None

    def _cumulative_boundaries_sec() -> list[float]:
        nonlocal cumulative_boundaries_sec_cache
        if cumulative_boundaries_sec_cache is not None:
            return list(cumulative_boundaries_sec_cache)
        points: list[float] = []
        running_duration = 0.0
        for idx, part in enumerate(parts, start=1):
            running_duration += max(0.0, float(probe_authoritative_duration_seconds(str(part)) or 0.0))
            if idx < len(parts) and running_duration > 0.0:
                points.append(float(running_duration))
        cumulative_boundaries_sec_cache = list(points)
        return points

    def _build_boundary_points(probe_parts: list[Path]) -> list[float]:
        points: list[float] = []
        total = 0.0
        for idx, probe_part in enumerate(probe_parts, start=1):
            total += max(0.0, float(probe_authoritative_duration_seconds(str(probe_part)) or 0.0))
            if idx < len(probe_parts) and total > 0.0:
                points.append(float(total))
        return points

    def _run_concat_probe(probe_parts: list[Path], label: str) -> str:
        if len(probe_parts) < 2:
            return ""
        probe_list = out_path.parent / f"{out_path.stem}.{label}.txt"
        probe_out = out_path.parent / f"{out_path.stem}.{label}.mp4"
        temp_files.append(probe_list)
        temp_files.append(probe_out)
        try:
            probe_out.unlink(missing_ok=True)
        except Exception:
            pass
        probe_list.write_text(
            "\n".join(["file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'" for p in probe_parts]),
            encoding="utf-8", errors="ignore",
        )
        probe_cmd = [
            _ffmpeg_bin(), "-y",
            "-f", "concat", "-safe", "0", "-i", str(probe_list),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-fflags", "+genpts",
            "-c", "copy",
            "-movflags", "+faststart",
            str(probe_out),
        ]
        ok, probe_output, _ = _run_capture(probe_cmd)
        if not ok:
            trimmed = str(probe_output or "")[-300:].replace("\n", " ")
            return f"ffmpeg_concat_failed:{trimmed}" if trimmed else "ffmpeg_concat_failed"
        issue = _probe_structural_media_issue(probe_out, include_packet_metrics=False)
        if not issue:
            issue = _probe_merge_boundary_decode_issue(probe_out, _build_boundary_points(probe_parts))
        return str(issue or "")

    stream_concat_profile_cache: dict[str, dict[str, str]] = {}

    def _stream_concat_profile(part_path: Path) -> dict[str, str]:
        key = str(part_path)
        cached = stream_concat_profile_cache.get(key)
        if cached is not None:
            return dict(cached)
        profile: dict[str, str] = {}
        try:
            cmd = [
                _ffprobe_bin(),
                "-v", "error",
                "-analyzeduration", "5M",
                "-probesize", "5M",
                "-select_streams", "v:0",
                "-show_entries",
                "stream=codec_type,codec_name,codec_tag_string,time_base",
                "-of", "json",
                str(part_path),
            ]
            r = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            data = json.loads(r.stdout or "{}") or {}
            for stream in data.get("streams") or []:
                if not isinstance(stream, dict):
                    continue
                stream_type = str(stream.get("codec_type") or "").strip().lower()
                if stream_type == "video" and "v_codec" not in profile:
                    profile["v_codec"] = _normalize_video_codec_name(str(stream.get("codec_name") or ""))
                    profile["v_tag"] = str(stream.get("codec_tag_string") or "").strip().lower()
                    profile["v_time_base"] = str(stream.get("time_base") or "").strip().lower()
        except Exception:
            profile = {}
        stream_concat_profile_cache[key] = dict(profile)
        return profile

    def _time_base_denominator(time_base: str) -> int:
        try:
            text = str(time_base or "").strip()
            if "/" not in text:
                return 0
            den = int(text.split("/", 1)[1] or 0)
            return max(0, den)
        except Exception:
            return 0

    def _probe_lightweight_adjacent_boundary_issue(left: Path, right: Path) -> str:
        def _merge_part_family(path: Path) -> str:
            name = str(path.name or "").lower()
            if ".systrans." in name:
                return "systrans"
            if ".fixed." in name:
                return "fixed"
            if ".canonical" in name:
                return "canonical"
            if ".norm" in name:
                return "normalized"
            return "raw"

        left_family = _merge_part_family(left)
        right_family = _merge_part_family(right)
        if {left_family, right_family} == {"systrans", "fixed"}:
            return f"container_boundary_profile_mismatch:kind=merge_part_family:left={left_family}:right={right_family}"
        left_profile = _stream_concat_profile(left)
        right_profile = _stream_concat_profile(right)
        left_codec = left_profile.get("v_codec", "")
        right_codec = right_profile.get("v_codec", "")
        if left_codec and right_codec and left_codec != right_codec:
            return f"container_boundary_profile_mismatch:kind=video_codec:left={left_codec}:right={right_codec}"
        left_tag = left_profile.get("v_tag", "")
        right_tag = right_profile.get("v_tag", "")
        if left_codec in {"hevc", "h264"} and right_codec == left_codec and left_tag and right_tag and left_tag != right_tag:
            return f"container_boundary_profile_mismatch:kind=video_tag:left={left_tag}:right={right_tag}:codec={left_codec}"
        left_tb = left_profile.get("v_time_base", "")
        right_tb = right_profile.get("v_time_base", "")
        left_den = _time_base_denominator(left_tb)
        right_den = _time_base_denominator(right_tb)
        if left_codec in {"hevc", "h264"} and right_codec == left_codec and left_den > 0 and right_den > 0:
            ratio = max(left_den, right_den) / max(1, min(left_den, right_den))
            if ratio >= 10.0:
                return f"container_boundary_profile_mismatch:kind=video_time_base:left={left_tb}:right={right_tb}:ratio={ratio:.3f}:codec={left_codec}"
        return ""

    def _probe_adjacent_boundary_issue(probe_parts: list[Path]) -> str:
        if len(probe_parts) < 2:
            return ""
        def _part_family(path: Path) -> str:
            name = str(path.name or "").lower()
            if ".systrans." in name:
                return "systrans"
            if ".fixed." in name:
                return "fixed"
            if ".canonical" in name:
                return "canonical"
            if ".norm" in name:
                return "normalized"
            return "raw"

        for left_idx in range(1, len(probe_parts)):
            left_family = _part_family(probe_parts[left_idx - 1])
            right_family = _part_family(probe_parts[left_idx])
            if {left_family, right_family} == {"systrans", "fixed"}:
                pair_issue = f"container_boundary_profile_mismatch:kind=merge_part_family:left={left_family}:right={right_family}"
                log.warning(
                    "pre-merge adjacent boundary probe failed left=%s right=%s left_path=%s right_path=%s issue=%s",
                    left_idx,
                    left_idx + 1,
                    str(probe_parts[left_idx - 1]),
                    str(probe_parts[left_idx]),
                    pair_issue,
                )
                return f"pre_merge_boundary_abnormal:left={left_idx}:right={left_idx + 1}:issue={pair_issue}"
        for left_idx in range(1, len(probe_parts)):
            pair_issue = _probe_lightweight_adjacent_boundary_issue(probe_parts[left_idx - 1], probe_parts[left_idx])
            if pair_issue:
                log.warning(
                    "pre-merge adjacent boundary probe failed left=%s right=%s left_path=%s right_path=%s issue=%s",
                    left_idx,
                    left_idx + 1,
                    str(probe_parts[left_idx - 1]),
                    str(probe_parts[left_idx]),
                    pair_issue,
                )
                return f"pre_merge_boundary_abnormal:left={left_idx}:right={left_idx + 1}:issue={pair_issue}"
        log.info(
            "pre-merge adjacent boundary heavy preflight skipped after lightweight pass parts=%s",
            len(probe_parts),
        )
        return ""

    def _parse_pre_merge_boundary_indexes(issue: str) -> tuple[int, int]:
        text = str(issue or "")
        try:
            left = 0
            right = 0
            for token in text.split(":"):
                if token.startswith("left="):
                    left = int(token.split("=", 1)[1] or 0)
                elif token.startswith("right="):
                    right = int(token.split("=", 1)[1] or 0)
            if left > 0 and right > 0:
                return left, right
        except Exception:
            pass
        return 0, 0

    def _issue_float(issue: str, key: str) -> float:
        for token in str(issue or "").split(":"):
            if token.startswith(key + "="):
                try:
                    return float(token.split("=", 1)[1] or 0.0)
                except Exception:
                    return 0.0
        return 0.0

    def _classify_pre_merge_boundary_observation(issue: str) -> tuple[str, str]:
        raw = str(issue or "")
        if ":issue=" in raw:
            raw = raw.split(":issue=", 1)[1]
        if raw.startswith("container_boundary_profile_mismatch:"):
            return "blocking_candidate", raw.split(":", 1)[0]
        if raw.startswith("boundary_decode_abnormal:") or raw.startswith("ffmpeg_concat_failed"):
            return "blocking_candidate", raw.split(":", 1)[0]
        if raw.startswith("stream_end_gap_abnormal:"):
            video_end = _issue_float(raw, "video_end")
            audio_end = _issue_float(raw, "audio_end")
            end_gap = abs(audio_end - video_end)
            if end_gap > STRUCTURAL_STREAM_END_GAP_ABNORMAL_SECONDS:
                return "blocking_candidate", f"stream_end_gap={end_gap:.3f}"
            return "warning", f"stream_end_gap={end_gap:.3f}"
        if raw.startswith("video_packet_duration_abnormal:"):
            max_packet = _issue_float(raw, "max")
            if max_packet > STRUCTURAL_PACKET_DURATION_ABNORMAL_SECONDS:
                return "blocking_candidate", f"video_packet_duration={max_packet:.3f}"
            return "warning", f"video_packet_duration={max_packet:.3f}"
        if raw.startswith("audio_packet_duration_abnormal:"):
            max_packet = _issue_float(raw, "max")
            if max_packet > STRUCTURAL_AUDIO_PACKET_DURATION_ABNORMAL_SECONDS:
                return "blocking_candidate", f"audio_packet_duration={max_packet:.3f}"
            return "warning", f"audio_packet_duration={max_packet:.3f}"
        return "warning", raw.split(":", 1)[0] if raw else ""

    def _should_run_heavy_merge_packet_probe() -> bool:
        reason_text = " ".join(
            str(x or "")
            for x in (
                early_pre_merge_boundary_issue,
                pre_merge_boundary_issue,
                last_merge_issue,
            )
        ).lower()
        return any(
            token in reason_text
            for token in (
                "packet_duration_abnormal",
                "merge_boundary_video_gap",
                "boundary_decode_abnormal",
                "duration_mismatch_abnormal",
            )
        )

    def _probe_merge_boundary_video_gap_issue(video_path: Path) -> str:
        if len(concat_input_parts) < 2 or not video_path.exists():
            return ""
        if not _should_run_heavy_merge_packet_probe():
            return ""
        try:
            timings = probe_stream_timings(str(video_path))
            video_timing = timings.get("video") or {}
            audio_timing = timings.get("audio") or {}
            video_start = float(video_timing.get("start_time") or 0.0)
            video_duration = float(video_timing.get("duration") or 0.0)
            audio_start = float(audio_timing.get("start_time") or 0.0)
            audio_duration = float(audio_timing.get("duration") or 0.0)
            if video_duration <= 0.0 or audio_duration <= 0.0:
                return ""
            start_gap = audio_start - video_start
            end_gap = (audio_start + audio_duration) - (video_start + video_duration)
            max_v = float(probe_max_packet_duration(str(video_path), stream_selector="v:0") or 0.0)
            max_a = float(probe_max_packet_duration(str(video_path), stream_selector="a:0") or 0.0)
            if max_v <= MERGE_BOUNDARY_VIDEO_GAP_PACKET_ABNORMAL_SECONDS:
                return ""
            if max_a > MERGE_BOUNDARY_VIDEO_GAP_AUDIO_PACKET_MAX_SECONDS:
                return ""
            if abs(start_gap) > MERGE_BOUNDARY_VIDEO_GAP_AV_TOLERANCE_SECONDS:
                return ""
            if abs(end_gap) > MERGE_BOUNDARY_VIDEO_GAP_AV_TOLERANCE_SECONDS:
                return ""
            video_anomaly = probe_packet_timeline_anomaly(str(video_path), stream_selector="v:0")
            audio_anomaly = probe_packet_timeline_anomaly(str(video_path), stream_selector="a:0")
            v_dts_back = int(video_anomaly.get("dts_backward_count") or 0)
            a_dts_back = int(audio_anomaly.get("dts_backward_count") or 0)
            if v_dts_back != 0 or a_dts_back != 0:
                return ""
            v_pts_back = int(video_anomaly.get("pts_backward_count") or 0)
            v_max_pts_back = float(video_anomaly.get("max_pts_backward_sec") or 0.0)
            return (
                "merge_boundary_video_gap:"
                f"max_v={max_v:.3f}:"
                f"max_a={max_a:.3f}:"
                f"start_gap={start_gap:.3f}:"
                f"end_gap={end_gap:.3f}:"
                f"v_pts_back={v_pts_back}:"
                f"v_dts_back={v_dts_back}:"
                f"a_dts_back={a_dts_back}:"
                f"v_max_pts_back={v_max_pts_back:.3f}"
            )
        except Exception:
            return ""

    def _diagnose_canonical_concat_failure(stage_name: str, probe_parts: list[Path], issue: str) -> None:
        if len(probe_parts) < 2:
            return
        try:
            for idx, probe_part in enumerate(probe_parts, start=1):
                metrics = _collect_structural_media_metrics(probe_part)
                part_issue = _structural_media_issue_from_metrics(metrics)
                part_warning = _structural_media_warning_from_metrics(metrics)
                log.info(
                    "%s diagnose canonical input idx=%s part=%s metrics=%s issue=%s warning=%s",
                    stage_name,
                    idx,
                    str(probe_part),
                    _format_structural_media_metrics(metrics),
                    part_issue or "",
                    part_warning or "",
                )
            first_bad_prefix = 0
            first_bad_issue = ""
            for prefix_len in range(2, len(probe_parts) + 1):
                prefix_issue = _run_concat_probe(probe_parts[:prefix_len], f"diag_prefix_{prefix_len:03d}")
                if prefix_issue:
                    first_bad_prefix = prefix_len
                    first_bad_issue = prefix_issue
                    break
            if first_bad_prefix <= 0:
                log.warning("%s diagnose canonical concat unable to reproduce earlier failing prefix final_issue=%s", stage_name, issue)
                return
            boundary_left = first_bad_prefix - 1
            boundary_right = first_bad_prefix
            log.warning(
                "%s diagnose canonical concat first failing prefix=%s boundary=%s|%s issue=%s final_issue=%s",
                stage_name,
                first_bad_prefix,
                boundary_left,
                boundary_right,
                first_bad_issue,
                issue,
            )
            pair_issue = _run_concat_probe(probe_parts[boundary_left - 1:boundary_right], f"diag_pair_{boundary_left:03d}_{boundary_right:03d}")
            if pair_issue:
                log.warning(
                    "%s diagnose canonical concat confirmed adjacent boundary=%s|%s issue=%s",
                    stage_name,
                    boundary_left,
                    boundary_right,
                    pair_issue,
                )
                return
            triplet_start = max(0, boundary_left - 2)
            triplet_end = min(len(probe_parts), boundary_right + 1)
            if triplet_end - triplet_start >= 3:
                triplet_issue = _run_concat_probe(probe_parts[triplet_start:triplet_end], f"diag_triplet_{triplet_start + 1:03d}_{triplet_end:03d}")
                log.warning(
                    "%s diagnose canonical concat adjacent pair clean, localized window=%s-%s issue=%s",
                    stage_name,
                    triplet_start + 1,
                    triplet_end,
                    triplet_issue or "",
                )
        except Exception as e:
            log.warning("%s diagnose canonical concat failed final_issue=%s err=%s", stage_name, issue, e)

    def _repair_pre_merge_boundary_inputs(issue: str) -> bool:
        nonlocal concat_input_parts, last_merge_issue
        left_idx, right_idx = _parse_pre_merge_boundary_indexes(issue)
        if left_idx <= 0 or right_idx <= 0:
            return False
        if left_idx > len(concat_input_parts) or right_idx > len(concat_input_parts):
            return False
        target_indexes = sorted({left_idx, right_idx})
        log.warning(
            "%s targeted boundary repair start out=%s left=%s right=%s issue=%s",
            _branch_code_tag(CAM_MRG_010),
            str(out_path),
            left_idx,
            right_idx,
            issue,
        )
        _emit_phase5_observation(
            on_observation,
            phase="merge",
            event="targeted_boundary_repair_started",
            branch_code=CAM_MRG_010,
            reason=issue,
            left=left_idx,
            right=right_idx,
            parts=len(parts),
        )
        repaired_parts = list(concat_input_parts)
        try:
            for repair_idx in target_indexes:
                src = concat_input_parts[repair_idx - 1]
                repaired_parts[repair_idx - 1] = _canonicalize_part(src, repair_idx)
            pair_issue = _run_concat_probe(
                [repaired_parts[left_idx - 1], repaired_parts[right_idx - 1]],
                f"targeted_boundary_pair_{left_idx:03d}_{right_idx:03d}",
            )
            if pair_issue:
                log.warning(
                    "%s targeted boundary repair pair still abnormal out=%s left=%s right=%s issue=%s",
                    _branch_code_tag(CAM_MRG_010),
                    str(out_path),
                    left_idx,
                    right_idx,
                    pair_issue,
                )
                last_merge_issue = f"pre_merge_boundary_abnormal:left={left_idx}:right={right_idx}:issue={pair_issue}"
                _emit_phase5_observation(
                    on_observation,
                    phase="merge",
                    event="targeted_boundary_repair_failed",
                    branch_code=CAM_MRG_010,
                    reason=last_merge_issue,
                    left=left_idx,
                    right=right_idx,
                    parts=len(parts),
                )
                return False
            all_issue = _probe_adjacent_boundary_issue(repaired_parts)
            if all_issue:
                log.warning(
                    "%s targeted boundary repair revealed remaining boundary issue out=%s issue=%s",
                    _branch_code_tag(CAM_MRG_010),
                    str(out_path),
                    all_issue,
                )
                last_merge_issue = all_issue
                _emit_phase5_observation(
                    on_observation,
                    phase="merge",
                    event="targeted_boundary_repair_failed",
                    branch_code=CAM_MRG_010,
                    reason=all_issue,
                    left=left_idx,
                    right=right_idx,
                    parts=len(parts),
                )
                return False
            concat_input_parts = repaired_parts
            last_merge_issue = ""
            log.info(
                "%s targeted boundary repair accepted out=%s left=%s right=%s",
                _branch_code_tag(CAM_MRG_010),
                str(out_path),
                left_idx,
                right_idx,
            )
            _emit_phase5_observation(
                on_observation,
                phase="merge",
                event="targeted_boundary_repair_succeeded",
                branch_code=CAM_MRG_010,
                reason=issue,
                left=left_idx,
                right=right_idx,
                parts=len(parts),
            )
            return True
        except Exception as e:
            last_merge_issue = issue
            log.warning(
                "%s targeted boundary repair failed out=%s left=%s right=%s issue=%s err=%s",
                _branch_code_tag(CAM_MRG_010),
                str(out_path),
                left_idx,
                right_idx,
                issue,
                e,
            )
            _emit_phase5_observation(
                on_observation,
                phase="merge",
                event="targeted_boundary_repair_failed",
                branch_code=CAM_MRG_010,
                reason=f"{issue}:err={e}",
                left=left_idx,
                right=right_idx,
                parts=len(parts),
            )
            return False

    def _parse_boundary_pair_from_issue(issue_text: str, probe_parts: list[Path]) -> tuple[int, int]:
        raw = str(issue_text or "")
        if not raw:
            return 0, 0
        if "left=" in raw and "right=" in raw:
            try:
                left = int(re.search(r"left=(\d+)", raw).group(1))  # type: ignore[union-attr]
                right = int(re.search(r"right=(\d+)", raw).group(1))  # type: ignore[union-attr]
                return left, right
            except Exception:
                pass
        m = re.search(r"boundary=([0-9.]+)", raw)
        if not m:
            return 0, 0
        try:
            boundary_sec = float(m.group(1) or 0.0)
        except Exception:
            return 0, 0
        if boundary_sec <= 0.0:
            return 0, 0
        points = _build_boundary_points(probe_parts)
        if not points:
            return 0, 0
        best_idx = 0
        best_gap = float("inf")
        for idx, point in enumerate(points, start=1):
            gap = abs(float(point) - boundary_sec)
            if gap < best_gap:
                best_gap = gap
                best_idx = idx
        if best_idx <= 0:
            return 0, 0
        tolerance = max(3.0, min(15.0, boundary_sec * 0.002))
        if best_gap > tolerance:
            return 0, 0
        return best_idx, best_idx + 1

    def _should_observe_boundary_video_gap(issue_text: str) -> bool:
        return (
            boundary_video_gap_action == "observe"
            and isinstance(issue_text, str)
            and issue_text.startswith("merge_boundary_video_gap:")
        )

    def _merge_output_expected_tail_silence() -> bool:
        if not _is_expected_tail_silence_output(out_path, include_packet_metrics=False):
            return False
        return any(_is_expected_tail_silence_output(p, include_packet_metrics=False) for p in concat_input_parts)

    def _probe_merge_expected_duration_issue(video_path: Path) -> str:
        if not video_path.exists():
            return "missing_output"
        expected_total = 0.0
        for part in concat_input_parts:
            expected_total += max(0.0, float(probe_authoritative_duration_seconds(str(part)) or 0.0))
        merged_total = float(probe_authoritative_duration_seconds(str(video_path)) or 0.0)
        if expected_total <= 0.0 or merged_total <= 0.0:
            return ""
        tolerance = max(3.0, min(30.0, expected_total * 0.005))
        gap = abs(merged_total - expected_total)
        if gap > tolerance:
            return f"merge_duration_mismatch:merged={merged_total:.3f}:expected={expected_total:.3f}:gap={gap:.3f}:tolerance={tolerance:.3f}"
        return ""

    def _accept_merge_output(stage_name: str) -> bool:
        nonlocal last_merge_issue
        issue = _probe_structural_media_issue(out_path, include_packet_metrics=False)
        if not issue:
            issue = _probe_merge_boundary_decode_issue(out_path, _cumulative_boundaries_sec())
        if not issue:
            issue = _probe_merge_boundary_video_gap_issue(out_path)
        if not issue:
            last_merge_issue = ""
            return True
        if _should_observe_boundary_video_gap(issue):
            log.info(
                "%s boundary video gap observed (action=observe, source-side artifact preserved) out=%s issue=%s",
                stage_name,
                str(out_path),
                issue,
            )
            _emit_phase5_observation(
                on_observation,
                phase="merge",
                event="boundary_video_gap_observed",
                branch_code=stage_name,
                reason=str(issue),
                parts=len(parts),
                action="observe",
            )
            last_merge_issue = ""
            return True
        if issue.startswith("stream_end_gap_abnormal:") and _merge_output_expected_tail_silence():
            log.info(
                "%s merged output has expected tail silence from source, accepted out=%s issue=%s",
                stage_name,
                str(out_path),
                issue,
            )
            last_merge_issue = ""
            return True
        if issue.startswith("stream_end_gap_abnormal:") and _merge_output_expected_tail_silence():
            log.info(
                "%s merged output has expected tail silence from source, accepted out=%s issue=%s",
                stage_name,
                str(out_path),
                issue,
            )
            last_merge_issue = ""
            return True
        last_merge_issue = str(issue or "")
        log.warning("%s produced pathological merged output out=%s issue=%s", stage_name, str(out_path), issue)
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        if _repair_merged_output(stage_name, last_merge_issue):
            return True
        return False

    def _probe_merge_output_issue(stage_name: str, *, delete_output_on_failure: bool = True) -> bool:
        nonlocal last_merge_issue
        issue = _probe_structural_media_issue(out_path, include_packet_metrics=False)
        if not issue:
            issue = _probe_merge_boundary_decode_issue(out_path, _cumulative_boundaries_sec())
        if not issue:
            issue = _probe_merge_boundary_video_gap_issue(out_path)
        if not issue:
            last_merge_issue = ""
            return True
        if _should_observe_boundary_video_gap(issue):
            log.info(
                "%s boundary video gap observed (action=observe, source-side artifact preserved) out=%s issue=%s",
                stage_name,
                str(out_path),
                issue,
            )
            _emit_phase5_observation(
                on_observation,
                phase="merge",
                event="boundary_video_gap_observed",
                branch_code=stage_name,
                reason=str(issue),
                parts=len(parts),
                action="observe",
            )
            last_merge_issue = ""
            return True
        last_merge_issue = str(issue or "")
        log.warning("%s produced pathological merged output out=%s issue=%s", stage_name, str(out_path), issue)
        if delete_output_on_failure:
            try:
                out_path.unlink(missing_ok=True)
            except Exception:
                pass
        return False

    def _repair_concat_input_with_cfr(stage_name: str, issue: str, repair_code: str, repaired_out: Path) -> bool:
        cfr_list = out_path.parent / (out_path.stem + "_concat_cfr.txt")
        temp_files.append(cfr_list)
        cfr_list.write_text(
            "\n".join(["file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'" for p in concat_input_parts]),
            encoding="utf-8", errors="ignore",
        )
        merged_audio_info = probe_audio_stream_info(str(concat_input_parts[0])) if concat_input_parts else {}
        merge_audio_candidates = [_audio_aac_args(merged_audio_info, bitrate) for bitrate in _audio_bitrate_candidates(merged_audio_info)]
        merge_video_candidates = _video_reencode_candidates(concat_input_parts[0], prefer_qsv=False) if concat_input_parts else [["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]]
        repair_prefix = [
            _ffmpeg_bin(), "-y",
            "-analyzeduration", "100M", "-probesize", "100M",
            "-fflags", "+genpts",
            "-f", "concat", "-safe", "0", "-i", str(cfr_list),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-avoid_negative_ts", "make_zero",
            "-vf", "setpts=PTS-STARTPTS,fps=25",
            "-af", "aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS",
        ]
        repair_suffix = [
            "-movflags", "+faststart",
            str(repaired_out),
        ]
        log.warning("%s %s attempting concat CFR repair out=%s issue=%s", _branch_code_tag(repair_code), stage_name, str(out_path), issue)
        ok, failed_cmd, failed_output, failed_code = _run_with_av_candidates(
            repair_prefix,
            repair_suffix,
            merge_video_candidates,
            merge_audio_candidates,
            f"{_branch_code_tag(repair_code)} concat cfr repair",
        )
        if ok:
            return True
        log.warning(
            "%s %s concat CFR repair failed out=%s issue=%s exit=%s cmd=%s ffmpeg_tail=%s",
            _branch_code_tag(repair_code),
            stage_name,
            str(out_path),
            issue,
            failed_code,
            " ".join(failed_cmd),
            failed_output[-1200:],
        )
        try:
            repaired_out.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    def _repair_ts_list_with_cfr(stage_name: str, issue: str, ts_concat_list: Path, repaired_out: Path) -> bool:
        merged_audio_info = probe_audio_stream_info(str(concat_input_parts[0])) if concat_input_parts else {}
        merge_audio_candidates = [_audio_aac_args(merged_audio_info, bitrate) for bitrate in _audio_bitrate_candidates(merged_audio_info)]
        merge_video_candidates = _video_reencode_candidates(concat_input_parts[0], prefer_qsv=False) if concat_input_parts else [["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]]
        repair_prefix = [
            _ffmpeg_bin(), "-y",
            "-analyzeduration", "100M", "-probesize", "100M",
            "-fflags", "+genpts",
            "-f", "concat", "-safe", "0", "-i", str(ts_concat_list),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-avoid_negative_ts", "make_zero",
            "-vf", "setpts=PTS-STARTPTS,fps=25",
            "-af", "aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS",
        ]
        repair_suffix = [
            "-movflags", "+faststart",
            str(repaired_out),
        ]
        repair_code = "CAM-MRG-062"
        log.warning("%s %s attempting final TS CFR repair out=%s issue=%s", _branch_code_tag(repair_code), stage_name, str(out_path), issue)
        ok, failed_cmd, failed_output, failed_code = _run_with_av_candidates(
            repair_prefix,
            repair_suffix,
            merge_video_candidates,
            merge_audio_candidates,
            f"{_branch_code_tag(repair_code)} final ts cfr repair",
        )
        if ok:
            return True
        log.warning(
            "%s %s final TS CFR repair failed out=%s issue=%s exit=%s cmd=%s ffmpeg_tail=%s",
            _branch_code_tag(repair_code),
            stage_name,
            str(out_path),
            issue,
            failed_code,
            " ".join(failed_cmd),
            failed_output[-1200:],
        )
        try:
            repaired_out.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    def _repair_merged_output(stage_name: str, issue: str) -> bool:
        normalized_issue = str(issue or "")
        cfr_issue = _classify_merge_boundary_cfr_repair(out_path, normalized_issue)
        effective_issue = cfr_issue or normalized_issue
        if not effective_issue.startswith("audio_packet_duration_abnormal:") and not effective_issue.startswith("merge_boundary_video_gap:"):
            return False
        repaired_out = out_path.with_name(out_path.stem + ".repaired.mp4")
        try:
            repaired_out.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            repair_code = CAM_MRG_061 if effective_issue.startswith("merge_boundary_video_gap:") else CAM_MRG_060
            if not _repair_concat_input_with_cfr(stage_name, effective_issue, repair_code, repaired_out):
                return False
            if not repaired_out.exists():
                return False
            try:
                out_path.unlink(missing_ok=True)
            except Exception:
                pass
            repaired_out.replace(out_path)
            repaired_issue = _probe_structural_media_issue(out_path, include_packet_metrics=False)
            if not repaired_issue:
                repaired_issue = _probe_merge_boundary_decode_issue(out_path, _cumulative_boundaries_sec())
            if not repaired_issue:
                repaired_issue = _probe_merge_boundary_video_gap_issue(out_path)
            if repaired_issue:
                log.warning("%s %s final merged output repair still abnormal out=%s issue=%s", _branch_code_tag(repair_code), stage_name, str(out_path), repaired_issue)
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return False
            log.info("%s %s final merged output repair succeeded out=%s", _branch_code_tag(repair_code), stage_name, str(out_path))
            return True
        except Exception as e:
            repair_code = CAM_MRG_061 if effective_issue.startswith("merge_boundary_video_gap:") else CAM_MRG_060
            log.warning("%s %s final merged output repair failed out=%s issue=%s err=%s", _branch_code_tag(repair_code), stage_name, str(out_path), effective_issue, e)
            try:
                repaired_out.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    video_codec_name_cache: dict[str, str] = {}

    def _probe_video_codec_name(video_path: Path) -> str:
        cache_key = str(video_path)
        cached = video_codec_name_cache.get(cache_key)
        if cached is not None:
            return str(cached or "")
        try:
            cmd = [
                _ffprobe_bin(),
                "-v", "error",
                "-analyzeduration", "5M",
                "-probesize", "5M",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ]
            r = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=8,
            )
            values = [str(x or "").strip().lower() for x in (r.stdout or "").splitlines() if str(x or "").strip()]
            codec_name = values[0] if values else ""
            video_codec_name_cache[cache_key] = codec_name
            return codec_name
        except Exception:
            video_codec_name_cache[cache_key] = ""
            return ""

    def _audio_copy_to_mp4_allowed(codec_name: str) -> bool:
        return str(codec_name or "").strip().lower() in {"aac", "alac", "ac3", "eac3", "mp3", "mp2"}

    def _audio_bitrate_candidates(audio_info: dict[str, Any]) -> list[str]:
        try:
            sample_rate = int(audio_info.get("sample_rate") or 0)
        except Exception:
            sample_rate = 0
        try:
            channels = int(audio_info.get("channels") or 0)
        except Exception:
            channels = 0
        try:
            bit_rate = int(audio_info.get("bit_rate") or 0)
        except Exception:
            bit_rate = 0
        candidates: list[int] = []

        def _add(value: int) -> None:
            if value <= 0:
                return
            if value not in candidates:
                candidates.append(value)

        if sample_rate > 0 and sample_rate <= 12000 and channels <= 1:
            defaults = [96000, 64000]
        elif sample_rate > 0 and sample_rate <= 24000 and channels <= 1:
            defaults = [128000, 96000, 64000]
        elif sample_rate > 0 and sample_rate <= 32000:
            defaults = [160000, 128000, 96000, 64000]
        else:
            defaults = [256000, 192000, 128000, 96000, 64000]
        if bit_rate > 0:
            normalized = max(64000, min(256000, int(math.ceil(bit_rate / 32000.0) * 32000)))
            _add(normalized)
            if normalized < 256000:
                _add(min(256000, normalized * 2))
        for item in defaults:
            _add(item)
        return [f"{int(max(64, round(v / 1000.0)))}k" for v in candidates]

    def _audio_aac_args(audio_info: dict[str, Any], bitrate: str) -> list[str]:
        args = ["-c:a", "aac", "-b:a", str(bitrate)]
        try:
            sample_rate = int(audio_info.get("sample_rate") or 0)
        except Exception:
            sample_rate = 0
        try:
            channels = int(audio_info.get("channels") or 0)
        except Exception:
            channels = 0
        if sample_rate > 0:
            args.extend(["-ar", str(sample_rate)])
        if channels > 0:
            args.extend(["-ac", str(channels)])
        return args

    def _audio_concat_profile(audio_info: dict[str, Any]) -> tuple[str, int, int]:
        try:
            sample_rate = int(audio_info.get("sample_rate") or 0)
        except Exception:
            sample_rate = 0
        try:
            channels = int(audio_info.get("channels") or 0)
        except Exception:
            channels = 0
        return (
            str(audio_info.get("codec_name") or "").strip().lower(),
            sample_rate,
            channels,
        )

    def _audio_probe_unknown(info: dict[str, Any]) -> bool:
        return str(info.get("_probe_status") or "").strip().lower() in {"timeout", "error"}

    def _audio_probe_confirmed_no_stream(info: dict[str, Any]) -> bool:
        return str(info.get("_probe_status") or "").strip().lower() == "no_stream"

    def _target_concat_audio_info(audio_infos: list[dict[str, Any]]) -> dict[str, Any]:
        rates: list[int] = []
        channels: list[int] = []
        bit_rates: list[int] = []
        for info in audio_infos:
            try:
                rate = int(info.get("sample_rate") or 0)
                if rate > 0:
                    rates.append(rate)
            except Exception:
                pass
            try:
                channel_count = int(info.get("channels") or 0)
                if channel_count > 0:
                    channels.append(channel_count)
            except Exception:
                pass
            try:
                bit_rate = int(info.get("bit_rate") or 0)
                if bit_rate > 0:
                    bit_rates.append(bit_rate)
            except Exception:
                pass
        return {
            "codec_name": "aac",
            "sample_rate": max(rates) if rates else 16000,
            "channels": max(1, min(2, max(channels) if channels else 1)),
            "bit_rate": max(bit_rates) if bit_rates else 0,
        }

    def _probe_concat_audio_profile_issue(probe_parts: list[Path]) -> tuple[str, set[int], dict[str, Any]]:
        if len(probe_parts) < 2:
            return "", set(), {}
        audio_infos = [probe_audio_stream_info(str(p)) for p in probe_parts]
        profiles = [_audio_concat_profile(info) for info in audio_infos]
        if not profiles:
            return "", set(), {}
        target_audio_info = _target_concat_audio_info(audio_infos)
        target_profile = _audio_concat_profile(target_audio_info)
        unsafe_indexes = {
            idx
            for idx, profile in enumerate(profiles, start=1)
            if profile != target_profile
        }
        if not unsafe_indexes:
            return "", set(), {}
        profile_text = ",".join(
            f"{idx}={codec or 'none'}/{sample_rate or 0}/{channels or 0}"
            for idx, (codec, sample_rate, channels) in enumerate(profiles, start=1)
        )
        issue = (
            "audio_profile_mismatch:"
            f"target={target_profile[0]}/{target_profile[1]}/{target_profile[2]}:"
            f"profiles={profile_text}"
        )
        return issue, unsafe_indexes, target_audio_info

    def _merge_part_family_for_path(path: Path) -> str:
        name = str(path.name or "").lower()
        if ".systrans." in name:
            return "systrans"
        if ".fixed." in name:
            return "fixed"
        return ""

    def _minimum_family_mismatch_cover(edges: list[tuple[int, int]], families: dict[int, str]) -> set[int]:
        """Pick the smallest part set that covers all systrans/fixed boundaries."""
        if not edges:
            return set()
        edge_set = {tuple(sorted((int(left), int(right)))) for left, right in edges if int(left) > 0 and int(right) > 0}
        indexes = sorted({idx for edge in edge_set for idx in edge})
        if not indexes:
            return set()

        def _better(left: set[int] | None, right: set[int] | None) -> set[int] | None:
            if left is None:
                return right
            if right is None:
                return left
            if len(right) != len(left):
                return right if len(right) < len(left) else left
            left_fixed = sum(1 for idx in left if families.get(idx) == "fixed")
            right_fixed = sum(1 for idx in right if families.get(idx) == "fixed")
            if right_fixed != left_fixed:
                return right if right_fixed > left_fixed else left
            return right if tuple(sorted(right)) < tuple(sorted(left)) else left

        best: dict[int, set[int] | None] = {0: set(), 1: {indexes[0]}}
        for pos in range(1, len(indexes)):
            idx = indexes[pos]
            prev = indexes[pos - 1]
            next_best: dict[int, set[int] | None] = {}
            for selected, covered in best.items():
                if covered is None:
                    continue
                must_cover_prev_edge = tuple(sorted((prev, idx))) in edge_set and not selected
                if not must_cover_prev_edge:
                    candidate = set(covered)
                    next_best[0] = _better(next_best.get(0), candidate)
                candidate = set(covered)
                candidate.add(idx)
                next_best[1] = _better(next_best.get(1), candidate)
            best = next_best
        result = _better(best.get(0), best.get(1))
        return set(result or set())

    _encoder_cache: dict[str, bool] = {}

    def _has_encoder(name: str) -> bool:
        cached = _encoder_cache.get(str(name))
        if cached is not None:
            return bool(cached)
        try:
            out = subprocess.check_output([_ffmpeg_bin(), "-hide_banner", "-encoders"], stderr=subprocess.STDOUT, universal_newlines=True)
            present = str(name or "") in str(out or "")
        except Exception:
            present = False
        _encoder_cache[str(name)] = bool(present)
        return bool(present)

    def _video_reencode_candidates(video_path: Path, *, prefer_qsv: bool = False) -> list[list[str]]:
        audio_info = probe_audio_stream_info(str(video_path))
        try:
            audio_bitrate_bps = int(audio_info.get("bit_rate") or 0)
        except Exception:
            audio_bitrate_bps = 0
        source_total_bitrate_bps = max(0, int(probe_bitrate_bps(str(video_path)) or 0))
        target_video_bitrate_bps = source_total_bitrate_bps
        if target_video_bitrate_bps > 0 and audio_bitrate_bps > 0 and target_video_bitrate_bps > audio_bitrate_bps:
            target_video_bitrate_bps = max(300000, target_video_bitrate_bps - audio_bitrate_bps)
        try:
            timings = probe_stream_timings(str(video_path))
            video_info = timings.get("video") or {}
            width = int(video_info.get("width") or 0)
            height = int(video_info.get("height") or 0)
        except Exception:
            width = 0
            height = 0
        if target_video_bitrate_bps > 0:
            quality_target_bps = int(round(float(target_video_bitrate_bps) * 1.35))
            if width >= 1920 or height >= 1080:
                quality_target_bps = max(quality_target_bps, 6000000)
            target_k = max(300, int(round(quality_target_bps / 1000.0)))
            maxrate_k = max(target_k, int(round(target_k * 1.20)))
            bufsize_k = max(maxrate_k * 2, target_k * 2)
            qsv_quality_args = ["-b:v", f"{target_k}k", "-maxrate", f"{maxrate_k}k", "-bufsize", f"{bufsize_k}k"]
            x264_quality_args = ["-b:v", f"{target_k}k", "-maxrate", f"{maxrate_k}k", "-bufsize", f"{bufsize_k}k"]
        else:
            qsv_quality_args = ["-global_quality", "16"]
            x264_quality_args = ["-crf", "16"]
        candidates: list[list[str]] = []
        x264_args = ["-c:v", "libx264", "-preset", "medium", *x264_quality_args, "-pix_fmt", "yuv420p"]
        qsv_args = ["-c:v", "h264_qsv", "-preset", "medium", *qsv_quality_args]
        if prefer_qsv and _has_encoder("h264_qsv"):
            candidates.append(qsv_args)
            candidates.append(x264_args)
        else:
            candidates.append(x264_args)
            if _has_encoder("h264_qsv"):
                candidates.append(qsv_args)
        return candidates

    def _normalize_video_codec_name(codec_name: str) -> str:
        normalized = str(codec_name or "").strip().lower()
        if normalized in {"h265", "hevc"}:
            return "hevc"
        if normalized in {"x264", "h264"}:
            return "h264"
        return normalized

    def _video_reencode_candidates_for_codec(video_path: Path, target_codec: str) -> list[list[str]]:
        target = _normalize_video_codec_name(target_codec)
        if target == "h264":
            return _video_reencode_candidates(video_path, prefer_qsv=False)
        if target == "hevc":
            candidates: list[list[str]] = []
            if _has_encoder("hevc_qsv"):
                candidates.append(["-c:v", "hevc_qsv", "-preset", "medium", "-global_quality", "18"])
            if _has_encoder("libx265"):
                candidates.append(["-c:v", "libx265", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"])
            return candidates
        return []

    def _select_target_video_codec(codec_by_index: dict[int, str]) -> str:
        counts: dict[str, int] = {}
        first_seen: dict[str, int] = {}
        for idx, codec in codec_by_index.items():
            normalized = _normalize_video_codec_name(codec)
            if not normalized:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
            first_seen.setdefault(normalized, int(idx))
        if not counts:
            return ""
        return sorted(counts.keys(), key=lambda item: (-counts[item], first_seen.get(item, 999999), item))[0]

    def _repair_video_codec_mismatch_inputs(codec_by_index: dict[int, str], reason: str) -> bool:
        nonlocal concat_input_parts, last_merge_issue
        target_codec = _select_target_video_codec(codec_by_index)
        if not target_codec:
            return False
        repair_indexes = [
            idx for idx, codec in sorted(codec_by_index.items())
            if _normalize_video_codec_name(codec) and _normalize_video_codec_name(codec) != target_codec
        ]
        if not repair_indexes:
            return True
        log.warning(
            "%s targeted video codec normalize start out=%s target=%s selected=%s reason=%s",
            _branch_code_tag("CAM-MRG-039"),
            str(out_path),
            target_codec,
            ",".join(str(x) for x in repair_indexes),
            reason,
        )
        _emit_phase5_observation(
            on_observation,
            phase="merge",
            event="targeted_video_codec_normalize_started",
            branch_code="CAM-MRG-039",
            reason=reason,
            target_codec=target_codec,
            selected_parts=repair_indexes,
            parts=len(parts),
        )
        repaired_parts = list(concat_input_parts)
        for idx in repair_indexes:
            src = concat_input_parts[idx - 1]
            repaired = out_path.parent / f"{out_path.stem}.vcodec{idx:03d}.{target_codec}.mp4"
            temp_files.append(repaired)
            try:
                repaired.unlink(missing_ok=True)
            except Exception:
                pass
            audio_info = probe_audio_stream_info(str(src))
            audio_candidates: list[list[str]] = []
            audio_codec = str(audio_info.get("codec_name") or "").strip().lower()
            if audio_codec and _audio_copy_to_mp4_allowed(audio_codec):
                audio_candidates.append(["-c:a", "copy"])
            for bitrate in _audio_bitrate_candidates(audio_info):
                audio_candidates.append(_audio_aac_args(audio_info, bitrate))
            video_candidates = _video_reencode_candidates_for_codec(src, target_codec)
            if not video_candidates:
                last_merge_issue = f"video_codec_target_encoder_unavailable:target={target_codec}"
                return False
            prefix = [
                _ffmpeg_bin(), "-y",
                "-fflags", "+genpts",
                "-i", str(src),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-avoid_negative_ts", "make_zero",
                "-vf", "setpts=PTS-STARTPTS",
            ]
            suffix = [
                "-movflags", "+faststart",
                str(repaired),
            ]
            ok, failed_cmd, failed_output, failed_code = _run_with_av_candidates(
                prefix,
                suffix,
                video_candidates,
                audio_candidates,
                f"{_branch_code_tag('CAM-MRG-039')} targeted video codec normalize part {idx}",
            )
            if not ok:
                if _looks_like_disk_full_text(failed_output):
                    raise RuntimeError("merge_disk_full")
                last_merge_issue = f"video_codec_target_normalize_failed:part{idx}:target={target_codec}"
                log.warning(
                    "%s targeted video codec normalize failed idx=%s src=%s out=%s target=%s exit=%s cmd=%s ffmpeg_tail=%s",
                    _branch_code_tag("CAM-MRG-039"),
                    idx,
                    str(src),
                    str(repaired),
                    target_codec,
                    failed_code,
                    " ".join(failed_cmd),
                    failed_output[-1200:],
                )
                return False
            issue = _probe_structural_media_issue(repaired)
            if issue:
                last_merge_issue = f"video_codec_target_normalize_invalid:part{idx}:{issue}"
                log.warning(
                    "%s targeted video codec normalize invalid idx=%s src=%s out=%s target=%s issue=%s",
                    _branch_code_tag("CAM-MRG-039"),
                    idx,
                    str(src),
                    str(repaired),
                    target_codec,
                    issue,
                )
                return False
            repaired_codec = _normalize_video_codec_name(_probe_video_codec_name(repaired))
            if repaired_codec != target_codec:
                last_merge_issue = f"video_codec_target_normalize_mismatch:part{idx}:got={repaired_codec}:target={target_codec}"
                return False
            repaired_parts[idx - 1] = repaired
        concat_input_parts = repaired_parts
        last_merge_issue = ""
        log.info(
            "%s targeted video codec normalize accepted out=%s target=%s selected=%s",
            _branch_code_tag("CAM-MRG-039"),
            str(out_path),
            target_codec,
            ",".join(str(x) for x in repair_indexes),
        )
        _emit_phase5_observation(
            on_observation,
            phase="merge",
            event="targeted_video_codec_normalize_succeeded",
            branch_code="CAM-MRG-039",
            reason=reason,
            target_codec=target_codec,
            selected_parts=repair_indexes,
            parts=len(parts),
        )
        return True

    def _run_with_audio_candidates(prefix: list[str], suffix: list[str], audio_candidates: list[list[str]], stage_name: str) -> tuple[bool, list[str], str, int]:
        candidates = audio_candidates or [[]]
        last_index = len(candidates) - 1
        last_cmd: list[str] = []
        last_output = ""
        last_code = -1
        for idx, audio_args in enumerate(candidates):
            if audio_args:
                log.info("%s trying audio args=%s", stage_name, " ".join(audio_args))
            cmd = [*prefix, *audio_args, *suffix]
            ok, output, code = _run_capture(cmd)
            if ok:
                return True, cmd, output, code
            last_cmd = cmd
            last_output = output
            last_code = code
            if idx < last_index:
                log.warning("%s failed, retrying next audio profile", stage_name)
        return False, last_cmd, last_output, last_code


    def _run_with_av_candidates(prefix: list[str], suffix: list[str], video_candidates: list[list[str]], audio_candidates: list[list[str]], stage_name: str) -> tuple[bool, list[str], str, int]:
        v_candidates = video_candidates or [[]]
        a_candidates = audio_candidates or [[]]
        last_cmd: list[str] = []
        last_output = ""
        last_code = -1
        total_attempts = max(1, len(v_candidates) * len(a_candidates))
        attempt = 0
        for video_args in v_candidates:
            for audio_args in a_candidates:
                attempt += 1
                cmd = [*prefix, *video_args, *audio_args, *suffix]
                if video_args or audio_args:
                    log.info("%s trying av args attempt=%s/%s video=%s audio=%s", stage_name, attempt, total_attempts, " ".join(video_args), " ".join(audio_args))
                ok, output, code = _run_capture(cmd)
                if ok:
                    return True, cmd, output, code
                last_cmd = cmd
                last_output = output
                last_code = code
                if attempt < total_attempts:
                    log.warning("%s failed, retrying next av profile", stage_name)
        return False, last_cmd, last_output, last_code

    def _canonicalize_part(part_path: Path, idx: int, target_audio_info: dict[str, Any] | None = None) -> Path:
        canonical_part = out_path.parent / f"{out_path.stem}.canonical{idx:03d}.mp4"
        temp_files.append(canonical_part)
        try:
            canonical_part.unlink(missing_ok=True)
        except Exception:
            pass
        audio_info = dict(target_audio_info or probe_audio_stream_info(str(part_path)))
        audio_candidates = [_audio_aac_args(audio_info, bitrate) for bitrate in _audio_bitrate_candidates(audio_info)]
        canonical_prefix = [
            _ffmpeg_bin(), "-y",
            "-fflags", "+genpts",
            "-i", str(part_path),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-avoid_negative_ts", "make_zero",
            "-c:v", "copy",
        ]
        canonical_suffix = [
            "-movflags", "+faststart",
            str(canonical_part),
        ]
        log.info("%s canonicalize part start idx=%s src=%s out=%s", _branch_code_tag(CAM_MRG_010), idx, str(part_path), str(canonical_part))
        ok, failed_cmd, failed_output, failed_code = _run_with_audio_candidates(
            canonical_prefix,
            canonical_suffix,
            audio_candidates,
            f"{_branch_code_tag(CAM_MRG_010)} canonicalize part {idx} audio-only",
        )
        if not ok:
            retry_prefix = [
                _ffmpeg_bin(), "-y",
                "-fflags", "+genpts",
                "-i", str(part_path),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-avoid_negative_ts", "make_zero",
            ]
            video_candidates = _video_reencode_candidates(part_path, prefer_qsv=False)
            ok, failed_cmd, failed_output, failed_code = _run_with_av_candidates(
                retry_prefix,
                canonical_suffix,
                video_candidates,
                audio_candidates,
                f"{_branch_code_tag(CAM_MRG_010)} canonicalize part {idx} av-reencode",
            )
        if not ok:
            if _looks_like_disk_full_text(failed_output):
                raise RuntimeError("merge_disk_full")
            log.warning(
                "%s canonicalize part %s failed src=%s out=%s exit=%s cmd=%s ffmpeg_tail=%s",
                _branch_code_tag(CAM_MRG_010),
                idx,
                str(part_path),
                str(canonical_part),
                failed_code,
                " ".join(failed_cmd),
                failed_output[-1200:],
            )
            raise RuntimeError(f"merge_canonicalize_failed:part{idx}")
        if target_audio_info:
            canonical_audio_info = probe_audio_stream_info(str(canonical_part))
            canonical_audio_profile = _audio_concat_profile(canonical_audio_info)
            target_audio_profile = _audio_concat_profile(target_audio_info)
            if canonical_audio_profile != target_audio_profile:
                try:
                    canonical_part.unlink(missing_ok=True)
                except Exception:
                    pass
                duration_sec = float(probe_authoritative_duration_seconds(str(part_path)) or probe_duration_seconds(str(part_path)) or 0.0)
                if duration_sec <= 0.0:
                    raise RuntimeError(f"merge_canonical_part_audio_missing:part{idx}")
                sample_rate = int(target_audio_info.get("sample_rate") or 16000)
                channels = int(target_audio_info.get("channels") or 1)
                layout = "stereo" if channels >= 2 else "mono"
                silence_audio_info = dict(target_audio_info)
                silence_audio_candidates = [_audio_aac_args(silence_audio_info, bitrate) for bitrate in _audio_bitrate_candidates(silence_audio_info)]
                silence_prefix = [
                    _ffmpeg_bin(), "-y",
                    "-fflags", "+genpts",
                    "-i", str(part_path),
                    "-f", "lavfi", "-t", f"{duration_sec:.3f}",
                    "-i", f"anullsrc=channel_layout={layout}:sample_rate={sample_rate}",
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-avoid_negative_ts", "make_zero",
                    "-c:v", "copy",
                ]
                silence_suffix = [
                    "-shortest",
                    "-movflags", "+faststart",
                    str(canonical_part),
                ]
                log.warning(
                    "%s canonicalize part %s source audio unavailable, muxing silent AAC track src=%s out=%s target=%s/%s/%s",
                    _branch_code_tag(CAM_MRG_010),
                    idx,
                    str(part_path),
                    str(canonical_part),
                    target_audio_profile[0],
                    target_audio_profile[1],
                    target_audio_profile[2],
                )
                ok, failed_cmd, failed_output, failed_code = _run_with_audio_candidates(
                    silence_prefix,
                    silence_suffix,
                    silence_audio_candidates,
                    f"{_branch_code_tag(CAM_MRG_010)} canonicalize part {idx} silent-audio",
                )
                if not ok:
                    if _looks_like_disk_full_text(failed_output):
                        raise RuntimeError("merge_disk_full")
                    log.warning(
                        "%s canonicalize part %s silent audio mux failed src=%s out=%s exit=%s cmd=%s ffmpeg_tail=%s",
                        _branch_code_tag(CAM_MRG_010),
                        idx,
                        str(part_path),
                        str(canonical_part),
                        failed_code,
                        " ".join(failed_cmd),
                        failed_output[-1200:],
                    )
                    raise RuntimeError(f"merge_canonicalize_silent_audio_failed:part{idx}")
        canonical_issue = _probe_structural_media_issue(canonical_part)
        if canonical_issue:
            log.warning(
                "%s canonicalize part %s produced invalid output src=%s out=%s issue=%s",
                _branch_code_tag(CAM_MRG_010),
                idx,
                str(part_path),
                str(canonical_part),
                canonical_issue,
            )
            try:
                canonical_part.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"merge_canonical_part_invalid:part{idx}:{canonical_issue}")
        canonical_metrics = _collect_structural_media_metrics(canonical_part)
        canonical_warning = _structural_media_warning_from_metrics(canonical_metrics)
        log.info(
            "%s canonicalize part accepted idx=%s out=%s metrics=%s warning=%s",
            _branch_code_tag(CAM_MRG_010),
            idx,
            str(canonical_part),
            _format_structural_media_metrics(canonical_metrics),
            canonical_warning or "",
        )
        return canonical_part

    requested_canonical_indexes = {int(x) for x in (canonicalize_part_indexes or set()) if int(x) > 0}
    concat_input_parts: list[Path] = list(parts)
    early_pre_merge_boundary_issue = ""
    early_family_mismatch_edges: list[tuple[int, int]] = []
    family_by_index = {idx: _merge_part_family_for_path(part) for idx, part in enumerate(concat_input_parts, start=1)}
    if not force_ts_bridge:
        for left_idx in range(1, len(concat_input_parts)):
            left_family = family_by_index.get(left_idx, "")
            right_family = family_by_index.get(left_idx + 1, "")
            if {left_family, right_family} == {"systrans", "fixed"}:
                early_family_mismatch_edges.append((left_idx, left_idx + 1))
    early_family_mismatch_indexes = _minimum_family_mismatch_cover(early_family_mismatch_edges, family_by_index)
    if early_family_mismatch_indexes:
        first_left, first_right = early_family_mismatch_edges[0]
        early_pre_merge_boundary_issue = (
            "pre_merge_boundary_abnormal:"
            f"left={first_left}:right={first_right}:"
            "issue=container_boundary_profile_mismatch:kind=merge_part_family:"
            f"left={family_by_index.get(first_left, '')}:right={family_by_index.get(first_right, '')}"
        )
        log.warning(
            "%s merge family mismatch boundaries detected out=%s edges=%s selected=%s issue=%s",
            _branch_code_tag(CAM_MRG_010),
            str(out_path),
            ",".join(f"{left}->{right}" for left, right in early_family_mismatch_edges),
            ",".join(str(x) for x in sorted(early_family_mismatch_indexes)),
            early_pre_merge_boundary_issue,
        )
        _emit_phase5_observation(
            on_observation,
            phase="merge",
            event="merge_family_mismatch_detected",
            branch_code=CAM_MRG_010,
            reason=early_pre_merge_boundary_issue,
            selected_parts=sorted(early_family_mismatch_indexes),
            parts=len(parts),
        )
        requested_canonical_indexes.update(early_family_mismatch_indexes)
        early_pre_merge_boundary_issue = ""
    ts_silent_audio_indexes: set[int] = set()
    audio_profile_issue, audio_profile_repair_indexes, audio_profile_target_info, ts_silent_audio_indexes = "", set(), {}, set()
    log.info(
        "%s merge skips audio profile probe in main path out=%s reason=%s selected=%s",
        _branch_code_tag(CAM_MRG_010),
        str(out_path),
        "policy_forced_ts_bridge" if force_ts_bridge else ("targeted_boundary_repair" if requested_canonical_indexes else "defer_to_merge_validation"),
        ",".join(str(x) for x in sorted(requested_canonical_indexes)),
    )
    if audio_profile_issue:
        log.warning(
            "%s merge audio profile mismatch detected out=%s selected=%s issue=%s",
            _branch_code_tag(CAM_MRG_010),
            str(out_path),
            ",".join(str(x) for x in sorted(audio_profile_repair_indexes or ts_silent_audio_indexes)),
            audio_profile_issue,
        )
        _emit_phase5_observation(
            on_observation,
            phase="merge",
            event="audio_profile_mismatch_detected",
            branch_code=CAM_MRG_010,
            reason=audio_profile_issue,
            selected_parts=sorted(audio_profile_repair_indexes or ts_silent_audio_indexes),
            parts=len(parts),
        )
        if ts_silent_audio_indexes:
            skip_mp4_concat_paths = True
            last_merge_issue = "audio_profile_missing_stream_for_ts_bridge"
            log.warning(
                "%s merge detected missing audio stream segments, bypassing MP4 concat paths and deferring silent-audio injection to TS fallback out=%s selected=%s",
                _branch_code_tag(CAM_MRG_040),
                str(out_path),
                ",".join(str(x) for x in sorted(ts_silent_audio_indexes)),
            )
            _emit_phase5_observation(
                on_observation,
                phase="merge",
                event="mp4_concat_bypassed_for_missing_audio_stream",
                branch_code=CAM_MRG_040,
                reason=last_merge_issue,
                selected_parts=sorted(ts_silent_audio_indexes),
                parts=len(parts),
            )
        else:
            requested_canonical_indexes.update(audio_profile_repair_indexes)
    if force_canonical_merge or requested_canonical_indexes:
        if force_canonical_merge:
            log.warning("%s merge canonical path forced for abnormal parts out=%s parts=%s", _branch_code_tag(CAM_MRG_010), str(out_path), len(parts))
            _emit_phase5_observation(on_observation, phase="merge", event="canonical_forced", branch_code=CAM_MRG_010, reason="force_canonical_merge", parts=len(parts))
        else:
            log.warning("%s merge canonicalizing selected abnormal parts out=%s parts=%s selected=%s", _branch_code_tag(CAM_MRG_010), str(out_path), len(parts), ",".join(str(x) for x in sorted(requested_canonical_indexes)))
            _emit_phase5_observation(on_observation, phase="merge", event="canonical_selected_parts", branch_code=CAM_MRG_010, reason="canonicalize_part_indexes", selected_parts=sorted(requested_canonical_indexes), parts=len(parts))
        concat_input_parts = []
        for idx, p in enumerate(parts, start=1):
            if force_canonical_merge or idx in requested_canonical_indexes:
                target_audio_info = audio_profile_target_info if idx in audio_profile_repair_indexes else None
                concat_input_parts.append(_canonicalize_part(p, idx, target_audio_info=target_audio_info))
            else:
                concat_input_parts.append(p)

    pre_merge_boundary_issue = early_pre_merge_boundary_issue or ("" if (skip_adjacent_preflight or force_ts_bridge) else _probe_adjacent_boundary_issue(concat_input_parts))
    if skip_adjacent_preflight or force_ts_bridge:
        log.info("%s skipping heavy adjacent boundary preflight out=%s parts=%s", _branch_code_tag(CAM_MRG_020), str(out_path), len(parts))
        _emit_phase5_observation(
            on_observation,
            phase="merge",
            event="adjacent_boundary_preflight_skipped",
            branch_code=CAM_MRG_020,
            reason="policy_forced_ts_bridge" if force_ts_bridge else "policy_controlled_lightweight_merge",
            parts=len(parts),
        )
    preflight_severity = ""
    preflight_metric = ""
    skip_mp4_concat_paths = bool(force_ts_bridge or ts_silent_audio_indexes)
    if requested_canonical_indexes and not skip_mp4_concat_paths:
        skip_mp4_concat_paths = True
        last_merge_issue = "canonicalized_subset_requires_non_mp4_concat"
        log.warning(
            "%s merge detected canonicalized subset, bypassing MP4 concat paths out=%s selected=%s",
            _branch_code_tag(CAM_MRG_040),
            str(out_path),
            ",".join(str(x) for x in sorted(requested_canonical_indexes)),
        )
        _emit_phase5_observation(
            on_observation,
            phase="merge",
            event="mp4_concat_bypassed_for_canonicalized_subset",
            branch_code=CAM_MRG_040,
            reason=last_merge_issue,
            selected_parts=sorted(requested_canonical_indexes),
            parts=len(parts),
        )
    if force_ts_bridge:
        last_merge_issue = "policy_forced_ts_bridge"
        log.warning(
            "%s merge policy forces TS-bridge path out=%s parts=%s",
            _branch_code_tag(CAM_MRG_040),
            str(out_path),
            len(parts),
        )
        _emit_phase5_observation(
            on_observation,
            phase="merge",
            event="mp4_concat_bypassed_for_policy",
            branch_code=CAM_MRG_040,
            reason=last_merge_issue,
            parts=len(parts),
        )
    if pre_merge_boundary_issue:
        preflight_severity, preflight_metric = _classify_pre_merge_boundary_observation(pre_merge_boundary_issue)
        log.warning(
            "%s pre-merge boundary observation out=%s severity=%s metric=%s issue=%s",
            _branch_code_tag(CAM_MRG_040),
            str(out_path),
            preflight_severity,
            preflight_metric,
            pre_merge_boundary_issue,
        )
        _emit_phase5_observation(
            on_observation,
            phase="merge",
            event="pre_merge_boundary_observed",
            branch_code=CAM_MRG_040,
            reason=pre_merge_boundary_issue,
            severity=preflight_severity,
            metric=preflight_metric,
            parts=len(parts),
        )
        if preflight_severity == "blocking_candidate":
            should_try_boundary_repair = "container_boundary_profile_mismatch:" not in pre_merge_boundary_issue
            if should_try_boundary_repair and _repair_pre_merge_boundary_inputs(pre_merge_boundary_issue):
                pre_merge_boundary_issue = ""
                preflight_severity = ""
                preflight_metric = ""
            else:
                skip_mp4_concat_paths = True
                last_merge_issue = pre_merge_boundary_issue
                log.warning(
                    "%s pre-merge boundary issue will bypass MP4 concat paths and enter TS-bridge fallback out=%s issue=%s",
                    _branch_code_tag(CAM_MRG_040),
                    str(out_path),
                    pre_merge_boundary_issue,
                )
                _emit_phase5_observation(
                    on_observation,
                    phase="merge",
                    event="mp4_concat_bypassed_for_boundary_issue",
                    branch_code=CAM_MRG_040,
                    reason=pre_merge_boundary_issue,
                    metric=preflight_metric,
                    parts=len(parts),
                )
    # Detect codec mismatch across segments before entering TS-bridge fallback.
    codec_by_index: dict[int, str] = {}
    codecs = set()
    for idx, p in enumerate(concat_input_parts, start=1):
        codec = _probe_video_codec_name(p)
        if codec:
            codec_by_index[idx] = codec
            codecs.add(codec)

    normalized_codecs = {_normalize_video_codec_name(c) for c in codecs if _normalize_video_codec_name(c)}
    has_codec_mismatch = len(normalized_codecs) > 1
    if has_codec_mismatch:
        mismatch_reason = f"codec_mismatch:{sorted(codecs)}"
        if skip_mp4_concat_paths and _repair_video_codec_mismatch_inputs(codec_by_index, mismatch_reason):
            codec_by_index = {}
            codecs = set()
            for idx, p in enumerate(concat_input_parts, start=1):
                codec = _probe_video_codec_name(p)
                if codec:
                    codec_by_index[idx] = codec
                    codecs.add(codec)
            normalized_codecs = {_normalize_video_codec_name(c) for c in codecs if _normalize_video_codec_name(c)}
            has_codec_mismatch = len(normalized_codecs) > 1
        if has_codec_mismatch and not skip_mp4_concat_paths:
            log.warning(
                "%s detected video codec mismatch across segments: %s. Skipping TS fallback, proceeding directly to single MP4 concat + CFR re-encode merge.",
                _branch_code_tag("CAM-MRG-039"),
                ", ".join(sorted(codecs)),
            )
            _emit_phase5_observation(
                on_observation,
                phase="merge",
                event="codec_mismatch_skipping_ts_fallback",
                branch_code="CAM-MRG-039",
                reason=f"codecs={sorted(codecs)}",
                parts=len(parts),
            )

            repaired_tmp = out_path.with_name(out_path.stem + ".mismatch_cfr.mp4")
            try:
                repaired_tmp.unlink(missing_ok=True)
            except Exception:
                pass

            ok = _repair_concat_input_with_cfr(
                "codec_mismatch_repair",
                mismatch_reason,
                "CAM-MRG-039",
                repaired_tmp,
            )
            if ok and repaired_tmp.exists():
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass
                repaired_tmp.replace(out_path)

                repaired_issue = _probe_structural_media_issue(out_path)
                if not repaired_issue:
                    repaired_issue = _probe_merge_boundary_decode_issue(out_path, _cumulative_boundaries_sec())
                if not repaired_issue:
                    repaired_issue = _probe_merge_boundary_video_gap_issue(out_path)
                if not repaired_issue:
                    repaired_issue = _detect_post_merge_freeze(out_path, _cumulative_boundaries_sec())

                if not repaired_issue:
                    log.info("%s codec mismatch repair succeeded out=%s", _branch_code_tag("CAM-MRG-039"), str(out_path))
                    return temp_files
                else:
                    log.warning("%s codec mismatch repair produced abnormal output out=%s issue=%s", _branch_code_tag("CAM-MRG-039"), str(out_path), repaired_issue)
                    raise RuntimeError(f"merge_codec_mismatch_repair_failed:{repaired_issue}")
            else:
                try:
                    repaired_tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise RuntimeError("merge_codec_mismatch_repair_failed:cfr_failed")
        if has_codec_mismatch:
            raise RuntimeError(f"merge_codec_mismatch_targeted_repair_failed:{last_merge_issue or sorted(codecs)}")

    concat_list = out_path.with_name(out_path.stem + "_concat_copy.txt")
    if not skip_mp4_concat_paths:
        log.info("%s trying direct concat out=%s parts=%s", _branch_code_tag(CAM_MRG_020), str(out_path), len(parts))
        temp_files.append(concat_list)
        concat_list.write_text(
            "\n".join(["file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'" for p in concat_input_parts]),
            encoding="utf-8", errors="ignore",
        )
        try:
            direct_concat_cmd = [
                _ffmpeg_bin(), "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy",
                "-movflags", "+faststart",
                str(out_path),
            ]
            ok, direct_output, _ = _run_capture(direct_concat_cmd)
            if ok and _accept_merge_output(_branch_code_tag(CAM_MRG_020)):
                log.info("%s direct concat accepted out=%s", _branch_code_tag(CAM_MRG_020), str(out_path))
                return temp_files
            if _looks_like_disk_full_text(direct_output):
                raise RuntimeError("merge_disk_full")
            _emit_phase5_observation(on_observation, phase="merge", event="fallback_entered", from_branch_code=CAM_MRG_020, to_branch_code=CAM_MRG_030, reason=last_merge_issue or "direct_concat_invalid", parts=len(parts))
        except Exception as e:
            last_merge_issue = str(e)
            log.warning("%s direct concat failed, fallback to normalized concat out=%s err=%s", _branch_code_tag(CAM_MRG_030), str(out_path), e)
            _emit_phase5_observation(on_observation, phase="merge", event="fallback_entered", from_branch_code=CAM_MRG_020, to_branch_code=CAM_MRG_030, reason=f"direct_concat_failed:{e}", parts=len(parts))

    norm_parts: list[Path] = []
    if not skip_mp4_concat_paths:
        for idx, p in enumerate(concat_input_parts, start=1):
            np = out_path.parent / f"{out_path.stem}.norm{idx:03d}.mp4"
            temp_files.append(np)
            try:
                np.unlink(missing_ok=True)
            except Exception:
                pass
            audio_info = probe_audio_stream_info(str(p))
            audio_codec = str(audio_info.get("codec_name") or "").strip().lower()
            audio_candidates: list[list[str]] = []
            if audio_codec and _audio_copy_to_mp4_allowed(audio_codec):
                audio_candidates.append(["-c:a", "copy"])
            for bitrate in _audio_bitrate_candidates(audio_info):
                audio_candidates.append(_audio_aac_args(audio_info, bitrate))
            normalize_prefix = [
                _ffmpeg_bin(), "-y", "-i", str(p),
                "-fflags", "+genpts",
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "copy",
            ]
            normalize_suffix = [
                "-movflags", "+faststart",
                str(np),
            ]
            log.info("%s normalize part start idx=%s src=%s out=%s", _branch_code_tag(CAM_MRG_030), idx, str(p), str(np))
            ok, _, failed_output, _ = _run_with_audio_candidates(normalize_prefix, normalize_suffix, audio_candidates, f"{_branch_code_tag(CAM_MRG_030)} normalize part {idx}")
            if not ok:
                if _looks_like_disk_full_text(failed_output):
                    raise RuntimeError("merge_disk_full")
                log.warning("%s normalize part %s failed, will try TS fallback", _branch_code_tag(CAM_MRG_030), idx)
                norm_parts = []
                break
            norm_parts.append(np)

    if norm_parts and len(norm_parts) == len(parts):
        norm_list = out_path.parent / (out_path.stem + "_concat_norm.txt")
        temp_files.append(norm_list)
        norm_list.write_text(
            "\n".join(["file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'" for p in norm_parts]),
            encoding="utf-8", errors="ignore",
        )
        nconcat = [
            _ffmpeg_bin(), "-y",
            "-f", "concat", "-safe", "0", "-i", str(norm_list),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-fflags", "+genpts",
            "-c", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
        ok, nconcat_output, _ = _run_capture(nconcat)
        if ok:
            if _accept_merge_output(f"{_branch_code_tag(CAM_MRG_031)} merge normalized concat"):
                log.info("%s normalized concat accepted out=%s", _branch_code_tag(CAM_MRG_031), str(out_path))
                return temp_files
        if _looks_like_disk_full_text(nconcat_output):
            raise RuntimeError("merge_disk_full")
        log.warning("%s merge normalized concat failed, trying TS-bridge fallback", _branch_code_tag(CAM_MRG_031))
        _emit_phase5_observation(on_observation, phase="merge", event="fallback_entered", from_branch_code=CAM_MRG_031, to_branch_code=CAM_MRG_040, reason=last_merge_issue or "normalized_concat_failed", parts=len(parts))

    log.warning("%s merge normalized concat failed, trying TS-bridge fallback", _branch_code_tag(CAM_MRG_031))
    _emit_phase5_observation(on_observation, phase="merge", event="fallback_entered", from_branch_code=CAM_MRG_031, to_branch_code=CAM_MRG_040, reason=last_merge_issue or "normalized_concat_unavailable", parts=len(parts))

    # 仅保留真正解码失败作为强制全段视频重编码的触发条件；
    # video_packet_duration_abnormal 在边界探测中常常只是两段时间戳跳变拼接出的假象
    # （每段自身 max_v_pkt 远小于阈值），TS copy + genpts 即可处理，不应升级到全段重编码。
    force_ts_video_reencode = "boundary_decode_abnormal" in last_merge_issue
    if force_ts_video_reencode:
        log.warning(
            "%s ts fallback escalating to video reencode because previous merge issue=%s out=%s",
            _branch_code_tag(CAM_MRG_041),
            last_merge_issue,
            str(out_path),
        )
        _emit_phase5_observation(on_observation, phase="merge", event="fallback_escalated", branch_code=CAM_MRG_041, reason=last_merge_issue or "force_ts_video_reencode", parts=len(parts))

    ts_dir = out_path.parent / f"{out_path.stem}.tsbridge"
    temp_files.append(ts_dir)
    try:
        if ts_dir.exists():
            shutil.rmtree(ts_dir, ignore_errors=True)
        ts_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    def _run_final_ts_merge(ts_concat_list: Path) -> tuple[bool, str, str]:
        if last_merge_issue.startswith("audio_packet_duration_abnormal:"):
            stage_name = f"{_branch_code_tag(CAM_MRG_050)} merge ts-bridge fallback"
            merged_audio_info = probe_audio_stream_info(str(concat_input_parts[0])) if concat_input_parts else {}
            merge_audio_candidates = [_audio_aac_args(merged_audio_info, bitrate) for bitrate in _audio_bitrate_candidates(merged_audio_info)]
            log.info("%s final ts merge switches to audio reencode out=%s issue=%s", _branch_code_tag(CAM_MRG_050), str(out_path), last_merge_issue)
            _emit_phase5_observation(on_observation, phase="merge", event="fallback_escalated", branch_code=CAM_MRG_050, reason=last_merge_issue, parts=len(parts))
            tmerge_prefix = [
                _ffmpeg_bin(), "-y",
                "-analyzeduration", "100M", "-probesize", "100M",
                "-fflags", "+genpts",
                "-f", "concat", "-safe", "0", "-i", str(ts_concat_list),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-avoid_negative_ts", "make_zero",
                "-c:v", "copy",
            ]
            tmerge_suffix = [
                "-movflags", "+faststart",
                str(out_path),
            ]
            ok, _, output, _ = _run_with_audio_candidates(tmerge_prefix, tmerge_suffix, merge_audio_candidates, f"{_branch_code_tag(CAM_MRG_050)} merge ts-bridge fallback audio-reencode")
            return ok, stage_name, output
        stage_name = f"{_branch_code_tag(CAM_MRG_051)} merge ts-bridge fallback"
        log.info("%s final ts merge uses copy path out=%s", _branch_code_tag(CAM_MRG_051), str(out_path))
        tmerge = [
            _ffmpeg_bin(), "-y",
            "-analyzeduration", "100M", "-probesize", "100M",
            "-f", "concat", "-safe", "0", "-i", str(ts_concat_list),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            str(out_path),
        ]
        ok, output, _ = _run_capture(tmerge)
        return ok, stage_name, output

    ts_parts: list[Path] = []
    for idx, p in enumerate(concat_input_parts, start=1):
        ts = ts_dir / f"part{idx:03d}.ts"
        temp_files.append(ts)
        try:
            ts.unlink(missing_ok=True)
        except Exception:
            pass
        audio_info = probe_audio_stream_info(str(p))
        audio_candidates = [_audio_aac_args(audio_info, bitrate) for bitrate in _audio_bitrate_candidates(audio_info)]
        video_codec = _probe_video_codec_name(p)
        video_bsf_args: list[str] = []
        if video_codec == "h264":
            video_bsf_args = ["-bsf:v", "h264_mp4toannexb"]
        elif video_codec in {"hevc", "h265"}:
            video_bsf_args = ["-bsf:v", "hevc_mp4toannexb"]
        ts_prefix = [
            _ffmpeg_bin(), "-y",
            "-fflags", "+genpts",
            "-i", str(p),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-avoid_negative_ts", "make_zero",
            "-c:v", "copy",
        ]
        ts_copy_suffix = [
            *video_bsf_args,
            "-f", "mpegts",
            str(ts),
        ]
        ts_reencode_suffix = [
            "-f", "mpegts",
            str(ts),
        ]
        ok = False
        failed_cmd: list[str] = []
        failed_output = ""
        failed_code = -1
        if not force_ts_video_reencode:
            log.info("%s ts fallback copy path start idx=%s src=%s out=%s", _branch_code_tag(CAM_MRG_040), idx, str(p), str(ts))
            ok, failed_cmd, failed_output, failed_code = _run_with_audio_candidates(ts_prefix, ts_copy_suffix, audio_candidates, f"{_branch_code_tag(CAM_MRG_040)} ts fallback part {idx}")
        if not ok:
            if _looks_like_disk_full_text(failed_output):
                raise RuntimeError("merge_disk_full")
            if force_ts_video_reencode:
                log.warning(
                    "%s ts fallback part %s skipping copy path and forcing reencode due to merge issue=%s out=%s",
                    _branch_code_tag(CAM_MRG_041),
                    idx,
                    last_merge_issue,
                    str(ts),
                )
            else:
                log.warning(
                    "%s ts fallback part %s copy path failed codec=%s out=%s exit=%s cmd=%s ffmpeg_tail=%s",
                    _branch_code_tag(CAM_MRG_040),
                    idx,
                    video_codec,
                    str(ts),
                    failed_code,
                    " ".join(failed_cmd),
                    failed_output[-1200:],
                )
            retry_prefix = [
                _ffmpeg_bin(), "-y",
                "-fflags", "+genpts",
                "-i", str(p),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-avoid_negative_ts", "make_zero",
            ]
            video_candidates = _video_reencode_candidates(p, prefer_qsv=False)
            log.info("%s ts fallback reencode start idx=%s src=%s out=%s", _branch_code_tag(CAM_MRG_041), idx, str(p), str(ts))
            ok, failed_cmd, failed_output, failed_code = _run_with_av_candidates(retry_prefix, ts_reencode_suffix, video_candidates, audio_candidates, f"{_branch_code_tag(CAM_MRG_041)} ts fallback part {idx} reencode")
        if not ok:
            if _looks_like_disk_full_text(failed_output):
                raise RuntimeError("merge_disk_full")
            log.warning(
                "%s ts fallback part %s reencode path failed codec=%s out=%s exit=%s cmd=%s ffmpeg_tail=%s",
                _branch_code_tag(CAM_MRG_041),
                idx,
                video_codec,
                str(ts),
                failed_code,
                " ".join(failed_cmd),
                failed_output[-1200:],
            )
            raise RuntimeError(f"merge_ts_convert_failed:part{idx}")
        if not ts.exists() or ts.stat().st_size <= 0:
            raise RuntimeError(f"merge_ts_convert_missing:part{idx}")
        log.info(
            "%s ts fallback part accepted idx=%s src=%s out=%s size=%s",
            _branch_code_tag(CAM_MRG_041 if force_ts_video_reencode else CAM_MRG_040),
            idx,
            str(p),
            str(ts),
            int(ts.stat().st_size),
        )
        ts_parts.append(ts)

    ts_list = ts_dir / "concat_ts.txt"
    temp_files.append(ts_list)
    ts_list.write_text(
        "\n".join(["file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'" for p in ts_parts]),
        encoding="utf-8", errors="ignore",
    )
    ok, merge_stage_name, tmerge_output = _run_final_ts_merge(ts_list)
    if not ok:
        if _looks_like_disk_full_text(tmerge_output):
            raise RuntimeError("merge_disk_full")
        log.warning("%s final ts merge ffmpeg command failed out=%s issue=%s", _branch_code_tag(CAM_MRG_050 if last_merge_issue.startswith('audio_packet_duration_abnormal:') else CAM_MRG_051), str(out_path), last_merge_issue)
        raise RuntimeError("merge_all_fallbacks_failed")
    if not _probe_merge_output_issue(merge_stage_name, delete_output_on_failure=False):
        _emit_phase5_observation(on_observation, phase="merge", event="post_merge_repair_considered", branch_code=CAM_MRG_060, reason=last_merge_issue or "merged_output_issue", parts=len(parts))
        ts_repair_issue = str(last_merge_issue or "")
        if ts_repair_issue.startswith("boundary_decode_abnormal:"):
            left_idx, right_idx = _parse_boundary_pair_from_issue(ts_repair_issue, concat_input_parts)
            if 0 < left_idx < right_idx <= len(concat_input_parts):
                log.warning(
                    "%s %s attempting targeted TS boundary repair left=%s right=%s issue=%s",
                    _branch_code_tag(CAM_MRG_010),
                    merge_stage_name,
                    left_idx,
                    right_idx,
                    ts_repair_issue,
                )
                original_parts = list(concat_input_parts)
                try:
                    target_audio_info = probe_audio_stream_info(str(concat_input_parts[0])) if concat_input_parts else {}
                    for repair_idx in (left_idx, right_idx):
                        concat_input_parts[repair_idx - 1] = _canonicalize_part(
                            original_parts[repair_idx - 1],
                            repair_idx,
                            target_audio_info=target_audio_info,
                        )
                        _build_ts_fallback_part(repair_idx, concat_input_parts[repair_idx - 1], ts_parts[repair_idx - 1])
                    ts_list.write_text(
                        "\n".join(["file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'" for p in ts_parts]),
                        encoding="utf-8", errors="ignore",
                    )
                    pair_issue = _run_concat_probe(
                        [concat_input_parts[left_idx - 1], concat_input_parts[right_idx - 1]],
                        f"targeted_ts_boundary_pair_{left_idx:03d}_{right_idx:03d}",
                    )
                    if not pair_issue:
                        ok, merge_stage_name, tmerge_output = _run_final_ts_merge(ts_list)
                        if ok and _probe_merge_output_issue(merge_stage_name, delete_output_on_failure=False):
                            return temp_files
                    log.warning(
                        "%s %s targeted TS boundary repair did not clear issue left=%s right=%s pair_issue=%s merge_issue=%s",
                        _branch_code_tag(CAM_MRG_010),
                        merge_stage_name,
                        left_idx,
                        right_idx,
                        pair_issue or "",
                        last_merge_issue or ts_repair_issue,
                    )
                except Exception as targeted_ts_err:
                    log.warning(
                        "%s %s targeted TS boundary repair failed left=%s right=%s issue=%s err=%s",
                        _branch_code_tag(CAM_MRG_010),
                        merge_stage_name,
                        left_idx,
                        right_idx,
                        ts_repair_issue,
                        targeted_ts_err,
                    )
                finally:
                    concat_input_parts = original_parts
        if (
            ts_repair_issue.startswith("merge_boundary_video_gap:")
            or ts_repair_issue.startswith("stream_end_gap_abnormal:")
            or ts_repair_issue.startswith("boundary_decode_abnormal:")
        ):
            ts_repaired_out = out_path.with_name(out_path.stem + ".tsrepaired.mp4")
            try:
                ts_repaired_out.unlink(missing_ok=True)
            except Exception:
                pass
            if _repair_ts_list_with_cfr(merge_stage_name, ts_repair_issue, ts_list, ts_repaired_out) and ts_repaired_out.exists():
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass
                ts_repaired_out.replace(out_path)
                repaired_issue = _probe_structural_media_issue(out_path, include_packet_metrics=False)
                if not repaired_issue:
                    repaired_issue = _probe_merge_boundary_decode_issue(out_path, _cumulative_boundaries_sec())
                if not repaired_issue:
                    repaired_issue = _probe_merge_boundary_video_gap_issue(out_path)
                if not repaired_issue:
                    _emit_phase5_observation(on_observation, phase="merge", event="post_merge_repair_succeeded", branch_code="CAM-MRG-062", reason=ts_repair_issue, parts=len(parts))
                    return temp_files
                last_merge_issue = str(repaired_issue or "")
                log.warning("%s %s final TS CFR repair still abnormal out=%s issue=%s", _branch_code_tag("CAM-MRG-062"), merge_stage_name, str(out_path), last_merge_issue)
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                ts_repaired_out.unlink(missing_ok=True)
            except Exception:
                pass
        if _repair_merged_output(merge_stage_name, last_merge_issue):
            _emit_phase5_observation(on_observation, phase="merge", event="post_merge_repair_succeeded", branch_code=CAM_MRG_060, reason=last_merge_issue or "merged_output_issue", parts=len(parts))
            return temp_files
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        log.warning("%s final ts merge rejected after structural probe out=%s issue=%s", _branch_code_tag(CAM_MRG_060), str(out_path), last_merge_issue)
        raise RuntimeError("merge_output_structural_invalid:ts_bridge_rejected")
    return temp_files


def _looks_like_disk_full_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return "no space left on device" in lowered or "disk full" in lowered or "error code: -28" in lowered


async def run_camera_task(raw: dict[str, Any], simulate: bool, on_progress) -> list[dict[str, Any]]:
    log = logging.getLogger("edge.runner")
    plan = build_camera_plan(raw, simulate)
    task_id = str(raw.get("taskId") or raw.get("id") or "")
    try:
        server_task_id = str(int(str(task_id).strip()))
    except Exception:
        server_task_id = str(task_id).strip() or "0"
    lesson_id = str(raw.get("lessonId") or "").strip() or "0"
    try:
        task_type = int(raw.get("taskType") or 0)
    except Exception:
        task_type = 0
    db_path = str(raw.get("__db_path") or "").strip() or None
    start_step = str(raw.get("__startStep") or "").strip().upper()

    nvr = raw.get("nvr") if isinstance(raw.get("nvr"), dict) else {}
    nvr_provider = _resolve_nvr_provider(raw, nvr)
    ip = str(
        raw.get("nvrAddress")
        or raw.get("nvrIp")
        or raw.get("ipAddress")
        or nvr.get("nvrAddress")
        or nvr.get("nvrIp")
        or nvr.get("ipAddress")
        or ""
    ).strip()
    port = int(raw.get("nvrPort") or nvr.get("nvrPort") or nvr.get("port") or raw.get("port") or 8000)
    nvr_device_id_value = int(raw.get("nvrDeviceId") or nvr.get("nvrDeviceId") or 0) or None
    username = str(raw.get("nvrAccount") or raw.get("account") or nvr.get("nvrAccount") or nvr.get("account") or "").strip()
    password = str(raw.get("nvrPassword") or raw.get("password") or nvr.get("nvrPassword") or nvr.get("password") or "").strip()
    channel = int(raw.get("nvrChannelNum") or nvr.get("nvrChannelNum") or raw.get("nvrChannelId") or nvr.get("nvrChannelId") or 1)
    expected_download_identity = _download_source_identity(
        nvr_device_id=nvr_device_id_value,
        ip=ip,
        port=port,
        web_channel=channel,
        provider=nvr_provider,
    )
    start_at = _parse_dt(str(raw.get("lessonStartAt") or ""))
    end_at = _parse_dt(str(raw.get("lessonEndAt") or ""))
    start_at, end_at = _align_to_lesson_sync(db_path, lesson_id, task_type, start_at, end_at)
    if end_at <= start_at:
        raise RuntimeError("invalid lesson time range")

    if not ip or not username:
        raise RuntimeError("missing nvr connection params")

    root = Path(_load_download_path())
    lesson_date = _resolve_lesson_date(raw.get("lessonDate"), start_at.isoformat(), default=start_at.strftime("%Y-%m-%d"))
    out_dir = _get_lesson_dir(lesson_date, lesson_id, root)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        base_dir = root if root.name.lower() == "videos" else (root / "Videos")
        raise RuntimeError(f"download_path_not_writable:{str(base_dir)}") from e

    prefix = _task_type_prefix(task_type)
    downloaded = out_dir / f"{prefix}_{server_task_id}.mp4"

    log.info(
        "download start task=%s lesson=%s channel=%s ip=%s start=%s end=%s out=%s",
        server_task_id,
        lesson_id,
        str(channel),
        ip,
        start_at.isoformat(),
        end_at.isoformat(),
        str(downloaded),
    )

    if not is_task_step_enabled("DOWNLOAD"):
        on_progress("DOWNLOAD", 1.0, "DOWNLOAD 已关闭，跳过执行")
        if not is_task_step_enabled("TRANSCODE"):
            on_progress("TRANSCODE", 1.0, "TRANSCODE 已关闭，跳过执行")
            return []
        if not downloaded.exists() or downloaded.stat().st_size < 1024:
            on_progress("TRANSCODE", 1.0, "TRANSCODE 无可用源视频，跳过执行")
            return []
        on_progress("TRANSCODE", 0.0, "转码中")
        if ffmpeg_exists():
            m3u8_path, _dl_size, tc_size = await _do_hls_transcode(
                downloaded, out_dir, raw, on_progress, server_task_id, log_prefix="transcode-skip-download"
            )
            return [
                {"path": m3u8_path, "sizeBytes": tc_size, "stepCode": "TRANSCODE", "fileType": "transcoded_video"},
            ]
        for i in range(plan.transcode_seconds):
            await asyncio.sleep(1)
            p = (i + 1) / float(plan.transcode_seconds)
            on_progress("TRANSCODE", p, f"TRANSCODE {int(p*100)}%")
        tc_size = int(downloaded.stat().st_size) if downloaded.exists() else 0
        return [
            {"path": str(downloaded), "sizeBytes": tc_size, "stepCode": "TRANSCODE", "fileType": "transcoded_video"},
        ]

    _dl_start_mono = time.monotonic()
    
    # 先扩展时间范围，添加前后padding，再进行分段
    # 这样分段之间不会有重叠，也不需要在每段下载时单独处理padding
    download_padding_sec = int(os.getenv("HIK_DOWNLOAD_PADDING_SECONDS", "0"))
    download_start = start_at - timedelta(seconds=download_padding_sec)
    download_end = end_at + timedelta(seconds=download_padding_sec)
    resume_state_path = _download_resume_state_path(out_dir, prefix, server_task_id)
    process_state_path = _process_resume_state_path(out_dir, prefix, server_task_id)
    existing_part_done_artifacts = list(out_dir.glob(f"{prefix}_{server_task_id}.part*.done"))
    existing_resume_artifacts = list(existing_part_done_artifacts)
    if not existing_resume_artifacts:
        existing_resume_artifacts = list(out_dir.glob(f"{prefix}_{server_task_id}.part*.mp4"))
    if not existing_resume_artifacts and not downloaded.exists():
        _clear_download_resume_state(resume_state_path)
        _clear_process_resume_state(process_state_path)
    resume_state = _load_download_resume_state(resume_state_path)
    process_resume_state = _load_process_resume_state(process_state_path)
    resume_subphase = str(process_resume_state.get("subphase") or resume_state.get("subphase") or "").strip().lower()
    explicit_reprocess = str(raw.get("__reprocessDownload") or "").strip() == "1"
    started_at_epoch = float(resume_state.get("started_at_epoch") or 0.0)
    if started_at_epoch <= 0:
        started_at_epoch = time.time()
        _save_download_resume_state(resume_state_path, started_at_epoch)
    if float(process_resume_state.get("started_at_epoch") or 0.0) <= 0:
        _save_process_resume_state(process_state_path, started_at_epoch)
    if resume_subphase:
        log.info(
            "download resume state detected task=%s subphase=%s download_state=%s process_state=%s",
            server_task_id,
            resume_subphase,
            str(resume_state_path),
            str(process_state_path),
        )

    def _safe_elapsed(raw_value: Any) -> float:
        try:
            return max(0.0, float(raw_value or 0.0))
        except Exception:
            return 0.0

    _segment_download_elapsed_base_seconds = max(
        _safe_elapsed(resume_state.get("segment_download_elapsed_seconds")),
        _safe_elapsed(resume_state.get("download_elapsed_seconds")),
    )
    _segment_download_elapsed_seconds = _segment_download_elapsed_base_seconds
    _segment_download_started_mono = time.monotonic()
    _segment_download_active = not explicit_reprocess and resume_subphase not in {"segment_process", "merge", "source_finalize"}
    _process_elapsed_base_seconds = 0.0 if explicit_reprocess else _safe_elapsed(process_resume_state.get("process_elapsed_seconds"))

    log.info(
        "download time range expanded: %s ~ %s -> %s ~ %s (padding=%ss)",
        start_at.isoformat(), end_at.isoformat(),
        download_start.isoformat(), download_end.isoformat(),
        download_padding_sec,
    )
    
    explicit_seg = str(os.getenv("EDGE_HIK_SEGMENT_SEC") or "").strip()
    adaptive_segmenting = not bool(explicit_seg)
    seg_sec = _segment_seconds()
    ranges = _segment_ranges(download_start, download_end, seg_sec)  # 使用扩展后的时间
    total_parts = len(ranges)
    _download_plan = {"total_parts": int(total_parts)}
    _download_total_seconds = max(1.0, float((download_end - download_start).total_seconds()))
    _process_started_mono = 0.0
    _phase5_observations: list[dict[str, Any]] = []
    log.info(
        "download segment policy task=%s seg_sec=%s total_parts=%s target_mb=%s est_bps=%s explicit_seg=%s",
        server_task_id,
        seg_sec,
        total_parts,
        _segment_target_mb(),
        _segment_estimated_bps(),
        explicit_seg or "",
    )

    def _fmt_speed(bps: float) -> str:
        try:
            return f"{(float(bps) / 1024.0 / 1024.0):.2f}MB/s"
        except Exception:
            return "0.00MB/s"

    def _fmt_mb(v: float) -> str:
        try:
            return f"{float(v):.2f}MB"
        except Exception:
            return "0.00MB"

    def _download_elapsed_seconds() -> float:
        nonlocal _segment_download_elapsed_seconds
        if not _segment_download_active:
            return max(0.0, float(_segment_download_elapsed_seconds))
        live_elapsed = max(0.0, time.monotonic() - float(_segment_download_started_mono))
        current_elapsed = max(0.0, float(_segment_download_elapsed_base_seconds) + live_elapsed)
        _segment_download_elapsed_seconds = max(float(_segment_download_elapsed_seconds), current_elapsed)
        return max(0.0, float(_segment_download_elapsed_seconds))

    def _process_elapsed_seconds() -> float:
        try:
            if _process_started_mono <= 0:
                return max(0.0, float(_process_elapsed_base_seconds))
            return max(0.0, float(_process_elapsed_base_seconds) + (time.monotonic() - float(_process_started_mono)))
        except Exception:
            return max(0.0, float(_process_elapsed_base_seconds))

    def _download_total_elapsed_seconds() -> float:
        return max(0.0, float(_download_elapsed_seconds()) + float(_process_elapsed_seconds()))

    def _freeze_segment_download_elapsed() -> None:
        nonlocal _segment_download_active, _segment_download_elapsed_seconds
        if not _segment_download_active:
            return
        _segment_download_elapsed_seconds = max(float(_segment_download_elapsed_seconds), float(_download_elapsed_seconds()))
        _segment_download_active = False

    def _make_process_progress_message(order: int, total_to_process: int, process_meta: dict[str, Any], status_message: str) -> str:
        formatted = _format_process_stage_status_message(
            status_message,
            classification=str(process_meta.get("normalize_classification") or ""),
            normalize_reason=str(process_meta.get("normalize_reason") or ""),
        )
        try:
            process_started_mono = float(process_meta.get("process_started_mono") or 0.0)
        except Exception:
            process_started_mono = 0.0
        part_elapsed = max(0.0, time.monotonic() - process_started_mono) if process_started_mono > 0 else 0.0
        total_elapsed = max(0.0, float(_download_total_elapsed_seconds() or 0.0))
        elapsed_suffix = f"，已耗时{_fmt_elapsed_short(part_elapsed)}，共耗时{_fmt_elapsed_short(total_elapsed)}"
        if int(total_to_process or 0) <= 1:
            return f"当前分段处理中，{formatted}{elapsed_suffix}"
        return f"集中处理第{order}/{total_to_process}段，{formatted}{elapsed_suffix}"

    def _make_process_start_message(order: int, total_to_process: int, process_meta: dict[str, Any]) -> str:
        try:
            process_started_mono = float(process_meta.get("process_started_mono") or 0.0)
        except Exception:
            process_started_mono = 0.0
        part_elapsed = max(0.0, time.monotonic() - process_started_mono) if process_started_mono > 0 else 0.0
        total_elapsed = max(0.0, float(_download_total_elapsed_seconds() or 0.0))
        elapsed_suffix = f"，已耗时{_fmt_elapsed_short(part_elapsed)}，共耗时{_fmt_elapsed_short(total_elapsed)}"
        if int(total_to_process or 0) <= 1:
            return f"分段下载完成，正在处理当前分段{elapsed_suffix}"
        return f"分段下载完成，正在集中处理第{order}/{total_to_process}段{elapsed_suffix}"

    def _build_process_state_snapshot() -> tuple[list[int], list[str]]:
        processed_parts: list[int] = []
        merge_paths: list[str] = []
        for idx, merge_path in enumerate(merge_part_paths, start=1):
            if idx > len(part_paths):
                break
            part_path = part_paths[idx - 1]
            if not part_path.exists() or part_path.stat().st_size <= 0:
                continue
            done_path = out_dir / f"{prefix}_{server_task_id}.part{idx:03d}.done"
            done_meta = _load_part_done_meta(done_path)
            if not _is_download_part_process_completed(done_meta):
                continue
            processed_parts.append(idx)
            merge_paths.append(str(merge_path))
        return processed_parts, merge_paths

    def _save_download_phase_state(subphase: str, *, current_part_idx: int = 0, process_index: int = 0, process_total: int = 0) -> None:
        nonlocal resume_state, process_resume_state
        processed_parts, merge_paths = _build_process_state_snapshot()
        now_epoch = float(time.time())
        subphase_text = str(subphase or "")
        current_part_value = int(current_part_idx or 0)
        total_parts_value = int(_download_plan.get("total_parts") or total_parts or 0)
        process_index_value = int(process_index or 0)
        process_total_value = int(process_total or 0)
        download_elapsed_value = float(_download_elapsed_seconds())
        process_elapsed_value = float(_process_elapsed_seconds())
        _save_download_resume_state(
            resume_state_path,
            started_at_epoch,
            subphase=subphase_text,
            current_part_idx=current_part_value,
            total_parts=total_parts_value,
            process_index=process_index_value,
            process_total=process_total_value,
            processed_parts=processed_parts,
            merge_part_paths=merge_paths,
            download_elapsed_seconds=download_elapsed_value,
            segment_download_elapsed_seconds=download_elapsed_value,
            process_elapsed_seconds=process_elapsed_value,
            updated_at_epoch=now_epoch,
        )
        resume_state = _load_download_resume_state(resume_state_path)
        if subphase_text in {"segment_process", "merge", "source_finalize"}:
            _save_process_resume_state(
                process_state_path,
                started_at_epoch,
                subphase=subphase_text,
                current_part_idx=current_part_value,
                total_parts=total_parts_value,
                process_index=process_index_value,
                process_total=process_total_value,
                processed_parts=processed_parts,
                merge_part_paths=merge_paths,
                segment_download_elapsed_seconds=download_elapsed_value,
                process_elapsed_seconds=process_elapsed_value,
                phase5_observations=list(_phase5_observations[-200:]),
                updated_at_epoch=now_epoch,
            )
            process_resume_state = _load_process_resume_state(process_state_path)

    def _record_phase5_observation(event: dict[str, Any]) -> None:
        payload = dict(event or {})
        payload["task_id"] = server_task_id
        payload["lesson_id"] = lesson_id
        payload["created_at_epoch"] = float(time.time())
        _phase5_observations.append(payload)
        if len(_phase5_observations) > 200:
            del _phase5_observations[:-200]
        log.info("phase5 observation task=%s payload=%s", server_task_id, json.dumps(payload, ensure_ascii=False, default=str))

    _download_status = {"phase": "exploring", "message": "通道探索中"}

    def _is_downloading_status(text: str, progress: float) -> bool:
        msg = str(text or "").strip()
        if not msg:
            return False
        if progress > 0:
            return True
        return (
            msg.startswith("SDK ")
            or "% ch=" in msg
        )

    def _is_reusing_status(text: str) -> bool:
        msg = str(text or "").strip()
        if not msg:
            return False
        return (
            "复用上次成功通道" in msg
            or "复用已确认通道" in msg
        )

    def _is_exploring_status(text: str) -> bool:
        msg = str(text or "").strip()
        if not msg:
            return False
        return (
            "通道计算中" in msg
            or "通道探索中" in msg
            or "开始下载" in msg
            or "探索" in msg
            or "候选通道" in msg
            or "尝试SDK下载" in msg
        )

    def _on_download_status(p: float, message: str) -> None:
        text = str(message or "").strip()
        if text:
            _download_status["message"] = text
        if _is_downloading_status(text, float(p or 0.0)):
            _download_status["phase"] = "downloading"
            return
        if _is_reusing_status(text):
            _download_status["phase"] = "reusing"
            on_progress("DOWNLOAD", 0.0, text)
            return
        if _is_exploring_status(text):
            _download_status["phase"] = "exploring"
            on_progress("DOWNLOAD", 0.0, "通道探索中")
            return
        if str(_download_status.get("phase") or "exploring") != "downloading":
            on_progress("DOWNLOAD", 0.0, text or "通道探索中")

    def _download_overall_progress(downloaded_duration_sec: float) -> float:
        capped_duration = min(_download_total_seconds, max(0.0, float(downloaded_duration_sec)))
        return min(0.96, (capped_duration / _download_total_seconds) * 0.96)

    def _download_part_done_progress(part_idx: int) -> float:
        part_duration_sec = max(1.0, float(_part_duration_sec.get(int(part_idx)) or 0.0))
        return _download_overall_progress(float(_completed_duration_sec) + part_duration_sec)

    def _on_part_progress(part_idx: int, p: float, size_bytes: int, speed_bps: float) -> None:
        if str(_download_status.get("phase") or "exploring") != "downloading":
            _download_status["phase"] = "downloading"
        clamped = _clamp(p)
        total_parts_now = int(_download_plan.get("total_parts") or total_parts or 1)
        part_duration_sec = max(1.0, float(_part_duration_sec.get(int(part_idx)) or 0.0))
        expected_bytes = float(_part_expected_bytes.get(int(part_idx)) or 0.0)
        byte_ratio = min(1.0, float(size_bytes) / expected_bytes) if expected_bytes > 0 else 0.0
        if clamped >= 1.0:
            visible_part = 1.0
        elif float(size_bytes) <= 0:
            visible_part = min(clamped, 0.02)
        else:
            state = _part_visual_progress.get(int(part_idx))
            now = time.monotonic()
            if state is None:
                state = {"shown": 0.0, "ts": now}
                _part_visual_progress[int(part_idx)] = state
            prev = float(state.get("shown") or 0.0)
            prev_ts = float(state.get("ts") or now)
            max_step = max(0.03, (now - prev_ts) * 0.18)
            target = max(byte_ratio, min(clamped, byte_ratio + 0.10))
            visible_part = min(target, prev + max_step)
            if visible_part < prev:
                visible_part = prev
            state["shown"] = visible_part
            state["ts"] = now
        downloaded_duration_sec = min(_download_total_seconds, float(_completed_duration_sec) + visible_part * part_duration_sec)
        overall = _download_overall_progress(downloaded_duration_sec)
        downloaded_mb = float(size_bytes) / 1024.0 / 1024.0
        display_total_bytes = float(_part_display_total_bytes.get(int(part_idx)) or 0.0)
        if display_total_bytes <= 0.0 and expected_bytes > 0.0:
            display_total_bytes = expected_bytes
            _part_display_total_bytes[int(part_idx)] = display_total_bytes
        elif display_total_bytes <= 0.0 and visible_part >= 0.20:
            display_total_bytes = max(1.0, float(size_bytes) / max(0.01, visible_part))
            _part_display_total_bytes[int(part_idx)] = display_total_bytes
        if display_total_bytes > 0.0:
            total_mb = max(downloaded_mb, display_total_bytes / 1024.0 / 1024.0)
            total_str = f"{total_mb:.0f}MB" if total_mb < 1024.0 else f"{total_mb / 1024.0:.2f}GB"
        else:
            total_str = "计算中"
        part_started = _part_started_at.get(int(part_idx)) or _dl_start_mono
        part_elapsed = max(0.0, time.monotonic() - float(part_started))
        part_h = int(part_elapsed) // 3600
        part_m = (int(part_elapsed) % 3600) // 60
        part_s = int(part_elapsed) % 60
        if part_h > 0:
            part_elapsed_str = f"{part_h}时{part_m}分{part_s}秒"
        elif part_m > 0:
            part_elapsed_str = f"{part_m}分{part_s}秒"
        else:
            part_elapsed_str = f"{part_s}秒"
        dl_elapsed = _download_elapsed_seconds()
        dl_h = int(dl_elapsed) // 3600
        dl_m = (int(dl_elapsed) % 3600) // 60
        dl_s = int(dl_elapsed) % 60
        if dl_h > 0:
            dl_elapsed_str = f"{dl_h}时{dl_m}分{dl_s}秒"
        elif dl_m > 0:
            dl_elapsed_str = f"{dl_m}分{dl_s}秒"
        else:
            dl_elapsed_str = f"{dl_s}秒"
        detail = f"{_fmt_speed(speed_bps)}，分段{part_idx}/{total_parts_now}，已下载{_fmt_mb(downloaded_mb)}/{total_str}，本段耗时{part_elapsed_str}，总耗时{dl_elapsed_str}"
        on_progress("DOWNLOAD", overall, detail)
        if not hasattr(_on_part_progress, "_last_log"):
            _on_part_progress._last_log = {"pct": -1, "t": 0.0}
        last = _on_part_progress._last_log
        pct = int(min(100.0, max(0.0, overall * 100.0)))
        now = time.monotonic()
        if pct != last["pct"] and (pct % 10 == 0 or now - last["t"] > 20):
            last["pct"] = pct
            last["t"] = now
            log.info("download progress task=%s part=%s/%s pct=%s msg=%s", server_task_id, part_idx, total_parts_now, pct, detail)

    def _clamp(v: float) -> float:
        try:
            if v != v:
                return 0.0
            if v < 0:
                return 0.0
            if v > 1:
                return 1.0
            return float(v)
        except Exception:
            return 0.0

    part_paths: list[Path] = []
    _hint_uid: int | None = None
    _hint_channel: int | None = None
    _hint_record_type: int | None = None
    _task_locked_channel: int | None = None
    _task_locked_record_type: int | None = None
    _persisted_fail_count = 0
    _disabled_history_channels: set[int] = set()
    _part_channel_used: dict[int, int] = {}
    shared_download_session = None
    shared_session_error: str = ""
    nvr_sdk_gate = _get_nvr_sdk_download_semaphore(nvr_device_id_value, ip, port)
    _part_started_at: dict[int, float] = {}
    _part_duration_sec: dict[int, float] = {}
    _part_expected_bytes: dict[int, float] = {}
    _part_display_total_bytes: dict[int, float] = {}
    _part_visual_progress: dict[int, dict[str, float]] = {}
    _completed_duration_sec = 0.0
    current_seg_sec = int(seg_sec)
    current_start = download_start  # 使用扩展后的开始时间
    part_idx = 1
    pending_reprocess_parts: set[int] = set()
    pending_reprocess_meta: dict[int, dict[str, Any]] = {}
    reused_part_meta: list[dict[str, Any]] = []
    merge_part_paths: list[Path] = []
    _force_canonical_merge = False
    _force_canonical_merge_reasons: list[str] = []
    _canonicalize_merge_parts: set[int] = set()
    pending_process_parts: list[tuple[int, Path, Path, dict[str, Any], str]] = []
    cleanup_targets: list[Path] = []
    download_batch_av_policy = DownloadBatchAvPolicy()
    skip_download_pipeline = bool(resume_subphase == "source_finalize" and downloaded.exists() and downloaded.stat().st_size > 1024)
    process_only_download = bool(explicit_reprocess and not skip_download_pipeline)
    try:
        if total_parts > 1 and not skip_download_pipeline and not process_only_download:
            reconcile = await asyncio.to_thread(
                _reconcile_download_resume_parts,
                out_dir=out_dir,
                prefix=prefix,
                server_task_id=server_task_id,
                download_start=download_start,
                download_end=download_end,
                initial_seg_sec=int(seg_sec),
                adaptive_segmenting=adaptive_segmenting,
                expected_identity=expected_download_identity,
                log=log,
            )
            current_start = reconcile.current_start
            current_seg_sec = int(reconcile.current_seg_sec)
            part_idx = int(reconcile.part_idx)
            _completed_duration_sec = float(reconcile.completed_duration_sec)
            pending_reprocess_parts = set(int(x) for x in reconcile.pending_reprocess)
            pending_reprocess_meta = dict(reconcile.pending_reprocess_meta or {})
            reused_part_meta = [dict(x) for x in (reconcile.reused_part_meta or ())]
            for meta in reused_part_meta:
                restored_part = Path(str(meta.get("part_path") or "").strip())
                restored_merge_part = Path(str(meta.get("merge_part_path") or "").strip() or str(restored_part))
                part_paths.append(restored_part)
                merge_part_paths.append(restored_merge_part)
                restored_idx = int(meta.get("part_idx") or 0)
                restored_duration = float(meta.get("part_duration_sec") or 0.0)
                if restored_idx > 0 and restored_duration > 0:
                    _part_duration_sec[restored_idx] = restored_duration
                restored_force_canonical_raw = meta.get("force_canonical_merge", None)
                restored_canonical_part_raw = meta.get("canonicalize_merge_part", None)
                if restored_force_canonical_raw is None and restored_canonical_part_raw is None:
                    restored_force_canonical, restored_canonical_part, _ = _resolve_download_merge_policy(str(meta.get("normalize_classification") or meta.get("normalize_reason") or ""))
                else:
                    restored_force_canonical = bool(restored_force_canonical_raw)
                    restored_canonical_part = bool(restored_canonical_part_raw)
                if restored_force_canonical:
                    _force_canonical_merge = True
                    _force_canonical_merge_reasons.append(f"part{restored_idx:03d}:{str(meta.get('normalize_reason') or meta.get('normalize_classification') or 'resume_restore')}")
                restored_merge_already_materialized = restored_merge_part.exists() and restored_merge_part != restored_part
                if (restored_force_canonical or restored_canonical_part) and not restored_merge_already_materialized:
                    _canonicalize_merge_parts.add(restored_idx)
                restored_channel = int(meta.get("channel_used") or 0)
                restored_record_type = int(meta.get("record_type_used") or 0)
                if restored_channel > 0:
                    _part_channel_used[restored_idx] = restored_channel
                    if _task_locked_channel is None:
                        _task_locked_channel = restored_channel
                        _task_locked_record_type = restored_record_type if restored_record_type >= 0 else None
                        _hint_uid = None
                        _hint_channel = restored_channel
                        _hint_record_type = _task_locked_record_type
                        log.info("download task lock restored from reconciled part task=%s sdk_channel=%s part=%s", server_task_id, restored_channel, restored_idx)
                    elif _task_locked_channel != restored_channel:
                        raise RuntimeError(f"同一任务下载通道不一致：已锁定SDK通道{_task_locked_channel}，已复用分段为SDK通道{restored_channel}")
            for restored_idx in sorted(pending_reprocess_parts):
                restored_part = out_dir / f"{prefix}_{server_task_id}.part{int(restored_idx):03d}.mp4"
                restored_done = out_dir / f"{prefix}_{server_task_id}.part{int(restored_idx):03d}.done"
                restored_meta = dict(pending_reprocess_meta.get(int(restored_idx)) or {})
                if _is_download_part_process_completed(restored_meta):
                    continue
                if not restored_part.exists() or restored_part.stat().st_size <= 0:
                    continue
                part_paths.append(restored_part)
                merge_part_paths.append(restored_part)
                try:
                    restored_duration = float(restored_meta.get("requested_sec") or 0.0)
                except Exception:
                    restored_duration = 0.0
                if restored_duration <= 0.0:
                    range_start_raw = str(restored_meta.get("range_start") or "").strip()
                    range_end_raw = str(restored_meta.get("range_end") or "").strip()
                    try:
                        restored_duration = max(1.0, float((_parse_dt(range_end_raw) - _parse_dt(range_start_raw)).total_seconds()))
                    except Exception:
                        restored_duration = 0.0
                if restored_duration > 0.0:
                    _part_duration_sec[int(restored_idx)] = restored_duration
                restored_meta["process_completed"] = False
                pending_process_parts.append((int(restored_idx), restored_part, restored_done, restored_meta, "恢复处理"))
                pending_reprocess_parts.discard(int(restored_idx))
                log.info(
                    "download resume reprocess restored task=%s part=%s raw=%s done=%s",
                    server_task_id,
                    int(restored_idx),
                    str(restored_part),
                    str(restored_done),
                )
            if reconcile.status_message:
                rollback_progress = _download_overall_progress(float(_completed_duration_sec))
                raw["__allowProgressRollback"] = "DOWNLOAD"
                on_progress(
                    "DOWNLOAD",
                    rollback_progress,
                    f"恢复下载：{reconcile.status_message}，当前从第{int(part_idx)}段继续，进度已回退到清理后的可恢复位置",
                )
                log.info(
                    "download resume reconciled task=%s next_part=%s completed_duration_sec=%.3f progress=%.2f%% msg=%s",
                    server_task_id,
                    int(part_idx),
                    float(_completed_duration_sec),
                    float(rollback_progress) * 100.0,
                    reconcile.status_message,
                )
        if process_only_download:
            on_progress("DOWNLOAD", 0.0, "重新处理模式：跳过重新下载，正在扫描原始分段")
            log.info("download reprocess mode task=%s skip_new_download=true", server_task_id)
            raw_reprocess_parts: list[tuple[int, Path]] = []
            raw_prefix = f"{prefix}_{server_task_id}.part"
            for candidate in sorted(out_dir.glob(f"{prefix}_{server_task_id}.part*.mp4")):
                name = candidate.name
                if not name.startswith(raw_prefix) or not name.endswith(".mp4"):
                    continue
                part_token = name[len(raw_prefix):-4]
                if not part_token.isdigit():
                    continue
                if candidate.exists() and candidate.stat().st_size > 0:
                    raw_reprocess_parts.append((int(part_token), candidate))
            if not raw_reprocess_parts and downloaded.exists() and downloaded.stat().st_size > 0:
                raw_reprocess_parts.append((1, downloaded))
            if not raw_reprocess_parts:
                raise RuntimeError("reprocess_raw_part_missing:1")
            total_parts = len(raw_reprocess_parts)
            _download_plan["total_parts"] = int(total_parts)
            _download_total_seconds = 0.0
            for idx, part in raw_reprocess_parts:
                done = out_dir / f"{prefix}_{server_task_id}.part{idx:03d}.done" if len(raw_reprocess_parts) > 1 else downloaded.with_suffix(".done")
                done_meta_payload = _load_part_done_meta(done)
                if not isinstance(done_meta_payload, dict):
                    done_meta_payload = {}
                part_duration = float(probe_duration_seconds(str(part)) or 0.0)
                if part_duration <= 0.0:
                    try:
                        part_duration = float(done_meta_payload.get("requested_sec") or 0.0)
                    except Exception:
                        part_duration = 0.0
                part_duration = max(1.0, part_duration)
                _download_total_seconds += part_duration
                _part_duration_sec[idx] = part_duration
                _part_expected_bytes[idx] = max(1.0, float(part.stat().st_size))
                part_paths.append(part)
                merge_part_paths.append(part)
                done_meta_payload = {
                    **expected_download_identity,
                    **done_meta_payload,
                    "requested_sec": part_duration,
                    "size_bytes": int(part.stat().st_size),
                    "process_completed": False,
                }
                done_meta_payload.pop("merge_part_path", None)
                done_meta_payload.pop("normalize_reason", None)
                done_meta_payload.pop("normalize_classification", None)
                done_meta_payload.pop("force_canonical_merge", None)
                done_meta_payload.pop("canonicalize_merge_part", None)
                done_meta_payload.pop("merge_risk_level", None)
                done_meta_payload.pop("process_started_mono", None)
                _save_part_done_meta(done, done_meta_payload)
                pending_process_parts.append((idx, part, done, done_meta_payload, "重新处理"))
                _save_download_phase_state("segment_download", current_part_idx=idx)
                _on_part_progress(idx, 1.0, int(part.stat().st_size), 0.0)
                log.info(
                    "download reprocess raw part queued task=%s part=%s/%s raw=%s duration=%.3fs size=%s",
                    server_task_id,
                    idx,
                    total_parts,
                    str(part),
                    part_duration,
                    int(part.stat().st_size),
                )
            _completed_duration_sec = min(_download_total_seconds, sum(float(_part_duration_sec.get(idx) or 0.0) for idx, _part in raw_reprocess_parts))
        if total_parts > 1 and not process_only_download:
            async def _open_shared_download_session():
                try:
                    async with nvr_sdk_gate:
                        log.info("download nvr sdk gate acquired task=%s key=%s action=open_session limit=%s", server_task_id, _nvr_download_limit_key(nvr_device_id_value, ip, port), PER_NVR_SDK_DOWNLOAD_LIMIT)
                        session = await asyncio.to_thread(
                            open_download_session,
                            provider=nvr_provider,
                            sdk_dir=None,
                            db_path=str(raw.get("__db_path") or "").strip() or None,
                            nvr_device_id=nvr_device_id_value,
                            ip=ip,
                            port=port,
                            username=username,
                            password=password,
                            channel=channel,
                            device_model=str(raw.get("deviceModel") or raw.get("nvrModel") or nvr.get("deviceModel") or nvr.get("nvrModel") or "") or None,
                        )
                    log.info("download shared SDK session ready task=%s parts=%s", server_task_id, total_parts)
                    return session, ""
                except Exception as e:
                    session_error = str(e or "").strip()
                    if session_error.startswith("NVR设备掉线或网络不可达") or session_error.startswith("NVR设备连接失败"):
                        try:
                            on_progress("DOWNLOAD", 0.0, session_error)
                        except Exception:
                            pass
                        log.warning("download shared SDK session unavailable task=%s: %s", server_task_id, session_error)
                        raise RuntimeError(session_error)
                    log.warning("download shared SDK session unavailable task=%s: %s; fallback to per-part flow", server_task_id, e)
                    return None, session_error

            try:
                shared_download_session, shared_session_error = await _open_shared_download_session()
            except Exception:
                shared_download_session = None

        @contextlib.asynccontextmanager
        async def _with_nvr_sdk_gate(action: str):
            key = _nvr_download_limit_key(nvr_device_id_value, ip, port)
            await nvr_sdk_gate.acquire()
            try:
                log.info("download nvr sdk gate acquired task=%s key=%s action=%s limit=%s", server_task_id, key, action, PER_NVR_SDK_DOWNLOAD_LIMIT)
                yield
            finally:
                nvr_sdk_gate.release()
                log.info("download nvr sdk gate released task=%s key=%s action=%s", server_task_id, key, action)

        def _is_allowed_restored_channel(channel_used: int) -> bool:
            try:
                ch = int(channel_used or 0)
            except Exception:
                return False
            if ch <= 0:
                return False
            if _task_locked_channel is not None:
                return ch == int(_task_locked_channel)
            session_obj = shared_download_session
            persisted_sdk_channel = int(getattr(session_obj, "persisted_sdk_channel", 0) or 0) if session_obj is not None else 0
            channel_offset = getattr(session_obj, "channel_offset", None) if session_obj is not None else None
            sdk_start_dchan = int(getattr(session_obj, "sdk_start_dchan", 0) or 0) if session_obj is not None else 0
            ip_chan_num = int(getattr(session_obj, "ip_chan_num", 0) or 0) if session_obj is not None else 0
            if persisted_sdk_channel > 0:
                return ch == persisted_sdk_channel
            if channel_offset is not None:
                try:
                    return ch == int(channel) + int(channel_offset)
                except Exception:
                    return False
            if sdk_start_dchan > 0 and ip_chan_num > 0:
                return ch == sdk_start_dchan + max(1, int(channel or 1)) - 1
            return ch == int(channel or 0)

        def _cancel_check() -> str | None:
            m = str(raw.get("__cancel") or "")
            return m if m in {"pause", "stop"} else None

        async def _process_downloaded_parts() -> None:
            nonlocal _force_canonical_merge, _process_started_mono, download_batch_av_policy
            if not pending_process_parts:
                return
            _process_started_mono = time.monotonic()
            total_to_process = len(pending_process_parts)
            if total_to_process == 1:
                only_idx, only_part, _only_done, _only_meta, _only_status_prefix = pending_process_parts[0]
                _record_phase5_observation({
                    "phase": "segment_process",
                    "event": "single_part_process",
                    "part": int(only_idx),
                    "raw_part_path": str(only_part),
                })
            batch_av_policy = await asyncio.to_thread(_detect_download_batch_av_policy, [item[1] for item in pending_process_parts])
            download_batch_av_policy = batch_av_policy
            _record_phase5_observation({
                "phase": "segment_process",
                "event": "batch_av_policy_resolved",
                "policy": batch_av_policy.name,
                "start_gap_median": float(batch_av_policy.start_gap_median),
                "start_gap_range": float(batch_av_policy.start_gap_range),
                "part_count": int(batch_av_policy.part_count),
                "reason": batch_av_policy.reason,
                "target_video_codec": batch_av_policy.target_video_codec,
                "video_codec_mix": bool(batch_av_policy.video_codec_mix),
                "video_codec_reason": batch_av_policy.video_codec_reason,
            })
            log.info(
                "download batch av policy task=%s policy=%s parts=%s median_gap=%.3fs range=%.3fs reason=%s target_video_codec=%s video_codec_mix=%s video_codec_reason=%s",
                server_task_id,
                batch_av_policy.name,
                batch_av_policy.part_count,
                batch_av_policy.start_gap_median,
                batch_av_policy.start_gap_range,
                batch_av_policy.reason,
                batch_av_policy.target_video_codec or "",
                bool(batch_av_policy.video_codec_mix),
                batch_av_policy.video_codec_reason or "",
            )
            for order, (process_idx, process_part, process_done, process_meta, status_prefix) in enumerate(pending_process_parts, start=1):
                cancel_mode = _cancel_check()
                if cancel_mode in {"pause", "stop"}:
                    raise RuntimeError(f"cancelled:{cancel_mode}")
                process_progress = 0.96
                process_meta["process_started_mono"] = time.monotonic()
                _save_download_phase_state("segment_process", current_part_idx=process_idx, process_index=order, process_total=total_to_process)
                on_progress("DOWNLOAD", process_progress, _make_process_start_message(order, total_to_process, process_meta))
                log.info("download part process start task=%s part=%s/%s raw=%s", server_task_id, order, total_to_process, str(process_part))
                normalize_result = await asyncio.to_thread(
                    _normalize_download_part,
                    process_part,
                    task_type,
                    lambda msg, _meta=process_meta, _order=order, _total=total_to_process: on_progress("DOWNLOAD", process_progress, _make_process_progress_message(_order, _total, _meta, msg)),
                    _download_total_elapsed_seconds,
                    _cancel_check,
                    batch_av_policy,
                )
                selected_merge_part = Path(str(getattr(normalize_result, "merge_part_path", "") or "").strip() or str(process_part))
                merge_ready_issue = await asyncio.to_thread(
                    _download_part_merge_ready_issue_for_batch_policy,
                    selected_merge_part,
                    str(getattr(normalize_result, "classification", "") or batch_av_policy.name),
                )
                if merge_ready_issue:
                    _record_phase5_observation({
                        "phase": "segment_process",
                        "event": "merge_ready_rejected",
                        "part": int(process_idx),
                        "merge_part_path": str(selected_merge_part),
                        "issue": str(merge_ready_issue),
                    })
                    log.warning(
                        "download part merge-ready rejected task=%s part=%s/%s merge_part=%s issue=%s",
                        server_task_id,
                        order,
                        total_to_process,
                        str(selected_merge_part),
                        merge_ready_issue,
                    )
                    raise RuntimeError(f"download_part_merge_ready_failed:part{process_idx}:{merge_ready_issue}")
                merge_part_index = -1
                for candidate_index, candidate_part in enumerate(part_paths):
                    if candidate_part == process_part:
                        merge_part_index = candidate_index
                        break
                if merge_part_index >= 0 and merge_part_index < len(merge_part_paths):
                    merge_part_paths[merge_part_index] = selected_merge_part
                if bool(getattr(normalize_result, "force_canonical_merge", False)):
                    _force_canonical_merge = True
                    _force_canonical_merge_reasons.append(f"part{process_idx:03d}:{str(getattr(normalize_result, 'normalize_reason', '') or 'abnormal')}")
                merge_part_already_materialized = selected_merge_part.exists() and selected_merge_part != process_part
                if (bool(getattr(normalize_result, "force_canonical_merge", False)) or bool(getattr(normalize_result, "canonicalize_merge_part", False))) and not merge_part_already_materialized:
                    _canonicalize_merge_parts.add(process_idx)
                process_meta["merge_part_path"] = str(selected_merge_part)
                process_meta["normalize_reason"] = str(getattr(normalize_result, "normalize_reason", "") or process_meta.get("normalize_reason") or "")
                process_meta["normalize_classification"] = str(getattr(normalize_result, "classification", "") or process_meta.get("normalize_classification") or "")
                process_meta["force_canonical_merge"] = bool(getattr(normalize_result, "force_canonical_merge", False))
                process_meta["canonicalize_merge_part"] = bool(getattr(normalize_result, "canonicalize_merge_part", False)) and not merge_part_already_materialized
                process_meta["merge_risk_level"] = int(getattr(normalize_result, "merge_risk_level", 0) or 0)
                process_meta["process_completed"] = True
                _record_phase5_observation({
                    "phase": "segment_process",
                    "event": "classification_resolved",
                    "part": int(process_idx),
                    "classification": str(process_meta.get("normalize_classification") or ""),
                    "normalize_reason": str(process_meta.get("normalize_reason") or ""),
                    "merge_part_path": str(selected_merge_part),
                    "merge_ready_issue": "",
                    "force_canonical_merge": bool(process_meta.get("force_canonical_merge")),
                    "canonicalize_merge_part": bool(process_meta.get("canonicalize_merge_part")),
                    "merge_risk_level": int(process_meta.get("merge_risk_level") or 0),
                })
                _save_part_done_meta(process_done, process_meta)
                _save_download_phase_state("segment_process", current_part_idx=process_idx, process_index=order, process_total=total_to_process)
                log.info(
                    "download part process done task=%s part=%s/%s merge_part=%s reason=%s classification=%s",
                    server_task_id,
                    order,
                    total_to_process,
                    str(selected_merge_part),
                    str(process_meta.get("normalize_reason") or ""),
                    str(process_meta.get("normalize_classification") or ""),
                )

        while current_start < download_end and not process_only_download:
            remaining_parts = len(_segment_ranges(current_start, download_end, max(30, int(current_seg_sec)))) if current_start < download_end else 0
            _download_plan["total_parts"] = max(int(part_idx - 1), int(part_idx - 1) + int(remaining_parts))
            rs = current_start
            re = rs + timedelta(seconds=int(current_seg_sec))
            if re > download_end:
                re = download_end
            if re <= rs:
                break
            idx = int(part_idx)
            part = out_dir / f"{prefix}_{server_task_id}.part{idx:03d}.mp4" if total_parts > 1 else downloaded
            done = out_dir / f"{prefix}_{server_task_id}.part{idx:03d}.done"
            part_paths.append(part)
            merge_part_paths.append(part)
            _save_download_phase_state("segment_download", current_part_idx=idx)
            if idx in pending_reprocess_parts and part.exists() and part.stat().st_size > 0:
                _part_started_at[idx] = time.monotonic()
                _part_duration_sec[idx] = max(1.0, float((re - rs).total_seconds()))
                _part_expected_bytes[idx] = max(1.0, float((re - rs).total_seconds()) * float(_segment_estimated_bytes_per_sec()))
                reprocess_meta = pending_reprocess_meta.get(idx) or {}
                on_progress("DOWNLOAD", _download_part_done_progress(idx), f"第{idx}段已下载完成，但上次处理中断；已加入集中处理队列")
                log.info(
                    "download part queued for process task=%s part=%s/%s raw=%s reason=%s",
                    server_task_id,
                    idx,
                    int(_download_plan.get("total_parts") or total_parts),
                    str(part),
                    str(reprocess_meta.get("normalize_reason") or reprocess_meta.get("normalize_classification") or "resume_reprocess"),
                )
                done_meta_payload = {
                    **expected_download_identity,
                    "range_start": rs.isoformat(),
                    "range_end": re.isoformat(),
                    "requested_sec": max(1.0, float((re - rs).total_seconds())),
                    "size_bytes": int(part.stat().st_size) if part.exists() else 0,
                    "next_seg_sec": int(reprocess_meta.get("next_seg_sec") or current_seg_sec or seg_sec),
                    "channel_used": int(reprocess_meta.get("channel_used") or 0),
                    "record_type_used": int(reprocess_meta.get("record_type_used") or 0),
                    "normalize_reason": str(reprocess_meta.get("normalize_reason") or "resume_reprocess"),
                    "normalize_classification": str(reprocess_meta.get("normalize_classification") or ""),
                    "process_completed": False,
                }
                _save_part_done_meta(done, done_meta_payload)
                pending_process_parts.append((idx, part, done, done_meta_payload, "重新处理"))
                _save_download_phase_state("segment_download", current_part_idx=idx)
                pending_reprocess_parts.discard(idx)
                _on_part_progress(idx, 1.0, int(part.stat().st_size) if part.exists() else 0, 0.0)
                _completed_duration_sec = min(_download_total_seconds, float(_completed_duration_sec) + float(_part_duration_sec.get(idx) or 0.0))
                current_start = re
                current_seg_sec = max(30, int(done_meta_payload.get("next_seg_sec") or current_seg_sec)) if adaptive_segmenting else int(seg_sec)
                part_idx += 1
                continue

            if done.exists() and part.exists() and part.stat().st_size > 0:
                done_meta = _load_part_done_meta(done)
                range_end_raw = str(done_meta.get("range_end") or "").strip()
                next_seg_sec = int(done_meta.get("next_seg_sec") or current_seg_sec or seg_sec)
                done_channel = int(done_meta.get("channel_used") or 0)
                done_record_type = int(done_meta.get("record_type_used") or 0)
                if done_channel > 0 and not _is_allowed_restored_channel(done_channel):
                    log.warning(
                        "discard restored download part due to invalid mapped channel task=%s part=%s sdk_channel=%s request_channel=%s",
                        server_task_id,
                        idx,
                        done_channel,
                        channel,
                    )
                    try:
                        part.unlink(missing_ok=True)
                    except Exception:
                        pass
                    try:
                        done.unlink(missing_ok=True)
                    except Exception:
                        pass
                    done_channel = 0
                if done_channel > 0:
                    _part_channel_used[idx] = done_channel
                    if _task_locked_channel is None:
                        _task_locked_channel = done_channel
                        _task_locked_record_type = done_record_type if done_record_type >= 0 else None
                        _hint_uid = None
                        _hint_channel = done_channel
                        _hint_record_type = _task_locked_record_type
                        log.info("download task lock restored task=%s sdk_channel=%s part=%s", server_task_id, done_channel, idx)
                    elif _task_locked_channel != done_channel:
                        raise RuntimeError(f"同一任务下载通道不一致：已锁定SDK通道{_task_locked_channel}，已存在分段为SDK通道{done_channel}")
                _part_duration_sec[idx] = max(1.0, float((re - rs).total_seconds()))
                pending_process_parts.append((idx, part, done, done_meta, "已存在"))
                _save_download_phase_state("segment_download", current_part_idx=idx)
                if range_end_raw:
                    try:
                        current_start = _parse_dt(range_end_raw)
                    except Exception:
                        current_start = re
                else:
                    current_start = re
                current_seg_sec = max(30, int(next_seg_sec)) if adaptive_segmenting else int(seg_sec)
                _on_part_progress(idx, 1.0, int(part.stat().st_size), 0.0)
                _completed_duration_sec = min(_download_total_seconds, float(_completed_duration_sec) + float(_part_duration_sec.get(idx) or 0.0))
                part_idx += 1
                continue
            _part_duration_sec[idx] = max(1.0, float((re - rs).total_seconds()))
            _part_expected_bytes[idx] = max(1.0, float((re - rs).total_seconds()) * float(_segment_estimated_bytes_per_sec()))
            log.info("download part start task=%s part=%s/%s range=%s-%s out=%s seg_sec=%s adaptive=%s", server_task_id, idx, int(_download_plan.get("total_parts") or total_parts), rs.isoformat(), re.isoformat(), str(part), current_seg_sec, adaptive_segmenting)
            part_retry_limit = 3 if _task_locked_channel is not None else 1
            part_attempt = 1
            is_last_part = re >= download_end
            while True:
                _part_started_at[idx] = time.monotonic()
                try:
                    if shared_download_session is not None:
                        async with _with_nvr_sdk_gate(f"part_{idx}_shared_session"):
                            res = await asyncio.to_thread(
                                download_by_time_with_session,
                                shared_download_session,
                                provider=nvr_provider,
                                start_time=rs,
                                end_time=re,
                                output_path=str(part),
                                on_progress=_on_download_status,
                                on_progress_ex=lambda p, sz, spd, _idx=idx: _on_part_progress(_idx, p, sz, spd),
                                cancel_check=_cancel_check,
                                hint_uid=_hint_uid,
                                hint_channel=_hint_channel,
                                hint_record_type=_hint_record_type,
                                locked_channel=_task_locked_channel,
                                locked_record_type=_task_locked_record_type,
                                excluded_channels=sorted(_disabled_history_channels),
                                apply_start_padding=False,
                                apply_end_padding=False,
                                allow_tail_partial=is_last_part,
                            )
                    else:
                        async with _with_nvr_sdk_gate(f"part_{idx}_direct_download"):
                            res = await asyncio.to_thread(
                                download_by_time,
                                provider=nvr_provider,
                                sdk_dir=None,
                                db_path=str(raw.get("__db_path") or "").strip() or None,
                                nvr_device_id=nvr_device_id_value,
                                ip=ip,
                                port=port,
                                username=username,
                                password=password,
                                channel=channel,
                                start_time=rs,
                                end_time=re,
                                output_path=str(part),
                                device_model=str(raw.get("deviceModel") or raw.get("nvrModel") or nvr.get("deviceModel") or nvr.get("nvrModel") or "") or None,
                                on_progress=_on_download_status,
                                on_progress_ex=lambda p, sz, spd, _idx=idx: _on_part_progress(_idx, p, sz, spd),
                                cancel_check=_cancel_check,
                                hint_uid=_hint_uid,
                                hint_channel=_hint_channel,
                                hint_record_type=_hint_record_type,
                                locked_channel=_task_locked_channel,
                                locked_record_type=_task_locked_record_type,
                                excluded_channels=sorted(_disabled_history_channels),
                                apply_start_padding=False,
                                apply_end_padding=False,
                                allow_tail_partial=is_last_part,
                            )
                    break
                except RuntimeError as e:
                    msg = str(e)
                    if msg in {"cancelled:pause", "cancelled:stop"}:
                        raise
                    try:
                        part.unlink(missing_ok=True)
                    except Exception:
                        pass
                    try:
                        done.unlink(missing_ok=True)
                    except Exception:
                        pass
                    if part_attempt >= part_retry_limit:
                        raise
                    retry_attempt = part_attempt + 1
                    retry_message = f"第{idx}段下载失败，正在重试（{retry_attempt}/{part_retry_limit}）"
                    if _task_locked_channel is not None:
                        retry_message += f"，固定SDK通道{int(_task_locked_channel)}"
                    on_progress("DOWNLOAD", _download_overall_progress(float(_completed_duration_sec)), retry_message)
                    log.warning("download part retry task=%s part=%s/%s attempt=%s/%s locked_channel=%s err=%s", server_task_id, idx, int(_download_plan.get("total_parts") or total_parts), retry_attempt, part_retry_limit, _task_locked_channel, msg)
                    if shared_download_session is not None:
                        await asyncio.to_thread(close_download_session, shared_download_session, provider=nvr_provider)
                        shared_download_session = None
                        shared_download_session, shared_session_error = await _open_shared_download_session()
                    part_attempt = retry_attempt
                    await asyncio.sleep(1)
            persisted_channel = int(getattr(res, "persisted_channel", 0) or 0)
            if bool(getattr(res, "persisted_failed", False)) and persisted_channel > 0:
                _persisted_fail_count += 1
                if _persisted_fail_count >= 2 and persisted_channel not in _disabled_history_channels:
                    _disabled_history_channels.add(persisted_channel)
                    if shared_download_session is not None and int(getattr(shared_download_session, "persisted_sdk_channel", 0) or 0) == persisted_channel:
                        shared_download_session.persisted_sdk_channel = None
                        shared_download_session.persisted_record_type = None
                    log.warning("download disable persisted mapping in current task task=%s sdk_channel=%s fail_count=%s", server_task_id, persisted_channel, _persisted_fail_count)
            elif persisted_channel > 0 and int(res.channel_used or 0) == persisted_channel:
                _persisted_fail_count = 0

            actual_channel_used = int(res.channel_used or 0)
            if actual_channel_used > 0:
                if _task_locked_channel is None:
                    _task_locked_channel = actual_channel_used
                    _task_locked_record_type = int(res.hint_record_type)
                    log.info("download task lock set task=%s sdk_channel=%s source=%s", server_task_id, _task_locked_channel, str(getattr(res, "mapping_source", "") or "?"))
                elif _task_locked_channel != actual_channel_used:
                    try:
                        part.unlink(missing_ok=True)
                    except Exception:
                        pass
                    try:
                        done.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise RuntimeError(f"同一任务下载通道不一致：已锁定SDK通道{_task_locked_channel}，本段实际为SDK通道{actual_channel_used}")
                _hint_uid = None
                _hint_channel = _task_locked_channel
                _hint_record_type = _task_locked_record_type
                _part_channel_used[idx] = actual_channel_used
            else:
                _hint_uid = None
                _hint_channel = None
                _hint_record_type = None
            requested_sec = max(1.0, float((re - rs).total_seconds()))
            actual_size_bytes = int(part.stat().st_size) if part.exists() else int(res.size_bytes)
            actual_duration_sec = float(getattr(res, "actual_duration_sec", 0.0) or 0.0)
            accepted_as_tail = bool(getattr(res, "accepted_as_tail", False))
            actual_range_end = re
            if accepted_as_tail and actual_duration_sec > 0.0:
                actual_range_end = min(re, rs + timedelta(seconds=actual_duration_sec))
                log.warning(
                    "download tail partial accepted task=%s part=%s/%s requested_end=%s actual_end=%s actual_duration=%.3fs",
                    server_task_id,
                    idx,
                    int(_download_plan.get("total_parts") or total_parts),
                    re.isoformat(),
                    actual_range_end.isoformat(),
                    actual_duration_sec,
                )
            next_seg_sec = _estimate_next_segment_seconds(actual_size_bytes, requested_sec, int(current_seg_sec)) if adaptive_segmenting else int(seg_sec)
            done_meta_payload = {
                **expected_download_identity,
                "range_start": rs.isoformat(),
                "range_end": actual_range_end.isoformat(),
                "requested_sec": requested_sec,
                "size_bytes": int(actual_size_bytes),
                "next_seg_sec": int(next_seg_sec),
                "channel_used": int(actual_channel_used),
                "record_type_used": int(res.hint_record_type),
                "process_completed": False,
            }
            _save_part_done_meta(done, done_meta_payload)
            _on_part_progress(idx, 1.0, int(actual_size_bytes), 0.0)
            if part.exists() and part.stat().st_size > 0:
                pending_process_parts.append((idx, part, done, done_meta_payload, "下载完成"))
            _save_download_phase_state("segment_download", current_part_idx=idx)
            log.info("download part done task=%s part=%s/%s size=%s ch=%s rt=%s next_seg_sec=%s", server_task_id, idx, int(_download_plan.get("total_parts") or total_parts), actual_size_bytes, res.channel_used, res.hint_record_type, next_seg_sec)
            _completed_duration_sec = min(_download_total_seconds, float(_completed_duration_sec) + float(_part_duration_sec.get(idx) or 0.0))
            current_start = re
            current_seg_sec = max(30, int(next_seg_sec)) if adaptive_segmenting else int(seg_sec)
            part_idx += 1
    except RuntimeError as e:
        msg = str(e)
        if msg == "cancelled:pause":
            log.info("download paused task=%s", server_task_id)
            raise PauseRequested()
        if msg == "cancelled:stop":
            log.info("download stopped task=%s", server_task_id)
            raise StopRequested()
        if _is_recoverable_download_interruption(msg):
            try:
                on_progress("DOWNLOAD", _download_overall_progress(float(_completed_duration_sec)), f"下载临时中断，已暂停等待恢复：{msg}")
            except Exception:
                pass
            log.warning("download interrupted and paused task=%s err=%s", server_task_id, msg)
            raise PauseRequested()
        if msg.startswith("NVR设备（") and msg.endswith("）无可下载视频！"):
            try:
                on_progress("DOWNLOAD", 0.0, msg)
            except Exception:
                pass
        if msg == "merge_disk_full":
            try:
                on_progress("DOWNLOAD", 0.97, f"分段下载完成，正在合并视频（{len(part_paths)}段），磁盘空间已满，已耗时{_fmt_elapsed_short(0)}，共耗时{_fmt_elapsed_short(_download_total_elapsed_seconds())}")
            except Exception:
                pass
            log.warning("download merge paused by disk full task=%s err=%s", server_task_id, msg)
            raise PauseRequested()
        elif msg.startswith("merge_output_structural_invalid:"):
            try:
                on_progress("DOWNLOAD", 0.97, f"视频合并失败：合并产物结构异常（{msg.split(':', 1)[1]}）")
            except Exception:
                pass
        elif msg == "merge_all_fallbacks_failed":
            try:
                on_progress("DOWNLOAD", 0.97, f"视频合并失败：所有合并方案均失败（共{len(part_paths)}段）")
            except Exception:
                pass
        elif msg.startswith("merge_ts_convert_failed:"):
            try:
                on_progress("DOWNLOAD", 0.97, f"视频合并失败：TS桥接重编码失败（{msg.split(':', 1)[1]}）")
            except Exception:
                pass
        elif msg.startswith("reprocess_raw_part_missing:"):
            missing_idx = msg.split(":", 1)[1] if ":" in msg else "?"
            try:
                on_progress("DOWNLOAD", 0.0, f"重新处理失败：缺少第{missing_idx}段原始分段，请先重新下载")
            except Exception:
                pass
        log.exception("download failed task=%s err=%s", server_task_id, msg)
        raise
    finally:
        if shared_download_session is not None:
            await asyncio.to_thread(close_download_session, shared_download_session, provider=nvr_provider)
            shared_download_session = None

    if pending_process_parts or skip_download_pipeline:
        _freeze_segment_download_elapsed()

    if not skip_download_pipeline:
        log.info("download process phase start task=%s pending_parts=%s resume_subphase=%s", server_task_id, len(pending_process_parts), resume_subphase)
        await _process_downloaded_parts()

    total_parts = len(part_paths)
    if total_parts == 1 and not skip_download_pipeline and merge_part_paths:
        single_processed = Path(str(merge_part_paths[0]))
        if single_processed.exists() and single_processed.stat().st_size > 0 and single_processed != downloaded:
            await asyncio.to_thread(
                _replace_file_or_raise_finalize_pending,
                single_processed,
                downloaded,
                log=log,
                action=f"single part process finalize task={server_task_id}",
                user_message="当前分段处理完成，正在等待最终提交",
            )
            log.info("download single part finalize task=%s raw=%s promoted=%s", server_task_id, str(downloaded), str(single_processed))
        for f in out_dir.glob(f"{prefix}_{server_task_id}.part*"):
            cleanup_targets.append(f)
    if total_parts > 1 and not skip_download_pipeline:
        _save_download_phase_state("merge", current_part_idx=int(total_parts))
        log.info("download merge phase checkpoint task=%s parts=%s process_state=%s", server_task_id, total_parts, str(process_state_path))
        known_channels = sorted({int(ch) for ch in _part_channel_used.values() if int(ch) > 0})
        if len(known_channels) > 1:
            raise RuntimeError(f"同一任务下载通道不一致，禁止合并：SDK通道={known_channels}")
        merge_input_names = [p.name for p in merge_part_paths]
        duplicate_merge_inputs = sorted({name for name in merge_input_names if merge_input_names.count(name) > 1})
        if duplicate_merge_inputs:
            raise RuntimeError(f"merge_duplicate_inputs:{','.join(duplicate_merge_inputs)}")
        log.info(
            "download merge inputs task=%s parts=%s inputs=%s",
            server_task_id,
            total_parts,
            ",".join(merge_input_names),
        )
        if (
            download_batch_av_policy.name != "nvr_pts_skew_same_origin"
            and merge_part_paths
            and all(str(p.name).endswith(".nvrskew.scaffold.mp4") for p in merge_part_paths)
        ):
            download_batch_av_policy = DownloadBatchAvPolicy(
                name="nvr_pts_skew_same_origin",
                part_count=len(merge_part_paths),
                reason="restored_nvr_skew_scaffold_merge_parts",
            )
            log.info(
                "download batch av policy restored from merge inputs task=%s policy=%s parts=%s reason=%s",
                server_task_id,
                download_batch_av_policy.name,
                download_batch_av_policy.part_count,
                download_batch_av_policy.reason,
            )
        if not ffmpeg_exists():
            raise RuntimeError("ffmpeg_missing")
        if _force_canonical_merge:
            log.warning(
                "%s download merge forcing canonical path task=%s parts=%s reasons=%s",
                _branch_code_tag(CAM_MRG_010),
                server_task_id,
                total_parts,
                ",".join(_force_canonical_merge_reasons[:12]),
            )
        elif _canonicalize_merge_parts:
            log.warning(
                "%s download merge will canonicalize selected parts task=%s parts=%s selected=%s",
                _branch_code_tag(CAM_MRG_010),
                server_task_id,
                total_parts,
                ",".join(str(x) for x in sorted(_canonicalize_merge_parts)),
            )
        if _process_started_mono <= 0:
            _process_started_mono = time.monotonic()
        merge_status_label = f"分段下载完成，正在合并视频（{total_parts}段）"
        on_progress("DOWNLOAD", 0.97, f"{merge_status_label}，已耗时{_fmt_elapsed_short(0)}，共耗时{_fmt_elapsed_short(_download_total_elapsed_seconds())}")
        log.info("download merge start task=%s parts=%s", server_task_id, total_parts)
        tmp_merged = out_dir / f"{prefix}_{server_task_id}.merged.mp4"
        _cleanup_merge_artifacts(out_dir, prefix, server_task_id)
        try:
            merge_temp_files = await asyncio.to_thread(
                _run_with_status_heartbeat,
                merge_status_label,
                lambda: _concat_parts(
                    merge_part_paths,
                    tmp_merged,
                    force_canonical_merge=_force_canonical_merge,
                    canonicalize_part_indexes=set(_canonicalize_merge_parts),
                    on_observation=_record_phase5_observation,
                    skip_adjacent_preflight=download_batch_av_policy.name == "nvr_pts_skew_same_origin",
                    boundary_video_gap_action=("observe" if download_batch_av_policy.name == "nvr_pts_skew_same_origin" else "repair"),
                    force_ts_bridge=download_batch_av_policy.name == ABSOLUTE_TIMELINE_TS_BRIDGE_POLICY,
                    cancel_check=_cancel_check,
                ),
                lambda msg: on_progress("DOWNLOAD", 0.97, msg),
                _download_total_elapsed_seconds,
                _cancel_check,
            )
        except RuntimeError as e:
            msg = str(e)
            if msg == "cancelled:pause":
                log.info("download merge paused task=%s", server_task_id)
                raise PauseRequested()
            if msg == "cancelled:stop":
                log.info("download merge stopped task=%s", server_task_id)
                raise StopRequested()
            raise
        if download_batch_av_policy.name == "nvr_pts_skew_same_origin":
            nvr_audio_merged = out_dir / f"{prefix}_{server_task_id}.merged.nvrskew_audio.mp4"
            nvr_audio_ok = await asyncio.to_thread(
                _run_with_status_heartbeat,
                "检测到NVR固定PTS偏移，正在按原始分段重建音频时间线",
                lambda: _rebuild_nvr_skew_merged_audio_from_raw_parts(tmp_merged, part_paths, nvr_audio_merged),
                lambda msg: on_progress("DOWNLOAD", 0.975, msg),
                _download_total_elapsed_seconds,
            )
            _record_phase5_observation({
                "phase": "merge",
                "event": "nvr_pts_skew_audio_rebuild",
                "ok": bool(nvr_audio_ok),
                "policy": download_batch_av_policy.name,
                "source": str(tmp_merged),
                "output": str(nvr_audio_merged),
            })
            if nvr_audio_ok:
                high_risk_reason = await asyncio.to_thread(_nvr_skew_output_high_risk, nvr_audio_merged, part_paths)
                if high_risk_reason:
                    nvr_video_duration_merged = out_dir / f"{prefix}_{server_task_id}.merged.nvrskew_video_duration.mp4"
                    nvr_video_duration_ok, nvr_video_duration_temp_files = await asyncio.to_thread(
                        _run_with_status_heartbeat,
                        "检测到NVR固定PTS偏移高风险输出，正在按分段视频真实时长重建",
                        lambda: _rebuild_nvr_skew_merged_by_raw_video_duration(part_paths, nvr_video_duration_merged),
                        lambda msg: on_progress("DOWNLOAD", 0.978, msg),
                        _download_total_elapsed_seconds,
                    )
                    merge_temp_files.extend(nvr_video_duration_temp_files)
                    _record_phase5_observation({
                        "phase": "merge",
                        "event": "nvr_pts_skew_high_risk_fallback",
                        "ok": bool(nvr_video_duration_ok),
                        "reason": high_risk_reason,
                        "policy": download_batch_av_policy.name,
                        "source": str(nvr_audio_merged),
                        "output": str(nvr_video_duration_merged),
                    })
                    if nvr_video_duration_ok:
                        merge_temp_files.append(nvr_audio_merged)
                        nvr_audio_merged = nvr_video_duration_merged
                merge_temp_files.append(tmp_merged)
                tmp_merged = nvr_audio_merged
        await asyncio.to_thread(
            _replace_file_or_raise_finalize_pending,
            tmp_merged,
            downloaded,
            log=log,
            action=f"download merge finalize task={server_task_id}",
            user_message="视频合并完成，正在等待最终提交",
        )
        cleanup_targets = list(part_paths)
        cleanup_targets.extend(merge_part_paths)
        cleanup_targets.extend(merge_temp_files)
        for f in out_dir.glob(f"{prefix}_{server_task_id}.part*"):
            cleanup_targets.append(f)
        for f in out_dir.glob(f"{prefix}_{server_task_id}.merged*"):
            cleanup_targets.append(f)
        on_progress("DOWNLOAD", 0.98, "视频合并完成，正在校准时长与封装")
        log.info("download merge done task=%s out=%s deferred_cleanup=%s", server_task_id, str(downloaded), len(cleanup_targets))

    if skip_download_pipeline:
        on_progress("DOWNLOAD", 0.98, "恢复执行：沿用已生成源视频，继续校准时长与封装")
        log.info("download resume source finalize task=%s using existing source=%s subphase=%s", server_task_id, str(downloaded), resume_subphase)

    _dl_file_size = int(downloaded.stat().st_size) if downloaded.exists() else 0
    _save_download_phase_state("source_finalize", current_part_idx=int(total_parts))
    log.info("download source finalize checkpoint task=%s source=%s process_state=%s", server_task_id, str(downloaded), str(process_state_path))
    
    actual_dur = await asyncio.to_thread(
        _normalize_source_video,
        downloaded,
        None,
        lambda msg: on_progress("DOWNLOAD", 0.99, msg),
        _record_phase5_observation,
    )
    if cleanup_targets:
        for p in cleanup_targets:
            try:
                if p.exists() and p != downloaded:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
            except Exception:
                pass
        log.info("download finalize cleanup done task=%s out=%s cleaned=%s", server_task_id, str(downloaded), len(cleanup_targets))
    if task_type == 1 and actual_dur > 0:
        log.info("课次%s taskType=1 下载完成，源视频实际时长%.3fs", lesson_id, actual_dur)
    elif task_type in (2, 3):
        log.info("课次%s taskType=%s 下载完成，源视频保留原始时长%.3fs，HLS按自然时长输出", lesson_id, task_type, actual_dur)
    
    _segment_elapsed = _download_elapsed_seconds()
    _process_elapsed = _process_elapsed_seconds()
    _dl_elapsed = _download_total_elapsed_seconds()
    _dl_size_str = f"{_dl_file_size / 1048576.0:.0f}MB" if _dl_file_size < 1073741824 else f"{_dl_file_size / 1073741824.0:.2f}GB"
    _segment_time_str = _fmt_elapsed_short(_segment_elapsed)
    _process_time_str = _fmt_elapsed_short(_process_elapsed)
    _dl_h = int(_dl_elapsed) // 3600
    _dl_m = (int(_dl_elapsed) % 3600) // 60
    _dl_s = int(_dl_elapsed) % 60
    _dl_time_str = f"{_dl_h}时{_dl_m}分{_dl_s}秒" if _dl_h > 0 else (f"{_dl_m}分{_dl_s}秒" if _dl_m > 0 else f"{_dl_s}秒")
    on_progress("DOWNLOAD", 1.0, f"视频下载完成，共{total_parts}个分段，总大小{_dl_size_str}，下载耗时{_segment_time_str}，修复合并耗时{_process_time_str}，共耗时{_dl_time_str}")
    _clear_download_resume_state(resume_state_path)
    _clear_process_resume_state(process_state_path)

    if not is_task_step_enabled("TRANSCODE"):
        on_progress("TRANSCODE", 1.0, "TRANSCODE 已关闭，跳过执行")
        return [
            {"path": str(downloaded), "sizeBytes": _dl_file_size, "stepCode": "DOWNLOAD", "fileType": "source_video"},
        ]

    if start_step == "DOWNLOAD":
        return [
            {"path": str(downloaded), "sizeBytes": _dl_file_size, "stepCode": "DOWNLOAD", "fileType": "source_video"},
        ]

    on_progress("TRANSCODE", 0.0, "转码中")
    if ffmpeg_exists():
        m3u8_path, dl_size, tc_size = await _do_hls_transcode(
            downloaded, out_dir, raw, on_progress, server_task_id, log_prefix="transcode"
        )
        return [
            {"path": str(downloaded), "sizeBytes": dl_size, "stepCode": "DOWNLOAD", "fileType": "source_video"},
            {"path": m3u8_path, "sizeBytes": tc_size, "stepCode": "TRANSCODE", "fileType": "transcoded_video"},
        ]

    for i in range(plan.transcode_seconds):
        await asyncio.sleep(1)
        p = (i + 1) / float(plan.transcode_seconds)
        on_progress("TRANSCODE", p, f"TRANSCODE {int(p*100)}%")
    dl_size = int(downloaded.stat().st_size) if downloaded.exists() else 0
    return [
        {"path": str(downloaded), "sizeBytes": dl_size, "stepCode": "DOWNLOAD", "fileType": "source_video"},
        {"path": str(downloaded), "sizeBytes": dl_size, "stepCode": "TRANSCODE", "fileType": "transcoded_video"},
    ]


async def run_camera_transcode_only(raw: dict[str, Any], on_progress) -> list[dict[str, Any]]:
    """Run only the TRANSCODE stage for an already-downloaded video."""
    log = logging.getLogger("edge.runner")
    if not is_task_step_enabled("TRANSCODE"):
        on_progress("TRANSCODE", 1.0, "TRANSCODE 已关闭，跳过执行")
        return []
    task_id = str(raw.get("taskId") or raw.get("id") or "")
    try:
        server_task_id = str(int(str(task_id).strip()))
    except Exception:
        server_task_id = str(task_id).strip() or "0"
    lesson_id = str(raw.get("lessonId") or "").strip() or "0"
    try:
        task_type = int(raw.get("taskType") or 0)
    except Exception:
        task_type = 0

    # 转码阶段直接使用下载阶段已写入的源视频路径（DB 中保存的 DOWNLOAD output_file_path），
    # 不再重新计算 lesson_date / 课次目录，避免下载/转码两端解析逻辑不一致造成"找不到文件"。
    db_path = str(raw.get("__db_path") or "").strip() or None
    downloaded: Path | None = None
    if db_path and lesson_id and lesson_id != "0":
        try:
            db = Db(DbConfig(path=str(db_path)))
            row = db.fetch_one(
                "SELECT s.output_file_path "
                "FROM edge_stream_task t "
                "JOIN edge_stream_task_step s ON s.task_id=t.id AND s.step_code='DOWNLOAD' "
                "WHERE t.lesson_id=? AND t.task_kind='CameraTask' AND t.task_type=? "
                "AND s.step_status=2 AND s.output_file_path IS NOT NULL AND s.output_file_path<>'' "
                "ORDER BY t.id DESC LIMIT 1",
                (int(lesson_id), int(task_type)),
            )
            if row is not None:
                persisted = Path(str(row["output_file_path"] or "").strip())
                if persisted.exists() and persisted.stat().st_size >= 1024:
                    downloaded = persisted
        except Exception as e:
            log.warning("transcode-only DB output_file_path lookup failed task=%s err=%s", server_task_id, e)

    # 兜底：DB 没有可用记录时，按下载阶段相同规则推算（lessonDate 优先，再退到 lessonStartAt 的日期）
    if downloaded is None or not downloaded.exists() or downloaded.stat().st_size < 1024:
        lesson_date = _resolve_lesson_date(raw.get("lessonDate"), raw.get("lessonStartAt"), default="unknown")
        root = Path(_load_download_path())
        prefix = _task_type_prefix(task_type)
        downloaded = _get_lesson_dir(lesson_date, lesson_id, root) / f"{prefix}_{server_task_id}.mp4"

    if not downloaded.exists() or downloaded.stat().st_size < 1024:
        raise RuntimeError(f"源视频不存在或为空: {downloaded}")

    # 转码产物（HLS 目录等）直接落在源视频所在目录下，无需另行计算课次目录。
    out_dir = downloaded.parent

    if not ffmpeg_exists():
        raise RuntimeError("ffmpeg_missing")

    on_progress("TRANSCODE", 0.0, "转码中")
    m3u8_path, dl_size, tc_size = await _do_hls_transcode(
        downloaded, out_dir, raw, on_progress, server_task_id, log_prefix="transcode-only"
    )
    return [
        {"path": m3u8_path, "sizeBytes": tc_size, "stepCode": "TRANSCODE", "fileType": "transcoded_video"},
    ]
