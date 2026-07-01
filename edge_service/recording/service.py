from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..db import bjt_now_iso
from ..utils import load_download_path
from ..video.ffmpeg import ffmpeg_bin, probe_duration_seconds, probe_stream_timings

_log = logging.getLogger("edge.mobile_record")

ACTIVE_STATUSES = ("starting", "recording", "stopping")
FINAL_STATUSES = ("finished", "cancelled", "failed", "interrupted")
HLS_SEGMENT_SECONDS = 10
MEDIA_START_WAIT_SECONDS = 3.0
AUTO_STOP_MEDIA_TOLERANCE_SECONDS = 1.0
AUTO_STOP_MAX_EXTRA_SECONDS = 20.0


def _parse_dt(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _format_size_mb(value: object) -> str:
    try:
        size_bytes = float(value or 0)
    except Exception:
        size_bytes = 0.0
    return f"{size_bytes / (1024 * 1024):.2f}MB"


def _now_dt() -> datetime:
    return datetime.fromisoformat(bjt_now_iso())


def _format_callback_time(value: object) -> str | None:
    dt = _parse_dt(value)
    if dt is None:
        text = str(value or "").strip()
        return text or None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_base_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text.rstrip("/")
    return ("http://" + text).rstrip("/")


def _safe_task_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("taskId 不能为空")
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text)
    if safe in {"", ".", ".."}:
        raise ValueError("taskId 不合法")
    return safe[:120]


