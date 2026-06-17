from __future__ import annotations

import asyncio
import contextlib
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..api_client import EdgeApiClient, EdgeTask
from ..base_url_store import BaseUrlStore
from ..config import EdgeConfig
from ..credential_store import CredentialStore
from ..db import Db, DbConfig
from ..state import EdgeState
from ..tasks.camera import FinalizePendingError, PauseRequested, StopRequested, run_camera_task, run_camera_transcode_only
from ..tasks.course import run_course_task
from ..token_store import TokenStore
from ..utils import get_lesson_dir as _get_lesson_dir_util, resolve_lesson_date as _resolve_lesson_date_util, task_type_prefix as _task_type_prefix_util
from ._task_state import RerunCleanupError

from ..constants import StepStatus, TaskStatus
from ._step_reporter import StepReporterMixin
from ._task_state import TaskStateMixin
from ._scheduler import SchedulerMixin


def _is_sqlite_busy_error(exc: BaseException) -> bool:
    text = str(exc or "").lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in text
        or "database table is locked" in text
        or "database schema is locked" in text
    )


def _cleanup_speech_task_files_safe(raw: dict[str, Any], remove_outputs: bool) -> None:
    try:
        from ..tasks.speech import cleanup_speech_task_files
    except Exception:
        return
    cleanup_speech_task_files(raw, remove_outputs)


def _has_speech_resume_checkpoint_safe(raw: dict[str, Any]) -> bool:
    try:
        from ..tasks.speech import has_speech_resume_checkpoint
    except Exception:
        return False
    return bool(has_speech_resume_checkpoint(raw))


def _mount_subtitle_to_video_safe(srt_path: Path, vtt_path: Path | None, video_path: Path) -> None:
    try:
        from ..tasks.subtitle import mount_subtitle_to_video
    except Exception:
        return
    mount_subtitle_to_video(srt_path, vtt_path, video_path)


