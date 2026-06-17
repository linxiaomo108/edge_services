from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from ..utils import is_task_step_enabled

log = logging.getLogger("edge.course")


async def run_course_task(
    raw: dict[str, Any],
    simulate: bool,
    on_progress,
    on_step_complete: Callable[[str, list[dict[str, Any]]], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    all_artifacts: list[dict[str, Any]] = []

    start_step = str(raw.get("__startStep") or "").strip().upper()
    step_order = ["SPEECH", "SUBTITLE", "ANALYSIS"]
    start_idx = step_order.index(start_step) if start_step in step_order else 0

    # ---------- SPEECH ----------
    if start_idx <= 0:
        if is_task_step_enabled("SPEECH"):
            from .speech import run_speech_task

            speech_artifacts = await run_speech_task(raw, on_progress)
            all_artifacts.extend(speech_artifacts)
            if on_step_complete is not None:
                await on_step_complete("SPEECH", speech_artifacts)
        else:
            on_progress("SPEECH", 1.0, "SPEECH 已关闭，跳过执行")
            if on_step_complete is not None:
                await on_step_complete("SPEECH", [])

    # ---------- SUBTITLE ----------
    if start_idx <= 1:
        if is_task_step_enabled("SUBTITLE"):
            from .subtitle import run_subtitle_task

            subtitle_artifacts = await run_subtitle_task(raw, on_progress)
            all_artifacts.extend(subtitle_artifacts)
            if on_step_complete is not None:
                await on_step_complete("SUBTITLE", subtitle_artifacts)
        else:
            on_progress("SUBTITLE", 1.0, "SUBTITLE 已关闭，跳过执行")
            if on_step_complete is not None:
                await on_step_complete("SUBTITLE", [])

    # ---------- ANALYSIS ----------
    if start_idx <= 2:
        if is_task_step_enabled("ANALYSIS"):
            from .analysis import run_analysis_task

            analysis_artifacts = await run_analysis_task(raw, on_progress)
            all_artifacts.extend(analysis_artifacts)
            if on_step_complete is not None:
                await on_step_complete("ANALYSIS", analysis_artifacts)
        else:
            on_progress("ANALYSIS", 1.0, "ANALYSIS 已关闭，跳过执行")
            if on_step_complete is not None:
                await on_step_complete("ANALYSIS", [])

    return all_artifacts
