from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..constants import StepStatus, TaskStatus
from ..utils import get_lesson_dir as _get_lesson_dir_util, resolve_lesson_date as _resolve_lesson_date_util, task_type_prefix as _task_type_prefix_util
from ..video.ffmpeg import _ffmpeg_bin, ffmpeg_exists


class RerunCleanupError(RuntimeError):
    pass


class TaskStateMixin:
    """任务状态持久化、清理、暂停/恢复/终止重置相关方法。"""

    def _append_task_log(self, task_db_id: int, step_code: str, message: str, log_level: str = "INFO") -> None:
        step = str(step_code or "").strip().upper()
        if not step:
            return
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), step, str(log_level or "INFO").upper(), str(message or "")),
            )
            conn.commit()

    def _stale_recover_threshold_seconds(self) -> int:
        try:
            import os

            v = int(os.getenv("EDGE_STALE_RECOVER_THRESHOLD_SEC") or "300")
            return max(60, v)
        except Exception:
            return 300

    def _parse_utc_iso(self, value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _is_recent_running_task(self, updated_time: str, start_time: str) -> bool:
        threshold = float(self._stale_recover_threshold_seconds())
        now = datetime.now(timezone.utc)
        updated_dt = self._parse_utc_iso(updated_time)
        if updated_dt is not None and (now - updated_dt).total_seconds() < threshold:
            return True
        start_dt = self._parse_utc_iso(start_time)
        if updated_dt is None and start_dt is not None and (now - start_dt).total_seconds() < threshold:
            return True
        return False

    def _load_current_step(self, task_db_id: int) -> str:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT current_step FROM edge_stream_task WHERE id=? LIMIT 1",
                (int(task_db_id),),
            ).fetchone()
            if r is None:
                return ""
            return str(r["current_step"] or "").strip().upper()

    def _find_task_by_server_id(self, server_task_id: int) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            r = conn.execute(
                """
SELECT
  t.id,
  t.task_status,
  (
    SELECT s.step_status
    FROM edge_stream_task_step s
    WHERE s.task_id=t.id AND s.step_code='DOWNLOAD'
    LIMIT 1
  ) AS download_step_status
 ,(
    SELECT s.finalize_pending
    FROM edge_stream_task_step s
    WHERE s.task_id=t.id AND s.step_code='DOWNLOAD'
    LIMIT 1
  ) AS download_finalize_pending
 ,(
    SELECT s.step_status
    FROM edge_stream_task_step s
    WHERE s.task_id=t.id AND s.step_code='TRANSCODE'
    LIMIT 1
  ) AS transcode_step_status
 ,(
    SELECT s.finalize_pending
    FROM edge_stream_task_step s
    WHERE s.task_id=t.id AND s.step_code='TRANSCODE'
    LIMIT 1
  ) AS transcode_finalize_pending
FROM edge_stream_task t
WHERE t.server_task_id=? AND t.task_kind='CameraTask'
ORDER BY t.id DESC
LIMIT 1
                """.strip(),
                (int(server_task_id),),
            ).fetchone()
            return dict(r) if r is not None else None

    def _resolve_lesson_dir_from_raw(self, raw: dict[str, Any]) -> Path:
        lesson_id = str(raw.get("lessonId") or "").strip() or "0"
        lesson_date = _resolve_lesson_date_util(raw.get("lessonDate"), raw.get("lessonStartAt"), raw.get("downloadStart"))
        return _get_lesson_dir_util(lesson_date, lesson_id)

    def _reset_download_part_done_meta(self, done_path: Path) -> bool:
        try:
            meta = json.loads(done_path.read_text(encoding="utf-8", errors="ignore") or "{}") or {}
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        changed = False
        for key in [
            "merge_part_path",
            "normalize_reason",
            "normalize_classification",
            "force_canonical_merge",
            "canonicalize_merge_part",
            "merge_risk_level",
            "process_started_mono",
        ]:
            if key in meta:
                meta.pop(key, None)
                changed = True
        if bool(meta.get("process_completed")):
            meta["process_completed"] = False
            changed = True
        if not changed:
            return False
        try:
            done_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8", errors="ignore")
            return True
        except Exception:
            return False

    def _cleanup_download_derived_files(self, task_db_id: int) -> int:
        raw = self._load_task_raw_by_dbid(task_db_id) or {}
        try:
            task_type = int(raw.get("taskType") or 0)
        except Exception:
            task_type = 0
        prefix = _task_type_prefix_util(task_type)
        lesson_id = str(raw.get("lessonId") or "").strip() or "0"
        lesson_date = _resolve_lesson_date_util(raw.get("lessonDate"), raw.get("lessonStartAt"), default="unknown")
        server_task_id = str(raw.get("taskId") or raw.get("id") or "").strip() or "0"
        out_dir = _get_lesson_dir_util(lesson_date, lesson_id)
        if not out_dir.exists():
            return 0
        cleaned = 0
        for pattern in [
            f"{prefix}_{server_task_id}.part*.systrans.mp4",
            f"{prefix}_{server_task_id}.part*.systrans.*.mp4",
            f"{prefix}_{server_task_id}.part*.fixed.mp4",
            f"{prefix}_{server_task_id}.part*.fixed.*.mp4",
            f"{prefix}_{server_task_id}.part*.timeline.tmp.mp4",
            f"{prefix}_{server_task_id}.part*.avsync.tmp.mp4",
            f"{prefix}_{server_task_id}.merged.mp4",
            f"{prefix}_{server_task_id}.merged*.mp4",
            f"{prefix}_{server_task_id}.merged*.txt",
            f"{prefix}_{server_task_id}.merged.preflight_*.mp4",
            f"{prefix}_{server_task_id}.merged.preflight_*.txt",
            f"{prefix}_{server_task_id}.norm*.mp4",
            f"{prefix}_{server_task_id}.ts*.ts",
            f"{prefix}_{server_task_id}_concat*.txt",
            f"{prefix}_{server_task_id}.mp4.*.attempt",
            f"{prefix}_{server_task_id}.mp4.rtsp_temp.mp4",
            f"{prefix}_{server_task_id}.aligned.mp4",
            f"{prefix}_{server_task_id}.faststart.mp4",
            f"{prefix}_{server_task_id}.download_state.json",
            f"{prefix}_{server_task_id}.process_state.json",
        ]:
            for f in out_dir.glob(pattern):
                try:
                    f.unlink(missing_ok=True)
                    cleaned += 1
                except Exception:
                    pass
        for d in out_dir.glob(f"{prefix}_{server_task_id}.merged*"):
            try:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    cleaned += 1
            except Exception:
                pass
        for d in out_dir.glob(f"{prefix}_{server_task_id}.tsbridge"):
            try:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    cleaned += 1
            except Exception:
                pass
        for done in out_dir.glob(f"{prefix}_{server_task_id}.part*.done"):
            if self._reset_download_part_done_meta(done):
                cleaned += 1
        return cleaned

    def _download_reprocess_state_paths(self, raw: dict[str, Any]) -> tuple[Path, Path]:
        try:
            task_type = int(raw.get("taskType") or 0)
        except Exception:
            task_type = 0
        prefix = _task_type_prefix_util(task_type)
        lesson_id = str(raw.get("lessonId") or "").strip() or "0"
        lesson_date = _resolve_lesson_date_util(raw.get("lessonDate"), raw.get("lessonStartAt"), default="unknown")
        server_task_id = str(raw.get("taskId") or raw.get("id") or "").strip() or "0"
        out_dir = _get_lesson_dir_util(lesson_date, lesson_id)
        return (
            out_dir / f"{prefix}_{server_task_id}.download_state.json",
            out_dir / f"{prefix}_{server_task_id}.process_state.json",
        )

    def _read_json_file(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "{}") or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _preserve_download_elapsed_for_reprocess(self, raw: dict[str, Any]) -> tuple[float, float]:
        download_state_path, process_state_path = self._download_reprocess_state_paths(raw)

        def _elapsed(value: Any) -> float:
            try:
                return max(0.0, float(value or 0.0))
            except Exception:
                return 0.0

        download_state = self._read_json_file(download_state_path)
        process_state = self._read_json_file(process_state_path)
        elapsed = max(
            _elapsed(download_state.get("segment_download_elapsed_seconds")),
            _elapsed(download_state.get("download_elapsed_seconds")),
            _elapsed(process_state.get("segment_download_elapsed_seconds")),
            _elapsed(process_state.get("download_elapsed_seconds")),
        )
        started_at = max(
            _elapsed(download_state.get("started_at_epoch")),
            _elapsed(process_state.get("started_at_epoch")),
        )
        return elapsed, started_at

    def _restore_download_elapsed_for_reprocess(self, raw: dict[str, Any], elapsed_seconds: float, started_at_epoch: float) -> None:
        try:
            elapsed = max(0.0, float(elapsed_seconds or 0.0))
        except Exception:
            elapsed = 0.0
        if elapsed <= 0.0:
            return
        try:
            started_at = float(started_at_epoch or 0.0)
        except Exception:
            started_at = 0.0
        if started_at <= 0.0:
            started_at = time.time()
        download_state_path, _process_state_path = self._download_reprocess_state_paths(raw)
        try:
            download_state_path.parent.mkdir(parents=True, exist_ok=True)
            download_state_path.write_text(
                json.dumps(
                    {
                        "started_at_epoch": started_at,
                        "subphase": "segment_download",
                        "download_elapsed_seconds": elapsed,
                        "segment_download_elapsed_seconds": elapsed,
                        "process_elapsed_seconds": 0.0,
                        "updated_at_epoch": time.time(),
                        "reprocess_elapsed_preserved": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
                errors="ignore",
            )
        except Exception:
            pass

    def _reset_task_for_reprocess(self, task_db_id: int) -> None:
        raw = self._load_task_raw_by_dbid(task_db_id) or {}
        try:
            task_type = int(raw.get("taskType") or 0)
        except Exception:
            task_type = 0
        prefix = _task_type_prefix_util(task_type)
        lesson_id = str(raw.get("lessonId") or "").strip() or "0"
        lesson_date = _resolve_lesson_date_util(raw.get("lessonDate"), raw.get("lessonStartAt"), default="unknown")
        server_task_id = str(raw.get("taskId") or raw.get("id") or "").strip() or "0"
        out_dir = _get_lesson_dir_util(lesson_date, lesson_id)
        raw_parts = list(out_dir.glob(f"{prefix}_{server_task_id}.part*.mp4")) if out_dir.exists() else []
        raw_parts = [p for p in raw_parts if p.exists() and p.stat().st_size > 0]
        if not raw_parts:
            raise RerunCleanupError("未找到可重新处理的原始分段")
        preserved_download_elapsed, preserved_started_at = self._preserve_download_elapsed_for_reprocess(raw)
        cleaned = self._cleanup_download_derived_files(task_db_id)
        self._restore_download_elapsed_for_reprocess(raw, preserved_download_elapsed, preserved_started_at)
        final = out_dir / f"{prefix}_{server_task_id}.mp4"
        if final.exists():
            try:
                final.unlink(missing_ok=True)
                cleaned += 1
            except Exception as e:
                raise RerunCleanupError(f"重新处理清理旧源视频失败: {final} ({e})") from e
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE edge_stream_task SET task_status=0, process_rate=0, current_step='', execute_node_id='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(task_db_id),),
            )
            conn.execute(
                "UPDATE edge_stream_task_step SET step_status=0, step_process=0, start_time=NULL, end_time=NULL, output_file_path=NULL, finalize_pending=0, finalize_src_path=NULL, finalize_dst_path=NULL, finalize_action=NULL, finalize_error='', finalize_retry_count=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code IN ('DOWNLOAD','TRANSCODE')",
                (int(task_db_id),),
            )
            conn.execute(
                "DELETE FROM edge_task_log WHERE task_id=? AND step_code IN ('DOWNLOAD','TRANSCODE')",
                (int(task_db_id),),
            )
            conn.execute(
                "DELETE FROM edge_lesson_output WHERE lesson_id=? AND server_task_id=? AND file_type IN ('source_video','transcoded_video')",
                (int(raw.get("lessonId") or 0), int(raw.get("taskId") or raw.get("id") or 0)),
            )
            conn.commit()
        self._append_task_log(int(task_db_id), "DOWNLOAD", f"手动重新处理：保留原始分段，已清理{cleaned}个派生产物并准备重新开始")
        self._queue_step_state_report(int(task_db_id), "DOWNLOAD")
        self._queue_step_state_report(int(task_db_id), "TRANSCODE")

    def _cleanup_analysis_outputs(self, raw: dict[str, Any]) -> None:
        lesson_dir = self._resolve_lesson_dir_from_raw(raw)
        report_dir = lesson_dir / "report"
        if report_dir.exists() and report_dir.is_dir():
            try:
                shutil.rmtree(report_dir)
                self._log.info("cleaned analysis dir: %s", report_dir)
            except Exception:
                for p in report_dir.rglob("*"):
                    try:
                        if p.is_file():
                            p.unlink(missing_ok=True)
                    except Exception:
                        pass
                for p in sorted(report_dir.rglob("*"), reverse=True):
                    try:
                        if p.is_dir():
                            p.rmdir()
                    except Exception:
                        pass
                try:
                    report_dir.rmdir()
                except Exception:
                    pass

    def _cleanup_subtitle_outputs(self, raw: dict[str, Any], strip_embedded_video_subtitles: bool = True) -> None:
        lesson_dir = self._resolve_lesson_dir_from_raw(raw)
        if not lesson_dir.exists():
            return
        for hls_dir in sorted(lesson_dir.glob("*_1080P")):
            if not hls_dir.is_dir():
                continue
            try:
                (hls_dir / "subtitles.zh.vtt").unlink(missing_ok=True)
            except Exception:
                pass
        videos: list[Path] = []
        if not strip_embedded_video_subtitles:
            return
        for video in sorted(lesson_dir.glob("*.mp4")):
            name = video.name.lower()
            if ".part" in name or "_nosub" in name or "_sub_tmp" in name or "_temp_embed" in name:
                continue
            try:
                if video.stat().st_size <= 0:
                    continue
            except Exception:
                continue
            videos.append(video)
        if not videos or not ffmpeg_exists():
            return
        ffmpeg_bin = _ffmpeg_bin()
        for video in videos:
            tmp_out = video.with_name(video.stem + ".rerun_strip_sub.mp4")
            bak = video.with_name(video.stem + ".rerun_strip_sub.bak.mp4")
            try:
                tmp_out.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                bak.unlink(missing_ok=True)
            except Exception:
                pass
            cmd = [
                ffmpeg_bin,
                "-y",
                "-i",
                str(video),
                "-map",
                "0:v",
                "-map",
                "0:a?",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(tmp_out),
            ]
            try:
                r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
                if r.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size <= 0:
                    try:
                        tmp_out.unlink(missing_ok=True)
                    except Exception:
                        pass
                    self._log.warning("strip subtitle streams failed: %s", video)
                    continue
                try:
                    video.rename(bak)
                except Exception:
                    bak = None
                try:
                    tmp_out.rename(video)
                except Exception:
                    shutil.copy2(str(tmp_out), str(video))
                    tmp_out.unlink(missing_ok=True)
                if bak is not None:
                    try:
                        bak.unlink(missing_ok=True)
                    except Exception:
                        pass
                self._log.info("cleaned embedded subtitles from video: %s", video)
            except Exception:
                try:
                    tmp_out.unlink(missing_ok=True)
                except Exception:
                    pass

    def _delete_lesson_outputs_for_steps(self, lesson_id: int, step_codes: list[str]) -> None:
        if int(lesson_id) <= 0:
            return
        step_to_types = {
            "DOWNLOAD": {"source_video"},
            "TRANSCODE": {"transcoded_video"},
            "SPEECH": {"subtitle_srt", "subtitle_vtt", "speech_report_html"},
            "SUBTITLE": {"subtitle_srt", "subtitled_video", "subtitle_vtt_hls"},
            "ANALYSIS": {"report_data", "report_web_html", "report_h5_html", "report_thumbnail"},
        }
        file_types: set[str] = set()
        for step_code in step_codes:
            file_types.update(step_to_types.get(str(step_code or "").strip().upper(), set()))
        if not file_types:
            return
        placeholders = ",".join("?" for _ in file_types)
        with self._db.connect() as conn:
            conn.execute(
                f"DELETE FROM edge_lesson_output WHERE lesson_id=? AND file_type IN ({placeholders})",
                (int(lesson_id), *sorted(file_types)),
            )
            conn.commit()

    def _mark_step_finalize_pending(
        self,
        task_db_id: int,
        step_code: str,
        *,
        src_path: str,
        dst_path: str,
        action: str,
        error: str,
        message: str,
    ) -> None:
        step = str(step_code or "").strip().upper() or "DOWNLOAD"
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT step_process FROM edge_stream_task_step WHERE task_id=? AND step_code=? LIMIT 1",
                (int(task_db_id), step),
            ).fetchone()
            progress = max(99, int(row["step_process"] or 0) if row is not None else 0)
            conn.execute(
                "UPDATE edge_stream_task SET task_status=?, current_step=?, process_rate=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(TaskStatus.PAUSED), step, progress, int(task_db_id)),
            )
            conn.execute(
                """
UPDATE edge_stream_task_step
SET step_status=?,
    step_process=?,
    finalize_pending=1,
    finalize_src_path=?,
    finalize_dst_path=?,
    finalize_action=?,
    finalize_error=?,
    updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE task_id=? AND step_code=?
                """.strip(),
                (int(StepStatus.PAUSED), progress, str(src_path or ""), str(dst_path or ""), str(action or ""), str(error or "")[:500], int(task_db_id), step),
            )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), step, "WARNING", str(message or "文件暂时被占用，系统将自动重试最终提交")),
            )
            conn.commit()
        self._queue_step_state_report(int(task_db_id), step)

    def _list_finalize_pending_steps(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                """
SELECT t.id AS task_db_id, t.server_task_id, t.task_kind, s.step_code, s.finalize_src_path, s.finalize_dst_path,
       s.finalize_action, s.finalize_error, s.finalize_retry_count
FROM edge_stream_task_step s
JOIN edge_stream_task t ON t.id=s.task_id
WHERE t.task_kind='CameraTask' AND s.finalize_pending=1
ORDER BY s.updated_time ASC
LIMIT ?
                """.strip(),
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in (rows or [])]

    def _complete_step_finalize_pending(self, task_db_id: int, step_code: str, final_path: str) -> None:
        step = str(step_code or "").strip().upper() or "DOWNLOAD"
        final_fp = str(final_path or "").strip()
        with self._db.connect() as conn:
            conn.execute(
                """
UPDATE edge_stream_task_step
SET step_status=?,
    step_process=100,
    end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    output_file_path=?,
    finalize_pending=0,
    finalize_src_path=NULL,
    finalize_dst_path=NULL,
    finalize_action=NULL,
    finalize_error='',
    updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE task_id=? AND step_code=?
                """.strip(),
                (int(StepStatus.SUCCESS), final_fp, int(task_db_id), step),
            )
            if step == "DOWNLOAD":
                next_step = self._get_first_pending_step(conn, int(task_db_id), "CameraTask")
                if next_step:
                    conn.execute(
                        "UPDATE edge_stream_task SET task_status=?, current_step=?, process_rate=100, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (int(TaskStatus.PENDING), str(next_step), int(task_db_id)),
                    )
                else:
                    conn.execute(
                        "UPDATE edge_stream_task SET task_status=?, current_step=?, process_rate=100, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (int(TaskStatus.SUCCESS), step, int(task_db_id)),
                    )
            else:
                conn.execute(
                    "UPDATE edge_stream_task SET task_status=?, current_step=?, process_rate=100, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (int(TaskStatus.SUCCESS), step, int(task_db_id)),
                )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), step, "INFO", "最终提交成功，已自动恢复任务"),
            )
            conn.commit()
        if step == "DOWNLOAD":
            self._append_task_log(int(task_db_id), "TRANSCODE", "等待自动进入转码")
        self._queue_step_state_report(int(task_db_id), step)

    def _mark_camera_download_complete_pending_transcode(self, task_db_id: int) -> bool:
        with self._db.connect() as conn:
            dl = conn.execute(
                "SELECT step_status, step_process FROM edge_stream_task_step WHERE task_id=? AND step_code='DOWNLOAD' LIMIT 1",
                (int(task_db_id),),
            ).fetchone()
            tc = conn.execute(
                "SELECT step_status FROM edge_stream_task_step WHERE task_id=? AND step_code='TRANSCODE' LIMIT 1",
                (int(task_db_id),),
            ).fetchone()
            if dl is None or tc is None:
                return False
            if int(dl["step_status"] or 0) != StepStatus.SUCCESS or int(dl["step_process"] or 0) < 100:
                return False
            if int(tc["step_status"] or 0) != StepStatus.PENDING:
                return False
            conn.execute(
                "UPDATE edge_stream_task SET task_status=?, current_step='TRANSCODE', process_rate=100, execute_node_id='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(TaskStatus.PENDING), int(task_db_id)),
            )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), "TRANSCODE", "INFO", "等待转码并发空闲"),
            )
            conn.commit()
        self._queue_step_state_report(int(task_db_id), "DOWNLOAD")
        self._queue_step_state_report(int(task_db_id), "TRANSCODE")
        return True

    def _fail_step_finalize_pending(self, task_db_id: int, step_code: str, reason: str) -> None:
        step = str(step_code or "").strip().upper() or "DOWNLOAD"
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE edge_stream_task SET task_status=?, current_step=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(TaskStatus.FAILED), step, int(task_db_id)),
            )
            conn.execute(
                """
UPDATE edge_stream_task_step
SET step_status=?,
    finalize_pending=0,
    finalize_error=?,
    end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE task_id=? AND step_code=?
                """.strip(),
                (int(StepStatus.FAILED), str(reason or "")[:500], int(task_db_id), step),
            )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), step, "ERROR", str(reason or "最终提交失败")),
            )
            conn.commit()
        self._queue_step_state_report(int(task_db_id), step)

    def _bump_step_finalize_retry(self, task_db_id: int, step_code: str, error: str) -> None:
        step = str(step_code or "").strip().upper() or "DOWNLOAD"
        with self._db.connect() as conn:
            conn.execute(
                """
UPDATE edge_stream_task_step
SET finalize_retry_count=COALESCE(finalize_retry_count,0)+1,
    finalize_error=?,
    updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE task_id=? AND step_code=?
                """.strip(),
                (str(error or "")[:500], int(task_db_id), step),
            )
            conn.commit()

    def _find_course_task_by_server_id(self, server_task_id: int) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            r = conn.execute(
                """
SELECT
  t.id,
  t.task_status,
  (
    SELECT s.step_status
    FROM edge_stream_task_step s
    WHERE s.task_id=t.id AND s.step_code='SPEECH'
    LIMIT 1
  ) AS speech_step_status,
  (
    SELECT s.step_status
    FROM edge_stream_task_step s
    WHERE s.task_id=t.id AND s.step_code='SUBTITLE'
    LIMIT 1
  ) AS subtitle_step_status,
  (
    SELECT s.step_status
    FROM edge_stream_task_step s
    WHERE s.task_id=t.id AND s.step_code='ANALYSIS'
    LIMIT 1
  ) AS analysis_step_status
FROM edge_stream_task t
WHERE t.server_task_id=? AND t.task_kind='CourseTask'
ORDER BY t.id DESC
LIMIT 1
                """.strip(),
                (int(server_task_id),),
            ).fetchone()
            return dict(r) if r is not None else None

    def _resolve_camera_active_step(self, task_db_id: int, preferred_step: str = "") -> str:
        with self._db.connect() as conn:
            return self._resolve_camera_active_step_conn(conn, task_db_id, preferred_step)

    def _resolve_camera_active_step_conn(self, conn, task_db_id: int, preferred_step: str = "") -> str:
        rows = conn.execute(
            "SELECT step_code, step_status, step_process, end_time, output_file_path FROM edge_stream_task_step WHERE task_id=? AND step_code IN ('DOWNLOAD','TRANSCODE')",
            (int(task_db_id),),
        ).fetchall()
        state_map = {
            str(r["step_code"] or "").strip().upper(): {
                "status": int(r["step_status"] or 0),
                "process": int(r["step_process"] or 0),
                "end_time": str(r["end_time"] or "").strip(),
                "output": str(r["output_file_path"] or "").strip(),
            }
            for r in (rows or [])
        }
        dl = state_map.get("DOWNLOAD", {})
        tc = state_map.get("TRANSCODE", {})
        dl_status = int(dl.get("status") or 0)
        tc_status = int(tc.get("status") or 0)
        preferred = str(preferred_step or "").strip().upper()
        if preferred == "TRANSCODE" and tc_status in (StepStatus.RUNNING, StepStatus.PAUSED):
            return "TRANSCODE"
        if preferred == "DOWNLOAD" and dl_status in (StepStatus.RUNNING, StepStatus.PAUSED):
            return "DOWNLOAD"
        if tc_status in (StepStatus.RUNNING, StepStatus.PAUSED):
            return "TRANSCODE"
        if dl_status in (StepStatus.RUNNING, StepStatus.PAUSED):
            return "DOWNLOAD"
        dl_done_like = dl_status == StepStatus.SUCCESS or (
            int(dl.get("process") or 0) >= 100 and (str(dl.get("end_time") or "") or str(dl.get("output") or ""))
        )
        if dl_done_like and tc_status != StepStatus.SUCCESS:
            return "TRANSCODE"
        task_row = conn.execute(
            "SELECT current_step FROM edge_stream_task WHERE id=? LIMIT 1",
            (int(task_db_id),),
        ).fetchone()
        current_step = str(task_row["current_step"] or "").strip().upper() if task_row is not None else ""
        if current_step in {"DOWNLOAD", "TRANSCODE"}:
            return current_step
        return "DOWNLOAD"

    def _count_camera_step_slots(
        self,
        step_code: str,
        *,
        include_statuses: tuple[int, ...] = (StepStatus.RUNNING,),
        exclude_task_db_id: int | None = None,
    ) -> int:
        return self._count_task_step_slots(
            "CameraTask",
            (str(step_code or "").strip().upper(),),
            include_statuses=include_statuses,
            exclude_task_db_id=exclude_task_db_id,
        )

    def _count_course_task_slots(
        self,
        step_code: str | None = None,
        *,
        include_statuses: tuple[int, ...] = (StepStatus.RUNNING,),
        exclude_task_db_id: int | None = None,
    ) -> int:
        target_step = str(step_code or "").strip().upper()
        if target_step in {"SPEECH", "SUBTITLE", "ANALYSIS"}:
            step_codes = (target_step,)
        else:
            step_codes = ("SPEECH", "SUBTITLE", "ANALYSIS")
        return self._count_task_step_slots(
            "CourseTask",
            step_codes,
            include_statuses=include_statuses,
            exclude_task_db_id=exclude_task_db_id,
        )

    def _count_task_step_slots(
        self,
        task_kind: str,
        step_codes: tuple[str, ...],
        *,
        include_statuses: tuple[int, ...],
        exclude_task_db_id: int | None = None,
    ) -> int:
        steps = tuple(str(step or "").strip().upper() for step in step_codes if str(step or "").strip())
        statuses = tuple(int(s) for s in include_statuses if int(s) >= 0)
        if not task_kind or not steps or not statuses:
            return 0
        step_placeholders = ",".join("?" for _ in steps)
        status_placeholders = ",".join("?" for _ in statuses)
        sql = (
            "SELECT COUNT(DISTINCT t.id) AS cnt "
            "FROM edge_stream_task t "
            "JOIN edge_stream_task_step s ON s.task_id=t.id "
            "WHERE t.task_kind=? "
            f"AND s.step_code IN ({step_placeholders}) "
            f"AND s.step_status IN ({status_placeholders})"
        )
        params: list[Any] = [str(task_kind), *steps, *statuses]
        if exclude_task_db_id is not None and int(exclude_task_db_id) > 0:
            sql += " AND t.id<>?"
            params.append(int(exclude_task_db_id))
        with self._db.connect() as conn:
            r = conn.execute(sql, tuple(params)).fetchone()
            return int((r["cnt"] if r is not None else 0) or 0)

    def _mark_step_paused(self, task_db_id: int, step_code: str) -> None:
        step = str(step_code or "").strip().upper()
        with self._db.connect() as conn:
            if step in {"", "DOWNLOAD", "TRANSCODE"}:
                step = self._resolve_camera_active_step_conn(conn, int(task_db_id), step)
            if step == "TRANSCODE":
                dl_row = conn.execute(
                    "SELECT step_status, step_process, end_time, output_file_path FROM edge_stream_task_step WHERE task_id=? AND step_code='DOWNLOAD' LIMIT 1",
                    (int(task_db_id),),
                ).fetchone()
                dl_done = dl_row is not None and (
                    int(dl_row["step_status"] or 0) == StepStatus.SUCCESS
                    or (int(dl_row["step_process"] or 0) >= 100 and (str(dl_row["end_time"] or "") or str(dl_row["output_file_path"] or "")))
                )
                if dl_done:
                    conn.execute(
                        "UPDATE edge_stream_task SET task_status=4, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (int(task_db_id),),
                    )
                    conn.execute(
                        "UPDATE edge_stream_task_step SET step_status=4, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code='TRANSCODE' AND step_status=1",
                        (int(task_db_id),),
                    )
                    conn.execute(
                        "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                        (int(task_db_id), "TRANSCODE", "INFO", "已暂停"),
                    )
                    conn.commit()
                    return
            conn.execute(
                "UPDATE edge_stream_task SET task_status=4, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(task_db_id),),
            )
            conn.execute(
                "UPDATE edge_stream_task_step SET step_status=4, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=? AND step_status=1",
                (int(task_db_id), step),
            )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), step, "INFO", "已暂停"),
            )
            conn.commit()
        if step in {"SPEECH", "SUBTITLE", "ANALYSIS"}:
            self._sync_same_lesson_course_step_state(int(task_db_id), step, task_status=TaskStatus.PAUSED, step_status=StepStatus.PAUSED, log_message="已暂停")
            self._queue_step_state_report(int(task_db_id), step, include_same_lesson=True)
            return
        self._queue_step_state_report(int(task_db_id), step)

    def _mark_step_resumed(self, task_db_id: int, step_code: str) -> None:
        step = str(step_code or "").strip().upper()
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE edge_stream_task SET task_status=1, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(task_db_id),),
            )
            conn.execute(
                "UPDATE edge_stream_task_step SET step_status=1, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=? AND step_status=4",
                (int(task_db_id), step),
            )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), step, "INFO", "继续执行"),
            )
            conn.commit()
        if step in {"SPEECH", "SUBTITLE", "ANALYSIS"}:
            self._sync_same_lesson_course_step_state(int(task_db_id), step, task_status=TaskStatus.RUNNING, step_status=StepStatus.RUNNING, log_message="继续执行")

    def _mark_step_stop_requested(self, task_db_id: int, step_code: str) -> None:
        step = str(step_code or "").strip().upper()
        with self._db.connect() as conn:
            if step == "DOWNLOAD":
                conn.execute(
                    "UPDATE edge_stream_task SET task_status=0, process_rate=0, current_step='', execute_node_id='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (int(task_db_id),),
                )
                conn.execute(
                    "UPDATE edge_stream_task_step SET step_status=0, step_process=0, start_time=NULL, end_time=NULL, finalize_pending=0, finalize_src_path=NULL, finalize_dst_path=NULL, finalize_action=NULL, finalize_error='', finalize_retry_count=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code IN ('DOWNLOAD','TRANSCODE')",
                    (int(task_db_id),),
                )
            elif step == "TRANSCODE":
                conn.execute(
                    "UPDATE edge_stream_task SET task_status=0, process_rate=0, current_step='DOWNLOAD', execute_node_id='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (int(task_db_id),),
                )
                conn.execute(
                    "UPDATE edge_stream_task_step SET step_status=0, step_process=0, start_time=NULL, end_time=NULL, finalize_pending=0, finalize_src_path=NULL, finalize_dst_path=NULL, finalize_action=NULL, finalize_error='', finalize_retry_count=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code='TRANSCODE'",
                    (int(task_db_id),),
                )
            else:
                conn.execute(
                    "UPDATE edge_stream_task SET task_status=0, process_rate=0, current_step='', execute_node_id='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (int(task_db_id),),
                )
                conn.execute(
                    "UPDATE edge_stream_task_step SET step_status=0, step_process=0, start_time=NULL, end_time=NULL, finalize_pending=0, finalize_src_path=NULL, finalize_dst_path=NULL, finalize_action=NULL, finalize_error='', finalize_retry_count=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=?",
                    (int(task_db_id), step),
                )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), step or "DOWNLOAD", "INFO", "已提交终止，等待后台清理"),
            )
            conn.commit()
        if step == "DOWNLOAD":
            self._queue_step_state_report(int(task_db_id), "DOWNLOAD")
            self._queue_step_state_report(int(task_db_id), "TRANSCODE")
        elif step:
            self._queue_step_state_report(int(task_db_id), step)

    def _sync_same_lesson_course_step_state(
        self,
        task_db_id: int,
        step_code: str,
        *,
        task_status: int,
        step_status: int,
        log_message: str,
    ) -> None:
        step = str(step_code or "").strip().upper()
        if step not in {"SPEECH", "SUBTITLE", "ANALYSIS"}:
            return
        with self._db.connect() as conn:
            src = conn.execute(
                "SELECT lesson_id, process_rate, current_step FROM edge_stream_task WHERE id=? AND task_kind='CourseTask' LIMIT 1",
                (int(task_db_id),),
            ).fetchone()
            if src is None:
                return
            lesson_id = int(src["lesson_id"] or 0)
            if lesson_id <= 0:
                return
            lesson_progress_row = conn.execute(
                "SELECT MAX(process_rate) AS max_process FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask'",
                (lesson_id,),
            ).fetchone()
            lesson_step_progress_row = conn.execute(
                "SELECT MAX(s.step_process) AS max_step_process FROM edge_stream_task_step s JOIN edge_stream_task t ON t.id=s.task_id WHERE t.lesson_id=? AND t.task_kind='CourseTask' AND s.step_code=?",
                (lesson_id, step),
            ).fetchone()
            progress = max(
                int(src["process_rate"] or 0),
                int(lesson_progress_row["max_process"] or 0) if lesson_progress_row is not None else 0,
                int(lesson_step_progress_row["max_step_process"] or 0) if lesson_step_progress_row is not None else 0,
            )
            others = conn.execute(
                "SELECT id FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND id!=?",
                (lesson_id, int(task_db_id)),
            ).fetchall()
            for row in (others or []):
                oid = int(row["id"])
                conn.execute(
                    "UPDATE edge_stream_task SET task_status=?, current_step=?, process_rate=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (int(task_status), step, progress, oid),
                )
                conn.execute(
                    "UPDATE edge_stream_task_step SET step_status=?, step_process=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=?",
                    (int(step_status), progress, oid, step),
                )
                conn.execute(
                    "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                    (oid, step, "INFO", str(log_message or "")),
                )
            if others:
                conn.commit()

    def _mark_transcode_failed(self, task_db_id: int, task) -> None:
        raw = task.raw if hasattr(task, "raw") else {}
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE edge_stream_task SET task_status=3, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(task_db_id),),
            )
            conn.execute(
                "UPDATE edge_stream_task_step SET step_status=3, end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code='TRANSCODE' AND step_status=1",
                (int(task_db_id),),
            )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), "TRANSCODE", "ERROR", "转码失败"),
            )
            conn.commit()
        if self._save_download_artifact_safe(task_db_id, task):
            pass

    def _save_download_artifact_safe(self, task_db_id: int, task) -> bool:
        try:
            self._save_download_artifact(task_db_id, task)
            return True
        except Exception:
            self._log.warning("save_download_artifact failed task=%s", self._server_task_id_for_db_log(task_db_id), exc_info=True)
            return False

    def _save_download_artifact(self, task_db_id: int, task) -> None:
        raw = task.raw if hasattr(task, "raw") else {}
        server_task_id_int = None
        try:
            server_task_id_int = int(str(raw.get("taskId") or raw.get("id") or "").strip())
        except Exception:
            server_task_id_int = None
        lesson_id = int(raw.get("lessonId") or 0)
        task_type = int(raw.get("taskType") or 0)
        prefix = _task_type_prefix_util(task_type)
        lesson_date = _resolve_lesson_date_util(raw.get("lessonDate"), raw.get("lessonStartAt"))
        if server_task_id_int is None or lesson_id <= 0:
            return
        out_dir = _get_lesson_dir_util(lesson_date, str(lesson_id))
        mp4 = out_dir / f"{prefix}_{server_task_id_int}.mp4"
        if not mp4.exists():
            return
        fp = str(mp4)
        sz = int(mp4.stat().st_size)
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE edge_stream_task_step SET output_file_path=? WHERE task_id=? AND step_code='DOWNLOAD' AND output_file_path IS NULL",
                (fp, int(task_db_id)),
            )
            ex = conn.execute(
                "SELECT id FROM edge_lesson_output WHERE lesson_id=? AND server_task_id=? AND file_type='source_video'",
                (lesson_id, server_task_id_int),
            ).fetchone()
            if not ex:
                conn.execute(
                    "INSERT INTO edge_lesson_output(lesson_id, server_task_id, file_type, file_path, file_size) VALUES (?,?,?,?,?)",
                    (lesson_id, server_task_id_int, "source_video", fp, sz),
                )
            conn.commit()

    def _save_artifacts(self, task_db_id: int, task, artifacts: list[dict[str, Any]] | None) -> None:
        if not artifacts:
            return
        raw = task.raw if hasattr(task, "raw") else {}
        lesson_id = int(raw.get("lessonId") or 0)
        server_task_id_int = None
        try:
            server_task_id_int = int(str(raw.get("taskId") or raw.get("id") or "").strip())
        except Exception:
            pass
        if lesson_id <= 0 or server_task_id_int is None:
            return
        with self._db.connect() as conn:
            dirty_steps: set[str] = set()
            for art in artifacts:
                file_type = str(art.get("file_type") or art.get("fileType") or "").strip()
                file_path = str(art.get("file_path") or art.get("path") or "").strip()
                file_size = int(art.get("file_size") or art.get("sizeBytes") or 0)
                step_code = str(art.get("step_code") or art.get("stepCode") or "").strip().upper()
                if not file_type or not file_path:
                    continue
                ex = conn.execute(
                    "SELECT id FROM edge_lesson_output WHERE lesson_id=? AND server_task_id=? AND file_type=?",
                    (lesson_id, server_task_id_int, file_type),
                ).fetchone()
                if ex:
                    conn.execute(
                        "UPDATE edge_lesson_output SET file_path=?, file_size=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (file_path, file_size, int(ex["id"])),
                    )
                else:
                    conn.execute(
                        "INSERT INTO edge_lesson_output(lesson_id, server_task_id, file_type, file_path, file_size) VALUES (?,?,?,?,?)",
                        (lesson_id, server_task_id_int, file_type, file_path, file_size),
                    )
                if step_code:
                    conn.execute(
                        "UPDATE edge_stream_task_step SET output_file_path=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=?",
                        (file_path, int(task_db_id), step_code),
                    )
                    dirty_steps.add(step_code)
            for step_code in dirty_steps:
                conn.execute(
                    """
INSERT INTO edge_step_report_state(task_id, step_code, report_dirty, last_error, updated_time)
VALUES (?,?,1,'',strftime('%Y-%m-%dT%H:%M:%SZ','now'))
ON CONFLICT(task_id, step_code) DO UPDATE SET
  report_dirty=1,
  last_error='',
  updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    """.strip(),
                    (int(task_db_id), str(step_code)),
                )
            conn.commit()

    def _replicate_same_lesson_artifacts(self, task_db_id: int, artifacts: list[dict[str, Any]] | None) -> None:
        if not artifacts:
            return
        with self._db.connect() as conn:
            t = conn.execute(
                "SELECT lesson_id, server_task_id FROM edge_stream_task WHERE id=? AND task_kind='CourseTask'",
                (int(task_db_id),),
            ).fetchone()
            if t is None:
                return
            lesson_id = int(t["lesson_id"])
            own_server_id = int(t["server_task_id"])
            others = conn.execute(
                "SELECT id, server_task_id FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND id!=?",
                (lesson_id, int(task_db_id)),
            ).fetchall()
            dirty_pairs: set[tuple[int, str]] = set()
            for r in (others or []):
                other_db_id = int(r["id"])
                other_server_id = int(r["server_task_id"])
                for art in artifacts:
                    file_type = str(art.get("file_type") or art.get("fileType") or "").strip()
                    file_path = str(art.get("file_path") or art.get("path") or "").strip()
                    file_size = int(art.get("file_size") or art.get("sizeBytes") or 0)
                    step_code = str(art.get("step_code") or art.get("stepCode") or "").strip().upper()
                    if not file_type or not file_path:
                        continue
                    ex = conn.execute(
                        "SELECT id FROM edge_lesson_output WHERE lesson_id=? AND server_task_id=? AND file_type=?",
                        (lesson_id, other_server_id, file_type),
                    ).fetchone()
                    if ex:
                        conn.execute(
                            "UPDATE edge_lesson_output SET file_path=?, file_size=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                            (file_path, file_size, int(ex["id"])),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO edge_lesson_output(lesson_id, server_task_id, file_type, file_path, file_size) VALUES (?,?,?,?,?)",
                            (lesson_id, other_server_id, file_type, file_path, file_size),
                        )
                    if step_code:
                        conn.execute(
                            "UPDATE edge_stream_task_step SET output_file_path=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=?",
                            (file_path, other_db_id, step_code),
                        )
                        dirty_pairs.add((other_db_id, step_code))
            for other_db_id, step_code in dirty_pairs:
                conn.execute(
                    """
INSERT INTO edge_step_report_state(task_id, step_code, report_dirty, last_error, updated_time)
VALUES (?,?,1,'',strftime('%Y-%m-%dT%H:%M:%SZ','now'))
ON CONFLICT(task_id, step_code) DO UPDATE SET
  report_dirty=1,
  last_error='',
  updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    """.strip(),
                    (int(other_db_id), str(step_code)),
                )
            if others:
                conn.commit()

    def _load_task_raw_by_dbid(self, task_db_id: int) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            t = conn.execute("SELECT * FROM edge_stream_task WHERE id=? LIMIT 1", (int(task_db_id),)).fetchone()
            if t is None:
                return None
            return {
                "id": int(t["server_task_id"]),
                "taskId": int(t["server_task_id"]),
                "taskKind": str(t["task_kind"] or ""),
                "lessonId": int(t["lesson_id"]),
                "taskType": int(t["task_type"]),
                "lessonDate": str(t["lesson_date"] or ""),
                "lessonStartAt": str(t["download_start"] or ""),
                "lessonEndAt": str(t["download_end"] or ""),
                "relate_class": str(t["relate_class"] or ""),
                "relate_lesson": str(t["relate_lesson"] or ""),
                "grade": str(t["grade"] if "grade" in t.keys() else "") or "",
                "subject": str(t["subject"] if "subject" in t.keys() else "") or "",
                "nvr": {
                    "nvrDeviceId": t["nvr_device_id"],
                    "nvrChannelNum": t["nvr_channel_num"],
                    "nvrChannelId": str(t["nvr_channel_id"] or ""),
                    "ipAddress": str(t["nvr_ip"] or ""),
                    "port": int(t["nvr_port"] or 8000),
                    "account": str(t["nvr_account"] or ""),
                    "password": str(t["nvr_password"] or ""),
                },
                "nvrDeviceId": t["nvr_device_id"],
                "nvrChannelNum": t["nvr_channel_num"],
                "nvrChannelId": str(t["nvr_channel_id"] or ""),
                "ipAddress": str(t["nvr_ip"] or ""),
                "port": int(t["nvr_port"] or 8000),
                "account": str(t["nvr_account"] or ""),
                "password": str(t["nvr_password"] or ""),
            }

    def _mark_task_paused(self, task_db_id: int) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE edge_stream_task SET updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(task_db_id),),
            )
            conn.execute(
                "UPDATE edge_stream_task_step SET step_status=4, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code='DOWNLOAD'",
                (int(task_db_id),),
            )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), "DOWNLOAD", "INFO", "已暂停"),
            )
            conn.commit()

    def _mark_task_resumed(self, task_db_id: int) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE edge_stream_task SET updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(task_db_id),),
            )
            conn.execute(
                "UPDATE edge_stream_task_step SET step_status=1, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code='DOWNLOAD'",
                (int(task_db_id),),
            )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (int(task_db_id), "DOWNLOAD", "INFO", "继续下载"),
            )
            conn.commit()

    def _stop_and_reset_task(self, task_db_id: int) -> None:
        """终止下载后清空临时文件/进度，将任务恢复到等待中。"""
        raw = self._load_task_raw_by_dbid(task_db_id) or {}
        try:
            task_type = int(raw.get("taskType") or 0)
        except Exception:
            task_type = 0
        prefix = _task_type_prefix_util(task_type)

        lesson_id = str(raw.get("lessonId") or "").strip() or "0"
        lesson_date = _resolve_lesson_date_util(raw.get("lessonDate"), raw.get("lessonStartAt"), default="unknown")

        server_task_id = str(raw.get("taskId") or raw.get("id") or "").strip() or "0"
        out_dir = _get_lesson_dir_util(lesson_date, lesson_id)
        cleanup_errors: list[str] = []

        def _is_retryable_file_busy_error(exc: BaseException) -> bool:
            if isinstance(exc, PermissionError):
                return True
            if isinstance(exc, OSError):
                winerror = int(getattr(exc, "winerror", 0) or 0)
                return winerror in {5, 32}
            return False

        def _sleep_for_retry(idx: int, *, base_delay_sec: float, max_delay_sec: float) -> None:
            delay = min(float(max_delay_sec), float(base_delay_sec) * (1.0 + float(idx) * 0.75))
            time.sleep(max(0.1, delay))

        def _describe_open_file_handles(path: Path, *, limit: int = 5) -> str:
            try:
                import psutil  # type: ignore
            except Exception:
                return ""
            target = str(path.resolve()).lower()
            holders: list[str] = []
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    open_files = proc.open_files() or []
                except Exception:
                    continue
                matched = False
                for item in open_files:
                    try:
                        current = str(getattr(item, "path", "") or "")
                    except Exception:
                        current = ""
                    if current and current.lower() == target:
                        matched = True
                        break
                if not matched:
                    continue
                try:
                    pid = int(proc.info.get("pid") or 0)
                except Exception:
                    pid = 0
                name = str(proc.info.get("name") or "?")
                holders.append(f"pid={pid},name={name}")
                if len(holders) >= int(limit):
                    break
            return "; ".join(holders)

        def _unlink_with_retry(path: Path, *, attempts: int = 12, delay_sec: float = 0.5, max_delay_sec: float = 2.0) -> None:
            last_error: BaseException | None = None
            for idx in range(max(1, int(attempts))):
                try:
                    path.unlink(missing_ok=True)
                    if idx > 0:
                        self._log.info("rerun cleanup unlink retry success path=%s retry=%s", path, idx + 1)
                    return
                except Exception as e:
                    last_error = e
                    if not _is_retryable_file_busy_error(e) or idx >= int(attempts) - 1:
                        raise
                    self._log.warning("rerun cleanup unlink busy retry=%s/%s path=%s err=%s", idx + 1, int(attempts), path, e)
                    _sleep_for_retry(idx, base_delay_sec=delay_sec, max_delay_sec=max_delay_sec)
            if last_error is not None:
                raise last_error

        def _rmtree_with_retry(path: Path, *, attempts: int = 10, delay_sec: float = 0.5, max_delay_sec: float = 2.0) -> None:
            last_error: BaseException | None = None
            for idx in range(max(1, int(attempts))):
                try:
                    shutil.rmtree(path)
                    if idx > 0:
                        self._log.info("rerun cleanup rmtree retry success path=%s retry=%s", path, idx + 1)
                    return
                except Exception as e:
                    last_error = e
                    if not _is_retryable_file_busy_error(e) or idx >= int(attempts) - 1:
                        raise
                    self._log.warning("rerun cleanup rmtree busy retry=%s/%s path=%s err=%s", idx + 1, int(attempts), path, e)
                    _sleep_for_retry(idx, base_delay_sec=delay_sec, max_delay_sec=max_delay_sec)
            if last_error is not None:
                raise last_error

        def _remove_file(path: Path, label: str) -> None:
            try:
                _unlink_with_retry(path)
            except Exception as e:
                holders = _describe_open_file_handles(path)
                diag = f"；疑似占用进程: {holders}" if holders else ""
                msg = f"{label} 删除失败: {path} ({e}){diag}"
                cleanup_errors.append(msg)
                self._log.warning(msg)

        def _remove_tree(path: Path, label: str) -> None:
            if not path.exists() or not path.is_dir():
                return
            try:
                _rmtree_with_retry(path)
                return
            except Exception as e:
                self._log.warning("%s 首次删除失败，回退逐项删除: %s (%s)", label, path, e)
            for p in path.rglob("*"):
                try:
                    if p.is_file():
                        _unlink_with_retry(p)
                except Exception as e:
                    msg = f"{label} 子文件删除失败: {p} ({e})"
                    cleanup_errors.append(msg)
                    self._log.warning(msg)
            for p in sorted(path.rglob("*"), reverse=True):
                try:
                    if p.is_dir():
                        p.rmdir()
                except Exception as e:
                    msg = f"{label} 子目录删除失败: {p} ({e})"
                    cleanup_errors.append(msg)
                    self._log.warning(msg)
            try:
                path.rmdir()
            except Exception as e:
                holders = _describe_open_file_handles(path)
                diag = f"；疑似占用进程: {holders}" if holders else ""
                msg = f"{label} 删除失败: {path} ({e}){diag}"
                cleanup_errors.append(msg)
                self._log.warning(msg)

        if out_dir.exists():
            for pattern in [
                f"{prefix}_{server_task_id}.part*.mp4",
                f"{prefix}_{server_task_id}.part*.done",
                f"{prefix}_{server_task_id}.merged.mp4",
                f"{prefix}_{server_task_id}.merged*.mp4",
                f"{prefix}_{server_task_id}.merged*.txt",
                f"{prefix}_{server_task_id}.norm*.mp4",
                f"{prefix}_{server_task_id}.ts*.ts",
                f"{prefix}_{server_task_id}.canonical*.mp4",
                f"{prefix}_{server_task_id}_concat*.txt",
                f"{prefix}_{server_task_id}.download_state.json",
                f"{prefix}_{server_task_id}.process_state.json",
                f"{prefix}_{server_task_id}.mp4.*.attempt",
                f"{prefix}_{server_task_id}.mp4.rtsp_temp.mp4",
                f"{prefix}_{server_task_id}.aligned.mp4",
                f"{prefix}_{server_task_id}.faststart.mp4",
            ]:
                for f in out_dir.glob(pattern):
                    _remove_file(f, "重新下载清理临时文件")
            for d in out_dir.glob(f"{prefix}_{server_task_id}.tsbridge*"):
                _remove_tree(d, "重新下载清理临时目录")
            final = out_dir / f"{prefix}_{server_task_id}.mp4"
            _remove_file(final, "重新下载清理旧源视频")
            hls_dir = out_dir / f"{prefix}_{server_task_id}_1080P"
            _remove_tree(hls_dir, "重新下载清理旧转码目录")

        if cleanup_errors:
            raise RerunCleanupError("；".join(cleanup_errors[:3]))

        with self._db.connect() as conn:
            conn.execute(
                "UPDATE edge_stream_task SET task_status=0, process_rate=0, current_step='', execute_node_id='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(task_db_id),),
            )
            conn.execute(
                "UPDATE edge_stream_task_step SET step_status=0, step_process=0, start_time=NULL, end_time=NULL, output_file_path=NULL, finalize_pending=0, finalize_src_path=NULL, finalize_dst_path=NULL, finalize_action=NULL, finalize_error='', finalize_retry_count=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=?",
                (int(task_db_id),),
            )
            conn.execute(
                "DELETE FROM edge_task_log WHERE task_id=?",
                (int(task_db_id),),
            )
            conn.execute(
                "DELETE FROM edge_lesson_output WHERE lesson_id=? AND server_task_id=?",
                (int(raw.get("lessonId") or 0), int(raw.get("taskId") or raw.get("id") or 0)),
            )
            conn.commit()
        self._append_task_log(int(task_db_id), "DOWNLOAD", "已终止，任务已重置等待重新执行")
        self._queue_step_state_report(int(task_db_id), "DOWNLOAD")
        self._queue_step_state_report(int(task_db_id), "TRANSCODE")

    def _stop_and_reset_transcode(self, task_db_id: int) -> None:
        raw = self._load_task_raw_by_dbid(task_db_id) or {}
        try:
            task_type = int(raw.get("taskType") or 0)
        except Exception:
            task_type = 0

        lesson_id = str(raw.get("lessonId") or "").strip() or "0"
        lesson_date = _resolve_lesson_date_util(raw.get("lessonDate"), raw.get("lessonStartAt"), default="unknown")

        server_task_id = str(raw.get("taskId") or raw.get("id") or "").strip() or "0"
        try:
            task_type = int(raw.get("taskType") or 0)
        except Exception:
            task_type = 0
        prefix = _task_type_prefix_util(task_type)
        out_dir = _get_lesson_dir_util(lesson_date, lesson_id)
        hls_dir = out_dir / f"{prefix}_{server_task_id}_1080P"

        if hls_dir.exists() and hls_dir.is_dir():
            import shutil
            try:
                shutil.rmtree(hls_dir)
                self._log.info("cleaned transcode dir: %s", hls_dir)
            except Exception:
                for p in hls_dir.glob("*"):
                    try:
                        if p.is_file():
                            p.unlink(missing_ok=True)
                    except Exception:
                        pass
                try:
                    hls_dir.rmdir()
                except Exception:
                    pass

        with self._db.connect() as conn:
            conn.execute(
                "UPDATE edge_stream_task SET task_status=0, process_rate=0, current_step='DOWNLOAD', execute_node_id='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (int(task_db_id),),
            )
            conn.execute(
                "UPDATE edge_stream_task_step SET step_status=0, step_process=0, start_time=NULL, end_time=NULL, output_file_path=NULL, finalize_pending=0, finalize_src_path=NULL, finalize_dst_path=NULL, finalize_action=NULL, finalize_error='', finalize_retry_count=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code='TRANSCODE'",
                (int(task_db_id),),
            )
            conn.execute(
                "DELETE FROM edge_task_log WHERE task_id=? AND step_code='TRANSCODE'",
                (int(task_db_id),),
            )
            conn.execute(
                "DELETE FROM edge_lesson_output WHERE lesson_id=? AND server_task_id=? AND file_type='transcoded_video'",
                (int(raw.get("lessonId") or 0), int(raw.get("taskId") or raw.get("id") or 0)),
            )
            conn.commit()
        self._append_task_log(int(task_db_id), "TRANSCODE", "已终止，转码已重置等待重新执行")
        self._queue_step_state_report(int(task_db_id), "TRANSCODE")

    def _load_step_process(self, task_db_id: int, step_code: str) -> int:
        step = str(step_code or "").strip().upper()
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT step_process FROM edge_stream_task_step WHERE task_id=? AND step_code=? LIMIT 1",
                (int(task_db_id), step),
            ).fetchone()
            if r is None:
                return 0
            try:
                return int(r["step_process"] or 0)
            except Exception:
                return 0

    def _recover_stale_tasks(self) -> None:
        """Convert stale running tasks to paused on startup while preserving progress."""
        try:
            with self._db.connect() as conn:
                rows = conn.execute(
                    "SELECT id, server_task_id, task_kind, current_step, execute_node_id, updated_time FROM edge_stream_task WHERE task_status=1"
                ).fetchall()
                for r in rows:
                    tid = int(r["id"])
                    kind = str(r["task_kind"] or "")
                    current_step = self._resolve_camera_active_step_conn(conn, tid, "") if kind == "CameraTask" else ""
                    active_step_row = conn.execute(
                        "SELECT start_time, updated_time FROM edge_stream_task_step WHERE task_id=? AND step_code=? LIMIT 1",
                        (tid, current_step or str(r["current_step"] or "").strip().upper()),
                    ).fetchone() if kind == "CameraTask" else None
                    task_updated_time = str(r["updated_time"] or "").strip()
                    step_updated_time = str(active_step_row["updated_time"] or "").strip() if active_step_row is not None else ""
                    step_start_time = str(active_step_row["start_time"] or "").strip() if active_step_row is not None else ""
                    newest_touch = step_updated_time or task_updated_time
                    if kind == "CameraTask":
                        self._log.info(
                            "recover interrupted camera task task=%s -> pause step=%s updated=%s",
                            r["server_task_id"], current_step, newest_touch or step_start_time,
                        )
                        self._mark_step_paused(tid, current_step)
                        continue
                    if self._is_recent_running_task(newest_touch, step_start_time):
                        self._log.info(
                            "skip stale recover for active task task=%s kind=%s node=%s updated=%s threshold=%ss",
                            r["server_task_id"],
                            kind,
                            str(r["execute_node_id"] or ""),
                            newest_touch or step_start_time,
                            self._stale_recover_threshold_seconds(),
                        )
                        continue
                    self._log.info(
                        "recover stale task task=%s kind=%s -> paused",
                        r["server_task_id"], kind,
                    )
                    conn.execute(
                        "UPDATE edge_stream_task SET task_status=4, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (tid,),
                    )
                    conn.execute(
                        "UPDATE edge_stream_task_step SET step_status=4, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_status=1",
                        (tid,),
                    )
                if rows:
                    conn.commit()
                    self._log.info("paused %d stale task(s) on startup", len(rows))
        except Exception:
            self._log.exception("recover_stale_tasks failed")

    def _cleanup_download_parts(self, task_db_id: int) -> None:
        """删除 DOWNLOAD 步骤遗留的临时分段文件。"""
        cleaned = self._cleanup_download_derived_files(task_db_id)
        if cleaned:
            self._log.info("cleanup_download_parts task=%s removed %d derived temp file(s) while preserving raw parts", self._server_task_id_for_db_log(task_db_id), cleaned)

    def _cleanup_transcode_dir(self, server_task_id: int) -> None:
        raw = self._load_task_raw_by_dbid_server(server_task_id)
        if not raw:
            return
        task_type = int(raw.get("taskType") or 0)
        prefix = _task_type_prefix_util(task_type)
        lesson_date = _resolve_lesson_date_util(raw.get("lessonDate"), raw.get("lessonStartAt"), raw.get("downloadStart"))
        lesson_id = str(raw.get("lessonId") or "").strip()
        od = _get_lesson_dir_util(lesson_date, str(lesson_id))
        stid = str(server_task_id)
        hls_dir = od / f"{prefix}_{stid}_1080P"

        if hls_dir.exists() and hls_dir.is_dir():
            import shutil
            try:
                shutil.rmtree(hls_dir)
                self._log.info("cleaned transcode dir: %s", hls_dir)
            except Exception:
                for p in hls_dir.glob("*"):
                    try:
                        if p.is_file():
                            p.unlink(missing_ok=True)
                    except Exception:
                        pass
                try:
                    hls_dir.rmdir()
                except Exception:
                    pass

    def _mark_task_done(self, task_db_id: int, ok: bool) -> None:
        with self._db.connect() as conn:
            status = 2 if ok else 3
            if ok:
                conn.execute(
                    "UPDATE edge_stream_task SET task_status=?, process_rate=100, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (status, int(task_db_id)),
                )
                conn.execute(
                    "UPDATE edge_stream_task_step SET finalize_pending=0, finalize_src_path=NULL, finalize_dst_path=NULL, finalize_action=NULL, finalize_error='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=?",
                    (int(task_db_id),),
                )
            else:
                conn.execute(
                    "UPDATE edge_stream_task SET task_status=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (status, int(task_db_id)),
                )
                conn.execute(
                    "UPDATE edge_stream_task_step SET step_status=3, end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_status=1",
                    (int(task_db_id),),
                )
                conn.execute(
                    "UPDATE edge_stream_task_step SET finalize_pending=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=?",
                    (int(task_db_id),),
                )
            conn.commit()
