from __future__ import annotations

import asyncio
import contextlib
import json as _json
import time
from pathlib import Path
from typing import Any

from ..api_client import EdgeTask
from ..constants import StepStatus, TaskStatus
from ..monitor_config import load_monitor_cfg as _load_monitor_cfg_fn
from ..tasks.camera import _is_retryable_file_busy_error, _replace_file_with_retry
from ..utils import as_int as _as_int, is_task_step_enabled as _is_task_step_enabled

_POLL_SUCCESS_LOG_INTERVAL_SEC = 300


class SchedulerMixin:
    """轮询、调领、并发调度、upsert 相关方法。"""

    def _course_step_order(self) -> list[str]:
        return [step for step in ("SPEECH", "SUBTITLE", "ANALYSIS") if _is_task_step_enabled(step)]

    def _next_course_step(self, step_code: str) -> str:
        order = self._course_step_order()
        step = str(step_code or "").strip().upper()
        try:
            idx = order.index(step)
        except ValueError:
            return ""
        next_idx = idx + 1
        if next_idx >= len(order):
            return ""
        return str(order[next_idx])

    def _repair_stuck_camera_transcode_tasks(self, limit: int = 10) -> int:
        repaired = 0
        with self._db.connect() as conn:
            rows = conn.execute(
                """
SELECT t.id, t.server_task_id
FROM edge_stream_task t
JOIN edge_stream_task_step s ON s.task_id=t.id AND s.step_code='TRANSCODE'
WHERE t.task_kind='CameraTask'
  AND t.task_status=?
  AND t.current_step='TRANSCODE'
  AND s.step_status=?
  AND COALESCE(s.step_process,0) >= 100
ORDER BY t.updated_time ASC
LIMIT ?
                """.strip(),
                (int(TaskStatus.RUNNING), int(StepStatus.SUCCESS), int(limit)),
            ).fetchall()
        for row in (rows or []):
            task_db_id = int(row["id"] or 0)
            server_task_id = str(row["server_task_id"] or "").strip()
            if task_db_id <= 0 or not server_task_id:
                continue
            if self._task_mem_key(server_task_id, "CameraTask") in self._inflight:
                continue
            raw = self._load_task_raw_by_dbid(task_db_id)
            if raw is None:
                continue
            try:
                task_type = int(raw.get("taskType") or 0)
            except Exception:
                task_type = 0
            prefix = _task_type_prefix_util(task_type)
            lesson_date = _resolve_lesson_date_util(raw.get("lessonDate"), raw.get("lessonStartAt"), raw.get("downloadStart"))
            lesson_id = str(raw.get("lessonId") or "").strip() or "0"
            out_dir = _get_lesson_dir_util(lesson_date, lesson_id)
            playlist = out_dir / f"{prefix}_{server_task_id}_1080P" / "index.m3u8"
            if not playlist.exists() or playlist.stat().st_size <= 0:
                continue
            total_size = 0
            try:
                for fp in playlist.parent.rglob("*"):
                    if fp.is_file():
                        total_size += int(fp.stat().st_size or 0)
            except Exception:
                total_size = int(playlist.stat().st_size or 0)
            holder = type("_RepairTaskHolder", (), {"raw": raw})()
            self._save_artifacts(
                task_db_id,
                holder,
                [{
                    "path": str(playlist),
                    "sizeBytes": int(total_size),
                    "stepCode": "TRANSCODE",
                    "fileType": "transcoded_video",
                }],
            )
            self._mark_task_done(task_db_id, True)
            self._queue_step_state_report(task_db_id, "TRANSCODE", is_final=True)
            repaired += 1
            self._log.info(
                "repaired stuck camera transcode task task=%s playlist=%s",
                server_task_id,
                str(playlist),
            )
        return repaired

    def _retry_finalize_pending_steps(self, limit: int = 10) -> int:
        rows = self._list_finalize_pending_steps(limit)
        recovered = 0
        for row in rows:
            task_db_id = int(row.get("task_db_id") or 0)
            server_task_id = str(row.get("server_task_id") or "").strip()
            task_kind = str(row.get("task_kind") or "CameraTask").strip() or "CameraTask"
            step_code = str(row.get("step_code") or "DOWNLOAD").strip().upper() or "DOWNLOAD"
            mem_key = self._task_mem_key(server_task_id, task_kind)
            if mem_key in self._inflight:
                continue
            src = Path(str(row.get("finalize_src_path") or "").strip())
            dst = Path(str(row.get("finalize_dst_path") or "").strip())
            action = str(row.get("finalize_action") or f"finalize pending {step_code.lower()} task={server_task_id}").strip()
            if not src.exists():
                self._fail_step_finalize_pending(task_db_id, step_code, f"最终提交失败：待提交文件不存在 {src}")
                continue
            try:
                _replace_file_with_retry(src, dst, log=self._log, action=action, attempts=3, delay_sec=0.5)
            except Exception as e:
                self._bump_step_finalize_retry(task_db_id, step_code, str(e))
                if not _is_retryable_file_busy_error(e):
                    self._fail_step_finalize_pending(task_db_id, step_code, f"最终提交失败：{e}")
                continue
            self._complete_step_finalize_pending(task_db_id, step_code, str(dst))
            if step_code == "DOWNLOAD":
                raw = self._load_task_raw_by_dbid(task_db_id)
                if raw is not None:
                    holder = type("_FinalizeTaskHolder", (), {"raw": raw})()
                    self._save_download_artifact_safe(task_db_id, holder)
            self._mark_step_report_dirty(task_db_id, step_code)
            recovered += 1
        return recovered

    def _load_monitor_cfg(self) -> dict:
        return _load_monitor_cfg_fn()

    def _download_limit(self, cfg: dict[str, Any] | None = None) -> int:
        cfg = cfg if isinstance(cfg, dict) else self._load_monitor_cfg()
        conc = cfg.get("concurrency") if isinstance(cfg, dict) else None
        if isinstance(conc, dict):
            try:
                v = int(conc.get("download"))
                if v >= 1:
                    return v
            except Exception:
                pass
        return 2

    def _can_start_course_step(self, step_code: str, cfg: dict[str, Any] | None = None) -> bool:
        step = str(step_code or "").strip().upper()
        if not _is_task_step_enabled(step):
            return False
        cfg = cfg if isinstance(cfg, dict) else self._load_monitor_cfg()
        if step == "SPEECH":
            return self._count_course_task_slots("SPEECH") < self._speech_limit(cfg)
        if step == "SUBTITLE":
            return self._count_course_task_slots("SUBTITLE") < self._subtitle_limit(cfg)
        if step == "ANALYSIS":
            return self._count_course_task_slots("ANALYSIS") < self._analysis_limit(cfg)
        return False

    async def _claim_course_step_once(self, step_code: str, inflight_task_ids: set[str]) -> bool:
        while True:
            row = await asyncio.to_thread(self._claim_next_task, self._cfg.edge_id, "CourseTask", (str(step_code or "").strip().upper(),))
            if row is None:
                return False
            if bool(row.get("skipped")):
                continue
            kind = str(row.get("taskKind") or "CourseTask")
            step = str(row.get("startStep") or step_code or "")
            mem_key = self._task_mem_key(str(row.get("taskId") or ""), kind)
            if mem_key in inflight_task_ids:
                return False
            if not self._spawn_claimed(row, kind, step):
                return False
            inflight_task_ids.add(mem_key)
            return True

    def _camera_total_limit(self, cfg: dict[str, Any] | None = None) -> int:
        cfg = cfg if isinstance(cfg, dict) else self._load_monitor_cfg()
        conc = cfg.get("concurrency") if isinstance(cfg, dict) else None
        if isinstance(conc, dict):
            for key in ("cameraTotal", "camera_total", "cameraShared", "camera_shared"):
                try:
                    v = int(conc.get(key))
                    if v >= 1:
                        return v
                except Exception:
                    pass
        return max(1, self._download_limit(cfg) + self._transcode_limit(cfg))

    def _count_camera_work_slots(self) -> int:
        return self._count_camera_step_slots("DOWNLOAD") + self._count_camera_step_slots("TRANSCODE")

    def _has_waiting_camera_step(self, step_code: str) -> bool:
        target = str(step_code or "").strip().upper()
        if target not in {"DOWNLOAD", "TRANSCODE"}:
            return False
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM edge_stream_task WHERE task_kind='CameraTask' AND task_status=? ORDER BY id ASC",
                (TaskStatus.PENDING,),
            ).fetchall()
            for row in (rows or []):
                first = self._get_first_pending_step(conn, int(row["id"]), "CameraTask")
                if first == target:
                    return True
        return False

    def _can_start_camera_step(self, step_code: str, cfg: dict[str, Any] | None = None) -> bool:
        step = str(step_code or "").strip().upper()
        cfg = cfg if isinstance(cfg, dict) else self._load_monitor_cfg()
        if step == "DOWNLOAD":
            return self._count_camera_step_slots("DOWNLOAD") < self._download_limit(cfg)
        if step == "TRANSCODE":
            return self._count_camera_step_slots("TRANSCODE") < self._transcode_limit(cfg)
        return False

    def _camera_step_start_order(self) -> tuple[str, str]:
        download_running = self._count_camera_step_slots("DOWNLOAD")
        transcode_running = self._count_camera_step_slots("TRANSCODE")
        if transcode_running <= download_running:
            return ("TRANSCODE", "DOWNLOAD")
        return ("DOWNLOAD", "TRANSCODE")

    async def _claim_camera_step_once(self, step_code: str, inflight_task_ids: set[str]) -> bool:
        while True:
            row = await asyncio.to_thread(self._claim_next_task, self._cfg.edge_id, "CameraTask", (str(step_code or "").strip().upper(),))
            if row is None:
                return False
            if bool(row.get("skipped")):
                continue
            kind = str(row.get("taskKind") or "CameraTask")
            step = str(row.get("startStep") or step_code or "")
            mem_key = self._task_mem_key(str(row.get("taskId") or ""), kind)
            if mem_key in inflight_task_ids:
                return False
            if not self._spawn_claimed(row, kind, step):
                return False
            inflight_task_ids.add(mem_key)
            return True

    def _transcode_limit(self, cfg: dict[str, Any] | None = None) -> int:
        cfg = cfg if isinstance(cfg, dict) else self._load_monitor_cfg()
        conc = cfg.get("concurrency") if isinstance(cfg, dict) else None
        if isinstance(conc, dict):
            try:
                v = int(conc.get("transcode"))
                if v >= 1:
                    return v
            except Exception:
                pass
        return 2

    def _speech_limit(self, cfg: dict[str, Any] | None = None) -> int:
        cfg = cfg if isinstance(cfg, dict) else self._load_monitor_cfg()
        conc = cfg.get("concurrency") if isinstance(cfg, dict) else None
        if isinstance(conc, dict):
            try:
                v = int(conc.get("asr"))
                if v >= 0:
                    return v
            except Exception:
                pass
        return 2

    def _subtitle_limit(self, cfg: dict[str, Any] | None = None) -> int:
        cfg = cfg if isinstance(cfg, dict) else self._load_monitor_cfg()
        conc = cfg.get("concurrency") if isinstance(cfg, dict) else None
        if isinstance(conc, dict):
            try:
                v = int(conc.get("subtitle"))
                if v >= 0:
                    return v
            except Exception:
                pass
        return 2

    def _analysis_limit(self, cfg: dict[str, Any] | None = None) -> int:
        cfg = cfg if isinstance(cfg, dict) else self._load_monitor_cfg()
        conc = cfg.get("concurrency") if isinstance(cfg, dict) else None
        if isinstance(conc, dict):
            try:
                v = int(conc.get("analysis"))
                if v >= 0:
                    return v
            except Exception:
                pass
        return 2

    def _is_auto_mode(self, cfg: dict[str, Any] | None = None) -> bool:
        cfg = cfg if isinstance(cfg, dict) else self._load_monitor_cfg()
        mode = str(cfg.get("executionMode") or "manual").strip().lower()
        return mode == "auto"

    def _is_connected(self, cfg: dict[str, Any] | None = None) -> bool:
        """检查是否已成功连接到服务器（有 accessKey 和 accessSecret）"""
        cfg = cfg if isinstance(cfg, dict) else self._load_monitor_cfg()
        server_addr = str(cfg.get("serverAddress") or "").strip()
        access_key = str(cfg.get("accessKey") or "").strip()
        access_secret = str(cfg.get("accessSecret") or "").strip()
        return bool(server_addr and access_key and access_secret)

    def _poll_task_sort_key(self, task: EdgeTask) -> tuple[int, int, int, int, str]:
        raw = task.raw if isinstance(task.raw, dict) else {}
        kind = str(task.task_kind or raw.get("taskKind") or "").strip()
        if kind == "CameraTask":
            kind_rank = 0
        elif kind == "CourseTask":
            kind_rank = 1
        else:
            kind_rank = 2
        task_type = _as_int(raw.get("taskType")) or 0
        lesson_id = _as_int(raw.get("lessonId")) or 0
        task_id = _as_int(raw.get("taskId") or raw.get("id")) or 0
        return (kind_rank, int(task_type), int(lesson_id), int(task_id), kind)

    async def _poll_once_locked(self) -> None:
        await self._state.set_poll_time()
        cfg = self._load_monitor_cfg()
        connection_name = str(cfg.get("connectionName") or "").strip()
        campus = str(cfg.get("campusCode") or "101").strip() or "101"
        start_date = str(cfg.get("startDate") or "").strip()
        server_addr = str(cfg.get("serverAddress") or "").strip()
        access_key = str(cfg.get("accessKey") or "").strip()
        access_secret = str(cfg.get("accessSecret") or "").strip()
        
        self._log.debug("poll检查: serverAddress=%s, accessKey=%s, accessSecret=%s", 
                       server_addr, access_key[:8] + "..." if access_key else "(empty)",
                       access_secret[:8] + "..." if access_secret else "(empty)")
        
        # 检查是否已连接到服务器
        if not connection_name:
            msg = "任务拉取失败：未填写连接名"
            self._log.warning("poll失败: %s", msg)
            await self._state.set_error(msg)
            return
        if not server_addr:
            self._log.debug("poll跳过: 服务器地址未配置")
            return
        if not access_key or not access_secret:
            self._log.debug("poll跳过: 服务器未连接成功（缺少 accessKey/accessSecret）")
            return
        if not start_date:
            msg = "未配置任务拉取日期（startDate），无法拉取任务！"
            self._log.warning(msg)
            await self._state.set_error(msg)
            return
        
        self._log.debug("poll开始: campusCode=%s, startDate=%s, serverAddress=%s", campus, start_date, server_addr)
        try:
            tasks = await self._client.fetch_external_poll_tasks(campus_code=campus, start_date=start_date)
        except Exception as e:
            msg = str(e) or "poll_failed"
            self._log.warning("poll failed: %s", msg)
            await self._state.set_error(msg)
            return
        tasks = sorted(tasks, key=self._poll_task_sort_key)
        remote_keys = {
            (int(server_task_id), task_kind)
            for t in tasks
            for raw in [t.raw if isinstance(t.raw, dict) else {}]
            for server_task_id in [_as_int(raw.get("id") or raw.get("taskId"))]
            for task_kind in [str(t.task_kind or raw.get("taskKind") or "").strip()]
            if server_task_id is not None and task_kind
        }
        cleared_absent = await asyncio.to_thread(self._clear_absent_inactive_tasks, remote_keys)
        if cleared_absent:
            self._log.info("poll清理接口已不存在的本地未完成任务: cleared=%s", cleared_absent)
        now_ts = time.time()
        last_log_ts = float(getattr(self, "_last_poll_success_log_at", 0.0) or 0.0)
        if now_ts - last_log_ts >= _POLL_SUCCESS_LOG_INTERVAL_SEC:
            self._log.info("poll完成: 获取到 %d 个任务", len(tasks))
            setattr(self, "_last_poll_success_log_at", now_ts)
        for t in tasks:
            try:
                await asyncio.to_thread(self._upsert_task_from_poll, t.raw)
            except Exception:
                self._log.exception("upsert task failed")
        await self._state.set_error(None)

    async def _poll_once(self) -> None:
        async with self._poll_lock:
            await self._poll_once_locked()

    async def poll_now(self) -> None:
        await self._poll_once()

    def _list_non_pending_task_keys(self) -> set[tuple[int, str]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                """
SELECT t.server_task_id, t.task_kind
FROM edge_stream_task t
WHERE t.task_status<>?
   OR t.process_rate>0
   OR EXISTS (
        SELECT 1
        FROM edge_stream_task_step s
        WHERE s.task_id=t.id
          AND (
              s.step_status<>?
              OR s.step_process>0
              OR s.start_time IS NOT NULL
              OR s.end_time IS NOT NULL
              OR COALESCE(s.output_file_path,'')<>''
              OR COALESCE(s.finalize_pending,0)<>0
          )
   )
                """.strip(),
                (TaskStatus.PENDING, StepStatus.PENDING),
            ).fetchall()
            return {
                (int(row["server_task_id"] or 0), str(row["task_kind"] or "").strip())
                for row in (rows or [])
                if int(row["server_task_id"] or 0) > 0 and str(row["task_kind"] or "").strip()
            }

    def _clear_pending_tasks_only(self) -> int:
        with self._db.connect() as conn:
            rows = conn.execute(
                """
SELECT t.id
FROM edge_stream_task t
WHERE t.task_status=?
  AND COALESCE(t.process_rate,0)=0
  AND NOT EXISTS (
        SELECT 1
        FROM edge_stream_task_step s
        WHERE s.task_id=t.id
          AND (
              s.step_status<>?
              OR s.step_process>0
              OR s.start_time IS NOT NULL
              OR s.end_time IS NOT NULL
              OR COALESCE(s.output_file_path,'')<>''
              OR COALESCE(s.finalize_pending,0)<>0
          )
  )
                """.strip(),
                (TaskStatus.PENDING, StepStatus.PENDING),
            ).fetchall()
            task_ids = [int(row["id"]) for row in (rows or []) if int(row["id"] or 0) > 0]
            if not task_ids:
                return 0
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(f"DELETE FROM edge_task_log WHERE task_id IN ({placeholders})", tuple(task_ids))
            conn.execute(f"DELETE FROM edge_stream_task_step WHERE task_id IN ({placeholders})", tuple(task_ids))
            conn.execute(f"DELETE FROM edge_stream_task WHERE id IN ({placeholders})", tuple(task_ids))
            conn.commit()
            return len(task_ids)

    def _clear_absent_inactive_tasks(self, remote_keys: set[tuple[int, str]]) -> int:
        inflight_keys = set(self._inflight.keys())
        with self._db.connect() as conn:
            rows = conn.execute(
                """
SELECT id, server_task_id, task_kind
FROM edge_stream_task
WHERE task_status IN (?, ?)
  AND NOT EXISTS (
        SELECT 1
        FROM edge_stream_task_step s
        WHERE s.task_id=edge_stream_task.id
          AND (
              s.step_status<>?
              OR s.step_process>0
              OR s.start_time IS NOT NULL
              OR s.end_time IS NOT NULL
              OR COALESCE(s.output_file_path,'')<>''
              OR COALESCE(s.finalize_pending,0)<>0
          )
  )
                """.strip(),
                (int(TaskStatus.PENDING), int(TaskStatus.FAILED), int(StepStatus.PENDING)),
            ).fetchall()
            task_ids: list[int] = []
            for row in (rows or []):
                server_task_id = int(row["server_task_id"] or 0)
                task_kind = str(row["task_kind"] or "").strip()
                if server_task_id <= 0 or not task_kind:
                    continue
                if (server_task_id, task_kind) in remote_keys:
                    continue
                if self._task_mem_key(str(server_task_id), task_kind) in inflight_keys:
                    continue
                task_ids.append(int(row["id"]))
            if not task_ids:
                return 0
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(f"DELETE FROM edge_task_log WHERE task_id IN ({placeholders})", tuple(task_ids))
            conn.execute(f"DELETE FROM edge_stream_task_step WHERE task_id IN ({placeholders})", tuple(task_ids))
            conn.execute(f"DELETE FROM edge_stream_task WHERE id IN ({placeholders})", tuple(task_ids))
            conn.commit()
            return len(task_ids)

    def _sync_lesson_metadata_from_poll_conn(
        self,
        conn,
        lesson_id: int,
        *,
        relate_class: str = "",
        relate_lesson: str = "",
        grade: str = "",
        subject: str = "",
    ) -> None:
        updates: list[str] = []
        params: list[Any] = []
        if str(relate_class or "").strip():
            updates.append("relate_class=?")
            params.append(str(relate_class or "").strip())
        if str(relate_lesson or "").strip():
            updates.append("relate_lesson=?")
            params.append(str(relate_lesson or "").strip())
        if str(grade or "").strip():
            updates.append("grade=?")
            params.append(str(grade or "").strip())
        if str(subject or "").strip():
            updates.append("subject=?")
            params.append(str(subject or "").strip())
        if not updates:
            return
        updates.append("updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')")
        params.append(int(lesson_id))
        conn.execute(
            f"UPDATE edge_stream_task SET {', '.join(updates)} WHERE lesson_id=?",
            tuple(params),
        )

    async def repoll_pending_tasks_only(self) -> dict[str, int]:
        async with self._start_lock:
            async with self._poll_lock:
                cfg = self._load_monitor_cfg()
                connection_name = str(cfg.get("connectionName") or "").strip()
                campus = str(cfg.get("campusCode") or "101").strip() or "101"
                start_date = str(cfg.get("startDate") or "").strip()
                server_addr = str(cfg.get("serverAddress") or "").strip()
                access_key = str(cfg.get("accessKey") or "").strip()
                access_secret = str(cfg.get("accessSecret") or "").strip()
                if not connection_name:
                    msg = "任务拉取失败：未填写连接名"
                    self._log.warning("pending repoll失败: %s", msg)
                    await self._state.set_error(msg)
                    return {"cleared": 0, "inserted": 0, "skipped": 0}
                if not server_addr:
                    self._log.debug("pending repoll跳过: 服务器地址未配置")
                    return {"cleared": 0, "inserted": 0, "skipped": 0}
                if not access_key or not access_secret:
                    self._log.debug("pending repoll跳过: 服务器未连接成功（缺少 accessKey/accessSecret）")
                    return {"cleared": 0, "inserted": 0, "skipped": 0}
                if not start_date:
                    msg = "未配置任务拉取日期（startDate），无法拉取任务！"
                    self._log.warning(msg)
                    await self._state.set_error(msg)
                    return {"cleared": 0, "inserted": 0, "skipped": 0}
                tasks = await self._client.fetch_external_poll_tasks(campus_code=campus, start_date=start_date)
                tasks = sorted(tasks, key=self._poll_task_sort_key)
                protected_keys = await asyncio.to_thread(self._list_non_pending_task_keys)
                cleared = await asyncio.to_thread(self._clear_pending_tasks_only)
                inserted = 0
                skipped = 0
                for task in tasks:
                    raw = task.raw if isinstance(task.raw, dict) else {}
                    server_task_id = _as_int(raw.get("id") or raw.get("taskId"))
                    task_kind = str(task.task_kind or raw.get("taskKind") or "").strip()
                    if server_task_id is None or not task_kind:
                        continue
                    if (int(server_task_id), task_kind) in protected_keys:
                        skipped += 1
                        continue
                    await asyncio.to_thread(self._upsert_task_from_poll, raw)
                    inserted += 1
                self._log.info(
                    "pending repoll完成: cleared=%s inserted=%s skipped_non_pending=%s startDate=%s",
                    cleared,
                    inserted,
                    skipped,
                    start_date,
                )
                await self._state.set_error(None)
                return {"cleared": cleared, "inserted": inserted, "skipped": skipped}

    async def reset_local_data_and_repoll(self, clear_fn) -> None:
        async with self._start_lock:
            async with self._poll_lock:
                inflight_tasks = [task for _, _, task in list(self._inflight.values())]
                for _, _, task in list(self._inflight.values()):
                    task.cancel()
                for task in inflight_tasks:
                    with contextlib.suppress(BaseException):
                        await task
                self._inflight.clear()
                self._camera_ctx.clear()
                self._done.clear()
                await asyncio.to_thread(clear_fn)
                await self._poll_once_locked()

    def _inflight_count(self, kind: str, step: str | None = None) -> int:
        if step:
            return sum(1 for k, s, _ in self._inflight.values() if k == kind and s == step)
        return sum(1 for k, _, __ in self._inflight.values() if k == kind)

    def _inflight_update_step(self, task_id: str, new_step: str) -> None:
        entry = self._inflight.get(self._task_mem_key(task_id, "CameraTask"))
        if entry is not None:
            self._inflight[self._task_mem_key(task_id, "CameraTask")] = (entry[0], str(new_step).upper(), entry[2])

    def _spawn_claimed(self, row: dict[str, Any], kind: str, step: str = "") -> bool:
        task_id = str(row.get("taskId") or "")
        task_db_id = int(row.get("dbId") or 0)
        raw = row.get("raw")
        if not task_id or not isinstance(raw, dict):
            return False
        mem_key = self._task_mem_key(task_id, kind)
        if mem_key in self._inflight:
            self._log.info("spawn skipped task=%s kind=%s step=%s reason=inflight", task_id, kind, step or "")
            return False
        if mem_key in self._done:
            self._done.discard(mem_key)
            self._log.info("spawn cleared completed marker task=%s kind=%s step=%s", task_id, kind, step or "")
        start_step = str(row.get("startStep") or step or "")
        if start_step:
            raw["__startStep"] = start_step
        t = asyncio.create_task(self._execute_db_task(task_db_id, EdgeTask(task_id=task_id, task_kind=kind, raw=raw)))
        self._inflight[mem_key] = (kind, start_step.upper() or "", t)
        return True

    async def _try_start_followup_course_task(self, server_task_id: str) -> None:
        async with self._start_lock:
            self._log.info("尝试触发后续 CourseTask: task=%s", server_task_id)
            info = await asyncio.to_thread(self._get_waiting_task_info, int(str(server_task_id).strip()), "CourseTask")
            if info is None:
                self._log.warning("未找到等待中的 CourseTask: task=%s", server_task_id)
                return
            kind = str(info.get("taskKind") or "CourseTask")
            step = str(info.get("firstStep") or "").upper()
            if not step:
                self._log.warning("CourseTask 没有待执行的步骤: task=%s", server_task_id)
                return
            self._log.info("找到 CourseTask: task=%s, kind=%s, firstStep=%s", server_task_id, kind, step)
            row = await asyncio.to_thread(self._claim_specific_task, self._cfg.edge_id, int(str(server_task_id).strip()), step, kind)
            if row is None:
                self._log.warning("无法认领 CourseTask: task=%s, step=%s", server_task_id, step)
                return
            if bool(row.get("skipped")):
                self._log.info("CourseTask 已跳过: task=%s", server_task_id)
                return
            if not self._spawn_claimed(row, kind, step):
                self._log.warning("自动触发后续任务未启动: task=%s kind=%s step=%s", server_task_id, kind, step)
                return
            self._log.info("自动触发后续任务成功: task=%s kind=%s step=%s", server_task_id, kind, step)

    async def _run_available(self) -> None:
        await self._flush_pending_reports()
        await asyncio.to_thread(self._retry_finalize_pending_steps)
        await asyncio.to_thread(self._repair_stuck_camera_transcode_tasks)
        inflight_task_ids: set[str] = set(self._inflight.keys())
        cfg = self._load_monitor_cfg()
        if not self._is_auto_mode(cfg):
            while self._can_start_camera_step("TRANSCODE", cfg):
                if not await self._claim_camera_step_once("TRANSCODE", inflight_task_ids):
                    break
            for course_step in self._course_step_order():
                while self._can_start_course_step(course_step, cfg):
                    if not await self._claim_course_step_once(course_step, inflight_task_ids):
                        break
            return
        spawned = True
        while spawned:
            spawned = False
            for camera_step in self._camera_step_start_order():
                if not self._can_start_camera_step(camera_step, cfg):
                    continue
                if await self._claim_camera_step_once(camera_step, inflight_task_ids):
                    spawned = True
            for course_step in self._course_step_order():
                while self._can_start_course_step(course_step, cfg):
                    if not await self._claim_course_step_once(course_step, inflight_task_ids):
                        break
                    spawned = True

    def _get_first_pending_step(self, conn, task_db_id: int, task_kind: str) -> str:
        step_order = {"CameraTask": ["DOWNLOAD", "TRANSCODE"], "CourseTask": self._course_step_order()}.get(task_kind, [])
        rows = conn.execute("SELECT step_code, step_status FROM edge_stream_task_step WHERE task_id=?", (int(task_db_id),)).fetchall()
        status_map = {str(r["step_code"] or "").strip().upper(): int(r["step_status"] or 0) for r in (rows or [])}
        for sc in step_order:
            if status_map.get(sc) in (None, StepStatus.PENDING):
                return sc
        return ""

    def _is_course_task_ready(self, conn, task_db_id: int, lesson_id: int, first_step: str) -> bool:
        course_steps = self._course_step_order()
        if not course_steps:
            return False
        if str(first_step or "").strip().upper() != str(course_steps[0]):
            return True
        rows = conn.execute(
            "SELECT id, task_type FROM edge_stream_task WHERE lesson_id=? AND task_kind='CameraTask' AND task_type IN (1,2)",
            (int(lesson_id),),
        ).fetchall()
        task_types: set[int] = set()
        for row in (rows or []):
            task_types.add(int(row["task_type"] or 0))
            step_rows = conn.execute(
                "SELECT step_code, step_status FROM edge_stream_task_step WHERE task_id=? AND step_code IN ('DOWNLOAD','TRANSCODE')",
                (int(row["id"]),),
            ).fetchall()
            sm = {str(r["step_code"] or "").strip().upper(): int(r["step_status"] or 0) for r in (step_rows or [])}
            if sm.get("DOWNLOAD") != StepStatus.SUCCESS or sm.get("TRANSCODE") != StepStatus.SUCCESS:
                return False
        return task_types.issuperset({1, 2})

    def _is_type3_course_visible(self, conn, lesson_id: int) -> bool:
        cam = conn.execute(
            "SELECT id FROM edge_stream_task WHERE lesson_id=? AND task_kind='CameraTask' AND task_type=3 ORDER BY id DESC LIMIT 1",
            (int(lesson_id),),
        ).fetchone()
        if cam is None:
            return False
        step_rows = conn.execute(
            "SELECT step_code, step_status FROM edge_stream_task_step WHERE task_id=? AND step_code IN ('DOWNLOAD','TRANSCODE')",
            (int(cam["id"]),),
        ).fetchall()
        sm = {str(r["step_code"] or "").strip().upper(): int(r["step_status"] or 0) for r in (step_rows or [])}
        return sm.get("DOWNLOAD") == StepStatus.SUCCESS and sm.get("TRANSCODE") == StepStatus.SUCCESS

    def _is_teacher_camera_download_ready(self, conn, lesson_id: int) -> bool:
        teacher = conn.execute(
            "SELECT id FROM edge_stream_task WHERE lesson_id=? AND task_kind='CameraTask' AND task_type=1 ORDER BY id DESC LIMIT 1",
            (int(lesson_id),),
        ).fetchone()
        if teacher is None:
            return False
        dl_row = conn.execute(
            "SELECT step_status, step_process, end_time, output_file_path FROM edge_stream_task_step WHERE task_id=? AND step_code='DOWNLOAD' LIMIT 1",
            (int(teacher["id"]),),
        ).fetchone()
        if dl_row is None:
            return False
        return int(dl_row["step_status"] or 0) == StepStatus.SUCCESS or (
            int(dl_row["step_process"] or 0) >= 100 and (str(dl_row["end_time"] or "") or str(dl_row["output_file_path"] or ""))
        )

    def _camera_task_wait_reason_conn(self, conn, task_db_id: int, lesson_id: int, task_type: int, first_step: str) -> str | None:
        step = str(first_step or "").strip().upper()
        if step != "TRANSCODE":
            return None
        if int(task_type or 0) not in (2, 3):
            return None
        if self._is_teacher_camera_download_ready(conn, int(lesson_id)):
            return None
        return "等待同课次老师视频下载完成以建立时长基准"

    def _get_camera_step_wait_reason(self, server_task_id: int, step_code: str) -> str | None:
        step = str(step_code or "").strip().upper()
        if step != "TRANSCODE":
            return None
        with self._db.connect() as conn:
            t = conn.execute(
                "SELECT id, lesson_id, task_type FROM edge_stream_task WHERE server_task_id=? AND task_kind='CameraTask' ORDER BY id DESC LIMIT 1",
                (int(server_task_id),),
            ).fetchone()
            if t is None:
                return None
            return self._camera_task_wait_reason_conn(conn, int(t["id"]), int(t["lesson_id"] or 0), int(t["task_type"] or 0), step)

    def _sync_course_task_from_lesson_state(self, conn, task_db_id: int, lesson_id: int, task_type: int) -> None:
        if int(task_type) != 3 or not self._is_type3_course_visible(conn, int(lesson_id)):
            return
        src = conn.execute(
            "SELECT id, task_status, current_step, process_rate FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND task_type IN (1,2) AND id!=? ORDER BY id DESC LIMIT 1",
            (int(lesson_id), int(task_db_id)),
        ).fetchone()
        if src is None:
            return
        conn.execute(
            "UPDATE edge_stream_task SET task_status=?, current_step=?, process_rate=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
            (int(src["task_status"] or 0), str(src["current_step"] or ""), int(src["process_rate"] or 0), int(task_db_id)),
        )
        src_steps = conn.execute(
            "SELECT step_code, step_status, step_process, start_time, end_time FROM edge_stream_task_step WHERE task_id=?",
            (int(src["id"]),),
        ).fetchall()
        for step_row in (src_steps or []):
            conn.execute(
                "UPDATE edge_stream_task_step SET step_status=?, step_process=?, start_time=?, end_time=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=?",
                (int(step_row["step_status"] or 0), int(step_row["step_process"] or 0), step_row["start_time"], step_row["end_time"], int(task_db_id), str(step_row["step_code"] or "")),
            )

    def _sync_visible_type3_course_tasks(self, conn, lesson_id: int) -> None:
        if not self._is_type3_course_visible(conn, int(lesson_id)):
            return
        rows = conn.execute(
            "SELECT id, task_type FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND task_type=3",
            (int(lesson_id),),
        ).fetchall()
        for row in (rows or []):
            self._sync_course_task_from_lesson_state(conn, int(row["id"]), int(lesson_id), int(row["task_type"] or 0))

    def _get_waiting_task_info(self, server_task_id: int, task_kind: str = "") -> dict[str, Any] | None:
        with self._db.connect() as conn:
            if task_kind:
                t = conn.execute(
                    "SELECT id, task_kind, task_status, task_type, lesson_id FROM edge_stream_task WHERE server_task_id=? AND task_kind=? ORDER BY id DESC LIMIT 1",
                    (int(server_task_id), task_kind),
                ).fetchone()
                self._log.debug("_get_waiting_task_info 查询: task=%s, task_kind=%s, result=%s", server_task_id, task_kind, "找到" if t else "未找到")
            else:
                t = conn.execute(
                    "SELECT id, task_kind, task_status, task_type, lesson_id FROM edge_stream_task WHERE server_task_id=? ORDER BY id DESC LIMIT 1",
                    (int(server_task_id),),
                ).fetchone()
            if t is None:
                self._log.warning("_get_waiting_task_info 未找到任务: task=%s, task_kind=%s", server_task_id, task_kind)
                return None
            if int(t["task_status"] or 0) != TaskStatus.PENDING:
                self._log.warning("_get_waiting_task_info 任务状态非等待中: task=%s, task_kind=%s, task_status=%s", server_task_id, task_kind, t["task_status"])
                return None
            kind = str(t["task_kind"] or "")
            if kind == "CourseTask" and int(t["task_type"] or 0) == 3:
                if not self._is_type3_course_visible(conn, int(t["lesson_id"] or 0)):
                    self._log.debug("_get_waiting_task_info taskType=3 课程不可见: task=%s", server_task_id)
                    return None
                return None
            tid = int(t["id"])
            first = self._get_first_pending_step(conn, tid, kind)
            if not first:
                self._log.warning("_get_waiting_task_info 没有待执行步骤: task=%s, task_kind=%s", server_task_id, kind)
                return None
            if kind == "CameraTask":
                wait_reason = self._camera_task_wait_reason_conn(conn, tid, int(t["lesson_id"] or 0), int(t["task_type"] or 0), first)
                if wait_reason:
                    self._log.info("_get_waiting_task_info CameraTask 尚未满足启动条件: task=%s, firstStep=%s, reason=%s", server_task_id, first, wait_reason)
                    return None
            if kind == "CourseTask" and not self._is_course_task_ready(conn, tid, int(t["lesson_id"] or 0), first):
                self._log.info("_get_waiting_task_info CourseTask 尚未满足启动条件: task=%s, firstStep=%s", server_task_id, first)
                return None
            self._log.info("_get_waiting_task_info 找到任务: task=%s, task_kind=%s, firstStep=%s", server_task_id, kind, first)
            return {"dbId": tid, "taskKind": kind, "firstStep": first}

    def _reset_task_for_transcode(self, server_task_id: int) -> bool:
        with self._db.connect() as conn:
            t = conn.execute(
                "SELECT id, task_kind, task_status FROM edge_stream_task WHERE server_task_id=? AND task_kind='CameraTask' ORDER BY id DESC LIMIT 1",
                (int(server_task_id),),
            ).fetchone()
            if t is None or str(t["task_kind"] or "") != "CameraTask":
                return False
            tid = int(t["id"])
            dl_step = conn.execute("SELECT step_status FROM edge_stream_task_step WHERE task_id=? AND step_code='DOWNLOAD'", (tid,)).fetchone()
            tc_step = conn.execute("SELECT step_status FROM edge_stream_task_step WHERE task_id=? AND step_code='TRANSCODE'", (tid,)).fetchone()
            if dl_step is None or tc_step is None:
                return False
            dl_st = int(dl_step["step_status"] or 0)
            tc_st = int(tc_step["step_status"] or 0)
            if dl_st != StepStatus.SUCCESS or tc_st == StepStatus.SUCCESS:
                return False
            if tc_st in (StepStatus.PENDING, StepStatus.FAILED):
                conn.execute(
                    "UPDATE edge_stream_task_step SET step_status=0, step_process=0, start_time=NULL, end_time=NULL, output_file_path=NULL, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code='TRANSCODE'",
                    (tid,),
                )
            conn.execute("UPDATE edge_stream_task SET task_status=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (tid,))
            conn.commit()
        self._cleanup_transcode_dir(server_task_id)
        self._log.info("reset task %s for transcode (task=%s)", tid, server_task_id)
        return True

    def _reset_course_task_for_step(self, server_task_id: int, step_code: str) -> bool:
        step = str(step_code or "").strip().upper()
        step_order = self._course_step_order()
        if step not in step_order:
            return False
        reset_index = step_order.index(step)
        reset_task_ids: list[int] = []
        with self._db.connect() as conn:
            t = conn.execute(
                "SELECT id, lesson_id, task_kind, task_type FROM edge_stream_task WHERE server_task_id=? AND task_kind='CourseTask' ORDER BY id DESC LIMIT 1",
                (int(server_task_id),),
            ).fetchone()
            if t is None:
                return False
            lesson_id = int(t["lesson_id"])
            if lesson_id <= 0 or str(t["task_kind"] or "") != "CourseTask":
                return False
            rows = conn.execute(
                "SELECT id, server_task_id, task_type FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND task_type IN (1,2)",
                (lesson_id,),
            ).fetchall()
            if not rows:
                return False
            for row in (rows or []):
                task_db_id = int(row["id"])
                reset_task_ids.append(task_db_id)
                conn.execute(
                    "UPDATE edge_stream_task SET task_status=0, current_step='', process_rate=0, execute_node_id='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (task_db_id,),
                )
                for idx, step_name in enumerate(step_order):
                    if idx < reset_index:
                        continue
                    conn.execute(
                        "UPDATE edge_stream_task_step SET step_status=0, step_process=0, start_time=NULL, end_time=NULL, output_file_path=NULL, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=?",
                        (task_db_id, step_name),
                    )
                    conn.execute("DELETE FROM edge_task_log WHERE task_id=? AND step_code=?", (task_db_id, step_name))
            conn.execute(
                "INSERT OR IGNORE INTO edge_lesson_lock(lesson_id, speech_done, subtitle_done, analysis_done) VALUES (?,?,?,?)",
                (lesson_id, 0, 0, 0),
            )
            if reset_index == 0:
                conn.execute("UPDATE edge_lesson_lock SET speech_done=0, subtitle_done=0, analysis_done=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE lesson_id=?", (lesson_id,))
            elif reset_index == 1:
                conn.execute("UPDATE edge_lesson_lock SET subtitle_done=0, analysis_done=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE lesson_id=?", (lesson_id,))
            else:
                conn.execute("UPDATE edge_lesson_lock SET analysis_done=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE lesson_id=?", (lesson_id,))
            conn.commit()
        for task_db_id in reset_task_ids:
            for step_name in step_order[reset_index:]:
                self._queue_step_state_report(int(task_db_id), step_name, include_same_lesson=True)
        self._log.info("reset course lesson=%s for step=%s (task=%s)", lesson_id, step, server_task_id)
        return True

    def _mark_lesson_lock_done(self, lesson_id: int) -> None:
        if int(lesson_id) <= 0:
            return
        with self._db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO edge_lesson_lock(lesson_id, speech_done, subtitle_done, analysis_done) VALUES (?,?,?,?)",
                (int(lesson_id), 0, 0, 0),
            )
            conn.execute(
                "UPDATE edge_lesson_lock SET speech_done=1, subtitle_done=1, analysis_done=1, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE lesson_id=?",
                (int(lesson_id),),
            )
            conn.commit()

    def _complete_same_lesson_tasks(self, task_db_id: int, ok: bool) -> None:
        with self._db.connect() as conn:
            t = conn.execute("SELECT lesson_id FROM edge_stream_task WHERE id=? AND task_kind='CourseTask'", (int(task_db_id),)).fetchone()
            if t is None:
                return
            lesson_id = int(t["lesson_id"])
            status = TaskStatus.SUCCESS if ok else TaskStatus.FAILED
            others = conn.execute(
                "SELECT id, task_type FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND id!=? AND task_status=1",
                (lesson_id, int(task_db_id)),
            ).fetchall()
            for r in (others or []):
                if int(r["task_type"] or 0) == 3 and not self._is_type3_course_visible(conn, lesson_id):
                    continue
                oid = int(r["id"])
                if ok:
                    conn.execute(
                        "UPDATE edge_stream_task SET task_status=?, process_rate=100, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (status, oid),
                    )
                    conn.execute(
                        "UPDATE edge_stream_task_step SET step_status=2, step_process=100, end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_status IN (0,1)",
                        (oid,),
                    )
                else:
                    conn.execute(
                        "UPDATE edge_stream_task SET task_status=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (status, oid),
                    )
                    conn.execute(
                        "UPDATE edge_stream_task_step SET step_status=3, end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_status=1",
                        (oid,),
                    )
                active_steps = conn.execute(
                    "SELECT step_code FROM edge_stream_task_step WHERE task_id=? AND step_status IN (2,3) ORDER BY id DESC LIMIT 3",
                    (oid,),
                ).fetchall()
                for sr in (active_steps or []):
                    conn.execute(
                        """INSERT INTO edge_step_report_state(task_id, step_code, report_dirty, updated_time)
                        VALUES(?,?,1,strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                        ON CONFLICT(task_id, step_code) DO UPDATE SET
                          report_dirty=1, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
                        (oid, str(sr["step_code"])),
                    )
            if others:
                conn.commit()

    def _sync_lesson_step_progress(self, task_db_id: int, step_code: str, progress: int, message: str) -> None:
        with self._db.connect() as conn:
            t = conn.execute("SELECT lesson_id FROM edge_stream_task WHERE id=? AND task_kind='CourseTask'", (int(task_db_id),)).fetchone()
            if t is None:
                return
            lesson_id = int(t["lesson_id"])
            enabled_course_steps = self._course_step_order()
            final_task_done = bool(progress >= 100 and enabled_course_steps and str(step_code).upper() == str(enabled_course_steps[-1]))
            others = conn.execute(
                "SELECT id, task_type FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND id!=?",
                (lesson_id, int(task_db_id)),
            ).fetchall()
            for r in (others or []):
                if int(r["task_type"] or 0) == 3 and not self._is_type3_course_visible(conn, lesson_id):
                    continue
                oid = int(r["id"])
                other_step = conn.execute(
                    "SELECT step_status FROM edge_stream_task_step WHERE task_id=? AND step_code=?",
                    (oid, str(step_code)),
                ).fetchone()
                other_step_status = int(other_step["step_status"] or 0) if other_step else StepStatus.PENDING
                if other_step_status in (StepStatus.SUCCESS, StepStatus.FAILED):
                    conn.execute(
                        "UPDATE edge_stream_task SET process_rate=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (progress, oid),
                    )
                else:
                    conn.execute(
                        "UPDATE edge_stream_task SET task_status=?, current_step=?, process_rate=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (TaskStatus.SUCCESS if final_task_done else TaskStatus.RUNNING, str(step_code), progress, oid),
                    )
                if progress < 100:
                    conn.execute(
                        "UPDATE edge_stream_task_step SET step_status=1, start_time=COALESCE(start_time, strftime('%Y-%m-%dT%H:%M:%SZ','now')), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=? AND step_status IN (0,1,4)",
                        (oid, str(step_code)),
                    )
                conn.execute(
                    "UPDATE edge_stream_task_step SET step_process=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=?",
                    (progress, oid, str(step_code)),
                )
                if progress >= 100:
                    conn.execute(
                        "UPDATE edge_stream_task_step SET step_status=2, end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=?",
                        (oid, str(step_code)),
                    )
                    nxt = self._next_course_step(str(step_code))
                    if nxt:
                        conn.execute(
                            "UPDATE edge_stream_task_step SET step_status=1, start_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=? AND step_status=0",
                            (oid, nxt),
                        )
                if progress in {0, 100} or progress % 25 == 0:
                    conn.execute("INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)", (oid, str(step_code), "INFO", str(message or "")))
                else:
                    last_log = conn.execute("SELECT id FROM edge_task_log WHERE task_id=? AND step_code=? ORDER BY id DESC LIMIT 1", (oid, str(step_code))).fetchone()
                    if last_log:
                        conn.execute("UPDATE edge_task_log SET message=? WHERE id=?", (str(message or ""), int(last_log["id"])))
                    else:
                        conn.execute("INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)", (oid, str(step_code), "INFO", str(message or "")))
            if others:
                conn.commit()

    def _claim_next_task(self, node_id: str, task_kind: str, allowed_steps: tuple[str, ...] | None = None) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT id, server_task_id, lesson_id, task_type FROM edge_stream_task WHERE task_status=0 AND task_kind=? ORDER BY server_task_id ASC, id ASC",
                (str(task_kind),),
            ).fetchall()
            candidate: tuple[int, str] | None = None
            allowed_step_set = set(allowed_steps) if allowed_steps else None
            first_step_cache: dict[int, str] = {}

            def _cached_first_pending_step(task_db_id: int) -> str:
                if task_db_id not in first_step_cache:
                    first_step_cache[task_db_id] = self._get_first_pending_step(conn, task_db_id, task_kind)
                return first_step_cache[task_db_id]

            for t in (rows or []):
                tid = int(t["id"])
                lesson_id = int(t["lesson_id"] or 0)
                task_type = int(t["task_type"] or 0)
                if task_kind == "CourseTask" and task_type == 3:
                    continue
                if task_kind == "CameraTask" and task_type in (2, 3):
                    same_lesson_type1_ready = False
                    for other in rows or []:
                        if int(other["lesson_id"] or 0) != lesson_id:
                            continue
                        if int(other["task_type"] or 0) != 1:
                            continue
                        other_first = _cached_first_pending_step(int(other["id"]))
                        if not other_first:
                            continue
                        if allowed_step_set and other_first not in allowed_step_set:
                            continue
                        same_lesson_type1_ready = True
                        break
                    if same_lesson_type1_ready:
                        continue
                if task_kind == "CourseTask":
                    lock = conn.execute("SELECT speech_done, subtitle_done, analysis_done FROM edge_lesson_lock WHERE lesson_id=?", (lesson_id,)).fetchone()
                    if lock is not None and int(lock["speech_done"] or 0) == 1 and int(lock["subtitle_done"] or 0) == 1 and int(lock["analysis_done"] or 0) == 1:
                        conn.execute("UPDATE edge_stream_task SET task_status=2, process_rate=100, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (tid,))
                        conn.execute("UPDATE edge_stream_task_step SET step_status=2, step_process=100, end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=?", (tid,))
                        continue
                first = _cached_first_pending_step(tid)
                if not first:
                    continue
                if task_kind == "CameraTask":
                    wait_reason = self._camera_task_wait_reason_conn(conn, tid, lesson_id, task_type, first)
                    if wait_reason:
                        continue
                if task_kind == "CourseTask" and not self._is_course_task_ready(conn, tid, lesson_id, first):
                    continue
                if allowed_step_set and first not in allowed_step_set:
                    continue
                candidate = (int(t["server_task_id"]), str(first))
                break
            conn.commit()
        if candidate is None:
            return None
        return self._claim_specific_task(node_id, candidate[0], candidate[1], task_kind)

    def _claim_specific_task(self, node_id: str, server_task_id: int, step_code: str, task_kind: str = "") -> dict[str, Any] | None:
        step = str(step_code or "").strip().upper()
        with self._db.connect() as conn:
            if task_kind:
                t = conn.execute("SELECT * FROM edge_stream_task WHERE server_task_id=? AND task_kind=? AND task_status=0 ORDER BY id DESC LIMIT 1", (int(server_task_id), str(task_kind))).fetchone()
            else:
                t = conn.execute("SELECT * FROM edge_stream_task WHERE server_task_id=? AND task_status=0 ORDER BY id DESC LIMIT 1", (int(server_task_id),)).fetchone()
            if t is None:
                return None
            tid = int(t["id"])
            task_kind = str(t["task_kind"] or "")
            lesson_id = int(t["lesson_id"])
            if task_kind == "CourseTask" and int(t["task_type"] or 0) == 3:
                return None
            step_check = conn.execute("SELECT step_code FROM edge_stream_task_step WHERE task_id=? AND step_code=? AND step_status=0", (tid, step)).fetchone()
            if step_check is None:
                return None
            if task_kind == "CourseTask":
                lock = conn.execute("SELECT speech_done, subtitle_done, analysis_done FROM edge_lesson_lock WHERE lesson_id=?", (lesson_id,)).fetchone()
                if lock is not None and int(lock["speech_done"] or 0) == 1 and int(lock["subtitle_done"] or 0) == 1 and int(lock["analysis_done"] or 0) == 1:
                    conn.execute("UPDATE edge_stream_task SET task_status=2, process_rate=100, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (tid,))
                    conn.execute("UPDATE edge_stream_task_step SET step_status=2, step_process=100, end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=?", (tid,))
                    conn.commit()
                    raw_skip = {
                        "id": int(t["server_task_id"]), "taskId": int(t["server_task_id"]), "taskKind": task_kind,
                        "lessonId": int(t["lesson_id"]), "taskType": int(t["task_type"]),
                        "lessonStartAt": str(t["download_start"] or ""), "lessonEndAt": str(t["download_end"] or ""),
                        "otherTasksId": str(t["other_tasks_id"] or ""),
                    }
                    return {"dbId": tid, "taskId": str(t["server_task_id"]), "raw": raw_skip, "taskKind": task_kind, "skipped": True}
            upd = conn.execute(
                "UPDATE edge_stream_task SET task_status=1, execute_node_id=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=? AND task_status=0",
                (str(node_id or ""), tid),
            )
            if getattr(upd, "rowcount", 0) != 1:
                conn.commit()
                return None
            upd2 = conn.execute(
                "UPDATE edge_stream_task_step SET step_status=1, step_process=0, start_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), end_time=NULL, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=? AND step_status=0",
                (tid, step),
            )
            if getattr(upd2, "rowcount", 0) != 1:
                conn.execute("UPDATE edge_stream_task SET task_status=0, execute_node_id='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (tid,))
                conn.commit()
                return None
            conn.execute("DELETE FROM edge_task_log WHERE task_id=? AND step_code=?", (tid, step))
            conn.execute("INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)", (int(tid), str(step), "INFO", "开始执行"))
            conn.execute("UPDATE edge_stream_task SET current_step=?, process_rate=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (str(step), int(tid)))
            if task_kind == "CourseTask":
                conn.execute("INSERT OR IGNORE INTO edge_lesson_lock(lesson_id, speech_done, subtitle_done, analysis_done) VALUES (?,?,?,?)", (lesson_id, 0, 0, 0))
                others = conn.execute(
                    "SELECT id FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND task_type IN (1,2) AND id!=? AND task_status=0",
                    (lesson_id, tid),
                ).fetchall()
                for r in (others or []):
                    oid = int(r["id"])
                    conn.execute("UPDATE edge_stream_task SET task_status=1, current_step=?, process_rate=0, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (str(step), oid))
                    conn.execute("UPDATE edge_stream_task_step SET step_status=1, step_process=0, start_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=? AND step_status=0", (oid, str(step)))
                    conn.execute("INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)", (oid, str(step), "INFO", "开始执行（同课次联动）"))
            conn.commit()
            actual_task_type = int(t["task_type"] or 0)
            if task_kind == "CourseTask" and step == "SPEECH":
                teacher_task = conn.execute("SELECT * FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND task_type=1 ORDER BY id DESC LIMIT 1", (lesson_id,)).fetchone()
                if teacher_task is not None:
                    actual_task_type = 1
                    t = teacher_task
            raw = {
                "id": int(t["server_task_id"]), "taskId": int(t["server_task_id"]), "taskKind": task_kind,
                "lessonId": int(t["lesson_id"]), "taskType": actual_task_type,
                "lessonDate": str(t["lesson_date"] or ""),
                "lessonStartAt": str(t["download_start"] or ""), "lessonEndAt": str(t["download_end"] or ""),
                "relate_class": str(t["relate_class"] or ""), "relate_lesson": str(t["relate_lesson"] or ""),
                "nvr": {
                    "nvrDeviceId": t["nvr_device_id"], "nvrChannelNum": t["nvr_channel_num"],
                    "nvrChannelId": str(t["nvr_channel_id"] or ""), "ipAddress": str(t["nvr_ip"] or ""),
                    "port": int(t["nvr_port"] or 8000), "account": str(t["nvr_account"] or ""), "password": str(t["nvr_password"] or ""),
                },
                "nvrDeviceId": t["nvr_device_id"], "nvrChannelNum": t["nvr_channel_num"],
                "nvrChannelId": str(t["nvr_channel_id"] or ""), "ipAddress": str(t["nvr_ip"] or ""),
                "port": int(t["nvr_port"] or 8000), "account": str(t["nvr_account"] or ""), "password": str(t["nvr_password"] or ""),
                "otherTasksId": str(t["other_tasks_id"] or ""),
            }
            return {"dbId": tid, "taskId": str(t["server_task_id"]), "raw": raw, "taskKind": task_kind, "startStep": step}

    def _upsert_task_from_poll(self, raw: dict[str, Any]) -> None:
        server_task_id = _as_int(raw.get("id") or raw.get("taskId"))
        lesson_id = _as_int(raw.get("lessonId"))
        task_kind = str(raw.get("taskKind") or "").strip()
        task_type = _as_int(raw.get("taskType"))
        if server_task_id is None or lesson_id is None or not task_kind or task_type is None:
            return
        nvr = raw.get("nvr") if isinstance(raw.get("nvr"), dict) else {}
        nvr_device_id = _as_int(raw.get("nvrDeviceId") or nvr.get("nvrDeviceId"))
        nvr_channel_num = _as_int(raw.get("nvrChannelNum") or nvr.get("nvrChannelNum"))
        nvr_channel_id = str(raw.get("nvrChannelId") or nvr.get("nvrChannelId") or "").strip()
        nvr_ip = str(raw.get("nvrAddress") or raw.get("nvrIp") or raw.get("nvrInnerIp") or raw.get("nvrLanIp") or raw.get("nvrIntranetIp") or raw.get("lanIp") or raw.get("localIp") or raw.get("ipAddress") or nvr.get("nvrAddress") or nvr.get("nvrIp") or nvr.get("nvrInnerIp") or nvr.get("nvrLanIp") or nvr.get("nvrIntranetIp") or nvr.get("lanIp") or nvr.get("localIp") or nvr.get("ipAddress") or "").strip()
        nvr_port = _as_int(raw.get("nvrPort") or nvr.get("nvrPort") or nvr.get("port") or raw.get("port")) or 8000
        nvr_account = str(raw.get("nvrAccount") or raw.get("nvrUsername") or raw.get("nvrUserName") or raw.get("nvrUser") or raw.get("username") or raw.get("account") or nvr.get("nvrAccount") or nvr.get("nvrUsername") or nvr.get("nvrUserName") or nvr.get("nvrUser") or nvr.get("username") or nvr.get("account") or "").strip()
        nvr_password = str(raw.get("nvrPassword") or raw.get("nvrPwd") or raw.get("nvrPass") or raw.get("password") or nvr.get("nvrPassword") or nvr.get("nvrPwd") or nvr.get("nvrPass") or nvr.get("password") or "").strip()
        relate_class = str(raw.get("relate_class") or raw.get("relateClass") or "").strip()
        relate_lesson = str(raw.get("relate_lesson") or raw.get("relateLesson") or "").strip()
        grade = str(raw.get("grade") or "").strip()
        subject = str(raw.get("subject") or "").strip()
        lesson_date = str(raw.get("lessonDate") or "").strip()
        room_name = str(raw.get("roomName") or raw.get("room_name") or "").strip()
        download_start = str(raw.get("lessonStartAt") or "").strip()
        download_end = str(raw.get("lessonEndAt") or "").strip()
        school_area_code = str(raw.get("schoolAreaCode") or raw.get("school_area_code") or raw.get("lessonSchoolAreaCode") or "").strip()
        branch_school_code = str(raw.get("branchSchoolCode") or raw.get("branch_school_code") or "").strip()
        other_tasks_raw = raw.get("otherTasksId") or raw.get("other_tasks_id") or ""
        other_tasks_id = _json.dumps(other_tasks_raw) if isinstance(other_tasks_raw, (list, tuple)) else str(other_tasks_raw).strip()
        if task_kind == "CourseTask" and not self._course_step_order():
            return
        with self._db.connect() as conn:
            self._sync_lesson_metadata_from_poll_conn(
                conn,
                int(lesson_id),
                relate_class=relate_class,
                relate_lesson=relate_lesson,
                grade=grade,
                subject=subject,
            )
            exist = conn.execute("SELECT id, task_status FROM edge_stream_task WHERE server_task_id=? AND task_kind=? ORDER BY id DESC LIMIT 1", (int(server_task_id), task_kind)).fetchone()
            if exist is not None:
                task_db_id = int(exist["id"])
                conn.execute(
                    "UPDATE edge_stream_task SET task_type=?, lesson_id=?, lesson_date=?, room_name=?, relate_class=?, relate_lesson=?, grade=?, subject=?, download_start=?, download_end=?, school_area_code=?, branch_school_code=?, other_tasks_id=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (int(task_type), int(lesson_id), lesson_date, room_name, relate_class, relate_lesson, grade, subject, download_start, download_end, school_area_code, branch_school_code, other_tasks_id, task_db_id),
                )
                if task_kind in ("CourseTask", "CameraTask"):
                    conn.execute("UPDATE edge_stream_task SET nvr_device_id=?, nvr_channel_num=?, nvr_channel_id=?, nvr_ip=?, nvr_port=?, nvr_account=?, nvr_password=? WHERE id=?", (nvr_device_id, nvr_channel_num, nvr_channel_id, nvr_ip, int(nvr_port), nvr_account, nvr_password, task_db_id))
                if task_kind == "CameraTask":
                    try:
                        step_rows = conn.execute("SELECT step_code FROM edge_stream_task_step WHERE task_id=?", (task_db_id,)).fetchall()
                        existing_steps = {str(r["step_code"] or "").strip().upper() for r in (step_rows or [])}
                    except Exception:
                        existing_steps = set()
                    for sc in ("DOWNLOAD", "TRANSCODE"):
                        if sc not in existing_steps:
                            conn.execute("INSERT INTO edge_stream_task_step(task_id, step_code, is_lesson_level, step_status, step_process) VALUES (?,?,?,?,?)", (task_db_id, sc, 0, 0, 0))
                elif int(task_type) == 3:
                    self._sync_course_task_from_lesson_state(conn, task_db_id, int(lesson_id), int(task_type))
                conn.commit()
                return
            cur2 = conn.execute(
                "INSERT INTO edge_stream_task(server_task_id, lesson_id, lesson_date, room_name, task_type, task_kind, nvr_device_id, nvr_channel_num, nvr_channel_id, nvr_ip, nvr_port, nvr_account, nvr_password, relate_class, relate_lesson, grade, subject, download_start, download_end, school_area_code, branch_school_code, other_tasks_id, task_status, current_step, process_rate, retry_count, execute_node_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(server_task_id), int(lesson_id), lesson_date, room_name, int(task_type), task_kind, nvr_device_id, nvr_channel_num, nvr_channel_id, nvr_ip, int(nvr_port), nvr_account, nvr_password, relate_class, relate_lesson, grade, subject, download_start, download_end, school_area_code, branch_school_code, other_tasks_id, 0, "", 0, 0, ""),
            )
            task_db_id = int(cur2.lastrowid)
            steps = {"CameraTask": ["DOWNLOAD", "TRANSCODE"], "CourseTask": self._course_step_order()}.get(task_kind, [])
            for s in steps:
                conn.execute("INSERT INTO edge_stream_task_step(task_id, step_code, is_lesson_level, step_status, step_process) VALUES (?,?,?,?,?)", (task_db_id, s, 0 if task_kind == "CameraTask" else 1, 0, 0))
            if task_kind == "CourseTask" and int(task_type) == 3:
                self._sync_course_task_from_lesson_state(conn, task_db_id, int(lesson_id), int(task_type))
            conn.commit()