class EdgeRunner(StepReporterMixin, TaskStateMixin, SchedulerMixin):
    def __init__(
        self,
        cfg: EdgeConfig,
        state: EdgeState,
        *,
        db_path: str,
        token_store: TokenStore | None = None,
        base_url_store: BaseUrlStore | None = None,
        credential_store: CredentialStore | None = None,
    ) -> None:
        self._cfg = cfg
        self._state = state
        self._stop = asyncio.Event()
        self._client = EdgeApiClient(cfg, token_store=token_store, base_url_store=base_url_store, credential_store=credential_store)
        self._log = __import__("logging").getLogger("edge.runner")
        self._inflight: dict[str, tuple[str, str, asyncio.Task]] = {}
        self._done: set[str] = set()
        self._db = Db(DbConfig(path=db_path))
        self._db.init_schema()
        self._start_lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._camera_ctx: dict[int, dict[str, Any]] = {}
        self._last_report_ts: dict[tuple[int, str], float] = {}
        self._report_interval_sec: int = 180
        self._progress_log_interval_sec: float = 20.0
        self._last_progress_log_ts: dict[tuple[int, str], float] = {}
        self._last_progress_warning_ts: dict[tuple[int, str, str], float] = {}
        self._progress_db_interval_sec: float = 20.0
        self._last_progress_db_write: dict[tuple[int, str], tuple[float, int]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _server_task_id_for_db_log(self, task_db_id: int) -> str:
        try:
            with self._db.connect() as conn:
                row = conn.execute(
                    "SELECT server_task_id FROM edge_stream_task WHERE id=? LIMIT 1",
                    (int(task_db_id),),
                ).fetchone()
            if row is not None:
                sid = str(row["server_task_id"] or "").strip()
                if sid:
                    return sid
        except Exception:
            pass
        return str(task_db_id)

    def _is_step_still_running(self, task_db_id: int, step_code: str) -> bool:
        step = str(step_code or "").strip().upper()
        if not step:
            return False
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT task_status, current_step FROM edge_stream_task WHERE id=? LIMIT 1",
                (int(task_db_id),),
            ).fetchone()
            if row is None or int(row["task_status"] or 0) != TaskStatus.RUNNING:
                return False
            step_row = conn.execute(
                "SELECT step_status, step_process, finalize_pending FROM edge_stream_task_step WHERE task_id=? AND step_code=? LIMIT 1",
                (int(task_db_id), step),
            ).fetchone()
            if step_row is None:
                return False
            step_status = int(step_row["step_status"] or 0)
            if step_status == StepStatus.RUNNING:
                return True
            if step_status in {StepStatus.FAILED, StepStatus.PAUSED}:
                return False
            if int(step_row["finalize_pending"] or 0) == 1:
                return False
            current_step = str(row["current_step"] or "").strip().upper()
            if step_status == StepStatus.SUCCESS and int(step_row["step_process"] or 0) >= 100 and current_step == step:
                return True
            return False

    def _task_mem_key(self, task_id: str | int, task_kind: str) -> str:
        return f"{str(task_kind or '').strip()}::{str(task_id or '').strip()}"

    def _row_mem_key(self, row: dict[str, Any], default_kind: str = "") -> str:
        return self._task_mem_key(row.get("taskId") or "", row.get("taskKind") or default_kind)

    def _find_same_lesson_course_ctx(self, lesson_id: int) -> dict[str, Any] | None:
        target = int(lesson_id or 0)
        if target <= 0:
            return None
        for ctx in self._camera_ctx.values():
            raw = ctx.get("raw") if isinstance(ctx, dict) else None
            if not isinstance(raw, dict):
                continue
            try:
                raw_lesson_id = int(raw.get("lessonId") or 0)
            except Exception:
                raw_lesson_id = 0
            if raw_lesson_id == target and str(raw.get("taskKind") or "CourseTask") == "CourseTask":
                return ctx
        return None

    async def _stop_downstream_tasks_before_download_rerun(self, *, lesson_id: int, exclude_task_db_id: int) -> None:
        target_lesson_id = int(lesson_id or 0)
        if target_lesson_id <= 0:
            return
        rows: list[dict[str, Any]] = []
        with self._db.connect() as conn:
            fetched = conn.execute(
                """
SELECT id, server_task_id, task_kind, task_status, current_step
FROM edge_stream_task
WHERE lesson_id=? AND id<>? AND task_status=?
ORDER BY id ASC
                """.strip(),
                (target_lesson_id, int(exclude_task_db_id), int(TaskStatus.RUNNING)),
            ).fetchall()
            rows = [dict(r) for r in (fetched or [])]
        for row in rows:
            task_db_id = int(row.get("id") or 0)
            sid = int(row.get("server_task_id") or 0)
            kind = str(row.get("task_kind") or "").strip()
            current_step = str(row.get("current_step") or "").strip().upper()
            if kind == "CameraTask":
                if current_step != "TRANSCODE":
                    continue
                self._log.info("download rerun stopping downstream camera transcode task=%s lesson_id=%s", sid, target_lesson_id)
                inflight = self._inflight.get(self._task_mem_key(sid, "CameraTask"))
                ctx = self._camera_ctx.get(task_db_id)
                if ctx and isinstance(ctx.get("raw"), dict):
                    ctx["raw"]["__cancel"] = "stop"
                if inflight is not None:
                    _, _, inflight_task = inflight
                    with contextlib.suppress(BaseException):
                        await inflight_task
                await asyncio.to_thread(self._stop_and_reset_transcode, task_db_id)
                sync_ok = await self._sync_reset_step_reports(task_db_id, "TRANSCODE")
                if not sync_ok:
                    raise RuntimeError(f"下游转码任务状态回写失败: {sid}")
                continue
            if kind == "CourseTask":
                if current_step not in {"SPEECH", "SUBTITLE", "ANALYSIS"}:
                    continue
                self._log.info("download rerun stopping downstream course task=%s step=%s lesson_id=%s", sid, current_step, target_lesson_id)
                inflight = self._inflight.get(self._task_mem_key(sid, "CourseTask"))
                ctx = self._camera_ctx.get(task_db_id)
                if ctx and isinstance(ctx.get("raw"), dict):
                    ctx["raw"]["__cancel"] = "stop"
                if inflight is not None:
                    _, _, inflight_task = inflight
                    with contextlib.suppress(BaseException):
                        await inflight_task
                reset_ok = await asyncio.to_thread(self._reset_course_task_for_step, sid, current_step)
                if not reset_ok:
                    raise RuntimeError(f"下游课次任务重置失败: {sid}-{current_step}")
                sync_ok = await self._sync_reset_step_reports(task_db_id, current_step)
                if not sync_ok:
                    raise RuntimeError(f"下游课次任务状态回写失败: {sid}-{current_step}")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stop(self) -> None:
        self._stop.set()
        self._log.info("graceful stop: signalling %d inflight task(s) to pause", len(self._inflight))
        for ctx in self._camera_ctx.values():
            raw = ctx.get("raw")
            if isinstance(raw, dict):
                raw["__cancel"] = "pause"
        for i in range(10):
            if not self._inflight:
                break
            await asyncio.sleep(0.5)
        for _, _, t in list(self._inflight.values()):
            t.cancel()
        for _, _, t in list(self._inflight.values()):
            with contextlib.suppress(Exception):
                await t
        await asyncio.to_thread(self._pause_all_running_tasks)

    def _pause_all_running_tasks(self) -> None:
        """Mark all task_status=1 tasks to paused while preserving current step progress."""
        try:
            with self._db.connect() as conn:
                rows = conn.execute(
                    "SELECT id, server_task_id, task_kind FROM edge_stream_task WHERE task_status=1"
                ).fetchall()
                for r in rows:
                    tid = int(r["id"])
                    kind = str(r["task_kind"] or "")
                    if kind == "CameraTask":
                        step = self._resolve_camera_active_step_conn(conn, tid, "")
                        self._log.info(
                            "pause inflight task on stop task=%s kind=%s step=%s",
                            r["server_task_id"], kind, step,
                        )
                        self._mark_step_paused(tid, step)
                        continue
                    self._log.info(
                        "pause inflight task on stop task=%s kind=%s",
                        r["server_task_id"], kind,
                    )
                    step_row = conn.execute(
                        "SELECT step_code FROM edge_stream_task_step WHERE task_id=? AND step_status=1 ORDER BY id ASC LIMIT 1",
                        (tid,),
                    ).fetchone()
                    step = str(step_row["step_code"] or "SPEECH").strip().upper() if step_row is not None else "SPEECH"
                    self._mark_step_paused(tid, step)
                if rows:
                    conn.commit()
        except Exception:
            self._log.exception("_pause_all_running_tasks failed")

    def _get_local_ip(self) -> str:
        _EXCLUDE_KEYWORDS = ("vmware", "vmnet", "vethernet", "virtualbox", "hyper-v",
                             "loopback", "wsl", "clash", "vpn", "tap", "tun", "ppp")
        try:
            import psutil as _psutil
            import socket as _socket
            addrs = _psutil.net_if_addrs()
            stats = _psutil.net_if_stats()
            tier1: list[str] = []
            tier2: list[str] = []
            tier3: list[str] = []
            for iface, saddrs in addrs.items():
                iface_lower = iface.lower()
                if any(kw in iface_lower for kw in _EXCLUDE_KEYWORDS):
                    continue
                st = stats.get(iface)
                if st and not st.isup:
                    continue
                for a in saddrs:
                    if a.family != _socket.AF_INET:
                        continue
                    ip = str(a.address or "")
                    if not ip or ip.startswith("127.") or ip.startswith("169.254."):
                        continue
                    if ip.startswith("192.168."):
                        tier1.append(ip)
                    elif ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31:
                        tier2.append(ip)
                    elif ip.startswith("10."):
                        tier3.append(ip)
            if tier1:
                return tier1[0]
            if tier2:
                return tier2[0]
            if tier3:
                return tier3[0]
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    async def _execute_db_task(self, task_db_id: int, task: EdgeTask) -> None:
        if task.task_kind in {"CameraTask", "CourseTask"}:
            self._camera_ctx[task_db_id] = {"raw": task.raw}
            task.raw["__db_path"] = self._db.path
        start_step = str(task.raw.get("__startStep") or "")
        self._log.info(
            "worker execute start task=%s kind=%s step=%s",
            task.task_id,
            task.task_kind,
            start_step or "AUTO",
        )
        try:
            if task.task_kind == "CameraTask" and start_step == "TRANSCODE":
                artifacts = await run_camera_transcode_only(task.raw, lambda stage, progress, msg: self._on_progress_db(task_db_id, stage, progress, msg))
                if not await asyncio.to_thread(self._is_step_still_running, task_db_id, "TRANSCODE"):
                    self._log.info("skip finalize completed transcode-only task because step no longer running task=%s", task.task_id)
                    return
                try:
                    await asyncio.to_thread(self._save_artifacts, task_db_id, task, artifacts)
                except Exception:
                    self._log.warning("save_artifacts failed task=%s", task.task_id, exc_info=True)
                await asyncio.to_thread(self._mark_task_done, task_db_id, True)
                final_step = await asyncio.to_thread(self._load_current_step, task_db_id)
                if final_step:
                    await asyncio.to_thread(self._mark_step_report_dirty, task_db_id, final_step)
                    self._fire_step_report(task_db_id, final_step, is_final=True)
                await self._try_start_followup_course_task(task.task_id)
            elif task.task_kind == "CameraTask":
                artifacts = await run_camera_task(task.raw, self._cfg.simulate, lambda stage, progress, msg: self._on_progress_db(task_db_id, stage, progress, msg))
                final_camera_step = await asyncio.to_thread(self._load_current_step, task_db_id)
                final_camera_step = str(final_camera_step or start_step or "TRANSCODE").strip().upper() or "TRANSCODE"
                if not await asyncio.to_thread(self._is_step_still_running, task_db_id, final_camera_step):
                    self._log.info("skip finalize completed camera task because step no longer running task=%s step=%s", task.task_id, final_camera_step)
                    return
                try:
                    await asyncio.to_thread(self._save_artifacts, task_db_id, task, artifacts)
                except Exception:
                    self._log.warning("save_artifacts failed task=%s", task.task_id, exc_info=True)
                artifact_steps = {str(a.get("stepCode") or "").strip().upper() for a in artifacts if isinstance(a, dict)}
                if start_step.strip().upper() == "DOWNLOAD" and "DOWNLOAD" in artifact_steps and "TRANSCODE" not in artifact_steps:
                    if await asyncio.to_thread(self._mark_camera_download_complete_pending_transcode, task_db_id):
                        await asyncio.to_thread(self._mark_step_report_dirty, task_db_id, "DOWNLOAD")
                        self._fire_step_report(task_db_id, "DOWNLOAD", is_final=True)
                        await asyncio.to_thread(self._mark_step_report_dirty, task_db_id, "TRANSCODE")
                        self._fire_step_report(task_db_id, "TRANSCODE", is_final=False)
                        self._log.info("camera download completed and queued transcode task=%s", task.task_id)
                        return
                await asyncio.to_thread(self._mark_task_done, task_db_id, True)
                final_step = await asyncio.to_thread(self._load_current_step, task_db_id)
                if final_step:
                    await asyncio.to_thread(self._mark_step_report_dirty, task_db_id, final_step)
                    self._fire_step_report(task_db_id, final_step, is_final=True)
                await self._try_start_followup_course_task(task.task_id)
            elif task.task_kind == "CourseTask":
                artifacts = await run_course_task(
                    task.raw,
                    False,
                    lambda stage, progress, msg: self._on_progress_db(task_db_id, stage, progress, msg),
                    on_step_complete=lambda step_code, step_artifacts: self._on_course_step_complete(task_db_id, task, step_code, step_artifacts),
                )
                final_course_step = await asyncio.to_thread(self._load_current_step, task_db_id)
                final_course_step = str(final_course_step or start_step or "ANALYSIS").strip().upper() or "ANALYSIS"
                if not await asyncio.to_thread(self._is_step_still_running, task_db_id, final_course_step):
                    self._log.info("skip finalize completed course task because step no longer running task=%s step=%s", task.task_id, final_course_step)
                    return
                try:
                    lesson_id = int(task.raw.get("lessonId") or 0)
                    if lesson_id > 0:
                        await asyncio.to_thread(self._mark_lesson_lock_done, lesson_id)
                except Exception:
                    self._log.warning("mark_lesson_lock_done failed task=%s", task.task_id, exc_info=True)
                await asyncio.to_thread(self._mark_task_done, task_db_id, True)
                final_step = await asyncio.to_thread(self._load_current_step, task_db_id)
                if final_step:
                    await asyncio.to_thread(self._mark_step_report_dirty, task_db_id, final_step)
                    self._fire_step_report(task_db_id, final_step, is_final=True)
                    try:
                        self._fire_same_lesson_reports(task_db_id, final_step, is_final=True)
                    except Exception:
                        self._log.debug("fire_same_lesson_reports failed task=%s step=%s", task.task_id, final_step, exc_info=True)
                try:
                    await asyncio.to_thread(self._complete_same_lesson_tasks, task_db_id, True)
                except Exception:
                    self._log.warning("complete_same_lesson_tasks(True) failed task=%s", task.task_id, exc_info=True)
            else:
                await asyncio.to_thread(self._mark_task_done, task_db_id, False)
            self._done.add(self._task_mem_key(task.task_id, task.task_kind))
        except PauseRequested:
            step = start_step.strip().upper() or ""
            if step not in {"DOWNLOAD", "TRANSCODE"}:
                step = await asyncio.to_thread(self._resolve_camera_active_step, task_db_id, step)
            if step not in {"DOWNLOAD", "TRANSCODE"}:
                step = "DOWNLOAD"
            await asyncio.to_thread(self._mark_step_paused, task_db_id, step)
            self._log.info("task paused task=%s step=%s", task.task_id, step)
        except StopRequested:
            cur_step = await asyncio.to_thread(self._load_current_step, task_db_id)
            if task.task_kind == "CameraTask" and cur_step == "TRANSCODE":
                await asyncio.to_thread(self._stop_and_reset_transcode, task_db_id)
            else:
                await asyncio.to_thread(self._stop_and_reset_task, task_db_id)
            self._log.info("task stopped task=%s step=%s", task.task_id, cur_step)
        except FinalizePendingError as e:
            await asyncio.to_thread(
                self._mark_step_finalize_pending,
                task_db_id,
                e.step_code,
                src_path=e.src_path,
                dst_path=e.dst_path,
                action=e.action,
                error=e.reason,
                message=e.user_message,
            )
            try:
                await self._do_step_report(task_db_id, e.step_code, is_final=False)
            except Exception:
                self._log.debug("step_report on finalize pending failed task=%s step=%s", task.task_id, e.step_code, exc_info=True)
            self._log.warning(
                "task finalize pending task=%s step=%s src=%s dst=%s reason=%s",
                task.task_id,
                e.step_code,
                e.src_path,
                e.dst_path,
                e.reason,
            )
        except Exception as e:
            msg = str(e)
            if task.task_kind == "CourseTask" and msg in {"cancelled:pause", "cancelled:stop"}:
                cur_step = await asyncio.to_thread(self._load_current_step, task_db_id)
                cur_step = cur_step or (start_step.strip().upper() or "SPEECH")
                if msg == "cancelled:pause":
                    await asyncio.to_thread(self._mark_step_paused, task_db_id, cur_step)
                    self._log.info("course task paused task=%s step=%s", task.task_id, cur_step)
                else:
                    raw = await asyncio.to_thread(self._load_task_raw_by_dbid, task_db_id)
                    if raw is not None and cur_step == "SPEECH":
                        await asyncio.to_thread(_cleanup_speech_task_files_safe, raw, True)
                    await asyncio.to_thread(self._reset_course_task_for_step, int(str(task.task_id or 0) or 0), cur_step)
                    self._log.info("course task stopped task=%s step=%s", task.task_id, cur_step)
                return
            cur_step = await asyncio.to_thread(self._load_current_step, task_db_id)
            try:
                await asyncio.to_thread(self._cleanup_download_parts, task_db_id)
            except Exception:
                self._log.debug("cleanup download parts on failure failed task=%s", task.task_id, exc_info=True)
            if task.task_kind == "CameraTask" and cur_step == "TRANSCODE":
                await asyncio.to_thread(self._mark_transcode_failed, task_db_id, task)
            else:
                await asyncio.to_thread(self._mark_task_done, task_db_id, False)
            if task.task_kind == "CourseTask":
                try:
                    await asyncio.to_thread(self._complete_same_lesson_tasks, task_db_id, False)
                except Exception:
                    self._log.warning("complete_same_lesson_tasks(False) failed task=%s", task.task_id, exc_info=True)
            if cur_step:
                try:
                    await self._do_step_report(task_db_id, cur_step, is_final=True)
                except Exception:
                    self._log.debug("step_report on failure failed task=%s step=%s", task.task_id, cur_step, exc_info=True)
            self._log.exception("task failed")
        finally:
            self._inflight.pop(self._task_mem_key(task.task_id, task.task_kind), None)
            self._camera_ctx.pop(task_db_id, None)

    async def try_start_task(self, server_id: str, step_code: str) -> tuple[bool, str]:
        async with self._start_lock:
            try:
                server_task_id = int(str(server_id or "").strip())
            except Exception:
                return False, "任务ID不合法"
            step = str(step_code or "").strip().upper()
            self._log.info("start request task=%s step=%s", server_task_id, step or "")
            if not step:
                return False, "步骤不合法"
            camera_steps = ["DOWNLOAD", "TRANSCODE"]
            course_steps = ["SPEECH", "SUBTITLE", "ANALYSIS"]
            if step in camera_steps:
                expected_kind = "CameraTask"
            elif step in course_steps:
                expected_kind = "CourseTask"
            else:
                return False, "未知步骤类型"
            info = await asyncio.to_thread(self._get_waiting_task_info, server_task_id, expected_kind)
            if info is None:
                if expected_kind == "CameraTask" and step == "TRANSCODE":
                    wait_reason = await asyncio.to_thread(self._get_camera_step_wait_reason, server_task_id, step)
                    if wait_reason:
                        return False, wait_reason
                if step == "TRANSCODE":
                    reset_ok = await asyncio.to_thread(self._reset_task_for_transcode, server_task_id)
                    if reset_ok:
                        info = await asyncio.to_thread(self._get_waiting_task_info, server_task_id, expected_kind)
                elif step in course_steps:
                    reset_ok = await asyncio.to_thread(self._reset_course_task_for_step, server_task_id, step)
                    if reset_ok:
                        info = await asyncio.to_thread(self._get_waiting_task_info, server_task_id, expected_kind)
                if info is None:
                    return False, "任务不可开始（可能已执行/已完成）"
            kind = str(info.get("taskKind") or "")
            expected = str(info.get("firstStep") or "")
            if step != expected:
                return False, "当前步骤不可手动开始"
            if kind == "CameraTask" and step == "DOWNLOAD":
                ok, msg = await asyncio.to_thread(self._validate_camera_task_ready, server_task_id)
                if not ok:
                    return False, msg
            if kind == "CameraTask" and step in {"DOWNLOAD", "TRANSCODE"} and not self._can_start_camera_step(step):
                if step == "DOWNLOAD":
                    return False, "下载并发已达上限"
                return False, "转码并发已达上限"
            if kind == "CourseTask" and not self._can_start_course_step(step):
                if step == "SPEECH":
                    return False, "语音识别并发已达上限"
                if step == "SUBTITLE":
                    return False, "字幕并发已达上限"
                if step == "ANALYSIS":
                    return False, "视频分析并发已达上限"
                return False, "已达并发上限"
            row = await asyncio.to_thread(self._claim_specific_task, self._cfg.edge_id, server_task_id, step, kind)
            if row is None:
                return False, "任务不可开始（未就绪）"
            if bool(row.get("skipped")):
                self._log.info("start skipped task=%s step=%s already completed", server_task_id, step)
                return True, "已完成"
            if not self._spawn_claimed(row, kind, step):
                self._log.warning("start not spawned task=%s kind=%s step=%s", server_task_id, kind, step)
                return False, "任务已在执行或未能启动"
            self._log.info("start accepted task=%s kind=%s step=%s", server_task_id, kind, step)
            return True, "开始执行"

    async def _sync_reset_step_reports(self, task_db_id: int, *step_codes: str) -> bool:
        ok = True
        seen: set[str] = set()
        for step_code in step_codes:
            step = str(step_code or "").strip().upper()
            if not step or step in seen:
                continue
            seen.add(step)
            try:
                await self._do_step_report(int(task_db_id), step, is_final=False)
            except Exception:
                ok = False
                self._log.warning("sync reset step report failed task=%s step=%s", self._server_task_id_for_db_log(task_db_id), step, exc_info=True)
        return ok

    def _validate_camera_task_ready(self, server_task_id: int) -> tuple[bool, str]:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT nvr_ip, nvr_account, nvr_password, nvr_channel_num FROM edge_stream_task WHERE server_task_id=? AND task_kind='CameraTask' ORDER BY id DESC LIMIT 1",
                (int(server_task_id),),
            ).fetchone()
            if r is None:
                return False, "未找到任务"
            ip = str(r["nvr_ip"] or "").strip()
            account = str(r["nvr_account"] or "").strip()
            password = str(r["nvr_password"] or "").strip()
            try:
                ch = int(r["nvr_channel_num"] or 0)
            except Exception:
                ch = 0
            missing = []
            if not ip:
                missing.append("NVR地址")
            if not account:
                missing.append("账号")
            if not password:
                missing.append("密码")
            if ch <= 0:
                missing.append("通道号")
            if missing:
                return False, "缺少NVR连接信息：" + "、".join(missing)
        return True, "ok"

    async def try_pause_task(self, *, server_id: str, step_code: str) -> tuple[bool, str]:
        step = str(step_code or "").upper()
        if step not in {"DOWNLOAD", "TRANSCODE", "SPEECH"}:
            return False, "仅支持暂停下载/转码/语音转写"
        async with self._start_lock:
            try:
                sid = int(str(server_id).strip())
            except Exception:
                return False, "任务ID不合法"
            self._log.info("pause request task=%s step=%s", sid, step)
            if step == "SPEECH":
                row = await asyncio.to_thread(self._find_course_task_by_server_id, sid)
            else:
                row = await asyncio.to_thread(self._find_task_by_server_id, sid)
            if row is None:
                return False, "未找到任务"
            task_db_id = int(row["id"])
            if int(row["task_status"] or 0) != TaskStatus.RUNNING:
                return False, "任务非进行中"
            if step == "DOWNLOAD":
                if int(row.get("download_step_status") or 0) != StepStatus.RUNNING:
                    return False, "下载步骤非进行中"
            elif step == "TRANSCODE":
                if int(row.get("transcode_step_status") or 0) != StepStatus.RUNNING:
                    return False, "转码步骤非进行中"
            else:
                if int(row.get("speech_step_status") or 0) != StepStatus.RUNNING:
                    return False, "语音转写步骤非进行中"
            ctx = self._camera_ctx.get(task_db_id)
            if step == "SPEECH" and (ctx is None or not isinstance(ctx.get("raw"), dict)):
                raw = await asyncio.to_thread(self._load_task_raw_by_dbid, task_db_id)
                if raw is not None:
                    ctx = self._find_same_lesson_course_ctx(int(raw.get("lessonId") or 0))
            if ctx and isinstance(ctx.get("raw"), dict):
                ctx["raw"]["__cancel"] = "pause"
            await asyncio.to_thread(self._mark_step_paused, task_db_id, step)
            self._log.info("pause %s task=%s", step.lower(), sid)
            return True, "已暂停"

    async def try_resume_task(self, *, server_id: str, step_code: str) -> tuple[bool, str]:
        step = str(step_code or "").upper()
        if step not in {"DOWNLOAD", "TRANSCODE", "SPEECH"}:
            return False, "仅支持继续下载/转码/语音转写"
        fallback_restart = False
        sid = 0
        async with self._start_lock:
            try:
                sid = int(str(server_id).strip())
            except Exception:
                return False, "任务ID不合法"
            self._log.info("resume request task=%s step=%s", sid, step)
            if step == "SPEECH":
                row = await asyncio.to_thread(self._find_course_task_by_server_id, sid)
            else:
                row = await asyncio.to_thread(self._find_task_by_server_id, sid)
            if row is None:
                return False, "未找到任务"
            task_db_id = int(row["id"])
            if step == "DOWNLOAD":
                if int(row.get("download_step_status") or 0) != StepStatus.PAUSED:
                    return False, "任务未暂停"
                if int(row.get("download_finalize_pending") or 0) == 1:
                    return False, "文件最终提交补偿中，请等待系统自动重试"
                if not self._can_start_camera_step("DOWNLOAD"):
                    return False, "下载并发已达上限"
            else:
                if step == "TRANSCODE":
                    if int(row.get("transcode_step_status") or 0) != StepStatus.PAUSED:
                        return False, "任务未暂停"
                    if int(row.get("transcode_finalize_pending") or 0) == 1:
                        return False, "文件最终提交补偿中，请等待系统自动重试"
                    wait_reason = await asyncio.to_thread(self._get_camera_step_wait_reason, sid, step)
                    if wait_reason:
                        return False, wait_reason
                    if not self._can_start_camera_step("TRANSCODE"):
                        return False, "转码并发已达上限"
                else:
                    if int(row.get("speech_step_status") or 0) != StepStatus.PAUSED:
                        return False, "任务未暂停"
                    if self._count_course_task_slots("SPEECH", exclude_task_db_id=task_db_id) >= self._speech_limit():
                        return False, "语音识别并发已达上限"
            raw = await asyncio.to_thread(self._load_task_raw_by_dbid, task_db_id)
            if raw is None:
                return False, "任务不可继续"
            if step == "SPEECH" and not await asyncio.to_thread(_has_speech_resume_checkpoint_safe, raw):
                reset_ok = await asyncio.to_thread(self._reset_course_task_for_step, sid, step)
                if not reset_ok:
                    return False, "任务不可继续"
                fallback_restart = True
            if fallback_restart:
                self._log.info("resume fallback to restart task=%s step=%s", sid, step)
            else:
                raw["__cancel"] = ""
                raw["__startStep"] = step
                raw["__db_path"] = self._db.path
                await asyncio.to_thread(self._mark_step_resumed, task_db_id, step)
                if step == "SPEECH":
                    active_ctx = self._find_same_lesson_course_ctx(int(raw.get("lessonId") or 0))
                    if active_ctx and isinstance(active_ctx.get("raw"), dict):
                        active_ctx["raw"]["__cancel"] = ""
                        self._log.info("resume speech task=%s -> reuse same-lesson inflight worker", sid)
                        return True, "继续执行"
                    self._spawn_claimed({"dbId": task_db_id, "taskId": str(sid), "raw": raw}, "CourseTask", step)
                else:
                    self._spawn_claimed({"dbId": task_db_id, "taskId": str(sid), "raw": raw}, "CameraTask", step)
                self._log.info("resume %s task=%s", step.lower(), sid)
                return True, "继续执行"
        if fallback_restart:
            return await self.try_start_task(server_id=str(sid), step_code=step)
        return False, "任务不可继续"

    async def try_stop_task(self, *, server_id: str, step_code: str) -> tuple[bool, str]:
        step = str(step_code or "").upper()
        camera_steps = {"DOWNLOAD", "TRANSCODE"}
        course_steps = {"SPEECH", "SUBTITLE", "ANALYSIS"}
        if step not in camera_steps | course_steps:
            return False, "仅支持终止有效步骤"
        async with self._start_lock:
            try:
                sid = int(str(server_id).strip())
            except Exception:
                return False, "任务ID不合法"
            self._log.info("stop request task=%s step=%s", sid, step)
            if step in camera_steps:
                row = await asyncio.to_thread(self._find_task_by_server_id, sid)
            else:
                row = await asyncio.to_thread(self._find_course_task_by_server_id, sid)
            if row is None:
                return False, "未找到任务"
            task_db_id = int(row["id"])
            if step in camera_steps:
                st = int(row["task_status"] or 0)
                step_st = int(row.get("download_step_status") or 0) if step == "DOWNLOAD" else int(row.get("transcode_step_status") or 0)
                ctx = self._camera_ctx.get(task_db_id)
                inflight = self._inflight.get(self._task_mem_key(sid, "CameraTask"))
                worker_active = inflight is not None and not inflight[2].done()
                if (st == TaskStatus.RUNNING and step_st == StepStatus.RUNNING) or worker_active:
                    if ctx and isinstance(ctx.get("raw"), dict):
                        previous_cancel = str(ctx["raw"].get("__cancel") or "").strip().lower()
                        ctx["raw"]["__cancel"] = "stop"
                        if previous_cancel and previous_cancel != "stop":
                            self._log.info(
                                "stop %s task=%s overrides cancel mode %s",
                                step.lower(),
                                sid,
                                previous_cancel,
                            )
                    await asyncio.to_thread(self._mark_step_stop_requested, task_db_id, step)
                    self._log.info("stop %s task=%s requested", step.lower(), sid)
                    return True, "已提交终止"
                if step == "TRANSCODE":
                    await asyncio.to_thread(self._stop_and_reset_transcode, task_db_id)
                    sync_ok = await self._sync_reset_step_reports(task_db_id, "TRANSCODE")
                else:
                    await asyncio.to_thread(self._stop_and_reset_task, task_db_id)
                    sync_ok = await self._sync_reset_step_reports(task_db_id, "DOWNLOAD", "TRANSCODE")
                if not sync_ok:
                    return False, "本地已终止，但服务端任务状态重置回写失败"
            else:
                step_st = int(row.get("speech_step_status") or 0) if step == "SPEECH" else (int(row.get("subtitle_step_status") or 0) if step == "SUBTITLE" else int(row.get("analysis_step_status") or 0))
                if int(row["task_status"] or 0) == TaskStatus.RUNNING and step_st == StepStatus.RUNNING:
                    inflight = self._inflight.get(self._task_mem_key(sid, "CourseTask"))
                    ctx = self._camera_ctx.get(task_db_id)
                    if ctx is None or not isinstance(ctx.get("raw"), dict):
                        raw = await asyncio.to_thread(self._load_task_raw_by_dbid, task_db_id)
                        if raw is not None:
                            ctx = self._find_same_lesson_course_ctx(int(raw.get("lessonId") or 0))
                    if ctx and isinstance(ctx.get("raw"), dict):
                        ctx["raw"]["__cancel"] = "stop"
                    if inflight is not None:
                        _, _, inflight_task = inflight
                        with contextlib.suppress(BaseException):
                            await inflight_task
                    sync_ok = await self._sync_reset_step_reports(task_db_id, step)
                    if not sync_ok:
                        return False, "本地已终止，但服务端任务状态重置回写失败"
                    self._log.info("stop %s task=%s (worker reset done)", step.lower(), sid)
                    return True, "已终止"
                raw = await asyncio.to_thread(self._load_task_raw_by_dbid, task_db_id)
                if raw is not None and step == "SPEECH":
                    await asyncio.to_thread(_cleanup_speech_task_files_safe, raw, True)
                reset_ok = await asyncio.to_thread(self._reset_course_task_for_step, sid, step)
                if not reset_ok:
                    return False, "任务不可终止"
                sync_ok = await self._sync_reset_step_reports(task_db_id, step)
                if not sync_ok:
                    return False, "本地已终止，但服务端任务状态重置回写失败"
            self._log.info("stop %s task=%s", step.lower(), sid)
            return True, "已终止"

    async def try_retry_task(self, *, server_id: str, step_code: str) -> tuple[bool, str]:
        step = str(step_code or "").upper()
        camera_steps = {"DOWNLOAD", "TRANSCODE"}
        course_steps = {"SPEECH", "SUBTITLE", "ANALYSIS"}
        if step not in camera_steps | course_steps:
            return False, "仅支持重试有效步骤"
        async with self._start_lock:
            try:
                sid = int(str(server_id).strip())
            except Exception:
                return False, "任务ID不合法"
            self._log.info("retry request task=%s step=%s", sid, step)
            if step in camera_steps:
                row = await asyncio.to_thread(self._find_task_by_server_id, sid)
            else:
                row = await asyncio.to_thread(self._find_course_task_by_server_id, sid)
            if row is None:
                return False, "未找到任务"
            task_db_id = int(row["id"])
            if step == "DOWNLOAD":
                if int(row.get("download_finalize_pending") or 0) == 1:
                    return False, "文件最终提交补偿中，请等待系统自动重试"
                if int(row.get("download_step_status") or 0) != StepStatus.FAILED:
                    return False, "当前步骤非异常状态"
                await asyncio.to_thread(self._stop_and_reset_task, task_db_id)
                sync_ok = await self._sync_reset_step_reports(task_db_id, "DOWNLOAD", "TRANSCODE")
            elif step == "TRANSCODE":
                if int(row.get("transcode_finalize_pending") or 0) == 1:
                    return False, "文件最终提交补偿中，请等待系统自动重试"
                if int(row.get("transcode_step_status") or 0) != StepStatus.FAILED:
                    return False, "当前步骤非异常状态"
                await asyncio.to_thread(self._stop_and_reset_transcode, task_db_id)
                sync_ok = await self._sync_reset_step_reports(task_db_id, "TRANSCODE")
            elif step == "SPEECH":
                if int(row.get("speech_step_status") or 0) != StepStatus.FAILED:
                    return False, "当前步骤非异常状态"
                raw = await asyncio.to_thread(self._load_task_raw_by_dbid, task_db_id)
                if raw is not None:
                    await asyncio.to_thread(_cleanup_speech_task_files_safe, raw, True)
                reset_ok = await asyncio.to_thread(self._reset_course_task_for_step, sid, step)
                if not reset_ok:
                    return False, "任务不可重试"
                sync_ok = await self._sync_reset_step_reports(task_db_id, step)
            elif step == "SUBTITLE":
                if int(row.get("subtitle_step_status") or 0) != StepStatus.FAILED:
                    return False, "当前步骤非异常状态"
                reset_ok = await asyncio.to_thread(self._reset_course_task_for_step, sid, step)
                if not reset_ok:
                    return False, "任务不可重试"
                sync_ok = await self._sync_reset_step_reports(task_db_id, step)
            else:
                if int(row.get("analysis_step_status") or 0) != StepStatus.FAILED:
                    return False, "当前步骤非异常状态"
                reset_ok = await asyncio.to_thread(self._reset_course_task_for_step, sid, step)
                if not reset_ok:
                    return False, "任务不可重试"
                sync_ok = await self._sync_reset_step_reports(task_db_id, step)
            if not sync_ok:
                return False, "服务端任务状态重置失败，未启动重试"
        self._log.info("retry reset done task=%s step=%s -> restart", sid, step)
        return await self.try_start_task(server_id=str(sid), step_code=step)

    async def try_rerun_task(self, *, server_id: str, step_code: str) -> tuple[bool, str]:
        step = str(step_code or "").upper()
        camera_steps = {"DOWNLOAD", "TRANSCODE"}
        course_steps = {"SPEECH", "SUBTITLE", "ANALYSIS"}
        if step not in camera_steps | course_steps:
            return False, "仅支持重新执行有效步骤"
        sid = 0
        async with self._start_lock:
            try:
                sid = int(str(server_id).strip())
            except Exception:
                return False, "任务ID不合法"
            self._log.info("rerun request task=%s step=%s", sid, step)
            if step in camera_steps:
                row = await asyncio.to_thread(self._find_task_by_server_id, sid)
            else:
                row = await asyncio.to_thread(self._find_course_task_by_server_id, sid)
            if row is None:
                return False, "未找到任务"
            task_db_id = int(row["id"])
            raw = await asyncio.to_thread(self._load_task_raw_by_dbid, task_db_id)
            if raw is None:
                return False, "未找到任务数据"
            lesson_id = int(raw.get("lessonId") or 0)
            sync_ok = True
            await asyncio.to_thread(self._append_task_log, task_db_id, step, "手动重新执行：已确认清空当前步骤及后续依赖产物，准备重置并重新开始")
            try:
                if step == "DOWNLOAD":
                    if int(row.get("download_finalize_pending") or 0) == 1:
                        return False, "文件最终提交补偿中，请等待系统自动重试"
                    if int(row.get("download_step_status") or 0) != StepStatus.SUCCESS:
                        return False, "当前步骤未完成，无法重新执行"
                    await self._stop_downstream_tasks_before_download_rerun(lesson_id=lesson_id, exclude_task_db_id=task_db_id)
                    await asyncio.to_thread(self._stop_and_reset_task, task_db_id)
                    if lesson_id > 0:
                        await asyncio.to_thread(self._delete_lesson_outputs_for_steps, lesson_id, ["SPEECH", "SUBTITLE", "ANALYSIS"])
                    await asyncio.to_thread(_cleanup_speech_task_files_safe, raw, True)
                    await asyncio.to_thread(self._cleanup_subtitle_outputs, raw, False)
                    await asyncio.to_thread(self._cleanup_analysis_outputs, raw)
                    course_row = await asyncio.to_thread(self._find_course_task_by_server_id, sid)
                    if course_row is not None:
                        await asyncio.to_thread(self._append_task_log, int(course_row["id"]), "SPEECH", "因上游视频重新执行，课次后续步骤已重置，等待重新开始")
                        await asyncio.to_thread(self._reset_course_task_for_step, sid, "SPEECH")
                    sync_ok = await self._sync_reset_step_reports(task_db_id, "DOWNLOAD", "TRANSCODE")
                elif step == "TRANSCODE":
                    if int(row.get("transcode_finalize_pending") or 0) == 1:
                        return False, "文件最终提交补偿中，请等待系统自动重试"
                    if int(row.get("transcode_step_status") or 0) != StepStatus.SUCCESS:
                        return False, "当前步骤未完成，无法重新执行"
                    await asyncio.to_thread(self._stop_and_reset_transcode, task_db_id)
                    if lesson_id > 0:
                        await asyncio.to_thread(self._delete_lesson_outputs_for_steps, lesson_id, ["SPEECH", "SUBTITLE", "ANALYSIS"])
                    await asyncio.to_thread(_cleanup_speech_task_files_safe, raw, True)
                    await asyncio.to_thread(self._cleanup_subtitle_outputs, raw)
                    await asyncio.to_thread(self._cleanup_analysis_outputs, raw)
                    course_row = await asyncio.to_thread(self._find_course_task_by_server_id, sid)
                    if course_row is not None:
                        await asyncio.to_thread(self._append_task_log, int(course_row["id"]), "SPEECH", "因上游转码重新执行，课次后续步骤已重置，等待重新开始")
                        await asyncio.to_thread(self._reset_course_task_for_step, sid, "SPEECH")
                    sync_ok = await self._sync_reset_step_reports(task_db_id, "TRANSCODE")
                elif step == "SPEECH":
                    if int(row.get("speech_step_status") or 0) != StepStatus.SUCCESS:
                        return False, "当前步骤未完成，无法重新执行"
                    await asyncio.to_thread(_cleanup_speech_task_files_safe, raw, True)
                    await asyncio.to_thread(self._cleanup_subtitle_outputs, raw)
                    await asyncio.to_thread(self._cleanup_analysis_outputs, raw)
                    if lesson_id > 0:
                        await asyncio.to_thread(self._delete_lesson_outputs_for_steps, lesson_id, ["SPEECH", "SUBTITLE", "ANALYSIS"])
                    reset_ok = await asyncio.to_thread(self._reset_course_task_for_step, sid, "SPEECH")
                    if not reset_ok:
                        return False, "任务不可重新执行"
                elif step == "SUBTITLE":
                    if int(row.get("subtitle_step_status") or 0) != StepStatus.SUCCESS:
                        return False, "当前步骤未完成，无法重新执行"
                    await asyncio.to_thread(self._cleanup_subtitle_outputs, raw)
                    await asyncio.to_thread(self._cleanup_analysis_outputs, raw)
                    if lesson_id > 0:
                        await asyncio.to_thread(self._delete_lesson_outputs_for_steps, lesson_id, ["SUBTITLE", "ANALYSIS"])
                    reset_ok = await asyncio.to_thread(self._reset_course_task_for_step, sid, "SUBTITLE")
                    if not reset_ok:
                        return False, "任务不可重新执行"
                else:
                    if int(row.get("analysis_step_status") or 0) != StepStatus.SUCCESS:
                        return False, "当前步骤未完成，无法重新执行"
                    await asyncio.to_thread(self._cleanup_analysis_outputs, raw)
                    if lesson_id > 0:
                        await asyncio.to_thread(self._delete_lesson_outputs_for_steps, lesson_id, ["ANALYSIS"])
                    reset_ok = await asyncio.to_thread(self._reset_course_task_for_step, sid, "ANALYSIS")
                    if not reset_ok:
                        return False, "任务不可重新执行"
            except RerunCleanupError as e:
                return False, str(e)
            if not sync_ok:
                return False, "服务端任务状态重置失败，未启动重新执行"
        self._log.info("rerun reset done task=%s step=%s -> restart", sid, step)
        return await self.try_start_task(server_id=str(sid), step_code=step)

    async def try_reprocess_task(self, *, server_id: str, step_code: str) -> tuple[bool, str]:
        step = str(step_code or "").upper()
        if step != "DOWNLOAD":
            return False, "仅支持下载步骤重新处理"
        sid = 0
        async with self._start_lock:
            try:
                sid = int(str(server_id).strip())
            except Exception:
                return False, "任务ID不合法"
            self._log.info("reprocess request task=%s step=%s", sid, step)
            row = await asyncio.to_thread(self._find_task_by_server_id, sid)
            if row is None:
                return False, "未找到任务"
            task_db_id = int(row["id"])
            if int(row.get("download_finalize_pending") or 0) == 1:
                return False, "文件最终提交补偿中，请等待系统自动重试"
            try:
                await asyncio.to_thread(self._reset_task_for_reprocess, task_db_id)
            except RerunCleanupError as e:
                return False, str(e)
            sync_ok = await self._sync_reset_step_reports(task_db_id, "DOWNLOAD", "TRANSCODE")
            if not sync_ok:
                return False, "服务端任务状态重置失败，未启动重新处理"
            ok, msg = await asyncio.to_thread(self._validate_camera_task_ready, sid)
            if not ok:
                return False, msg
            if not self._can_start_camera_step("DOWNLOAD"):
                return False, "下载并发已达上限"
            claimed = await asyncio.to_thread(self._claim_specific_task, self._cfg.edge_id, sid, "DOWNLOAD", "CameraTask")
            if claimed is None:
                return False, "任务不可开始（未就绪）"
            if bool(claimed.get("skipped")):
                return True, "已完成"
            claimed_raw = claimed.get("raw")
            if isinstance(claimed_raw, dict):
                claimed_raw["__reprocessDownload"] = "1"
            self._spawn_claimed(claimed, "CameraTask", "DOWNLOAD")
        self._log.info("reprocess reset done task=%s step=%s -> process-only restart", sid, step)
        return True, "开始重新处理"

    def _on_progress_db(self, task_db_id: int, stage: str, progress: float, message: str) -> None:
        p = int(max(0.0, min(1.0, float(progress))) * 100.0)
        step_code = str(stage or "").upper()
        if step_code == "TRANSCODE" and float(progress) > 0.0 and p == 0:
            p = 1
        defer_final_report = p >= 100 and self._should_defer_final_step_report(int(task_db_id), step_code)
        log_key = (int(task_db_id), step_code)
        now = time.monotonic()
        last_logged_at = float(self._last_progress_log_ts.get(log_key) or 0.0)
        if p in {0, 100} or (now - last_logged_at) >= float(self._progress_log_interval_sec):
            self._last_progress_log_ts[log_key] = now
            server_task_id = self._server_task_id_for_db_log(task_db_id)
            self._log.info("task=%s stage=%s progress=%s%% %s", server_task_id, stage, p, message)

        def _log_progress_warning(kind: str, template: str, *args: object) -> None:
            warn_key = (int(task_db_id), step_code, str(kind))
            last_warn_at = float(self._last_progress_warning_ts.get(warn_key) or 0.0)
            if (now - last_warn_at) >= float(self._progress_log_interval_sec):
                self._last_progress_warning_ts[warn_key] = now
                self._log.warning(template, *args)

        if p == 0 and str(stage).upper() in {"TRANSCODE", "SPEECH", "SUBTITLE", "ANALYSIS"}:
            try:
                ctx = self._camera_ctx.get(int(task_db_id))
                if ctx is not None:
                    raw = ctx.get("raw") or {}
                    tid_str = str(raw.get("taskId") or raw.get("id") or "").strip()
                    if tid_str:
                        self._inflight_update_step(tid_str, str(stage).upper())
            except Exception:
                pass
        db_write_key = (int(task_db_id), step_code)
        last_db_write = self._last_progress_db_write.get(db_write_key)
        if p not in {0, 100} and last_db_write is not None:
            last_db_write_at, last_db_progress = last_db_write
            if int(last_db_progress) == int(p) and (now - float(last_db_write_at)) < float(self._progress_db_interval_sec):
                return
        allow_progress_rollback = False
        effective_progress = int(p)
        max_attempts = 2 if p in {0, 100} else 1
        for attempt in range(1, max_attempts + 1):
            try:
                with self._db.connect() as conn:
                    task_row = conn.execute(
                        "SELECT task_kind, current_step FROM edge_stream_task WHERE id=? LIMIT 1",
                        (int(task_db_id),),
                    ).fetchone()
                    current_step = str(task_row["current_step"] or "").upper() if task_row is not None else ""
                    task_kind = str(task_row["task_kind"] or "") if task_row is not None else ""
                    step_orders = {
                        "CameraTask": {"DOWNLOAD": 0, "TRANSCODE": 1},
                        "CourseTask": {"SPEECH": 0, "SUBTITLE": 1, "ANALYSIS": 2},
                    }
                    order_map = step_orders.get(task_kind, {})
                    current_order = order_map.get(current_step, -1)
                    incoming_order = order_map.get(step_code, -1)
                    if current_order >= 0 and incoming_order >= 0 and incoming_order < current_order:
                        self._log.warning(
                            "ignore stale progress task=%s current_step=%s incoming_step=%s progress=%s%%",
                            self._server_task_id_for_db_log(task_db_id), current_step, step_code, p,
                        )
                        return
                    step_row = conn.execute(
                        "SELECT step_status, step_process FROM edge_stream_task_step WHERE task_id=? AND step_code=? LIMIT 1",
                        (int(task_db_id), str(stage)),
                    ).fetchone()
                    current_step_status = int(step_row["step_status"] or 0) if step_row is not None else 0
                    current_step_process = int(step_row["step_process"] or 0) if step_row is not None else 0
                    try:
                        ctx = self._camera_ctx.get(int(task_db_id))
                        raw_ctx = ctx.get("raw") if isinstance(ctx, dict) else None
                        cancel_mode = str(raw_ctx.get("__cancel") or "").strip().lower() if isinstance(raw_ctx, dict) else ""
                    except Exception:
                        cancel_mode = ""
                    if cancel_mode in {"pause", "stop"}:
                        self._log.info(
                            "ignore progress after cancel request task=%s step=%s mode=%s progress=%s%%",
                            self._server_task_id_for_db_log(task_db_id), step_code, cancel_mode, p,
                        )
                        return
                    if current_step_status == StepStatus.PAUSED and p < 100:
                        self._log.info(
                            "ignore progress for paused step task=%s step=%s progress=%s%%",
                            self._server_task_id_for_db_log(task_db_id), step_code, p,
                        )
                        return
                    allow_progress_rollback = False
                    rollback_reason = ""
                    try:
                        ctx = self._camera_ctx.get(int(task_db_id))
                        raw_ctx = ctx.get("raw") if isinstance(ctx, dict) else None
                        raw_rollback = str(raw_ctx.get("__allowProgressRollback") or "").strip().upper() if isinstance(raw_ctx, dict) else ""
                        if raw_rollback == step_code:
                            allow_progress_rollback = True
                            rollback_reason = str(message or "").strip()
                    except Exception:
                        allow_progress_rollback = False
                    effective_progress = int(p if allow_progress_rollback else max(p, current_step_process))
                    skip_numeric = False
                    if current_step_status == StepStatus.SUCCESS and p < 100:
                        _log_progress_warning(
                            "completed_regressive",
                            "ignore regressive progress for completed step task=%s step=%s progress=%s%% stored=%s%%",
                            self._server_task_id_for_db_log(task_db_id), step_code, p, current_step_process,
                        )
                        skip_numeric = True
                    elif p < current_step_process and not allow_progress_rollback:
                        _log_progress_warning(
                            "non_monotonic",
                            "ignore non-monotonic progress task=%s step=%s progress=%s%% stored=%s%%",
                            self._server_task_id_for_db_log(task_db_id), step_code, p, current_step_process,
                        )
                        skip_numeric = True
                    elif p < current_step_process and allow_progress_rollback:
                        self._log.info(
                            "allow progress rollback task=%s step=%s progress=%s%% stored=%s%% reason=%s",
                            self._server_task_id_for_db_log(task_db_id), step_code, p, current_step_process, rollback_reason,
                        )
                    if not skip_numeric:
                        conn.execute(
                            "UPDATE edge_stream_task SET current_step=?, process_rate=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                            (str(stage), p, int(task_db_id)),
                        )
                        conn.execute(
                            "UPDATE edge_stream_task_step SET step_process=?, updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=?",
                            (p, int(task_db_id), str(stage)),
                        )
                        if p >= 100:
                            conn.execute(
                                "UPDATE edge_stream_task_step SET step_status=2, end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=?",
                                (int(task_db_id), str(stage)),
                            )
                            nxt = {"DOWNLOAD": "TRANSCODE", "SPEECH": "SUBTITLE", "SUBTITLE": "ANALYSIS"}.get(str(stage), "")
                            if task_kind == "CameraTask" and str(stage).upper() == "DOWNLOAD":
                                nxt = ""
                            if nxt:
                                conn.execute(
                                    "UPDATE edge_stream_task_step SET step_status=1, start_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=? AND step_status=0",
                                    (int(task_db_id), nxt),
                                )
                            if str(stage) == "DOWNLOAD":
                                try:
                                    ctx = self._camera_ctx.get(int(task_db_id))
                                    if ctx and isinstance(ctx.get("raw"), dict):
                                        raw = ctx["raw"]
                                        stid_int = int(str(raw.get("taskId") or raw.get("id") or "").strip())
                                        lid = int(raw.get("lessonId") or 0)
                                        tt = int(raw.get("taskType") or 0)
                                        pfx = _task_type_prefix_util(tt)
                                        ld = _resolve_lesson_date_util(raw.get("lessonDate"), raw.get("lessonStartAt"), raw.get("downloadStart"))
                                        od2 = _get_lesson_dir_util(ld, str(lid))
                                        mp4 = od2 / f"{pfx}_{stid_int}.mp4"
                                        if mp4.exists():
                                            fp2 = str(mp4)
                                            sz2 = int(mp4.stat().st_size)
                                            conn.execute(
                                                "UPDATE edge_stream_task_step SET output_file_path=? WHERE task_id=? AND step_code='DOWNLOAD' AND output_file_path IS NULL",
                                                (fp2, int(task_db_id)),
                                            )
                                            ex = conn.execute(
                                                "SELECT id FROM edge_lesson_output WHERE lesson_id=? AND server_task_id=? AND file_type='source_video'",
                                                (lid, stid_int),
                                            ).fetchone()
                                            if not ex:
                                                conn.execute(
                                                    "INSERT INTO edge_lesson_output(lesson_id, server_task_id, file_type, file_path, file_size) VALUES (?,?,?,?,?)",
                                                    (lid, stid_int, "source_video", fp2, sz2),
                                                )
                                except Exception:
                                    self._log.warning("save lesson_output source_video failed task=%s", self._server_task_id_for_db_log(task_db_id), exc_info=True)
                            if str(stage) == "TRANSCODE":
                                try:
                                    self._try_delayed_subtitle_mount(task_db_id, conn)
                                except Exception:
                                    self._log.debug("delayed subtitle mount check failed", exc_info=True)
                                try:
                                    task_row = conn.execute(
                                        "SELECT lesson_id, task_kind, task_type FROM edge_stream_task WHERE id=?",
                                        (int(task_db_id),),
                                    ).fetchone()
                                    if task_row is not None and str(task_row["task_kind"] or "") == "CameraTask" and int(task_row["task_type"] or 0) == 3:
                                        self._sync_visible_type3_course_tasks(conn, int(task_row["lesson_id"] or 0))
                                except Exception:
                                    self._log.debug("sync visible type3 course tasks failed", exc_info=True)
                    if p in {0, 100} or p % 25 == 0:
                        conn.execute(
                            "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                            (int(task_db_id), str(stage), "INFO", str(message or "")),
                        )
                    else:
                        last_log = conn.execute(
                            "SELECT id FROM edge_task_log WHERE task_id=? AND step_code=? ORDER BY id DESC LIMIT 1",
                            (int(task_db_id), str(stage)),
                        ).fetchone()
                        if last_log:
                            conn.execute("UPDATE edge_task_log SET message=? WHERE id=?", (str(message or ""), int(last_log["id"])))
                        else:
                            conn.execute(
                                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                                (int(task_db_id), str(stage), "INFO", str(message or "")),
                            )
                    conn.commit()
                self._last_progress_db_write[db_write_key] = (time.monotonic(), int(p))
                break
            except sqlite3.OperationalError as e:
                if not _is_sqlite_busy_error(e):
                    raise
                if attempt < max_attempts:
                    self._log.warning(
                        "progress db busy, retrying task=%s step=%s progress=%s%% attempt=%s/%s err=%s",
                        self._server_task_id_for_db_log(task_db_id), step_code, p, attempt, max_attempts, e,
                    )
                    time.sleep(1.0)
                    continue
                self._log.warning(
                    "progress db busy, skipped non-fatal progress update task=%s step=%s progress=%s%% attempts=%s err=%s",
                    self._server_task_id_for_db_log(task_db_id), step_code, p, max_attempts, e,
                )
                return

        if allow_progress_rollback:
            try:
                ctx = self._camera_ctx.get(int(task_db_id))
                raw_ctx = ctx.get("raw") if isinstance(ctx, dict) else None
                if isinstance(raw_ctx, dict) and str(raw_ctx.get("__allowProgressRollback") or "").strip().upper() == step_code:
                    raw_ctx["__allowProgressRollback"] = ""
            except Exception:
                pass

        if not defer_final_report:
            try:
                self._mark_step_report_dirty(int(task_db_id), str(stage))
            except Exception:
                pass

        try:
            self._sync_lesson_step_progress(int(task_db_id), str(stage), effective_progress, str(message or ""))
        except Exception:
            pass

        rkey = (int(task_db_id), str(stage))
        now = time.monotonic()
        do_report = False
        is_final = False
        if p == 0:
            self._last_report_ts[rkey] = now
            do_report = True
        elif p >= 100:
            self._last_report_ts.pop(rkey, None)
            do_report = not defer_final_report
            is_final = not defer_final_report
        else:
            last = self._last_report_ts.get(rkey, 0.0)
            if now - last >= self._report_interval_sec:
                self._last_report_ts[rkey] = now
                do_report = True

        if do_report:
            self._fire_step_report(int(task_db_id), str(stage), is_final=is_final)
            try:
                self._fire_same_lesson_reports(int(task_db_id), str(stage), is_final=is_final)
            except Exception:
                pass

    def _try_delayed_subtitle_mount(self, task_db_id: int, conn) -> None:
        t = conn.execute(
            "SELECT server_task_id, task_type, lesson_id, lesson_date, task_kind FROM edge_stream_task WHERE id=?",
            (int(task_db_id),),
        ).fetchone()
        if t is None or str(t["task_kind"]) != "CameraTask":
            return
        lesson_id = int(t["lesson_id"] or 0)
        if lesson_id <= 0:
            return
        lock = conn.execute("SELECT speech_done, subtitle_done FROM edge_lesson_lock WHERE lesson_id=?", (lesson_id,)).fetchone()
        if lock is None or int(lock["speech_done"] or 0) != 1:
            return
        task_type = int(t["task_type"] or 0)
        server_task_id = int(t["server_task_id"] or 0)
        lesson_date = str(t["lesson_date"] or "").strip()
        try:
            ctx = self._camera_ctx.get(int(task_db_id))
            if ctx and isinstance(ctx.get("raw"), dict):
                raw = ctx["raw"]
                lesson_date = _resolve_lesson_date_util(raw.get("lessonDate"), lesson_date, raw.get("lessonStartAt"))
        except Exception:
            pass
        lesson_dir = _get_lesson_dir_util(lesson_date, str(lesson_id))
        if not lesson_dir.exists():
            return
        srt_path = None
        vtt_path = None
        for f in sorted(lesson_dir.glob("teacher_*.zh.srt")):
            if f.exists() and f.stat().st_size > 0:
                srt_path = f
                break
        for f in sorted(lesson_dir.glob("teacher_*.zh.vtt")):
            if f.exists() and f.stat().st_size > 0:
                vtt_path = f
                break
        if srt_path is None and vtt_path is None:
            return
        pfx = _task_type_prefix_util(task_type)
        video_path = lesson_dir / f"{pfx}_{server_task_id}.mp4"
        if not video_path.exists():
            return
        self._log.info(
            "delayed subtitle mount: task=%s taskType=%s video=%s srt=%s",
            self._server_task_id_for_db_log(task_db_id), task_type, video_path.name, srt_path.name if srt_path else "none",
        )
        try:
            _mount_subtitle_to_video_safe(srt_path, vtt_path, video_path)
        except Exception as e:
            self._log.warning("延迟字幕挂载失败: %s", e)
        course_task = conn.execute(
            "SELECT id FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND server_task_id=?",
            (lesson_id, server_task_id),
        ).fetchone()
        if course_task is None:
            course_task = conn.execute(
                "SELECT id FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND task_type=?",
                (lesson_id, task_type),
            ).fetchone()
        if course_task is not None:
            ct_id = int(course_task["id"])
            for step_code in ("SPEECH", "SUBTITLE"):
                conn.execute(
                    "UPDATE edge_stream_task_step SET step_status=2, step_process=100, end_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE task_id=? AND step_code=? AND step_status IN (0,1)",
                    (ct_id, step_code),
                )
            conn.execute(
                "INSERT INTO edge_task_log(task_id, step_code, log_level, message) VALUES (?,?,?,?)",
                (ct_id, "SUBTITLE", "INFO", f"延迟字幕挂载完成（{video_path.name}）"),
            )
            self._log.info("CourseTask task=%s SPEECH/SUBTITLE 状态已同步（延迟挂载）", self._server_task_id_for_db_log(ct_id))

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._recover_stale_tasks)
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception as e:
                await self._state.set_error(str(e))
                self._log.exception("runner poll error")
            try:
                await self._run_available()
            except Exception as e:
                self._log.exception("runner loop error: %s", e)
            await asyncio.sleep(self._cfg.poll_interval_sec)

    async def _execute_task(self, task: EdgeTask) -> None:
        await self._state.start_task(task.task_id, task.task_kind)
        reporter = asyncio.create_task(self._report_loop())
        artifacts: list[dict[str, Any]] | None = None
        try:
            await self._state.update_running("START", 0.0, "RUNNING")
            if task.task_kind == "CameraTask":
                artifacts = await run_camera_task(task.raw, self._cfg.simulate, self._on_progress)
            elif task.task_kind == "CourseTask":
                artifacts = await run_course_task(task.raw, False, self._on_progress)
            else:
                await self._state.finish_task("FAILED", f"unknown taskKind: {task.task_kind}")
                await self._final_report(artifacts=None)
                return
            await self._state.finish_task("SUCCEEDED", "ok")
            await self._final_report(artifacts=artifacts)
        except Exception as e:
            await self._state.finish_task("FAILED", str(e))
            await self._final_report(artifacts=None)
        finally:
            reporter.cancel()
            with contextlib.suppress(Exception):
                await reporter
            await self._state.clear_running()

    def _on_progress(self, stage: str, progress: float, message: str) -> None:
        asyncio.create_task(self._state.update_running(stage, progress, message))

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.report_interval_sec)
            snap = await self._state.snapshot()
            if snap.running is None:
                return
            await self._report_once(snap.running, artifacts=None)

    async def _final_report(self, artifacts: list[dict[str, Any]] | None) -> None:
        snap = await self._state.snapshot()
        if snap.running is None:
            return
        await self._report_once(snap.running, artifacts=artifacts)

    async def _report_once(self, running, artifacts: list[dict[str, Any]] | None) -> None:
        await self._client.report_task(
            task_id=running.task_id,
            task_kind=running.task_kind,
            status=running.status,
            stage=running.stage,
            progress=running.progress,
            message=running.message,
            artifacts=artifacts,
        )
        await self._state.mark_reported()