def _mask_secret(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 2:
        return "*" * len(text)
    return f"{text[:1]}***{text[-1:]}(len={len(text)})"


def _mask_url_secret(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return re.sub(r"(rtsp://[^:/@\s]+:)([^@\s]+)(@)", lambda m: m.group(1) + _mask_secret(m.group(2)) + m.group(3), text)


def _format_duration(value: object) -> str:
    seconds = max(0, _as_int(value, 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _normalize_log_text(value: object, *, limit: int = 300) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _humanize_ffmpeg_warning(line: str) -> str:
    text = _normalize_log_text(line, limit=500)
    lower = text.lower()
    if "non-monotonic dts" in lower:
        return "检测到时间戳回退，ffmpeg 已尝试自动校正；录制可继续，但需要关注最终成片时间轴。"
    if "invalid data found when processing input" in lower:
        return "输入流数据异常，ffmpeg 无法继续读取当前 RTSP 内容。"
    if "option rw_timeout not found" in lower:
        return "当前 ffmpeg 不支持 rw_timeout 参数，录制命令启动失败。"
    if "error opening input file" in lower:
        return "无法打开 RTSP 输入流，请检查地址、端口、账号密码和网络连通性。"
    if "connection refused" in lower or "timed out" in lower:
        return "连接 RTSP 过程中网络异常或超时。"
    return ""


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _pid_exists(pid: int) -> bool:
    if int(pid or 0) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


class MobileRecordService:
    def __init__(self, *, db, load_monitor_cfg, session_manager) -> None:
        self._db = db
        self._load_monitor_cfg = load_monitor_cfg
        self._session_manager = session_manager
        self._lock = asyncio.Lock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._finalizing_tasks: set[str] = set()
        self._deleting_tasks: set[str] = set()
        self._scheduler_task: asyncio.Task | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._noisy_warning_seen: dict[tuple[str, str], int] = {}
        self._media_started_events: dict[str, threading.Event] = {}

    async def start_background(self) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.recover_incomplete_tasks)
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def aclose(self) -> None:
        self._closed = True
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._scheduler_task

    def _record_root(self) -> Path:
        root = Path(load_download_path())
        base = root if root.name.lower() == "videos" else root / "Videos"
        return base / "Record"

    def _default_callback_url(self) -> str:
        cfg = self._load_monitor_cfg() if callable(self._load_monitor_cfg) else {}
        if not isinstance(cfg, dict):
            cfg = {}
        server_base = _normalize_base_url(cfg.get("serverAddress"))
        if not server_base:
            server = cfg.get("server") if isinstance(cfg.get("server"), dict) else {}
            server_base = _normalize_base_url(server.get("address"))
        if not server_base:
            return ""
        return f"{server_base}/api/v1/record-task/callback"

    def _callback_headers(self) -> dict[str, str]:
        cfg = self._load_monitor_cfg() if callable(self._load_monitor_cfg) else {}
        if not isinstance(cfg, dict):
            cfg = {}
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "EdgeServiceClient-MobileRecord/1.0",
        }
        token = str(cfg.get("authToken") or cfg.get("token") or "").strip()
        if token:
            headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        access_key = str(cfg.get("accessKey") or "").strip()
        access_secret = str(cfg.get("accessSecret") or "").strip()
        if access_key:
            headers["accessKey"] = access_key
            headers["access_key"] = access_key
        if access_secret:
            headers["accessSecret"] = access_secret
            headers["access_secret"] = access_secret
        return headers

    def _task_elapsed_seconds(self, row: dict[str, Any] | None, *, now: datetime | None = None) -> int:
        if not row:
            return 0
        start_dt = _parse_dt(row.get("start_time"))
        if not start_dt:
            return 0
        current = now or _now_dt()
        return max(0, int((current - start_dt).total_seconds()))

    def _task_log_prefix(self, task_id: str, status: str) -> str:
        return f"taskId={task_id} status={status}"

    def _log_task_started(self, task_id: str, req: dict[str, Any], *, status: str) -> None:
        _log.info(
            "%s 启动录制 录制参数[campusCode=%s, nvrDeviceId=%s, ipAddress=%s, port=%s, account=%s, password=%s, nvrChannelId=%s, nvrChannelNum=%s, estimatedDurationSeconds=%s(%s)] recordUserId=%s recordUserName=%s",
            self._task_log_prefix(task_id, status),
            str(req.get("campusCode") or "").strip(),
            _as_int(req.get("nvrDeviceId")),
            str(req.get("ipAddress") or "").strip(),
            _as_int(req.get("port"), 554),
            str(req.get("account") or "").strip(),
            _mask_secret(req.get("password")),
            str(req.get("nvrChannelId") or "").strip(),
            _as_int(req.get("nvrChannelNum")),
            _as_int(req.get("estimatedDurationSeconds")),
            _format_duration(req.get("estimatedDurationSeconds")),
            str(req.get("recordUserId") or "").strip(),
            str(req.get("recordUserName") or "").strip(),
        )

    def _log_task_progress(self, task_id: str, *, status: str, elapsed_seconds: int, estimated_seconds: int) -> None:
        _log.info(
            "%s 录制中 已录制时长=%s(%s) 预计录制时长=%s(%s)",
            self._task_log_prefix(task_id, status),
            int(elapsed_seconds),
            _format_duration(elapsed_seconds),
            int(estimated_seconds),
            _format_duration(estimated_seconds),
        )

    def _log_task_extended(self, row: dict[str, Any], *, extend_seconds: int, operator_user_id: str) -> None:
        task_id = str(row.get("task_id") or "")
        status = str(row.get("status") or "")
        elapsed_seconds = self._task_elapsed_seconds(row)
        estimated_seconds = _as_int(row.get("estimated_duration_seconds")) + _as_int(row.get("extend_duration_seconds"))
        _log.info(
            "%s 延长录制 已录制时长=%s(%s) 预计录制时长=%s(%s) 本次增加预计录制时长=%s(%s) recordUserId=%s recordUserName=%s operatorUserId=%s",
            self._task_log_prefix(task_id, status),
            int(elapsed_seconds),
            _format_duration(elapsed_seconds),
            int(estimated_seconds),
            _format_duration(estimated_seconds),
            int(extend_seconds),
            _format_duration(extend_seconds),
            str(row.get("record_user_id") or ""),
            str(row.get("record_user_name") or ""),
            str(operator_user_id or ""),
        )

    def _log_task_stopped(self, row: dict[str, Any], *, stop_reason: str) -> None:
        task_id = str(row.get("task_id") or "")
        status = str(row.get("status") or "")
        elapsed_seconds = self._task_elapsed_seconds(row)
        _log.info(
            "%s 结束录制 stopReason=%s 已录制时长=%s(%s) segmentCount=%s fileSize=%s playUrl=%s",
            self._task_log_prefix(task_id, status),
            str(stop_reason or ""),
            int(elapsed_seconds),
            _format_duration(elapsed_seconds),
            _as_int(row.get("segment_count")),
            _format_size_mb(row.get("file_size")),
            str(row.get("play_url") or ""),
        )

    def _log_task_failed(self, task_id: str, *, status: str, reason: str) -> None:
        _log.warning("%s 任务失败 原因=%s", self._task_log_prefix(task_id, status), _normalize_log_text(reason, limit=600))

    def _record_warning_category(self, line: str) -> tuple[str, str] | None:
        lower = _normalize_log_text(line, limit=500).lower()
        if "stream hevc is not hvc1" in lower:
            return ("hevc_tag", "检测到 HEVC 标签不是 hvc1，当前录制会继续；同类原始日志已省略。")
        if "codec pcm_alaw" in lower and "private data stream" in lower:
            return ("pcm_alaw_private", "检测到音频以私有数据流方式写入，当前录制会继续；同类原始日志已省略。")
        if "packet with pts" in lower and "duration 0" in lower:
            return ("hls_pts_precision", "检测到分片时间戳精度告警，ffmpeg 会继续写分片；同类原始日志已省略。")
        if "skipping invalid undecodable nalu" in lower or "pps id out of range" in lower:
            return ("hevc_decode_noise", "检测到源流存在 HEVC 解码噪声，当前录制会继续；同类原始日志已省略。")
        return None

    def _log_ffmpeg_runtime_warning(self, task_id: str, line: str) -> None:
        status = "recording"
        row = self._fetch_task(task_id)
        if row:
            status = str(row.get("status") or status)
        category = self._record_warning_category(line)
        if category is not None:
            category_key, summary = category
            seen_key = (task_id, category_key)
            seen_count = self._noisy_warning_seen.get(seen_key, 0)
            self._noisy_warning_seen[seen_key] = seen_count + 1
            if seen_count == 0:
                _log.warning("%s 录制中告警=%s 原始日志=%s", self._task_log_prefix(task_id, status), summary, _normalize_log_text(line, limit=500))
            return
        human = _humanize_ffmpeg_warning(line)
        raw = _normalize_log_text(line, limit=1000)
        if human:
            _log.warning("%s 录制中告警=%s 原始日志=%s", self._task_log_prefix(task_id, status), human, raw)
            return
        _log.warning("%s 录制中告警 原始日志=%s", self._task_log_prefix(task_id, status), raw)

    def _public_play_url(self, request_base_url: str, task_id: str) -> str:
        base = self._session_manager.build_public_access_base_url(str(request_base_url or "").rstrip("/"))
        return f"{base}/api/mobile-record/play/{task_id}"

    def _lan_play_url(self, request_base_url: str, task_id: str) -> str:
        base = self._session_manager.build_lan_access_base_url(str(request_base_url or "").rstrip("/"))
        return f"{base}/api/mobile-record/play/{task_id}"

    def _event(self, task_id: str, event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
        try:
            self._db.execute(
                "INSERT INTO edge_mobile_record_event(task_id,event_type,message,payload,created_time) VALUES (?,?,?,?,?)",
                (str(task_id), str(event_type), str(message or ""), json.dumps(payload or {}, ensure_ascii=False), bjt_now_iso()),
            )
        except Exception:
            _log.exception("mobile record event write failed task=%s type=%s", task_id, event_type)

    def _fetch_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._db.fetch_one("SELECT * FROM edge_mobile_record_task WHERE task_id=? LIMIT 1", (str(task_id),))
        return dict(row) if row else None

    def _build_callback_payload_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        current = dict(row or {})
        status = str(current.get("status") or "")
        if status in ACTIVE_STATUSES or (
            str(current.get("m3u8_path") or "") and (
                _as_int(current.get("segment_count")) <= 0
                or _as_int(current.get("file_size")) <= 0
                or float(current.get("duration_seconds") or 0.0) <= 0.0
            )
        ):
            info = self._collect_hls_info(current, lightweight=True)
            if info["ok"]:
                current["segment_count"] = int(info["segment_count"])
                current["file_size"] = int(info["file_size"])
                current["duration_seconds"] = float(info["duration"])
                if str(info.get("codec") or ""):
                    current["codec"] = str(info.get("codec") or "")
        return {
            "taskId": str(current.get("task_id") or ""),
            "classroomId": str(current.get("classroom_id") or "") or None,
            "cameraId": str(current.get("nvr_channel_id") or current.get("nvr_channel_num") or "") or None,
            "status": status,
            "stopReason": str(current.get("stop_reason") or "") or None,
            "playUrl": str(current.get("play_url") or "") or None,
            "outputDir": str(current.get("output_dir") or "") or None,
            "m3u8Path": str(current.get("m3u8_path") or "") or None,
            "segmentCount": int(current.get("segment_count") or 0),
            "fileSize": int(current.get("file_size") or 0),
            "duration": float(current.get("duration_seconds") or 0.0),
            "codec": str(current.get("codec") or "") or None,
            "format": "hls",
            "startTime": _format_callback_time(current.get("start_time")),
            "finishTime": _format_callback_time(current.get("finish_time")),
            "errorMessage": str(current.get("error_message") or "") or None,
        }

    def _build_callback_payload_for_start_failure(self, req: dict[str, Any], error_message: str) -> dict[str, Any]:
        now_text = _format_callback_time(bjt_now_iso())
        return {
            "taskId": str(req.get("taskId") or ""),
            "classroomId": str(req.get("classroomId") or "") or None,
            "cameraId": str(req.get("nvrChannelId") or req.get("nvrChannelNum") or "") or None,
            "status": "failed",
            "stopReason": None,
            "playUrl": None,
            "outputDir": None,
            "m3u8Path": None,
            "segmentCount": 0,
            "fileSize": 0,
            "duration": 0.0,
            "codec": None,
            "format": "hls",
            "startTime": None,
            "finishTime": now_text,
            "errorMessage": str(error_message or "") or "start_failed",
        }

    async def send_callback_for_start_failure(self, req: dict[str, Any], error_message: str) -> None:
        callback_url = self._default_callback_url()
        task_id = str(req.get("taskId") or "").strip()
        if not callback_url or not task_id:
            return
        payload = self._build_callback_payload_for_start_failure(req, error_message)
        await self._send_callback_payload(task_id=task_id, url=callback_url, payload=payload)

    def _active_for_classroom(self, classroom_id: str) -> dict[str, Any] | None:
        row = self._db.fetch_one(
            f"SELECT * FROM edge_mobile_record_task WHERE classroom_id=? AND status IN ({','.join('?' for _ in ACTIVE_STATUSES)}) ORDER BY id DESC LIMIT 1",
            (str(classroom_id), *ACTIVE_STATUSES),
        )
        return dict(row) if row else None

    def _active_for_user(self, record_user_id: str) -> dict[str, Any] | None:
        row = self._db.fetch_one(
            f"SELECT * FROM edge_mobile_record_task WHERE record_user_id=? AND status IN ({','.join('?' for _ in ACTIVE_STATUSES)}) ORDER BY id DESC LIMIT 1",
            (str(record_user_id), *ACTIVE_STATUSES),
        )
        return dict(row) if row else None

    def _active_for_channel(self, nvr_device_id: int, nvr_channel_num: int) -> dict[str, Any] | None:
        if int(nvr_device_id or 0) <= 0 or int(nvr_channel_num or 0) <= 0:
            return None
        row = self._db.fetch_one(
            f"SELECT * FROM edge_mobile_record_task WHERE nvr_device_id=? AND nvr_channel_num=? AND status IN ({','.join('?' for _ in ACTIVE_STATUSES)}) ORDER BY id DESC LIMIT 1",
            (int(nvr_device_id), int(nvr_channel_num), *ACTIVE_STATUSES),
        )
        return dict(row) if row else None

    def _status_payload(self, row: dict[str, Any] | None, *, request_base_url: str = "") -> dict[str, Any]:
        if not row:
            return {
                "exists": False,
                "taskId": "",
                "classroomId": "",
                "classroomName": "",
                "recordUserId": "",
                "recordUserName": "",
                "status": "idle",
                "isRecording": False,
                "selectable": True,
                "startTime": "",
                "maxEndTime": "",
                "finishTime": "",
                "elapsedSeconds": 0,
                "remainingSeconds": 0,
                "estimatedDurationSeconds": 0,
                "extendDurationSeconds": 0,
                "playUrl": "",
                "outputDir": "",
                "segmentCount": 0,
                "fileSize": 0,
                "durationSeconds": 0.0,
                "codec": "",
                "errorMessage": "",
            }
        status = str(row.get("status") or "")
        start_dt = _parse_dt(row.get("start_time"))
        max_dt = _parse_dt(row.get("max_end_time"))
        now = _now_dt()
        elapsed = max(0, int((now - start_dt).total_seconds())) if start_dt else 0
        remaining = max(0, int((max_dt - now).total_seconds())) if max_dt and status in ACTIVE_STATUSES else 0
        task_id = str(row.get("task_id") or "")
        play_url = str(row.get("play_url") or "")
        if play_url == "" and str(row.get("m3u8_path") or "") and request_base_url:
            play_url = self._public_play_url(request_base_url, task_id)
        return {
            "exists": True,
            "taskId": task_id,
            "classroomId": str(row.get("classroom_id") or ""),
            "classroomName": str(row.get("classroom_name") or ""),
            "recordUserId": str(row.get("record_user_id") or ""),
            "recordUserName": str(row.get("record_user_name") or ""),
            "status": status,
            "isRecording": status in ACTIVE_STATUSES,
            "selectable": status not in ACTIVE_STATUSES,
            "startTime": str(row.get("start_time") or ""),
            "maxEndTime": str(row.get("max_end_time") or ""),
            "finishTime": str(row.get("finish_time") or ""),
            "elapsedSeconds": elapsed,
            "remainingSeconds": remaining,
            "estimatedDurationSeconds": int(row.get("estimated_duration_seconds") or 0),
            "extendDurationSeconds": int(row.get("extend_duration_seconds") or 0),
            "playUrl": play_url or "",
            "outputDir": str(row.get("output_dir") or ""),
            "segmentCount": int(row.get("segment_count") or 0),
            "fileSize": int(row.get("file_size") or 0),
            "durationSeconds": float(row.get("duration_seconds") or 0.0),
            "codec": str(row.get("codec") or ""),
            "errorMessage": str(row.get("error_message") or ""),
        }

    def classroom_statuses(self, classroom_ids: list[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for classroom_id in classroom_ids:
            cid = str(classroom_id or "").strip()
            if not cid:
                continue
            active = self._active_for_classroom(cid)
            payload = self._status_payload(active)
            payload["classroomId"] = cid
            payload["isRecording"] = bool(active)
            payload["selectable"] = not bool(active)
            if not active:
                payload["exists"] = False
                payload["status"] = "idle"
            items.append(payload)
        if items:
            return items
        rows = self._db.fetch_all(
            f"SELECT * FROM edge_mobile_record_task WHERE status IN ({','.join('?' for _ in ACTIVE_STATUSES)}) ORDER BY id DESC",
            ACTIVE_STATUSES,
        )
        return [self._status_payload(dict(row)) for row in rows]

    def _build_record_cmd(self, rtsp_url: str, out_dir: Path) -> list[str]:
        return [
            ffmpeg_bin(),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostats",
            "-progress",
            "pipe:1",
            "-rtsp_transport",
            "tcp",
            "-rtsp_flags",
            "prefer_tcp",
            "-timeout",
            "15000000",
            "-analyzeduration",
            "20000000",
            "-probesize",
            "20000000",
            "-use_wallclock_as_timestamps",
            "1",
            "-fflags",
            "+genpts+discardcorrupt",
            "-i",
            str(rtsp_url),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-muxdelay",
            "0.1",
            "-muxpreload",
            "0.1",
            "-f",
            "hls",
            "-hls_time",
            str(HLS_SEGMENT_SECONDS),
            "-hls_list_size",
            "0",
            "-hls_flags",
            "independent_segments",
            "-hls_segment_filename",
            str(out_dir / "segment_%06d.ts"),
            str(out_dir / "index.m3u8"),
        ]

    def _drain_ffmpeg_stderr(self, task_id: str, proc: subprocess.Popen) -> None:
        def _worker() -> None:
            stream = proc.stderr
            if stream is None:
                return
            try:
                for raw in iter(stream.readline, b""):
                    line = bytes(raw or b"").decode("utf-8", errors="ignore").strip()
                    if line:
                        self._log_ffmpeg_runtime_warning(task_id, line)
            except Exception:
                _log.debug("mobile record ffmpeg stderr reader ended task=%s", task_id, exc_info=True)

        threading.Thread(target=_worker, name=f"mobile-record-ffmpeg-{task_id}", daemon=True).start()

    def _drain_ffmpeg_stdout_progress(self, task_id: str, proc: subprocess.Popen) -> None:
        event = self._media_started_events.setdefault(task_id, threading.Event())

        def _worker() -> None:
            stream = proc.stdout
            if stream is None:
                return
            try:
                for raw in iter(stream.readline, b""):
                    line = bytes(raw or b"").decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    key, sep, value = line.partition("=")
                    if not sep:
                        continue
                    if key in {"out_time_us", "out_time_ms"}:
                        if _as_int(value) > 0:
                            event.set()
                    elif key == "frame":
                        if _as_int(value) > 0:
                            event.set()
            except Exception:
                _log.debug("mobile record ffmpeg progress reader ended task=%s", task_id, exc_info=True)

        threading.Thread(target=_worker, name=f"mobile-record-progress-stdout-{task_id}", daemon=True).start()

    def _start_record_progress_logger(self, task_id: str, proc: subprocess.Popen, req: dict[str, Any], out_dir: Path, started_at: datetime, estimated_seconds: int) -> None:
        def _worker() -> None:
            last_progress_callback_at = time.time()
            while proc.poll() is None and not self._closed:
                try:
                    now_dt = _now_dt()
                    elapsed = max(0, int((now_dt - started_at).total_seconds()))
                    current_row = self._fetch_task(task_id)
                    current_estimated = estimated_seconds
                    if current_row:
                        current_estimated = _as_int(current_row.get("estimated_duration_seconds")) + _as_int(current_row.get("extend_duration_seconds"))
                    self._log_task_progress(task_id, status="recording", elapsed_seconds=elapsed, estimated_seconds=current_estimated)
                    now_ts = time.time()
                    if now_ts - last_progress_callback_at >= 180:
                        last_progress_callback_at = now_ts
                        self._schedule_callback_from_thread(task_id)
                except Exception:
                    _log.debug("mobile record progress log failed task=%s", task_id, exc_info=True)
                time.sleep(30)

        threading.Thread(target=_worker, name=f"mobile-record-progress-{task_id}", daemon=True).start()

    def _schedule_callback_from_thread(self, task_id: str) -> None:
        if self._loop is None or self._loop.is_closed():
            return

        async def _callback_current() -> None:
            await self._callback_later(self._fetch_task(task_id))

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_callback_current(), self._loop)

    async def start_recording(self, req: dict[str, Any], *, request_base_url: str) -> tuple[bool, dict[str, Any], int]:
        async with self._lock:
            task_id = _safe_task_id(req.get("taskId"))
            classroom_id = str(req.get("classroomId") or "").strip()
            record_user_id = str(req.get("recordUserId") or "").strip()
            if not classroom_id:
                return False, {"code": "BAD_REQUEST", "message": "classroomId 不能为空"}, 400
            if not record_user_id:
                return False, {"code": "BAD_REQUEST", "message": "recordUserId 不能为空"}, 400
            if self._fetch_task(task_id):
                return False, {"code": "TASK_EXISTS", "message": "taskId 已存在", "current": self._status_payload(self._fetch_task(task_id))}, 409
            active_room = self._active_for_classroom(classroom_id)
            if active_room:
                return False, {"code": "CLASSROOM_RECORDING_CONFLICT", "message": "该教室正在录制中", "current": self._status_payload(active_room)}, 409
            nvr_device_id_value = _as_int(req.get("nvrDeviceId"))
            nvr_channel_num_value = _as_int(req.get("nvrChannelNum"))
            active_channel = self._active_for_channel(nvr_device_id_value, nvr_channel_num_value)
            if active_channel:
                return False, {"code": "CHANNEL_RECORDING_CONFLICT", "message": "当前通道/教室已有录制任务", "current": self._status_payload(active_channel)}, 409

            estimated = max(60, min(_as_int(req.get("estimatedDurationSeconds"), 1800), 6 * 3600))
            now = _now_dt()
            max_end = now + timedelta(seconds=estimated)
            out_dir = self._record_root() / now.strftime("%Y-%m-%d") / task_id
            out_dir.mkdir(parents=True, exist_ok=True)

            try:
                session, _reused = await asyncio.to_thread(
                    self._session_manager.create_or_get_session,
                    campus_code=str(req.get("campusCode") or "").strip(),
                    nvr_device_id=nvr_device_id_value,
                    nvr_channel_num=nvr_channel_num_value,
                    nvr_channel_id=str(req.get("nvrChannelId") or "").strip(),
                    ip_address=str(req.get("ipAddress") or "").strip(),
                    port=_as_int(req.get("port"), 554),
                    account=str(req.get("account") or "").strip(),
                    password=str(req.get("password") or "").strip(),
                    provider=str(req.get("provider") or "").strip() or None,
                    candidate_channels=req.get("candidateChannels") if isinstance(req.get("candidateChannels"), list) else None,
                    stream_profile=str(req.get("streamProfile") or "main").strip() or "main",
                    output_protocol="mpegts",
                    reuse_if_exists=True,
                )
            except Exception as exc:
                fallback_session = self._session_manager.get_session_by_key(nvr_device_id_value, nvr_channel_num_value)
                if fallback_session is None:
                    raise
                session = fallback_session
                _reused = True
                _log.warning(
                    "[MOBILE_RECORD_SESSION_FALLBACK] client_ip=%s taskId=%s reason=%s nvrDeviceId=%s nvrChannelNum=%s fallbackSessionId=%s fallbackRtspUrl=%s",
                    str(req.get("__clientIp") or ""),
                    task_id,
                    exc,
                    nvr_device_id_value,
                    nvr_channel_num_value,
                    getattr(session, "session_id", ""),
                    _mask_url_secret(getattr(session, "rtsp_url", "")),
                )
            _log.info(
                "[MOBILE_RECORD_SESSION] client_ip=%s taskId=%s nvrDeviceId=%s nvrIp=%s nvrPort=%s nvrAccount=%s nvrPassword=%s "
                "nvrChannelId=%s nvrChannelNum=%s recordUserId=%s recordUserName=%s estimatedDurationSeconds=%s rtspUrl=%s outputDir=%s",
                str(req.get("__clientIp") or ""),
                task_id,
                _as_int(req.get("nvrDeviceId")),
                str(req.get("ipAddress") or ""),
                _as_int(req.get("port"), 554),
                str(req.get("account") or ""),
                _mask_secret(req.get("password")),
                str(req.get("nvrChannelId") or ""),
                _as_int(req.get("nvrChannelNum")),
                record_user_id,
                str(req.get("recordUserName") or "").strip(),
                estimated,
                _mask_url_secret(session.rtsp_url),
                str(out_dir),
            )
            cmd = self._build_record_cmd(session.rtsp_url, out_dir)
            _log.info("[MOBILE_RECORD_FFMPEG_START] taskId=%s cmd=%s", task_id, _mask_url_secret(subprocess.list2cmdline(cmd)))
            callback_url = self._default_callback_url()
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.sleep(0.6)
            if proc.poll() is not None:
                stderr_text = ""
                try:
                    raw_err = proc.stderr.read() if proc.stderr is not None else b""
                    stderr_text = bytes(raw_err or b"").decode("utf-8", errors="ignore").strip()
                except Exception:
                    stderr_text = ""
                raise RuntimeError(f"record_ffmpeg_start_failed:exit={proc.returncode}:stderr={stderr_text[:500]}")
            self._processes[task_id] = proc
            play_url = self._public_play_url(request_base_url, task_id)
            now_iso = now.isoformat(timespec="seconds")
            with self._db.connect() as conn:
                conn.execute(
                    """
INSERT INTO edge_mobile_record_task(
  task_id,classroom_id,classroom_name,nvr_device_id,nvr_channel_num,nvr_channel_id,nvr_ip,nvr_port,nvr_account,provider,
  record_user_id,record_user_name,status,estimated_duration_seconds,extend_duration_seconds,start_time,max_end_time,
  output_dir,m3u8_path,play_url,ffmpeg_pid,callback_url,created_time,updated_time
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """.strip(),
                    (
                        task_id,
                        classroom_id,
                        str(req.get("classroomName") or "").strip(),
                        _as_int(req.get("nvrDeviceId")),
                        _as_int(req.get("nvrChannelNum")),
                        str(req.get("nvrChannelId") or "").strip(),
                        str(req.get("ipAddress") or "").strip(),
                        _as_int(req.get("port"), 554),
                        str(req.get("account") or "").strip(),
                        str(req.get("provider") or "hikvision").strip() or "hikvision",
                        record_user_id,
                        str(req.get("recordUserName") or "").strip(),
                        "starting",
                        estimated,
                        0,
                        now_iso,
                        None,
                        str(out_dir),
                        str(out_dir / "index.m3u8"),
                        play_url,
                        int(proc.pid or 0),
                        callback_url,
                        bjt_now_iso(),
                        bjt_now_iso(),
                    ),
                )
                conn.commit()
            self._drain_ffmpeg_stderr(task_id, proc)
            self._drain_ffmpeg_stdout_progress(task_id, proc)
            self._event(task_id, "starting", "录制进程已启动，等待媒体写入", {"pid": proc.pid, "outDir": str(out_dir)})
            try:
                media_started_at, media_info = await self._wait_for_hls_media_start(task_id, proc, out_dir)
            except Exception as exc:
                await self._terminate_process(task_id, int(proc.pid or 0), graceful=False)
                self._db.execute(
                    "UPDATE edge_mobile_record_task SET status='failed', finish_time=?, error_message=?, updated_time=? WHERE task_id=?",
                    (bjt_now_iso(), f"录像设备取流失败：{exc}", bjt_now_iso(), task_id),
                )
                self._event(task_id, "failed", "录制媒体启动失败", {"error": str(exc)})
                return False, {"code": "NETWORK_ERROR", "message": f"录像设备取流失败：{exc}"}, 502

            start_iso = media_started_at.isoformat(timespec="seconds")
            max_iso = (media_started_at + timedelta(seconds=estimated)).isoformat(timespec="seconds")
            self._db.execute(
                "UPDATE edge_mobile_record_task SET status='recording', start_time=?, max_end_time=?, updated_time=? WHERE task_id=?",
                (start_iso, max_iso, bjt_now_iso(), task_id),
            )
            self._event(task_id, "start", "录制已开始", {"pid": proc.pid, "outDir": str(out_dir), "media": media_info})
            row = self._fetch_task(task_id)
            self._log_task_started(task_id, req, status="recording")
            self._start_record_progress_logger(task_id, proc, req, out_dir, media_started_at, estimated)
            await self._callback_later(row)
            return True, self._status_payload(row, request_base_url=request_base_url), 200

    async def prewarm_recording_session(self, req: dict[str, Any]) -> None:
        nvr_device_id_value = _as_int(req.get("nvrDeviceId"))
        nvr_channel_num_value = _as_int(req.get("nvrChannelNum"))
        session, reused = await asyncio.to_thread(
            self._session_manager.create_or_get_session,
            campus_code=str(req.get("campusCode") or "").strip(),
            nvr_device_id=nvr_device_id_value,
            nvr_channel_num=nvr_channel_num_value,
            nvr_channel_id=str(req.get("nvrChannelId") or "").strip(),
            ip_address=str(req.get("ipAddress") or "").strip(),
            port=_as_int(req.get("port"), 554),
            account=str(req.get("account") or "").strip(),
            password=str(req.get("password") or "").strip(),
            provider=str(req.get("provider") or "").strip() or None,
            candidate_channels=req.get("candidateChannels") if isinstance(req.get("candidateChannels"), list) else None,
            stream_profile=str(req.get("streamProfile") or "main").strip() or "main",
            output_protocol="mpegts",
            reuse_if_exists=True,
        )
        _log.info(
            "[MOBILE_RECORD_PREWARM] taskId=%s nvrDeviceId=%s nvrChannelNum=%s reused=%s sessionId=%s rtspUrl=%s",
            str(req.get("taskId") or ""),
            nvr_device_id_value,
            nvr_channel_num_value,
            bool(reused),
            getattr(session, "session_id", ""),
            _mask_url_secret(getattr(session, "rtsp_url", "")),
        )

    def _read_playlist_duration_seconds(self, m3u8: Path) -> float:
        if not m3u8.exists():
            return 0.0
        total = 0.0
        try:
            for line in m3u8.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.startswith("#EXTINF:"):
                    continue
                value = line.split(":", 1)[1].rstrip(",").strip()
                if value:
                    total += float(value)
        except Exception:
            return 0.0
        return float(total)

    def _collect_hls_info(self, row: dict[str, Any], *, lightweight: bool = False) -> dict[str, Any]:
        out_dir = Path(str(row.get("output_dir") or ""))
        m3u8 = Path(str(row.get("m3u8_path") or out_dir / "index.m3u8"))
        segments = sorted(out_dir.glob("segment_*.ts")) if out_dir.exists() else []
        file_size = sum(int(p.stat().st_size) for p in segments if p.exists())
        duration = self._read_playlist_duration_seconds(m3u8)
        codec = str(row.get("codec") or "")
        if m3u8.exists():
            if duration <= 0.0 and not lightweight:
                try:
                    duration = float(probe_duration_seconds(str(m3u8)) or 0.0)
                except Exception:
                    duration = 0.0
            if not codec and not lightweight:
                try:
                    timings = probe_stream_timings(str(m3u8))
                    codec = str(timings.get("video_codec") or "")
                except Exception:
                    codec = ""
        return {
            "ok": bool(m3u8.exists() and len(segments) > 0),
            "m3u8": str(m3u8),
            "segment_count": len(segments),
            "file_size": int(file_size),
            "duration": float(duration),
            "codec": codec,
        }

    def _has_started_hls_media(self, out_dir: Path) -> tuple[bool, dict[str, Any]]:
        m3u8 = out_dir / "index.m3u8"
        segments = sorted(out_dir.glob("segment_*.ts")) if out_dir.exists() else []
        first_segment = segments[0] if segments else None
        first_size = int(first_segment.stat().st_size) if first_segment and first_segment.exists() else 0
        playlist_duration = self._read_playlist_duration_seconds(m3u8)
        ok = bool(m3u8.exists() or first_size > 0)
        return ok, {
            "m3u8": str(m3u8),
            "segmentCount": len(segments),
            "firstSegment": str(first_segment or ""),
            "firstSegmentSize": first_size,
            "playlistDuration": playlist_duration,
        }

    async def _wait_for_hls_media_start(self, task_id: str, proc: subprocess.Popen, out_dir: Path) -> tuple[datetime, dict[str, Any]]:
        deadline = time.time() + MEDIA_START_WAIT_SECONDS
        last_info: dict[str, Any] = {}
        event = self._media_started_events.setdefault(task_id, threading.Event())
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr_text = ""
                with contextlib.suppress(Exception):
                    raw_err = proc.stderr.read() if proc.stderr is not None else b""
                    stderr_text = bytes(raw_err or b"").decode("utf-8", errors="ignore").strip()
                raise RuntimeError(f"record_ffmpeg_start_failed:exit={proc.returncode}:stderr={stderr_text[:500]}")
            ok, info = self._has_started_hls_media(out_dir)
            last_info = info
            if event.is_set() or ok:
                started_at = _now_dt()
                _log.info(
                    "%s 确认录制媒体已开始 progress=%s segmentCount=%s firstSegmentSize=%s playlistDuration=%.3fs",
                    self._task_log_prefix(task_id, "recording"),
                    "yes" if event.is_set() else "no",
                    int(info.get("segmentCount") or 0),
                    int(info.get("firstSegmentSize") or 0),
                    float(info.get("playlistDuration") or 0.0),
                )
                return started_at, info
            await asyncio.sleep(0.25)
        if proc.poll() is None:
            started_at = _now_dt()
            _log.warning(
                "%s 未在 %.1fs 内拿到明确进度，但进程仍在运行，按启动成功继续；需关注后续 HLS 是否生成。last=%s",
                self._task_log_prefix(task_id, "recording"),
                MEDIA_START_WAIT_SECONDS,
                last_info,
            )
            return started_at, last_info
        raise TimeoutError(f"record_media_start_timeout:{last_info}")

    def _target_record_seconds(self, row: dict[str, Any]) -> int:
        return max(
            0,
            _as_int(row.get("estimated_duration_seconds")) + _as_int(row.get("extend_duration_seconds")),
        )

    def _should_defer_auto_stop_for_media_duration(self, row: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        target_seconds = self._target_record_seconds(row)
        start_dt = _parse_dt(row.get("start_time"))
        if target_seconds <= 0 or start_dt is None:
            return False, {"target": target_seconds, "duration": 0.0, "wall_elapsed": 0.0}

        now_dt = _now_dt()
        wall_elapsed = max(0.0, (now_dt - start_dt).total_seconds())
        info = self._collect_hls_info(row, lightweight=True)
        media_duration = float(info.get("duration") or 0.0)
        missing = float(target_seconds) - media_duration
        if missing <= AUTO_STOP_MEDIA_TOLERANCE_SECONDS:
            info.update({"target": target_seconds, "wall_elapsed": wall_elapsed, "missing": missing})
            return False, info
        if wall_elapsed >= float(target_seconds) + AUTO_STOP_MAX_EXTRA_SECONDS:
            info.update({"target": target_seconds, "wall_elapsed": wall_elapsed, "missing": missing})
            return False, info
        info.update({"target": target_seconds, "wall_elapsed": wall_elapsed, "missing": missing})
        return True, info

    async def _terminate_process(self, task_id: str, pid: int, *, graceful: bool = True) -> None:
        proc = self._processes.get(task_id)
        if proc is not None and proc.poll() is None:
            if graceful and proc.stdin:
                with contextlib.suppress(Exception):
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
            try:
                await asyncio.to_thread(proc.wait, timeout=12)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, timeout=5)
                except Exception:
                    with contextlib.suppress(Exception):
                        proc.kill()
            self._processes.pop(task_id, None)
            return
        if _pid_exists(pid):
            args = ["taskkill", "/PID", str(int(pid)), "/T"]
            if not graceful:
                args.append("/F")
            with contextlib.suppress(Exception):
                await asyncio.to_thread(subprocess.run, args, capture_output=True, timeout=10)

    async def _finalize_stop(
        self,
        task_id: str,
        *,
        action: str,
        operator_user_id: str,
        reason: str,
        request_base_url: str,
    ) -> None:
        try:
            row = self._fetch_task(task_id)
            if not row:
                return
            await self._terminate_process(task_id, _as_int(row.get("ffmpeg_pid")), graceful=True)
            row = self._fetch_task(task_id) or row
            if str(action or "finish").lower() == "cancel":
                out_dir = Path(str(row.get("output_dir") or ""))
                if out_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, out_dir, True)
                self._db.execute(
                    """
UPDATE edge_mobile_record_task
SET status='cancelled', finish_time=?, m3u8_path='', play_url='', segment_count=0, file_size=0,
    duration_seconds=0, codec='', error_message='', updated_time=?
WHERE task_id=?
                    """.strip(),
                    (bjt_now_iso(), bjt_now_iso(), task_id),
                )
                self._event(task_id, "cancel", "录制已取消并删除文件", {"operatorUserId": operator_user_id})
                final_row = self._fetch_task(task_id)
                if final_row:
                    self._log_task_stopped(final_row, stop_reason=str(reason or "cancel"))
                await self._callback_later(final_row)
                return

            info = self._collect_hls_info(row, lightweight=True)
            if not info["ok"]:
                self._db.execute(
                    "UPDATE edge_mobile_record_task SET status='failed', finish_time=?, error_message=?, updated_time=? WHERE task_id=?",
                    (bjt_now_iso(), "录制文件无效：未生成有效 HLS 分片", bjt_now_iso(), task_id),
                )
                self._event(task_id, "failed", "录制文件无效", info)
                final_row = self._fetch_task(task_id)
                self._log_task_failed(task_id, status="failed", reason=str(final_row.get("error_message") if final_row else "录制文件无效：未生成有效 HLS 分片"))
                await self._callback_later(final_row)
                return

            self._db.execute(
                """
UPDATE edge_mobile_record_task
SET status='finished', finish_time=?, m3u8_path=?, play_url=?, segment_count=?, file_size=?,
    duration_seconds=?, codec=?, updated_time=?
WHERE task_id=?
                """.strip(),
                (
                    bjt_now_iso(),
                    str(info["m3u8"]),
                    self._public_play_url(request_base_url, task_id) if request_base_url else str(row.get("play_url") or ""),
                    int(info["segment_count"]),
                    int(info["file_size"]),
                    float(info["duration"]),
                    str(info["codec"]),
                    bjt_now_iso(),
                    task_id,
                ),
            )
            self._write_meta(self._fetch_task(task_id))
            self._event(task_id, "finish", "录制已完成", info)
            final_row = self._fetch_task(task_id)
            if final_row:
                self._log_task_stopped(final_row, stop_reason=str(reason or "manual"))
            await self._callback_later(final_row)
        finally:
            self._finalizing_tasks.discard(task_id)
            self._media_started_events.pop(task_id, None)

    async def stop_recording(self, task_id: str, *, action: str = "finish", operator_user_id: str = "", reason: str = "manual", request_base_url: str = "") -> tuple[bool, dict[str, Any], int]:
        task_id = str(task_id or "").strip()
        if not task_id:
            return False, {"code": "BAD_REQUEST", "message": "taskId 不能为空"}, 400
        async with self._lock:
            row = self._fetch_task(task_id)
            if not row:
                return False, {"code": "NOT_FOUND", "message": "录制任务不存在"}, 404
            status = str(row.get("status") or "")
            if status in FINAL_STATUSES:
                return True, self._status_payload(row, request_base_url=request_base_url), 200
            if task_id in self._finalizing_tasks or status == "stopping":
                return True, self._status_payload(self._fetch_task(task_id) or row, request_base_url=request_base_url), 200
            now = bjt_now_iso()
            self._db.execute(
                "UPDATE edge_mobile_record_task SET status=?, stop_reason=?, manual_stop_time=?, updated_time=? WHERE task_id=?",
                ("stopping", str(reason or "manual"), now if reason != "auto_timeout" else None, now, task_id),
            )
            self._finalizing_tasks.add(task_id)
            asyncio.create_task(
                self._finalize_stop(
                    task_id,
                    action=str(action or "finish").lower(),
                    operator_user_id=operator_user_id,
                    reason=reason,
                    request_base_url=request_base_url,
                )
            )
            return True, self._status_payload(self._fetch_task(task_id) or row, request_base_url=request_base_url), 200

    async def _delete_recording_files_background(self, task_id: str, *, operator_user_id: str, output_dir: str, status: str) -> None:
        try:
            out_dir = Path(str(output_dir or ""))
            if out_dir.exists():
                await asyncio.to_thread(shutil.rmtree, out_dir, True)

            self._db.execute(
                """
UPDATE edge_mobile_record_task
SET m3u8_path='', play_url='', segment_count=0, file_size=0,
    duration_seconds=0, codec='', error_message='录制文件已删除', updated_time=?
WHERE task_id=?
                """.strip(),
                (bjt_now_iso(), task_id),
            )
            self._event(task_id, "delete_files", "录制文件已删除", {"operatorUserId": operator_user_id})
            _log.info(
                "%s 删除录制文件完成 operatorUserId=%s outputDir=%s",
                self._task_log_prefix(task_id, status),
                str(operator_user_id or ""),
                str(out_dir),
            )
        except Exception as exc:
            self._db.execute(
                "UPDATE edge_mobile_record_task SET error_message=?, updated_time=? WHERE task_id=?",
                (f"录制文件删除失败：{exc}", bjt_now_iso(), task_id),
            )
            self._event(task_id, "delete_files_failed", "录制文件删除失败", {"operatorUserId": operator_user_id, "error": str(exc)})
            _log.warning(
                "%s 删除录制文件失败 operatorUserId=%s outputDir=%s error=%s",
                self._task_log_prefix(task_id, status),
                str(operator_user_id or ""),
                str(output_dir or ""),
                exc,
            )
        finally:
            self._deleting_tasks.discard(task_id)

    async def delete_recording_files(self, task_id: str, *, operator_user_id: str = "") -> tuple[bool, dict[str, Any], int]:
        task_id = str(task_id or "").strip()
        if not task_id:
            return False, {"code": "BAD_REQUEST", "message": "taskId 不能为空"}, 400
        async with self._lock:
            row = self._fetch_task(task_id)
            if not row:
                return False, {"code": "NOT_FOUND", "message": "录制任务不存在"}, 404
            status = str(row.get("status") or "")
            if status in ACTIVE_STATUSES or task_id in self._finalizing_tasks:
                return False, {"code": "BAD_STATUS", "message": "任务正在录制或停止中，请先结束后再删除"}, 409
            if task_id in self._deleting_tasks:
                return True, {"code": "SUCCESS", "message": "删除成功"}, 200

            out_dir = Path(str(row.get("output_dir") or ""))
            if out_dir.exists():
                root = self._record_root().resolve()
                target = out_dir.resolve()
                try:
                    target.relative_to(root)
                except Exception:
                    return False, {"code": "BAD_PATH", "message": f"录制目录不在允许删除范围内：{target}"}, 400
            self._deleting_tasks.add(task_id)
            asyncio.create_task(
                self._delete_recording_files_background(
                    task_id,
                    operator_user_id=operator_user_id,
                    output_dir=str(out_dir),
                    status=status,
                )
            )
            _log.info(
                "%s 删除录制文件请求已受理 operatorUserId=%s outputDir=%s",
                self._task_log_prefix(task_id, status),
                str(operator_user_id or ""),
                str(out_dir),
            )
            return True, {"code": "SUCCESS", "message": "删除成功"}, 200

    def _write_meta(self, row: dict[str, Any] | None) -> None:
        if not row:
            return
        try:
            out_dir = Path(str(row.get("output_dir") or ""))
            if not out_dir.exists():
                return
            payload = self._status_payload(row)
            (out_dir / "meta.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            _log.exception("mobile record meta write failed task=%s", row.get("task_id"))

    async def extend_recording(self, task_id: str, extend_seconds: int, operator_user_id: str = "") -> tuple[bool, dict[str, Any], int]:
        async with self._lock:
            row = self._fetch_task(task_id)
            if not row:
                return False, {"code": "NOT_FOUND", "message": "录制任务不存在"}, 404
            if str(row.get("status") or "") != "recording":
                return False, {"code": "BAD_STATUS", "message": "只有录制中任务允许延长", "current": self._status_payload(row)}, 400
            seconds = max(60, min(int(extend_seconds or 0), 3 * 3600))
            max_dt = _parse_dt(row.get("max_end_time")) or _now_dt()
            new_max = max_dt + timedelta(seconds=seconds)
            self._db.execute(
                "UPDATE edge_mobile_record_task SET extend_duration_seconds=extend_duration_seconds+?, max_end_time=?, updated_time=? WHERE task_id=?",
                (seconds, new_max.isoformat(timespec="seconds"), bjt_now_iso(), task_id),
            )
            self._event(task_id, "extend", "录制时长已延长", {"extendSeconds": seconds, "operatorUserId": operator_user_id})
            updated_row = self._fetch_task(task_id)
            if updated_row:
                self._log_task_extended(updated_row, extend_seconds=seconds, operator_user_id=operator_user_id)
            await self._callback_later(updated_row)
            return True, self._status_payload(updated_row), 200

    def get_status(self, *, task_id: str = "", classroom_id: str = "", request_base_url: str = "") -> dict[str, Any]:
        row = None
        if str(task_id or "").strip():
            row = self._fetch_task(str(task_id).strip())
        elif str(classroom_id or "").strip():
            row = self._active_for_classroom(str(classroom_id).strip())
        return self._status_payload(row, request_base_url=request_base_url)

    def list_tasks(self, *, classroom_id: str = "", record_user_id: str = "", status: str = "", date: str = "", limit: int = 100) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if classroom_id:
            where.append("classroom_id=?")
            params.append(classroom_id)
        if record_user_id:
            where.append("record_user_id=?")
            params.append(record_user_id)
        if status:
            where.append("status=?")
            params.append(status)
        if date:
            where.append("substr(start_time,1,10)=?")
            params.append(date)
        sql = "SELECT * FROM edge_mobile_record_task"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 100), 500)))
        return [self._status_payload(dict(row)) for row in self._db.fetch_all(sql, params)]

    async def _scheduler_loop(self) -> None:
        while not self._closed:
            try:
                await self._auto_stop_due_tasks()
            except Exception:
                _log.exception("mobile record scheduler tick failed")
            await asyncio.sleep(5)

    async def _auto_stop_due_tasks(self) -> None:
        now = bjt_now_iso()
        rows = self._db.fetch_all(
            "SELECT * FROM edge_mobile_record_task WHERE status='recording' AND max_end_time IS NOT NULL AND max_end_time<=?",
            (now,),
        )
        for row in rows:
            task_id = str(row["task_id"])
            defer, info = self._should_defer_auto_stop_for_media_duration(dict(row))
            if defer:
                delay = max(1, min(5, int(float(info.get("missing") or 0.0)) + 1))
                next_check = (_now_dt() + timedelta(seconds=delay)).isoformat(timespec="seconds")
                self._db.execute(
                    "UPDATE edge_mobile_record_task SET max_end_time=?, updated_time=? WHERE task_id=?",
                    (next_check, bjt_now_iso(), task_id),
                )
                _log.info(
                    "%s 自动停止延后 mediaDuration=%.3fs target=%ss missing=%.3fs wallElapsed=%.1fs nextCheck=%s",
                    self._task_log_prefix(task_id, "recording"),
                    float(info.get("duration") or 0.0),
                    int(info.get("target") or 0),
                    float(info.get("missing") or 0.0),
                    float(info.get("wall_elapsed") or 0.0),
                    next_check,
                )
                continue
            self._db.execute("UPDATE edge_mobile_record_task SET auto_stop_time=?, updated_time=? WHERE task_id=?", (bjt_now_iso(), bjt_now_iso(), task_id))
            self._event(task_id, "auto_stop", "到达预计录制时长，自动停止")
            await self.stop_recording(task_id, action="finish", reason="auto_timeout")

    def recover_incomplete_tasks(self) -> None:
        rows = self._db.fetch_all(
            f"SELECT * FROM edge_mobile_record_task WHERE status IN ({','.join('?' for _ in ACTIVE_STATUSES)})",
            ACTIVE_STATUSES,
        )
        for row_obj in rows:
            row = dict(row_obj)
            task_id = str(row.get("task_id") or "")
            pid = _as_int(row.get("ffmpeg_pid"))
            if pid > 0 and _pid_exists(pid):
                self._event(task_id, "recover", "服务重启后检测到录制进程仍存在，继续等待后续控制", {"pid": pid})
                continue
            info = self._collect_hls_info(row, lightweight=True)
            if info["ok"]:
                self._db.execute(
                    "UPDATE edge_mobile_record_task SET status='finished', finish_time=?, segment_count=?, file_size=?, duration_seconds=?, codec=?, updated_time=? WHERE task_id=?",
                    (bjt_now_iso(), int(info["segment_count"]), int(info["file_size"]), float(info["duration"]), str(info["codec"]), bjt_now_iso(), task_id),
                )
                self._event(task_id, "recover_finished", "服务重启后修正为已完成", info)
                final_row = self._fetch_task(task_id) or row
                self._log_task_stopped(final_row, stop_reason=str(row.get("stop_reason") or "recover_finished"))
                asyncio.run(self._callback_later(final_row))
            else:
                self._db.execute(
                    "UPDATE edge_mobile_record_task SET status='interrupted', finish_time=?, error_message=?, updated_time=? WHERE task_id=?",
                    (bjt_now_iso(), "边缘服务重启后未发现有效录制进程或 HLS 文件", bjt_now_iso(), task_id),
                )
                self._event(task_id, "recover_interrupted", "服务重启后修正为中断", info)
                self._log_task_failed(task_id, status="interrupted", reason="边缘服务重启后未发现有效录制进程或 HLS 文件")
                asyncio.run(self._callback_later(self._fetch_task(task_id)))

    async def _callback_later(self, row: dict[str, Any] | None) -> None:
        if not row:
            return
        if not str(row.get("callback_url") or "").strip():
            row = dict(row)
            row["callback_url"] = self._default_callback_url()
        if not str(row.get("callback_url") or "").strip():
            return
        asyncio.create_task(self._send_callback(dict(row)))

    async def _send_callback(self, row: dict[str, Any]) -> None:
        task_id = str(row.get("task_id") or "")
        url = str(row.get("callback_url") or "").strip()
        payload = self._build_callback_payload_from_row(row)
        await self._send_callback_payload(task_id=task_id, url=url, payload=payload)

    async def _send_callback_payload(self, *, task_id: str, url: str, payload: dict[str, Any]) -> None:
        delays = [0, 10, 30, 60, 300]
        last_error = ""
        status = str(payload.get("status") or "")
        stale_retry_statuses = {"starting", "recording", "stopping"}
        payload_summary = {
            "taskId": str(payload.get("taskId") or task_id),
            "status": status,
            "stopReason": payload.get("stopReason"),
            "segmentCount": payload.get("segmentCount"),
            "fileSize": payload.get("fileSize"),
            "fileSizeMB": _format_size_mb(payload.get("fileSize")),
            "duration": payload.get("duration"),
            "playUrl": payload.get("playUrl"),
        }
        headers = self._callback_headers()
        header_keys = ",".join(sorted(headers.keys()))
        for idx, delay in enumerate(delays):
            if delay > 0:
                await asyncio.sleep(delay)
            current_row = self._fetch_task(task_id)
            current_status = str(current_row.get("status") or "") if current_row else ""
            if status in stale_retry_statuses and current_status and current_status != status:
                _log.info(
                    "[MOBILE_RECORD_CALLBACK] taskId=%s status=%s result=stale_skip currentStatus=%s",
                    task_id,
                    status,
                    current_status,
                )
                return
            try:
                log_fn = _log.info if idx == 0 else _log.debug
                log_fn(
                    "[MOBILE_RECORD_CALLBACK] taskId=%s status=%s attempt=%s/%s url=%s headerKeys=%s payload=%s",
                    task_id,
                    status,
                    idx + 1,
                    len(delays),
                    url,
                    header_keys,
                    json.dumps(payload_summary, ensure_ascii=False),
                )
                async with httpx.AsyncClient(timeout=15.0) as client:
                    res = await client.post(url, json=payload, headers=headers)
                body: dict[str, Any] = {}
                with contextlib.suppress(Exception):
                    body = dict(res.json() or {})
                if 200 <= int(res.status_code) < 300 and str(body.get("code") or "").upper() == "SUCCESS":
                    self._db.execute(
                        "UPDATE edge_mobile_record_task SET callback_status='success', callback_retry_count=?, callback_last_error='', updated_time=? WHERE task_id=?",
                        (idx, bjt_now_iso(), task_id),
                    )
                    self._event(task_id, "callback_success", "服务端回调成功", {"statusCode": res.status_code, "responseCode": body.get("code"), "responseMessage": body.get("message")})
                    _log.info(
                        "[MOBILE_RECORD_CALLBACK] taskId=%s status=%s result=success httpStatus=%s responseCode=%s responseMessage=%s",
                        task_id,
                        status,
                        res.status_code,
                        str(body.get("code") or ""),
                        _normalize_log_text(str(body.get("message") or ""), limit=300),
                    )
                    return
                response_code = str(body.get("code") or "")
                response_message = str(body.get("message") or "")
                last_error = f"status={res.status_code}, code={response_code}, message={response_message}"
            except Exception as exc:
                last_error = str(exc)
            _log.warning(
                "[MOBILE_RECORD_CALLBACK] taskId=%s status=%s result=retrying attempt=%s/%s playUrl=%s fileSize=%s error=%s",
                task_id,
                status,
                idx + 1,
                len(delays),
                str(payload.get("playUrl") or ""),
                _format_size_mb(payload.get("fileSize")),
                _normalize_log_text(last_error, limit=600),
            )
            self._db.execute(
                "UPDATE edge_mobile_record_task SET callback_status='retrying', callback_retry_count=?, callback_last_error=?, updated_time=? WHERE task_id=?",
                (idx + 1, last_error, bjt_now_iso(), task_id),
            )
        self._db.execute(
            "UPDATE edge_mobile_record_task SET callback_status='failed', callback_last_error=?, updated_time=? WHERE task_id=?",
            (last_error, bjt_now_iso(), task_id),
        )
        self._event(task_id, "callback_failed", "服务端回调失败", {"error": last_error})
        _log.warning(
            "[MOBILE_RECORD_CALLBACK] taskId=%s status=%s result=failed playUrl=%s fileSize=%s error=%s",
            task_id,
            status,
            str(payload.get("playUrl") or ""),
            _format_size_mb(payload.get("fileSize")),
            _normalize_log_text(last_error, limit=600),
        )

    def resolve_play_file(self, task_id: str, rel_path: str) -> Path | None:
        row = self._fetch_task(str(task_id or "").strip())
        if not row or str(row.get("status") or "") != "finished":
            return None
        out_dir = Path(str(row.get("output_dir") or "")).resolve()
        target = (out_dir / str(rel_path or "index.m3u8")).resolve()
        try:
            target.relative_to(out_dir)
        except Exception:
            return None
        return target if target.exists() and target.is_file() else None
