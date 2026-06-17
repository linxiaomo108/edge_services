from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from .db import bjt_now_iso


TaskStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"]


@dataclass
class Artifact:
    path: str
    size_bytes: int


@dataclass
class TaskRuntime:
    task_id: str
    task_kind: str
    status: TaskStatus = "PENDING"
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    artifacts: list[Artifact] = field(default_factory=list)
    started_at: str = field(default_factory=bjt_now_iso)
    updated_at: str = field(default_factory=bjt_now_iso)
    last_report_at: str | None = None

    def set_progress(self, stage: str, progress: float, message: str = "") -> None:
        self.stage = stage
        self.progress = max(0.0, min(1.0, float(progress)))
        self.message = message
        self.updated_at = bjt_now_iso()


@dataclass
class EdgeRuntimeState:
    edge_id: str
    running: TaskRuntime | None = None
    queue: list[dict[str, Any]] = field(default_factory=list)
    last_poll_at: str | None = None
    last_error: str | None = None
    started_at: str = field(default_factory=bjt_now_iso)


class EdgeState:
    def __init__(self, edge_id: str) -> None:
        self._lock = asyncio.Lock()
        self._state = EdgeRuntimeState(edge_id=edge_id)

    async def snapshot(self) -> EdgeRuntimeState:
        async with self._lock:
            return EdgeRuntimeState(
                edge_id=self._state.edge_id,
                running=self._state.running,
                queue=list(self._state.queue),
                last_poll_at=self._state.last_poll_at,
                last_error=self._state.last_error,
                started_at=self._state.started_at,
            )

    async def set_poll_time(self) -> None:
        async with self._lock:
            self._state.last_poll_at = bjt_now_iso()

    async def set_error(self, message: str | None) -> None:
        async with self._lock:
            self._state.last_error = message

    async def enqueue(self, tasks: list[dict[str, Any]]) -> None:
        async with self._lock:
            existing = {str(t.get("taskId") or t.get("id") or "") for t in self._state.queue}
            if self._state.running is not None:
                existing.add(self._state.running.task_id)
            for t in tasks:
                tid = str(t.get("taskId") or t.get("id") or "")
                if tid and tid not in existing:
                    self._state.queue.append(t)
                    existing.add(tid)

    async def pop_next(self) -> dict[str, Any] | None:
        async with self._lock:
            if not self._state.queue:
                return None
            return self._state.queue.pop(0)

    async def start_task(self, task_id: str, task_kind: str) -> None:
        async with self._lock:
            self._state.running = TaskRuntime(task_id=task_id, task_kind=task_kind, status="RUNNING")

    async def update_running(self, stage: str, progress: float, message: str = "") -> None:
        async with self._lock:
            if self._state.running is None:
                return
            self._state.running.set_progress(stage=stage, progress=progress, message=message)

    async def add_artifact(self, path: str, size_bytes: int) -> None:
        async with self._lock:
            if self._state.running is None:
                return
            self._state.running.artifacts.append(Artifact(path=path, size_bytes=int(size_bytes)))
            self._state.running.updated_at = bjt_now_iso()

    async def mark_reported(self) -> None:
        async with self._lock:
            if self._state.running is None:
                return
            self._state.running.last_report_at = bjt_now_iso()

    async def finish_task(self, status: TaskStatus, message: str = "") -> None:
        async with self._lock:
            if self._state.running is None:
                return
            self._state.running.status = status
            self._state.running.message = message
            self._state.running.progress = 1.0 if status == "SUCCEEDED" else self._state.running.progress
            self._state.running.updated_at = bjt_now_iso()

    async def clear_running(self) -> None:
        async with self._lock:
            self._state.running = None

    async def to_public_dict(self) -> dict[str, Any]:
        snap = await self.snapshot()
        running = None
        if snap.running is not None:
            running = {
                "taskId": snap.running.task_id,
                "taskKind": snap.running.task_kind,
                "status": snap.running.status,
                "stage": snap.running.stage,
                "progress": snap.running.progress,
                "message": snap.running.message,
                "artifacts": [{"path": a.path, "sizeBytes": a.size_bytes} for a in snap.running.artifacts],
                "startedAt": snap.running.started_at,
                "updatedAt": snap.running.updated_at,
                "lastReportAt": snap.running.last_report_at,
            }
        return {
            "edgeId": snap.edge_id,
            "startedAt": snap.started_at,
            "lastPollAt": snap.last_poll_at,
            "lastError": snap.last_error,
            "queueSize": len(snap.queue),
            "queue": [
                {
                    "taskId": str(t.get("taskId") or t.get("id") or ""),
                    "taskKind": str(t.get("taskKind") or ""),
                }
                for t in snap.queue
            ],
            "running": running,
        }
