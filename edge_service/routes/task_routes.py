from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse, Response

from ..constants import StepStatus, TaskStatus
from ..utils import (
    is_task_step_enabled as _is_task_step_enabled,
    resolve_lesson_date as _resolve_lesson_date,
    task_type_prefix as _task_type_prefix,
)


def create_task_router(
    *,
    db,
    runner,
    tasks_lock: asyncio.Lock,
    get_lesson_dir: Callable[[str, str], Any],
    start_task_model: type[Any],
) -> APIRouter:
    router = APIRouter()
    StartTaskRequest = start_task_model

    def _label(step_type: str) -> str:
        if step_type == "download":
            return "下载"
        if step_type == "transcode":
            return "转码"
        if step_type == "asr":
            return "语音转写"
        if step_type == "subtitle":
            return "字幕挂载"
        if step_type == "analysis":
            return "视频分析"
        return "任务"

    def _parse_task_ref(task_ref: str) -> tuple[str, str] | None:
        tid = str(task_ref or "").strip()
        if not tid or "-" not in tid:
            return None
        server_id, step = tid.split("-", 1)
        return server_id.strip(), step.strip().upper()

    def _step_completed(step: dict[str, Any]) -> bool:
        status = int(step.get("step_status", 0) or 0)
        process = int(step.get("step_process", 0) or 0)
        output = str(step.get("output_file_path") or "")
        if status != 2:
            return False
        return process >= 100 or bool(output)

    @router.post("/api/tasks")
    async def api_tasks(req: dict[str, Any] = Body(default_factory=dict)):
        def _row_status(step_status: int) -> tuple[str, str]:
            if step_status == 2:
                return "已完成", "completed"
            if step_status == 1:
                return "进行中", "running"
            if step_status == 3:
                return "失败", "error"
            if step_status == 4:
                return "已暂停", "paused"
            return "等待中", "pending"

        def _step_type(step_code: str) -> str:
            c = str(step_code or "").upper()
            if c == "DOWNLOAD":
                return "download"
            if c == "TRANSCODE":
                return "transcode"
            if c == "SPEECH":
                return "asr"
            if c == "SUBTITLE":
                return "subtitle"
            if c == "ANALYSIS":
                return "analysis"
            return "download"

        def _can_reprocess(group: dict[str, Any], step_type: str) -> bool:
            if step_type != "download":
                return False
            try:
                lesson_id = str(group.get("lesson_id") or "").strip() or "0"
                lesson_date = _resolve_lesson_date(group.get("lesson_date"), group.get("download_start"), default="unknown")
                task_type = int(group.get("task_type") or 0)
                server_task_id = str(group.get("server_task_id") or "").strip() or "0"
                prefix = _task_type_prefix(task_type)
                out_dir = Path(get_lesson_dir(str(lesson_date), lesson_id))
                if not out_dir.exists():
                    return False
                for p in out_dir.glob(f"{prefix}_{server_task_id}.part*.mp4"):
                    try:
                        if p.exists() and p.stat().st_size > 0:
                            return True
                    except Exception:
                        continue
                return False
            except Exception:
                return False

        camera_steps = ["DOWNLOAD", "TRANSCODE"]
        course_steps = [step for step in ("SPEECH", "SUBTITLE", "ANALYSIS") if _is_task_step_enabled(step)]
        requested_status = str((req or {}).get("status") or "running").strip().lower()
        if requested_status not in {"pending", "running", "completed", "error", "all"}:
            requested_status = "running"
        requested_type = str((req or {}).get("taskType") or "all").strip().lower()
        if requested_type not in {"all", "download", "transcode", "asr", "subtitle", "analysis"}:
            requested_type = "all"
        query = str((req or {}).get("query") or "").strip().lower()
        effective_status = "all" if query else requested_status
        room_name_filter = str((req or {}).get("roomName") or "").strip()
        completed_range = str((req or {}).get("completedRange") or "7d").strip().lower()
        if completed_range not in {"7d", "30d", "60d", "custom"}:
            completed_range = "7d"
        completed_start_date = str((req or {}).get("completedStartDate") or "").strip()
        completed_end_date = str((req or {}).get("completedEndDate") or "").strip()
        try:
            page = max(1, int((req or {}).get("page") or 1))
        except Exception:
            page = 1
        try:
            page_size = max(1, min(100, int((req or {}).get("pageSize") or 20)))
        except Exception:
            page_size = 20

        def _parse_date(value: str):
            try:
                text = str(value or "").strip()[:10]
                if not text:
                    return None
                return datetime.strptime(text, "%Y-%m-%d").date()
            except Exception:
                return None

        today = datetime.now().date()
        completed_start = None
        completed_end = None
        if requested_status == "completed":
            if completed_range == "custom":
                completed_start = _parse_date(completed_start_date)
                completed_end = _parse_date(completed_end_date)
            else:
                days = 7 if completed_range == "7d" else 30 if completed_range == "30d" else 60
                completed_end = today
                completed_start = today - timedelta(days=days - 1)
            if completed_start and completed_end and completed_start > completed_end:
                completed_start, completed_end = completed_end, completed_start
        where_parts: list[str] = []
        where_params: list[Any] = []
        if query:
            id_prefix = f"{query}%"
            class_like = f"%{query}%"
            where_parts.append("(CAST(t.server_task_id AS TEXT) LIKE ? OR LOWER(COALESCE(t.relate_class, '')) LIKE ?)")
            where_params.extend([id_prefix, class_like])
        if requested_status == "completed" and (completed_start is not None or completed_end is not None):
            completed_step_where = ["x.task_id = t.id", "x.step_status = 2"]
            if completed_start is not None:
                completed_step_where.append("DATE(COALESCE(NULLIF(x.end_time, ''), t.lesson_date)) >= ?")
                where_params.append(completed_start.isoformat())
            if completed_end is not None:
                completed_step_where.append("DATE(COALESCE(NULLIF(x.end_time, ''), t.lesson_date)) <= ?")
                where_params.append(completed_end.isoformat())
            where_parts.append(f"EXISTS (SELECT 1 FROM edge_stream_task_step x WHERE {' AND '.join(completed_step_where)})")
        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        sql = """
WITH latest_log AS (
  SELECT task_id, step_code, MAX(id) AS log_id
  FROM edge_task_log
  GROUP BY task_id, step_code
),
latest_detail_log AS (
  SELECT task_id, step_code, MAX(id) AS log_id
  FROM edge_task_log
  WHERE message NOT IN ('已暂停', '继续执行', '已终止')
    AND message NOT LIKE '开始执行%'
  GROUP BY task_id, step_code
)
SELECT
  t.id AS task_db_id,
  t.server_task_id,
  t.lesson_id,
  t.lesson_date,
  t.room_name,
  t.task_type,
  t.task_kind,
  t.task_status,
  t.current_step,
  t.relate_class,
  t.relate_lesson,
  t.download_start,
  t.download_end,
  t.other_tasks_id,
  t.nvr_device_id,
  t.nvr_ip,
  t.nvr_channel_num,
  s.step_code,
  s.step_status,
  s.step_process,
  s.start_time,
  s.end_time,
  s.output_file_path,
  COALESCE(ll.message, '') AS last_message,
  COALESCE(ldl.message, '') AS detail_message
FROM edge_stream_task t
JOIN edge_stream_task_step s ON s.task_id = t.id
LEFT JOIN latest_log llid ON llid.task_id = t.id AND llid.step_code = s.step_code
LEFT JOIN edge_task_log ll ON ll.id = llid.log_id
LEFT JOIN latest_detail_log ldlid ON ldlid.task_id = t.id AND ldlid.step_code = s.step_code
LEFT JOIN edge_task_log ldl ON ldl.id = ldlid.log_id
{where_sql}
ORDER BY t.server_task_id ASC, t.task_type ASC, s.id ASC
        """.strip().format(where_sql=where_sql)
        rows = await asyncio.to_thread(db.fetch_all, sql, where_params)
        task_groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for row in rows:
            key = f"{row['server_task_id']}_{row['task_type']}_{row['task_kind']}"
            if key not in task_groups:
                task_groups[key] = {
                    "server_task_id": row["server_task_id"],
                    "task_type": int(row["task_type"] or 0),
                    "task_kind": str(row["task_kind"] or ""),
                    "task_status": int(row["task_status"] or 0),
                    "current_step": str(row["current_step"] or "").upper(),
                    "lesson_id": row["lesson_id"],
                    "lesson_date": str(row["lesson_date"] or ""),
                    "room_name": str(row["room_name"] or "").strip(),
                    "relate_class": str(row["relate_class"] or "-") or "-",
                    "relate_lesson": str(row["relate_lesson"] or "-") or "-",
                    "download_start": str(row["download_start"] or ""),
                    "download_end": str(row["download_end"] or ""),
                    "other_tasks_id": str(row["other_tasks_id"] or ""),
                    "nvr_device_id": row["nvr_device_id"],
                    "nvr_ip": row["nvr_ip"],
                    "nvr_channel_num": row["nvr_channel_num"],
                    "steps": [],
                }
            task_groups[key]["steps"].append(
                {
                    "step_code": str(row["step_code"] or ""),
                    "step_status": int(row["step_status"] or 0),
                    "step_process": int(row["step_process"] or 0),
                    "start_time": str(row["start_time"] or ""),
                    "end_time": str(row["end_time"] or ""),
                    "output_file_path": str(row["output_file_path"] or ""),
                    "last_message": str(row["last_message"] or "").strip(),
                    "detail_message": str(row["detail_message"] or "").strip(),
                }
            )

        def _state_rank(task_status: int, current_step: str, target_step: str, step_status: int) -> int:
            if str(current_step or "").upper() == str(target_step or "").upper() and int(task_status or 0) == 1:
                return 500
            if int(step_status or 0) == 4:
                return 400
            if int(step_status or 0) == 3:
                return 300
            if int(step_status or 0) == 1:
                return 250
            if int(step_status or 0) == 2:
                return 200
            return 100

        lesson_step_shadow: dict[tuple[int, str], dict[str, Any]] = {}
        for group in task_groups.values():
            if str(group.get("task_kind") or "") != "CourseTask":
                continue
            lesson_id = int(group.get("lesson_id") or 0)
            current_step = str(group.get("current_step") or "").upper()
            task_status = int(group.get("task_status") or 0)
            for step in group.get("steps") or []:
                step_code = str(step.get("step_code") or "").upper()
                step_status = int(step.get("step_status") or 0)
                step_process = int(step.get("step_process") or 0)
                rank = _state_rank(task_status, current_step, step_code, step_status)
                candidate = {
                    "task_status": TaskStatus.RUNNING if rank == 500 else task_status,
                    "current_step": step_code if rank >= 400 else current_step,
                    "step_status": StepStatus.RUNNING if rank == 500 else step_status,
                    "step_process": step_process,
                    "last_message": str(step.get("last_message") or "").strip(),
                    "detail_message": str(step.get("detail_message") or "").strip(),
                    "start_time": str(step.get("start_time") or ""),
                    "end_time": str(step.get("end_time") or ""),
                    "output_file_path": str(step.get("output_file_path") or ""),
                    "rank": rank,
                }
                key2 = (lesson_id, step_code)
                current = lesson_step_shadow.get(key2)
                if current is None or rank > int(current.get("rank") or 0) or (rank == int(current.get("rank") or 0) and step_process >= int(current.get("step_process") or 0)):
                    lesson_step_shadow[key2] = candidate

        def _make_row(group: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
            sc = step["step_code"]
            step_type = _step_type(sc)
            step_status = int(step["step_status"] or 0)
            status_text, status_type = _row_status(step_status)
            room_name = str(group.get("room_name") or "").strip()
            display_room = ((room_name if room_name.endswith("教室") else f"{room_name}教室") if room_name else _label(step_type))
            current_step = str(group.get("current_step") or "").upper()
            task_status = int(group.get("task_status") or 0)
            if current_step == sc and task_status == 1 and step_status != 2:
                if step_status == 4:
                    status_text, status_type = "已暂停", "paused"
                else:
                    status_text, status_type = "进行中", "running"
            last_msg = step["last_message"]
            detail_msg = str(step.get("detail_message") or "").strip() or last_msg
            if status_type == "running" and step_type == "download":
                display_status = "下载中"
            elif status_type == "running" and step_type == "transcode":
                display_status = "转码中"
            elif status_type == "running" and step_type == "asr":
                display_status = "语音转写中"
            elif status_type == "running" and step_type == "subtitle":
                display_status = "字幕挂载中"
            elif status_type == "running" and step_type == "analysis":
                display_status = "分析中"
            elif status_type == "error" and ("NVR设备掉线" in detail_msg or "NVR设备连接失败" in detail_msg):
                display_status = detail_msg
            else:
                display_status = last_msg if (status_type == "running" and last_msg) else status_text
            return {
                "id": f"{group['server_task_id']}-{sc}",
                "displayId": f"{group['server_task_id']}-{display_room}",
                "type": step_type,
                "name": _label(step_type),
                "classInfo": group["relate_class"],
                "lessonInfo": group["relate_lesson"],
                "lessonId": str(group["lesson_id"] or ""),
                "lessonDate": str(group.get("lesson_date") or ""),
                "roomName": room_name,
                "lessonStartAt": group["download_start"],
                "lessonEndAt": group["download_end"],
                "taskType": group["task_type"],
                "status": display_status,
                "statusType": status_type,
                "progress": step["step_process"],
                "startTime": step["start_time"],
                "endTime": step["end_time"],
                "detail": detail_msg if step_type in ("download", "transcode", "asr", "subtitle", "analysis") else "",
                "outputFilePath": step["output_file_path"],
                "serverTaskId": str(group["server_task_id"] or ""),
                "canReprocess": _can_reprocess(group, step_type),
                "downloadMeta": {
                    "serverTaskId": str(group["server_task_id"] or ""),
                    "nvrId": str(group["nvr_device_id"] or ""),
                    "nvrAddress": str(group["nvr_ip"] or ""),
                    "nvrChannel": str(group["nvr_channel_num"] or ""),
                    "lessonStartAt": group["download_start"],
                    "lessonEndAt": group["download_end"],
                    "otherTasksId": str(group.get("other_tasks_id") or ""),
                }
                if step_type == "download"
                else None,
            }

        def _group_visible(group: dict[str, Any]) -> bool:
            if str(group["task_kind"] or "") != "CourseTask":
                return True
            step_map = {s["step_code"]: s for s in group.get("steps") or []}
            speech_step = step_map.get("SPEECH", {})
            speech_status = int(speech_step.get("step_status", 0) or 0)
            if int(group["task_type"] or 0) in (1, 2) and speech_status == 0:
                camera_key = f"{group['server_task_id']}_{group['task_type']}_CameraTask"
                camera_group = task_groups.get(camera_key)
                if not camera_group:
                    return False
                camera_step_map = {s["step_code"]: s for s in camera_group["steps"]}
                if not _step_completed(camera_step_map.get("DOWNLOAD", {})):
                    return False
                if not _step_completed(camera_step_map.get("TRANSCODE", {})):
                    return False
            if int(group["task_type"] or 0) != 3:
                return True
            camera_key = f"{group['server_task_id']}_{group['task_type']}_CameraTask"
            camera_group = task_groups.get(camera_key)
            if not camera_group:
                return False
            camera_step_map = {s["step_code"]: s for s in camera_group["steps"]}
            return _step_completed(camera_step_map.get("DOWNLOAD", {})) and _step_completed(camera_step_map.get("TRANSCODE", {}))

        tasks: list[dict[str, Any]] = []
        for group in task_groups.values():
            if not _group_visible(group):
                continue
            step_map = {s["step_code"]: s for s in group["steps"]}
            task_kind = group["task_kind"]
            step_order = camera_steps if task_kind == "CameraTask" else course_steps
            download_step = step_map.get("DOWNLOAD", {}) if task_kind == "CameraTask" else {}
            transcode_step = step_map.get("TRANSCODE", {}) if task_kind == "CameraTask" else {}
            download_done = _step_completed(download_step) if task_kind == "CameraTask" else True
            current_step = str(group.get("current_step") or "").upper()
            if task_kind == "CameraTask" and download_done and int(transcode_step.get("step_status", 0) or 0) in (1, 4):
                current_step = "TRANSCODE"
            task_status = int(group.get("task_status") or 0)
            completed: list[dict[str, Any]] = []
            current_row = None
            failed_row = None
            first_pending = None
            for step_code in step_order:
                step = step_map.get(step_code)
                if step is None:
                    continue
                if task_kind == "CameraTask" and step_code == "TRANSCODE" and not download_done:
                    continue
                step_for_display = dict(step)
                if task_kind == "CourseTask":
                    shadow = lesson_step_shadow.get((int(group.get("lesson_id") or 0), str(step_code).upper()))
                    if shadow is not None:
                        step_for_display.update(
                            {
                                "step_status": int(shadow.get("step_status") or step_for_display.get("step_status") or 0),
                                "step_process": int(shadow.get("step_process") or step_for_display.get("step_process") or 0),
                                "last_message": str(shadow.get("last_message") or step_for_display.get("last_message") or "").strip(),
                                "detail_message": str(shadow.get("detail_message") or step_for_display.get("detail_message") or "").strip(),
                                "start_time": str(shadow.get("start_time") or step_for_display.get("start_time") or ""),
                                "end_time": str(shadow.get("end_time") or step_for_display.get("end_time") or ""),
                                "output_file_path": str(shadow.get("output_file_path") or step_for_display.get("output_file_path") or ""),
                            }
                        )
                if task_kind == "CameraTask" and step_code == "DOWNLOAD" and download_done:
                    step_for_display["step_status"] = 2
                step_status = int(step_for_display.get("step_status") or 0)
                if step_status == 2 and not _step_completed(step_for_display):
                    step_status = 1
                    step_for_display["step_status"] = 1
                row_group = dict(group)
                if task_kind == "CourseTask":
                    shadow = lesson_step_shadow.get((int(group.get("lesson_id") or 0), str(step_code).upper()))
                    if shadow is not None:
                        row_group["task_status"] = int(shadow.get("task_status") or row_group.get("task_status") or 0)
                        row_group["current_step"] = str(shadow.get("current_step") or row_group.get("current_step") or "").upper()
                row_current_step = str(row_group.get("current_step") or current_step).upper()
                row_task_status = int(row_group.get("task_status") or task_status)
                row_data = _make_row(row_group, step_for_display)
                if step_status == 2:
                    completed.append(row_data)
                    continue
                if row_current_step == step_code and row_task_status == 1:
                    current_row = row_data
                    continue
                if step_status in (1, 4):
                    current_row = row_data
                    continue
                if step_status == 3 and failed_row is None:
                    failed_row = row_data
                    continue
                if step_status == 0 and first_pending is None:
                    first_pending = row_data
            tasks.extend(completed)
            if current_row is not None:
                tasks.append(current_row)
                continue
            if failed_row is not None:
                tasks.append(failed_row)
                continue
            if first_pending is not None:
                tasks.append(first_pending)

        status_counts = {
            "pending": sum(1 for t in tasks if str(t.get("statusType") or "") == "pending"),
            "running": sum(1 for t in tasks if str(t.get("statusType") or "") in {"running", "paused", "error"}),
            "completed": sum(1 for t in tasks if str(t.get("statusType") or "") == "completed"),
            "error": sum(1 for t in tasks if str(t.get("statusType") or "") == "error"),
            "all": len(tasks),
        }

        def _matches_requested_status(item: dict[str, Any]) -> bool:
            item_status = str(item.get("statusType") or "")
            if effective_status == "all":
                return True
            if effective_status == "running":
                return item_status in {"running", "paused", "error"}
            return item_status == effective_status

        def _matches_requested_type(item: dict[str, Any]) -> bool:
            if requested_type == "all":
                return True
            return str(item.get("type") or "").strip().lower() == requested_type

        def _matches_query(item: dict[str, Any]) -> bool:
            if not query:
                return True
            values = [
                item.get("serverTaskId"),
                item.get("id"),
                item.get("displayId"),
                item.get("classInfo"),
            ]
            return any(query in str(value or "").strip().lower() for value in values)

        def _matches_completed_date(item: dict[str, Any]) -> bool:
            if effective_status != "completed":
                return True
            if completed_start is None and completed_end is None:
                return True
            item_date = _parse_date(str(item.get("endTime") or item.get("lessonDate") or ""))
            if item_date is None:
                return False
            if completed_start is not None and item_date < completed_start:
                return False
            if completed_end is not None and item_date > completed_end:
                return False
            return True

        def _matches_room(item: dict[str, Any]) -> bool:
            if not room_name_filter:
                return True
            return str(item.get("roomName") or "").strip() == room_name_filter

        def _time_sort_key(value: Any) -> str:
            return str(value or "").strip()

        scoped_tasks = [t for t in tasks if _matches_requested_status(t) and _matches_requested_type(t) and _matches_completed_date(t) and _matches_query(t)]
        room_options = sorted({str(t.get("roomName") or "").strip() for t in scoped_tasks if str(t.get("roomName") or "").strip()})
        filtered_tasks = [t for t in scoped_tasks if _matches_room(t)]
        if effective_status == "pending":
            filtered_tasks.sort(
                key=lambda item: (
                    _time_sort_key(item.get("lessonDate")),
                    _time_sort_key(item.get("lessonStartAt") or item.get("startTime")),
                    _time_sort_key(item.get("lessonEndAt") or item.get("endTime")),
                    str(item.get("id") or ""),
                ),
            )
        elif effective_status == "completed":
            filtered_tasks.sort(
                key=lambda item: (
                    _time_sort_key(item.get("endTime")),
                    _time_sort_key(item.get("startTime")),
                    str(item.get("id") or ""),
                ),
                reverse=True,
            )
        else:
            filtered_tasks.sort(
                key=lambda item: (
                    _time_sort_key(item.get("startTime") or item.get("lessonStartAt")),
                    _time_sort_key(item.get("endTime")),
                    str(item.get("id") or ""),
                ),
                reverse=True,
            )
        total = len(filtered_tasks)
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages
        start = max(0, (page - 1) * page_size)
        paged_tasks = filtered_tasks[start:start + page_size]

        payload = json.dumps(
            {
                "ok": True,
                "tasks": paged_tasks,
                "page": page,
                "pageSize": page_size,
                "total": total,
                "totalPages": total_pages,
                "status": requested_status,
                "taskType": requested_type,
                "statusCounts": status_counts,
                "roomOptions": room_options,
                "query": query,
                "roomName": room_name_filter,
                "completedRange": completed_range,
                "completedStartDate": completed_start.isoformat() if completed_start is not None else "",
                "completedEndDate": completed_end.isoformat() if completed_end is not None else "",
            },
            ensure_ascii=False,
        )
        async with tasks_lock:
            return Response(content=payload, media_type="application/json")

    @router.post("/api/task/start")
    async def api_task_start(req: dict[str, Any] = Body(...)):
        parsed = _parse_task_ref(req.get("id"))
        if parsed is None:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id, step = parsed
        ok, msg = await runner.try_start_task(server_id=server_id, step_code=step)
        return JSONResponse({"ok": ok, "message": msg})

    @router.post("/api/task/pause")
    async def api_task_pause(req: dict[str, Any] = Body(...)):
        parsed = _parse_task_ref(req.get("id"))
        if parsed is None:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id, step = parsed
        ok, msg = await runner.try_pause_task(server_id=server_id, step_code=step)
        return JSONResponse({"ok": ok, "message": msg})

    @router.post("/api/task/resume")
    async def api_task_resume(req: dict[str, Any] = Body(...)):
        parsed = _parse_task_ref(req.get("id"))
        if parsed is None:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id, step = parsed
        ok, msg = await runner.try_resume_task(server_id=server_id, step_code=step)
        return JSONResponse({"ok": ok, "message": msg})

    @router.post("/api/task/stop")
    async def api_task_stop(req: dict[str, Any] = Body(...)):
        parsed = _parse_task_ref(req.get("id"))
        if parsed is None:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id, step = parsed
        ok, msg = await runner.try_stop_task(server_id=server_id, step_code=step)
        return JSONResponse({"ok": ok, "message": msg})

    @router.post("/api/task/retry")
    async def api_task_retry(req: dict[str, Any] = Body(...)):
        parsed = _parse_task_ref(req.get("id"))
        if parsed is None:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id, step = parsed
        ok, msg = await runner.try_retry_task(server_id=server_id, step_code=step)
        return JSONResponse({"ok": ok, "message": msg})

    @router.post("/api/task/rerun")
    async def api_task_rerun(req: dict[str, Any] = Body(...)):
        parsed = _parse_task_ref(req.get("id"))
        if parsed is None:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id, step = parsed
        ok, msg = await runner.try_rerun_task(server_id=server_id, step_code=step)
        return JSONResponse({"ok": ok, "message": msg})

    @router.post("/api/task/reprocess")
    async def api_task_reprocess(req: dict[str, Any] = Body(...)):
        parsed = _parse_task_ref(req.get("id"))
        if parsed is None:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id, step = parsed
        ok, msg = await runner.try_reprocess_task(server_id=server_id, step_code=step)
        return JSONResponse({"ok": ok, "message": msg})

    @router.post("/api/task/open-folder")
    async def api_task_open_folder(req: dict[str, Any] = Body(...)):
        parsed = _parse_task_ref(req.get("id"))
        if parsed is None:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id, step = parsed
        if step not in ("DOWNLOAD", "TRANSCODE"):
            return JSONResponse({"ok": False, "message": "仅支持下载/转码任务"}, status_code=400)
        try:
            sid = int(server_id)
        except Exception:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        row = await asyncio.to_thread(
            lambda: db.fetch_one(
                "SELECT t.id, t.lesson_id, t.lesson_date, t.download_start, t.task_type, t.server_task_id, s.output_file_path "
                "FROM edge_stream_task t "
                "LEFT JOIN edge_stream_task_step s ON s.task_id=t.id AND s.step_code=? "
                "WHERE t.server_task_id=? ORDER BY t.id DESC LIMIT 1",
                (step, int(sid)),
            )
        )
        if row is None:
            return JSONResponse({"ok": False, "message": "未找到任务"}, status_code=404)
        output_file_path = str(row["output_file_path"] or "").strip()
        out_dir: Path | Any
        if output_file_path:
            output_path = Path(output_file_path)
            out_dir = output_path.parent if output_path.suffix else output_path
        else:
            lesson_id = str(row["lesson_id"] or "").strip() or "0"
            lesson_date = _resolve_lesson_date(row["lesson_date"], row["download_start"], default="unknown")
            out_dir = get_lesson_dir(lesson_date, lesson_id)
            if step == "TRANSCODE":
                task_type = int(row["task_type"] or 0)
                stid = str(row["server_task_id"] or "")
                prefix = _task_type_prefix(task_type)
                out_dir = out_dir / f"{prefix}_{stid}_1080P"
        try:
            os.makedirs(str(out_dir), exist_ok=True)
            try:
                os.startfile(str(out_dir))
            except Exception:
                try:
                    subprocess.Popen(["explorer", str(out_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
        except Exception:
            return JSONResponse({"ok": False, "message": "目录不可用", "path": str(out_dir)})
        return JSONResponse({"ok": True, "path": str(out_dir)})

    return router
