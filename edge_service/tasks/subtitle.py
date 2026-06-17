from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from ..utils import safe_name as _safe_name, get_lesson_dir as _get_lesson_dir, resolve_lesson_date as _resolve_lesson_date, fmt_duration as _fmt_duration
from ..video.ffmpeg import embed_srt_to_mp4

log = logging.getLogger("edge.subtitle")


def _parse_other_task_ids(raw: dict[str, Any]) -> list[int]:
    val = raw.get("otherTasksId") or raw.get("other_tasks_id") or ""
    if isinstance(val, (list, tuple)):
        return [int(x) for x in val if x is not None]
    s = str(val).strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [int(x) for x in parsed if x is not None]
    except Exception:
        pass
    parts = re.split(r"[,;\s]+", s)
    result = []
    for p in parts:
        p = p.strip()
        if p:
            try:
                result.append(int(p))
            except ValueError:
                pass
    return result


def _find_teacher_video(lesson_dir: Path) -> Path | None:
    if not lesson_dir.exists():
        return None
    for f in sorted(lesson_dir.glob("teacher_*.mp4")):
        name = f.name.lower()
        if ".part" in name or "_nosub" in name or "_sub_tmp" in name:
            continue
        if f.stat().st_size > 0:
            return f
    return None


def _resolve_srt_path(teacher_video: Path) -> Path | None:
    srt = teacher_video.parent / f"{teacher_video.stem}.zh.srt"
    return srt if srt.exists() else None


def _resolve_vtt_path(teacher_video: Path) -> Path | None:
    vtt = teacher_video.parent / f"{teacher_video.stem}.zh.vtt"
    return vtt if vtt.exists() else None


def _has_srt_payload(srt_path: Path) -> bool:
    try:
        text = srt_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    text = str(text or "").strip()
    return bool(text) and "-->" in text


def _collect_source_videos(lesson_dir: Path) -> list[Path]:
    """收集课次目录下所有源视频（排除临时文件和已转码文件）"""
    if not lesson_dir.exists():
        return []
    result: list[Path] = []
    for f in sorted(lesson_dir.glob("*.mp4")):
        name = f.name.lower()
        if ".part" in name or "_nosub" in name or "_sub_tmp" in name or "_temp_embed" in name:
            continue
        if "_transcoded" in name or "_optimized" in name:
            continue
        if f.stat().st_size == 0:
            continue
        result.append(f)
    return result


def _collect_hls_dirs(lesson_dir: Path) -> list[Path]:
    """收集课次目录下所有 HLS 目录（*_1080P）"""
    if not lesson_dir.exists():
        return []
    result: list[Path] = []
    for d in sorted(lesson_dir.iterdir()):
        if d.is_dir() and d.name.endswith("_1080P"):
            # 确认是有效的 HLS 目录（有 index.m3u8）
            if (d / "index.m3u8").exists():
                result.append(d)
    return result


def _embed_subtitle(video_path: Path, srt_path: Path) -> bool:
    """将 SRT 字幕软挂载到 MP4 容器中（替换原文件）"""
    if not _has_srt_payload(srt_path):
        log.info("跳过字幕挂载 %s: SRT无有效内容", video_path.name)
        return False
    tmp_out = video_path.parent / f"{video_path.stem}_temp_embed.mp4"
    try:
        embed_srt_to_mp4(str(video_path), str(srt_path), str(tmp_out))
        if tmp_out.exists() and tmp_out.stat().st_size > 0:
            bak = video_path.parent / f"{video_path.stem}_nosub.mp4"
            try:
                video_path.rename(bak)
            except Exception:
                pass
            try:
                tmp_out.rename(video_path)
            except Exception:
                shutil.copy2(str(tmp_out), str(video_path))
                tmp_out.unlink(missing_ok=True)
            try:
                bak.unlink(missing_ok=True)
            except Exception:
                pass
            return True
        return False
    except Exception as e:
        log.warning("字幕挂载失败 %s: %s", video_path.name, e)
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _copy_vtt_to_hls(vtt_path: Path, hls_dir: Path) -> bool:
    """复制 VTT 字幕到 HLS 目录"""
    dst = hls_dir / "subtitles.zh.vtt"
    try:
        shutil.copy(str(vtt_path), str(dst))
        log.info("VTT 复制到 HLS 目录: %s", dst)
        return True
    except Exception as e:
        log.warning("复制 VTT 到 HLS 失败: %s", e)
        return False


def _resolve_lesson_dir(raw: dict[str, Any]) -> Path:
    """根据任务原始数据获取课次目录"""
    lesson_id = str(raw.get("lessonId") or "").strip()
    lesson_date = _resolve_lesson_date(raw.get("lessonDate"), raw.get("lessonStartAt"))
    return _get_lesson_dir(lesson_date, lesson_id)


def mount_subtitle_to_video(srt_path: Path, vtt_path: Path | None, video_path: Path) -> bool:
    """为单个视频挂载字幕（源视频嵌入SRT + HLS目录复制VTT）。
    供延迟挂载（taskType=3完成后）调用。
    """
    ok = False
    if srt_path.exists() and video_path.exists():
        ok = _embed_subtitle(video_path, srt_path)
        if ok:
            log.info("字幕已挂载到源视频: %s", video_path.name)

    # 复制 VTT 到对应 HLS 目录
    if vtt_path and vtt_path.exists():
        hls_dir = video_path.parent / f"{video_path.stem}_1080P"
        if hls_dir.exists() and hls_dir.is_dir():
            _copy_vtt_to_hls(vtt_path, hls_dir)

    return ok


async def run_subtitle_task(
    raw: dict[str, Any],
    on_progress: Callable,
) -> list[dict[str, Any]]:
    _task_start = time.monotonic()

    def _cancel_check() -> str:
        mode = str(raw.get("__cancel") or "").strip().lower()
        if mode in {"pause", "stop"}:
            raise RuntimeError(f"cancelled:{mode}")
        return ""

    def _est_total(elapsed: float, pct: int) -> float:
        if pct <= 0 or elapsed <= 0:
            return -1.0
        return elapsed / (pct / 100.0)

    def _progress_msg(action: str, pct: int, elapsed: float) -> str:
        parts = [action, f"已耗时{_fmt_duration(elapsed)}"]
        est = _est_total(elapsed, pct)
        if est > 0:
            parts.append(f"预计时长{_fmt_duration(est)}")
        return "，".join(parts)

    on_progress("SUBTITLE", 0.0, _progress_msg("准备字幕挂载", 0, 0))
    log.info("SUBTITLE 0%% 准备字幕挂载")
    _cancel_check()

    lesson_dir = _resolve_lesson_dir(raw)

    # 查找老师视频及其字幕文件（SPEECH 阶段生成）
    teacher_video = _find_teacher_video(lesson_dir)
    if teacher_video is None:
        raise RuntimeError(f"找不到老师视频文件: {lesson_dir}")

    srt_path = _resolve_srt_path(teacher_video)
    vtt_path = _resolve_vtt_path(teacher_video)
    if srt_path is None and vtt_path is None:
        raise RuntimeError(f"找不到字幕文件: {teacher_video.stem}.zh.srt/vtt")

    # ---------- 收集所有需要挂载的目标 ----------
    source_videos = _collect_source_videos(lesson_dir)
    hls_dirs = _collect_hls_dirs(lesson_dir)

    # 合计任务数：源视频嵌入 + HLS 目录复制
    total_jobs = len(source_videos) + len(hls_dirs)
    if total_jobs == 0:
        source_videos = [teacher_video]
        total_jobs = 1

    artifacts: list[dict[str, Any]] = []
    done_jobs = 0
    if srt_path is not None and srt_path.exists():
        artifacts.append({
            "path": str(srt_path),
            "sizeBytes": int(srt_path.stat().st_size),
            "stepCode": "SUBTITLE",
            "fileType": "subtitle_srt",
        })

    # ---------- 挂载字幕到源视频 ----------
    elapsed = time.monotonic() - _task_start
    on_progress("SUBTITLE", 0.05, _progress_msg(f"挂载字幕到源视频（共{len(source_videos)}个）", 5, elapsed))
    log.info("SUBTITLE 5%% 源视频挂载: %d 个", len(source_videos))

    for video in source_videos:
        _cancel_check()
        done_jobs += 1
        pct = int((done_jobs / total_jobs) * 90) + 5
        elapsed = time.monotonic() - _task_start
        on_progress("SUBTITLE", pct / 100.0, _progress_msg(f"挂载字幕到 {video.name}", pct, elapsed))
        log.info("SUBTITLE %d%% 挂载字幕到 %s", pct, video.name)

        if srt_path:
            ok = await asyncio.to_thread(_embed_subtitle, video, srt_path)
            if ok:
                artifacts.append({
                    "path": str(video),
                    "sizeBytes": int(video.stat().st_size),
                    "stepCode": "SUBTITLE",
                    "fileType": "subtitled_video",
                })

    # ---------- 复制 VTT 到所有 HLS 目录 ----------
    if vtt_path:
        for hls_dir in hls_dirs:
            _cancel_check()
            done_jobs += 1
            pct = int((done_jobs / total_jobs) * 90) + 5
            elapsed = time.monotonic() - _task_start
            on_progress("SUBTITLE", pct / 100.0, _progress_msg(f"复制字幕到 {hls_dir.name}", pct, elapsed))
            log.info("SUBTITLE %d%% 复制VTT到 %s", pct, hls_dir.name)

            ok = await asyncio.to_thread(_copy_vtt_to_hls, vtt_path, hls_dir)
            if ok:
                artifacts.append({
                    "path": str(hls_dir / "subtitles.zh.vtt"),
                    "sizeBytes": int((hls_dir / "subtitles.zh.vtt").stat().st_size) if (hls_dir / "subtitles.zh.vtt").exists() else 0,
                    "stepCode": "SUBTITLE",
                    "fileType": "subtitle_vtt_hls",
                })

    _cancel_check()
    elapsed = time.monotonic() - _task_start
    on_progress("SUBTITLE", 1.0, f"字幕已挂载到源视频、转码视频，共耗时{_fmt_duration(elapsed)}")
    log.info("SUBTITLE 100%% 字幕挂载完成: %d源视频, %dHLS目录, 总耗时=%.0f秒", len(source_videos), len(hls_dirs), elapsed)

    return artifacts
