from __future__ import annotations

import audioop
import contextlib
import functools
import json as _json
import logging
import os
import queue
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger("edge.ffmpeg")
FFPROBE_TIMEOUT_SECONDS = 20


def _media_probe_cache_key(path: str) -> tuple[str, int, int]:
    try:
        resolved = str(Path(path).resolve())
    except Exception:
        resolved = str(path)
    try:
        stat = Path(resolved).stat()
        return resolved, int(stat.st_size), int(stat.st_mtime_ns)
    except Exception:
        return resolved, 0, 0


def _ffmpeg_bin() -> str:
    configured = str(os.getenv("EDGE_FFMPEG_BIN") or "").strip()
    if configured:
        if shutil.which(configured) or Path(configured).exists():
            return configured
    import sys as _sys
    candidates: list[Path] = []
    try:
        exe_dir = Path(_sys.executable).resolve().parent
        candidates.append(exe_dir / "ffmpeg" / "ffmpeg.exe")
        candidates.append(exe_dir.parent / "ffmpeg" / "ffmpeg.exe")
    except Exception:
        pass
    try:
        cwd = Path.cwd().resolve()
        candidates.append(cwd / "ffmpeg" / "ffmpeg.exe")
        candidates.append(cwd.parent / "ffmpeg" / "ffmpeg.exe")
    except Exception:
        pass
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("ffmpeg")
    if found:
        return found
    return "ffmpeg"


ffmpeg_bin = _ffmpeg_bin


def _ffprobe_bin() -> str:
    b = _ffmpeg_bin()
    lower_b = b.lower()
    if lower_b.endswith("ffmpeg.exe"):
        try:
            return str(Path(b).with_name("ffprobe.exe"))
        except Exception:
            return "ffprobe"
    if lower_b.endswith("ffmpeg"):
        try:
            sibling = Path(b).with_name("ffprobe")
            if sibling.exists():
                return str(sibling)
        except Exception:
            pass
        return "ffprobe"
    return "ffprobe"


def _run(cmd: list[str], *, cancel_check: Callable[[], str | None] | None = None) -> None:
    if cancel_check is None:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        if r.returncode != 0:
            tail = "\n".join((r.stdout or "").strip().splitlines()[-15:])
            _log.error("ffmpeg failed exit=%s cmd=%s\n%s", r.returncode, " ".join(cmd[:6]) + " ...", tail)
            raise RuntimeError(f"ffmpeg failed: exit={r.returncode}")
        return
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
            mode = str(cancel_check() or "").strip().lower()
        except Exception:
            mode = ""
        if mode in {"pause", "stop"}:
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                chunk, _ = proc.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    proc.kill()
                chunk, _ = proc.communicate(timeout=2.0)
            output = str(chunk or "")
            raise RuntimeError(f"cancelled:{mode}")
        try:
            chunk, _ = proc.communicate(timeout=0.5)
            output = str(chunk or "")
            break
        except subprocess.TimeoutExpired:
            continue
    if proc.returncode != 0:
        tail = "\n".join((output or "").strip().splitlines()[-15:])
        _log.error("ffmpeg failed exit=%s cmd=%s\n%s", proc.returncode, " ".join(cmd[:6]) + " ...", tail)
        raise RuntimeError(f"ffmpeg failed: exit={proc.returncode}")


def _check_output_with_timeout(
    cmd: list[str],
    *,
    stderr: int | None = None,
    text: bool = True,
    encoding: str = "utf-8",
    errors: str = "ignore",
    timeout: int = FFPROBE_TIMEOUT_SECONDS,
) -> str:
    started = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=text,
        encoding=encoding if text else None,
        errors=errors if text else None,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - started
        _log.warning(
            "media probe timeout elapsed=%.1fs timeout=%ss tool=%s target=%s args=%s",
            elapsed,
            timeout,
            Path(str(cmd[0])).name if cmd else "",
            str(cmd[-1] if cmd else ""),
            " ".join(str(x) for x in cmd[1:-1])[:500],
        )
        with contextlib.suppress(Exception):
            proc.terminate()
        try:
            out, _ = proc.communicate(timeout=3.0)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                proc.kill()
            out, _ = proc.communicate(timeout=3.0)
        raise TimeoutError(f"process_timeout:{timeout}")
    elapsed = time.perf_counter() - started
    if elapsed >= max(5.0, float(timeout) * 0.5):
        _log.info(
            "media probe slow elapsed=%.1fs timeout=%ss tool=%s target=%s args=%s",
            elapsed,
            timeout,
            Path(str(cmd[0])).name if cmd else "",
            str(cmd[-1] if cmd else ""),
            " ".join(str(x) for x in cmd[1:-1])[:500],
        )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=out)
    return str(out or "")


def probe_duration_seconds(path: str) -> float:
    cache_key = _media_probe_cache_key(path)
    return _probe_duration_seconds_cached(*cache_key)


@functools.lru_cache(maxsize=512)
def _probe_duration_seconds_cached(path: str, _size: int, _mtime_ns: int) -> float:
    p = _ffprobe_bin()
    cmd = [p, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        out = _check_output_with_timeout(cmd, stderr=subprocess.STDOUT).strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def probe_authoritative_duration_seconds(path: str) -> float:
    format_duration = float(probe_duration_seconds(path) or 0.0)
    timings = probe_stream_timings(path)
    stream_ends: list[float] = []
    for codec_type in ("video", "audio"):
        timing = timings.get(codec_type) or {}
        try:
            start_time = float(timing.get("start_time") or 0.0)
        except Exception:
            start_time = 0.0
        try:
            duration = float(timing.get("duration") or 0.0)
        except Exception:
            duration = 0.0
        end_time = max(0.0, start_time) + max(0.0, duration)
        if end_time > 0.0:
            stream_ends.append(float(end_time))
    if not stream_ends:
        return format_duration
    stream_ends = sorted(stream_ends)
    min_end = float(stream_ends[0])
    max_end = float(stream_ends[-1])
    if len(stream_ends) >= 2 and (max_end - min_end) > 60.0 and max_end > min_end * 1.2:
        return min_end
    if format_duration > 0.0:
        if (format_duration - max_end) > 60.0 and format_duration > max_end * 1.2:
            return max_end
        if (max_end - format_duration) > 60.0 and max_end > format_duration * 1.2:
            return max_end
    return max_end if max_end > 0.0 else format_duration


def probe_media_start_seconds(path: str) -> float:
    cache_key = _media_probe_cache_key(path)
    return _probe_media_start_seconds_cached(*cache_key)


@functools.lru_cache(maxsize=512)
def _probe_media_start_seconds_cached(path: str, _size: int, _mtime_ns: int) -> float:
    try:
        starts: list[float] = []
        timings = _probe_stream_timings_cached(path, _size, _mtime_ns)
        for codec_type in ("video", "audio"):
            timing = timings.get(codec_type) or {}
            raw = timing.get("start_time")
            if raw is None:
                continue
            starts.append(float(raw))
        return min(starts) if starts else 0.0
    except Exception:
        return 0.0


def _probe_stream_start_times(path: str) -> dict[str, float]:
    cache_key = _media_probe_cache_key(path)
    return _probe_stream_start_times_cached(*cache_key)


@functools.lru_cache(maxsize=512)
def _probe_stream_start_times_cached(path: str, _size: int, _mtime_ns: int) -> dict[str, float]:
    try:
        result: dict[str, float] = {}
        timings = _probe_stream_timings_cached(path, _size, _mtime_ns)
        for codec_type in ("video", "audio"):
            timing = timings.get(codec_type) or {}
            raw = timing.get("start_time")
            if raw is None:
                continue
            result[codec_type] = float(raw)
        return result
    except Exception:
        return {}


def probe_stream_timings(path: str) -> dict[str, dict[str, float]]:
    cache_key = _media_probe_cache_key(path)
    return _probe_stream_timings_cached(*cache_key)


@functools.lru_cache(maxsize=512)
def _probe_stream_timings_cached(path: str, _size: int, _mtime_ns: int) -> dict[str, dict[str, float]]:
    p = _ffprobe_bin()
    cmd = [
        p,
        "-v",
        "quiet",
        "-analyzeduration",
        "5M",
        "-probesize",
        "5M",
        "-print_format",
        "json",
        "-show_entries",
        "stream=codec_type,start_time,duration,codec_name,codec_tag_string,time_base,width,height",
        "-show_streams",
        path,
    ]
    try:
        out = _check_output_with_timeout(cmd, stderr=subprocess.DEVNULL)
        data = _json.loads(out or "{}")
        res: dict[str, dict[str, float]] = {}
        for s in data.get("streams", []) or []:
            codec_type = str((s or {}).get("codec_type") or "").strip().lower()
            if codec_type not in {"video", "audio"} or codec_type in res:
                continue
            try:
                start_time = float((s or {}).get("start_time") or 0.0)
            except Exception:
                start_time = 0.0
            try:
                duration = float((s or {}).get("duration") or 0.0)
            except Exception:
                duration = 0.0
            res[codec_type] = {
                "start_time": start_time,
                "duration": duration,
                "codec_name": str((s or {}).get("codec_name") or "").strip().lower(),
                "codec_tag_string": str((s or {}).get("codec_tag_string") or "").strip().lower(),
                "time_base": str((s or {}).get("time_base") or "").strip().lower(),
            }
        return res
    except Exception:
        return {}


def probe_max_packet_duration(path: str, *, stream_selector: str) -> float:
    cache_key = _media_probe_cache_key(path)
    return _probe_max_packet_duration_cached(cache_key[0], cache_key[1], cache_key[2], stream_selector)


@functools.lru_cache(maxsize=512)
def _probe_max_packet_duration_cached(path: str, _size: int, _mtime_ns: int, stream_selector: str) -> float:
    p = _ffprobe_bin()
    cmd = [
        p,
        "-v",
        "error",
        "-select_streams",
        stream_selector,
        "-show_entries",
        "packet=duration_time",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        out = _check_output_with_timeout(cmd, stderr=subprocess.DEVNULL, timeout=min(12, max(FFPROBE_TIMEOUT_SECONDS, 12)))
    except Exception:
        return 0.0
    max_duration = 0.0
    for line in (out or "").splitlines():
        raw = str(line or "").strip()
        if not raw or raw == "N/A":
            continue
        try:
            value = float(raw)
        except Exception:
            continue
        if value > max_duration:
            max_duration = value
    return max_duration


def probe_packet_timeline_anomaly(path: str, *, stream_selector: str) -> dict[str, float | int]:
    cache_key = _media_probe_cache_key(path)
    return _probe_packet_timeline_anomaly_cached(cache_key[0], cache_key[1], cache_key[2], stream_selector)


@functools.lru_cache(maxsize=256)
def _probe_packet_timeline_anomaly_cached(path: str, _size: int, _mtime_ns: int, stream_selector: str) -> dict[str, float | int]:
    p = _ffprobe_bin()
    cmd = [
        p,
        "-v",
        "error",
        "-select_streams",
        stream_selector,
        "-show_packets",
        "-show_entries",
        "packet=pts_time,dts_time,duration_time,size,pos",
        "-of",
        "csv=p=0",
        path,
    ]
    try:
        out = _check_output_with_timeout(cmd, stderr=subprocess.DEVNULL, timeout=min(12, max(FFPROBE_TIMEOUT_SECONDS, 12)))
    except Exception:
        return {
            "packet_count": 0,
            "pts_backward_count": 0,
            "dts_backward_count": 0,
            "max_pts_backward_sec": 0.0,
            "max_dts_backward_sec": 0.0,
            "first_backward_pts_time": 0.0,
            "first_backward_pos": 0.0,
        }
    packet_count = 0
    pts_backward_count = 0
    dts_backward_count = 0
    max_pts_backward_sec = 0.0
    max_dts_backward_sec = 0.0
    first_backward_pts_time = 0.0
    first_backward_pos = 0.0
    prev_pts: float | None = None
    prev_dts: float | None = None
    for line in (out or "").splitlines():
        raw = str(line or "").strip()
        if not raw:
            continue
        parts = [str(x or "").strip() for x in raw.split(",")]
        if len(parts) < 5:
            continue
        try:
            pts = float(parts[0])
            dts = float(parts[1])
        except Exception:
            continue
        try:
            pos = float(parts[4])
        except Exception:
            pos = 0.0
        packet_count += 1
        if prev_pts is not None and pts < prev_pts:
            backward = float(prev_pts - pts)
            pts_backward_count += 1
            max_pts_backward_sec = max(max_pts_backward_sec, backward)
            if first_backward_pts_time <= 0.0:
                first_backward_pts_time = float(pts)
                first_backward_pos = float(pos)
        if prev_dts is not None and dts < prev_dts:
            backward = float(prev_dts - dts)
            dts_backward_count += 1
            max_dts_backward_sec = max(max_dts_backward_sec, backward)
            if first_backward_pts_time <= 0.0:
                first_backward_pts_time = float(pts)
                first_backward_pos = float(pos)
        prev_pts = pts
        prev_dts = dts
    return {
        "packet_count": int(packet_count),
        "pts_backward_count": int(pts_backward_count),
        "dts_backward_count": int(dts_backward_count),
        "max_pts_backward_sec": float(max_pts_backward_sec),
        "max_dts_backward_sec": float(max_dts_backward_sec),
        "first_backward_pts_time": float(first_backward_pts_time),
        "first_backward_pos": float(first_backward_pos),
    }


def _read_wav_rms_windows(wav_path: str, *, window_ms: int = 50) -> tuple[list[int], float, float]:
    with wave.open(wav_path, "rb") as wf:
        frame_rate = int(wf.getframerate() or 16000)
        sample_width = int(wf.getsampwidth() or 2)
        total_frames = int(wf.getnframes() or 0)
        if frame_rate <= 0 or total_frames <= 0:
            return [], 0.0, 0.0
        chunk_frames = max(1, int(frame_rate * window_ms / 1000))
        rms_values: list[int] = []
        while True:
            frames = wf.readframes(chunk_frames)
            if not frames:
                break
            rms_values.append(int(audioop.rms(frames, sample_width) or 0))
        return rms_values, chunk_frames / float(frame_rate), total_frames / float(frame_rate)


def _detect_activity_regions_from_rms(
    rms_values: list[int],
    *,
    window_sec: float,
    total_dur: float,
    rms_threshold: float,
    consecutive_windows: int = 3,
    max_gap_sec: float = 1.2,
    pad_sec: float = 0.25,
    min_region_sec: float = 0.45,
    min_active_ratio: float = 0.12,
) -> list[tuple[float, float]]:
    if not rms_values or window_sec <= 0 or total_dur <= 0:
        return []
    max_gap_windows = max(1, int(round(max_gap_sec / window_sec)))
    regions: list[tuple[float, float]] = []
    active_streak = 0
    silent_streak = 0
    idx = 0
    region_start_idx: int | None = None
    last_active_idx: int | None = None
    region_active_windows = 0

    def _finalize(end_idx: int | None) -> None:
        nonlocal region_start_idx, last_active_idx, region_active_windows, silent_streak
        if region_start_idx is None or last_active_idx is None:
            region_start_idx = None
            last_active_idx = None
            region_active_windows = 0
            silent_streak = 0
            return
        raw_start = max(0.0, region_start_idx * window_sec - pad_sec)
        raw_end = min(total_dur, ((end_idx if end_idx is not None else last_active_idx) + 1) * window_sec + pad_sec)
        dur = max(0.0, raw_end - raw_start)
        region_windows = max(1, (last_active_idx - region_start_idx) + 1)
        active_ratio = region_active_windows / float(region_windows)
        if dur >= min_region_sec and active_ratio >= min_active_ratio:
            if regions and raw_start - regions[-1][1] <= max_gap_sec:
                prev_start, prev_end = regions[-1]
                regions[-1] = (prev_start, max(prev_end, raw_end))
            else:
                regions.append((raw_start, raw_end))
        region_start_idx = None
        last_active_idx = None
        region_active_windows = 0
        silent_streak = 0

    for rms in rms_values:
        is_active = float(rms) >= float(rms_threshold)
        if is_active:
            active_streak += 1
            silent_streak = 0
            if region_start_idx is None and active_streak >= consecutive_windows:
                region_start_idx = max(0, idx - consecutive_windows + 1)
                region_active_windows = consecutive_windows
            elif region_start_idx is not None:
                region_active_windows += 1
            last_active_idx = idx
        else:
            active_streak = 0
            if region_start_idx is not None:
                silent_streak += 1
                if silent_streak >= max_gap_windows:
                    _finalize(last_active_idx)
        idx += 1
    _finalize(last_active_idx)
    return [(max(0.0, st), min(total_dur, max(st, et))) for st, et in regions if et > st + 0.05]


def _detect_early_sparse_activity_start(
    rms_values: list[int],
    *,
    window_sec: float,
    total_dur: float,
    noise_floor: float,
    dynamic_range: float,
) -> float:
    if not rms_values or window_sec <= 0 or total_dur <= 0:
        return 0.0
    early_threshold = max(220.0, noise_floor * 1.35, noise_floor + dynamic_range * 0.06)
    early_regions = _detect_activity_regions_from_rms(
        rms_values,
        window_sec=window_sec,
        total_dur=total_dur,
        rms_threshold=early_threshold,
        consecutive_windows=2,
        max_gap_sec=0.35,
        pad_sec=0.05,
        min_region_sec=0.12,
        min_active_ratio=0.18,
    )
    if not early_regions:
        return 0.0
    first_start, first_end = early_regions[0]
    if (first_end - first_start) < 0.12:
        return 0.0
    if first_start > min(1.2, max(0.4, total_dur * 0.20)):
        return 0.0
    return max(0.0, float(first_start))


def probe_audio_content_offset(input_path: str, *, probe_sec: float = 20.0, sample_rate: int = 16000) -> float:
    if not has_audio_stream(input_path):
        return 0.0
    tmp_wav = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tf:
            tmp_wav = tf.name
        cmd = [
            _ffmpeg_bin(),
            "-y",
            "-t",
            f"{max(1.0, float(probe_sec)):.3f}",
            "-i",
            input_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(max(8000, int(sample_rate))),
            "-af",
            "highpass=f=120,lowpass=f=3500,aresample=async=1",
            "-f",
            "wav",
            tmp_wav,
        ]
        _run(cmd)
        rms_values, window_sec, total_dur = _read_wav_rms_windows(tmp_wav, window_ms=50)
        if not rms_values or window_sec <= 0 or total_dur <= 0:
            return 0.0
        sorted_rms = sorted(rms_values)
        low_bucket_count = max(8, len(sorted_rms) // 5)
        lead_bucket_count = max(8, min(len(rms_values), int(round(1.5 / max(window_sec, 0.001)))))
        low_median = float(statistics.median(sorted_rms[:low_bucket_count])) if sorted_rms else 0.0
        lead_median = float(statistics.median(rms_values[:lead_bucket_count])) if rms_values[:lead_bucket_count] else 0.0
        peak_index = min(len(sorted_rms) - 1, max(0, int(round((len(sorted_rms) - 1) * 0.95))))
        peak_level = float(sorted_rms[peak_index]) if sorted_rms else 0.0
        noise_floor = max(low_median, lead_median)
        dynamic_range = max(0.0, peak_level - noise_floor)
        thresholds = [
            max(900.0, noise_floor * 3.2, noise_floor + dynamic_range * 0.30),
            max(650.0, noise_floor * 2.4, noise_floor + dynamic_range * 0.22),
            max(450.0, noise_floor * 1.8, noise_floor + dynamic_range * 0.15),
            400.0,
        ]
        unique_thresholds: list[float] = []
        for threshold in thresholds:
            rounded = round(float(threshold), 2)
            if rounded not in unique_thresholds:
                unique_thresholds.append(rounded)
        early_sparse_start = _detect_early_sparse_activity_start(
            rms_values,
            window_sec=window_sec,
            total_dur=total_dur,
            noise_floor=noise_floor,
            dynamic_range=dynamic_range,
        )
        fallback_start = 0.0
        selected_start = 0.0
        for threshold in unique_thresholds:
            regions = _detect_activity_regions_from_rms(
                rms_values,
                window_sec=window_sec,
                total_dur=total_dur,
                rms_threshold=threshold,
            )
            if not regions:
                continue
            first_start = float(regions[0][0])
            if fallback_start <= 0.0:
                fallback_start = first_start
            if selected_start <= 0.0 and first_start > 0.15:
                selected_start = first_start
        if early_sparse_start > 0.0:
            if selected_start <= 0.0:
                return early_sparse_start
            if (selected_start - early_sparse_start) >= max(0.8, min(2.5, selected_start * 0.6)):
                return early_sparse_start
        if selected_start > 0.0:
            return selected_start
        return max(0.0, fallback_start)
    except Exception:
        return 0.0
    finally:
        if tmp_wav:
            try:
                Path(tmp_wav).unlink(missing_ok=True)
            except Exception:
                pass
    return 0.0


def remux_faststart(input_path: str, output_path: str, *, cancel_check: Callable[[], str | None] | None = None) -> None:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg_bin(), "-y", "-i", input_path, "-c", "copy", "-movflags", "+faststart", str(out)]
    _run(cmd, cancel_check=cancel_check)


def has_audio_stream(input_path: str) -> bool:
    """检测视频文件是否包含音频流"""
    cache_key = _media_probe_cache_key(input_path)
    return _has_audio_stream_cached(*cache_key)


@functools.lru_cache(maxsize=512)
def _has_audio_stream_cached(input_path: str, _size: int, _mtime_ns: int) -> bool:
    try:
        import json as _json
        cmd = [
            _ffprobe_bin(),
            "-v", "quiet",
            "-analyzeduration", "5M",
            "-probesize", "5M",
            "-print_format", "json",
            "-show_streams",
            input_path,
        ]
        out = _check_output_with_timeout(cmd, stderr=subprocess.STDOUT)
        streams = (_json.loads(out or "{}").get("streams") or [])
        return any(str((s or {}).get("codec_type") or "").strip().lower() == "audio" for s in streams)
    except Exception:
        return False


def get_audio_start_time(input_path: str) -> float:
    """获取视频文件中音频流的 start_time（秒）。用于修正字幕时间戳偏移。"""
    cache_key = _media_probe_cache_key(input_path)
    return _get_audio_start_time_cached(*cache_key)


@functools.lru_cache(maxsize=512)
def _get_audio_start_time_cached(input_path: str, _size: int, _mtime_ns: int) -> float:
    import json as _json
    try:
        cmd = [
            _ffprobe_bin(), "-v", "quiet", "-analyzeduration", "5M", "-probesize", "5M", "-print_format", "json",
            "-show_streams", "-select_streams", "a:0", input_path,
        ]
        out = _check_output_with_timeout(cmd, stderr=subprocess.DEVNULL, timeout=12)
        streams = _json.loads(out).get("streams", [])
        if streams:
            return float(streams[0].get("start_time") or 0.0)
    except Exception:
        pass
    return 0.0


def probe_audio_stream_info(input_path: str) -> dict[str, Any]:
    cache_key = _media_probe_cache_key(input_path)
    return _probe_audio_stream_info_cached(*cache_key)


@functools.lru_cache(maxsize=512)
def _probe_audio_stream_info_cached(input_path: str, _size: int, _mtime_ns: int) -> dict[str, Any]:
    import json as _json
    try:
        cmd = [
            _ffprobe_bin(), "-v", "quiet", "-analyzeduration", "1M", "-probesize", "1M", "-print_format", "json",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate,channel_layout",
            input_path,
        ]
        out = _check_output_with_timeout(cmd, stderr=subprocess.DEVNULL)
        streams = _json.loads(out).get("streams", [])
        if not streams:
            return {"_probe_status": "no_stream"}
        s = streams[0] or {}
        try:
            sample_rate = int(str(s.get("sample_rate") or "0").strip() or 0)
        except Exception:
            sample_rate = 0
        try:
            channels = int(s.get("channels") or 0)
        except Exception:
            channels = 0
        try:
            bit_rate = int(str(s.get("bit_rate") or "0").strip() or 0)
        except Exception:
            bit_rate = 0
        return {
            "_probe_status": "ok",
            "codec_name": str(s.get("codec_name") or "").strip().lower(),
            "sample_rate": sample_rate,
            "channels": channels,
            "bit_rate": bit_rate,
            "channel_layout": str(s.get("channel_layout") or "").strip().lower(),
        }
    except TimeoutError:
        return {"_probe_status": "timeout"}
    except Exception:
        return {"_probe_status": "error"}


def rebuild_zero_based_timeline(
    input_path: str,
    output_path: str,
    precise_audio_trim_sec: float = 0.0,
    target_duration_sec: float = 0.0,
    force_full_rebuild: bool = False,
    target_video_codec: str = "",
    cancel_check: Callable[[], str | None] | None = None,
) -> None:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    stream_starts = _probe_stream_start_times(input_path)
    media_start = float(probe_media_start_seconds(input_path) or 0.0)
    video_start = float(stream_starts.get("video") or 0.0)
    audio_start = float(stream_starts.get("audio") or 0.0)
    av_gap = video_start - audio_start
    has_audio = has_audio_stream(input_path)
    timings = probe_stream_timings(input_path)
    video_duration = float((timings.get("video") or {}).get("duration") or 0.0)
    audio_duration = float((timings.get("audio") or {}).get("duration") or 0.0)
    duration_delta = audio_duration - video_duration if video_duration > 0 and audio_duration > 0 else 0.0
    audio_info = probe_audio_stream_info(input_path) if has_audio else {}
    source_total_bitrate_bps = max(0, int(probe_bitrate_bps(input_path) or 0))

    def _normalize_target_video_codec(codec_name: str) -> str:
        normalized = str(codec_name or "").strip().lower()
        if normalized in {"h265", "hevc"}:
            return "hevc"
        if normalized in {"x264", "h264"}:
            return "h264"
        return ""

    def _source_video_codec() -> str:
        try:
            cmd = [
                _ffprobe_bin(),
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                input_path,
            ]
            return _normalize_target_video_codec(_check_output_with_timeout(cmd).strip())
        except Exception:
            return ""

    def _video_quality_args() -> list[str]:
        try:
            audio_bitrate_bps = int(audio_info.get("bit_rate") or 0)
        except Exception:
            audio_bitrate_bps = 0
        target_video_bitrate_bps = source_total_bitrate_bps
        if target_video_bitrate_bps > 0 and audio_bitrate_bps > 0 and target_video_bitrate_bps > audio_bitrate_bps:
            target_video_bitrate_bps = max(300000, target_video_bitrate_bps - audio_bitrate_bps)
        if target_video_bitrate_bps > 0:
            target_k = max(300, int(round(target_video_bitrate_bps / 1000.0)))
            maxrate_k = max(target_k, int(round(target_k * 1.15)))
            bufsize_k = max(maxrate_k * 2, target_k * 2)
            return [
                "-b:v",
                f"{target_k}k",
                "-maxrate",
                f"{maxrate_k}k",
                "-bufsize",
                f"{bufsize_k}k",
            ]
        return []

    def _video_encode_args() -> list[str]:
        target = _normalize_target_video_codec(target_video_codec)
        quality_args = _video_quality_args()
        if target == "hevc":
            if _has_encoder("hevc_qsv"):
                if quality_args:
                    return ["-c:v", "hevc_qsv", "-preset", "medium", *quality_args, "-tag:v", "hvc1"]
                return ["-c:v", "hevc_qsv", "-preset", "medium", "-global_quality", "18", "-tag:v", "hvc1"]
            if _has_encoder("libx265"):
                if quality_args:
                    return ["-c:v", "libx265", "-preset", "veryfast", *quality_args, "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
                return ["-c:v", "libx265", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
            _log.warning("rebuild_zero_based_timeline target hevc requested but encoder unavailable, fallback h264 input=%s", input_path)
        if quality_args:
            return ["-c:v", "libx264", "-preset", "veryfast", *quality_args]
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]

    def _audio_bitrate_candidates() -> list[str]:
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
            normalized = max(64000, min(256000, int(round(bit_rate / 1000.0)) * 1000))
            _add(normalized)
            if normalized < 256000:
                _add(min(256000, normalized * 2))
        for item in defaults:
            _add(item)
        return [f"{int(max(64, round(v / 1000.0)))}k" for v in candidates]

    def _audio_encode_args(bitrate: str) -> list[str]:
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

    def _validate_audio_after_rebuild(expected_trim_sec: float, *, preserve_audio_duration: bool = False) -> None:
        if not has_audio:
            return
        if not has_audio_stream(str(out)):
            raise RuntimeError("audio_stream_lost_after_rebuild")
        timings = probe_stream_timings(str(out))
        out_video = float((timings.get("video") or {}).get("duration") or 0.0)
        out_audio = float((timings.get("audio") or {}).get("duration") or 0.0)
        trim = max(0.0, float(expected_trim_sec or 0.0))
        # 视频比音频长是常见且无害的：PS/录像 tail 音频缺失、av_gap 头部裁剪都会造成这种偏差。
        # 仅当音频流完全丢失（前面已判断）或输出音频极短（< 源音频 duration 的 1/3）时，才视为异常。
        src_audio_dur = 0.0
        try:
            src_timings = probe_stream_timings(input_path)
            src_audio_dur = float((src_timings.get("audio") or {}).get("duration") or 0.0)
        except Exception:
            src_audio_dur = 0.0
        if preserve_audio_duration and src_audio_dur > 0 and out_audio > 0:
            if (src_audio_dur - out_audio) > max(2.0, src_audio_dur * 0.01):
                raise RuntimeError(
                    f"audio_duration_shrunk_after_timestamp_only_rebuild:src_audio={src_audio_dur:.3f}:out_video={out_video:.3f}:out_audio={out_audio:.3f}"
                )
        if src_audio_dur > 0 and out_audio > 0:
            min_expected_audio = max(0.0, src_audio_dur - trim) * 0.5
            if out_audio < min_expected_audio and (src_audio_dur - out_audio) > max(60.0, trim + 30.0):
                raise RuntimeError(
                    f"audio_duration_far_too_short_after_rebuild:src_audio={src_audio_dur:.3f}:out_video={out_video:.3f}:out_audio={out_audio:.3f}:trim={trim:.3f}"
                )
        max_audio_packet_dur = float(probe_max_packet_duration(str(out), stream_selector="a:0") or 0.0)
        if max_audio_packet_dur > 1.0:
            raise RuntimeError(
                f"audio_packet_duration_abnormal_after_rebuild:max={max_audio_packet_dur:.3f}:out_video={out_video:.3f}:out_audio={out_audio:.3f}:trim={trim:.3f}"
            )

    base_cmd = [
        _ffmpeg_bin(),
        "-y",
        "-fflags",
        "+genpts",
        "-analyzeduration",
        "100M",
        "-probesize",
        "100M",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-vf",
        "setpts=PTS-STARTPTS",
        * _video_encode_args(),
    ]
    video_filter = "setpts=PTS-STARTPTS"
    audio_filter = "asetpts=PTS-STARTPTS"
    precise_trim = max(0.0, float(precise_audio_trim_sec or 0.0))
    target_duration = max(0.0, float(target_duration_sec or 0.0))
    if target_duration <= 0.0 and video_duration > 0.0:
        target_duration = float(video_duration)
    _log.info(
        "rebuild_zero_based_timeline start input=%s output=%s has_audio=%s video_start=%.3f audio_start=%.3f av_gap=%.3f precise_trim=%.3f target_video_codec=%s",
        input_path,
        output_path,
        has_audio,
        video_start,
        audio_start,
        av_gap,
        precise_trim,
        _normalize_target_video_codec(target_video_codec) or "default",
    )
    audio_content_offset = 0.0
    audio_early_offset = 0.0
    if has_audio and precise_trim <= 0.05 and av_gap > 0.05:
        audio_early_offset = float(av_gap)
    if has_audio and (precise_trim > 0.05 or audio_early_offset > 0.05):
        try:
            probe_window = precise_trim if precise_trim > 0.05 else audio_early_offset
            audio_content_offset = float(probe_audio_content_offset(input_path, probe_sec=min(20.0, max(8.0, probe_window + 2.0))) or 0.0)
        except Exception:
            audio_content_offset = 0.0
    packet_timeline = probe_packet_timeline_anomaly(input_path, stream_selector="a:0") if has_audio else {}
    pts_backward_count = int(packet_timeline.get("pts_backward_count") or 0)
    dts_backward_count = int(packet_timeline.get("dts_backward_count") or 0)
    max_pts_backward_sec = float(packet_timeline.get("max_pts_backward_sec") or 0.0)
    max_dts_backward_sec = float(packet_timeline.get("max_dts_backward_sec") or 0.0)
    first_backward_pts_time = float(packet_timeline.get("first_backward_pts_time") or 0.0)
    timestamp_dominant_audio_late = bool(
        has_audio
        and precise_trim > 0.05
        and abs(duration_delta) <= max(0.6, min(3.0, precise_trim * 0.08))
        and (pts_backward_count + dts_backward_count) <= 2
        and max(max_pts_backward_sec, max_dts_backward_sec) <= 0.05
        and audio_content_offset <= min(4.0, max(1.0, precise_trim * 0.25))
    )
    timestamp_only_audio_late = bool(
        has_audio
        and precise_trim > 0.05
        and (
            audio_content_offset <= min(0.8, max(0.2, precise_trim * 0.25))
            or timestamp_dominant_audio_late
        )
    )
    audio_packet_timeline_disorder = bool(
        has_audio
        and timestamp_only_audio_late
        and (pts_backward_count > 0 or dts_backward_count > 0)
        and max(max_pts_backward_sec, max_dts_backward_sec) >= 0.005
    )
    audio_early_timestamp_only = bool(
        has_audio
        and audio_early_offset > 0.05
        and audio_content_offset <= min(1.0, max(0.2, audio_early_offset * 0.25))
    )
    if timestamp_only_audio_late:
        _log.info(
            "rebuild_zero_based_timeline detected timestamp_only_audio_late trim=%.3f content_offset=%.3f input=%s",
            precise_trim,
            audio_content_offset,
            input_path,
        )
    if timestamp_dominant_audio_late and audio_content_offset > min(0.8, max(0.2, precise_trim * 0.25)):
        _log.info(
            "rebuild_zero_based_timeline treat as timestamp_dominant_audio_late trim=%.3f content_offset=%.3f duration_delta=%.3f pts_back=%s dts_back=%s input=%s",
            precise_trim,
            audio_content_offset,
            duration_delta,
            pts_backward_count,
            dts_backward_count,
            input_path,
        )
    if audio_packet_timeline_disorder:
        _log.warning(
            "rebuild_zero_based_timeline detected audio_packet_timeline_disorder pts_back=%s dts_back=%s max_pts_back=%.6f max_dts_back=%.6f first_back_pts=%.3f input=%s",
            pts_backward_count,
            dts_backward_count,
            max_pts_backward_sec,
            max_dts_backward_sec,
            first_backward_pts_time,
            input_path,
        )
    if audio_early_timestamp_only:
        _log.info(
            "rebuild_zero_based_timeline detected timestamp_only_audio_early offset=%.3f content_offset=%.3f input=%s",
            audio_early_offset,
            audio_content_offset,
            input_path,
        )
    audio_early_content_trim = 0.0
    def _reset_audio_filter(*, use_resample: bool = False, trim_to_target: bool = False, pad_to_target: bool = False) -> str:
        parts: list[str] = []
        if use_resample:
            parts.append("aresample=async=1:first_pts=0")
        parts.append("asetpts=PTS-STARTPTS")
        if trim_to_target and target_duration > 0.0:
            parts.append(f"atrim=end={target_duration:.3f}")
            if pad_to_target:
                parts.append(f"apad=whole_dur={target_duration:.3f}")
                parts.append(f"atrim=end={target_duration:.3f}")
            parts.append("asetpts=PTS-STARTPTS")
        return ",".join(parts)

    def _trim_leading_audio_and_pad_filter(trim_seconds: float) -> str:
        parts = [
            f"atrim=start={max(0.0, float(trim_seconds or 0.0)):.3f}",
            "asetpts=PTS-STARTPTS",
        ]
        if target_duration > 0.0:
            parts.append(f"apad=whole_dur={target_duration:.3f}")
            parts.append(f"atrim=end={target_duration:.3f}")
            parts.append("asetpts=PTS-STARTPTS")
        return ",".join(parts)

    if has_audio and audio_early_offset > 0.05 and not force_full_rebuild:
        _log.info(
            "rebuild_zero_based_timeline strategy=audio_earlier_than_video_timestamp_only offset=%.3f content_offset=%.3f input=%s",
            audio_early_offset,
            audio_content_offset,
            input_path,
        )
        # 用 itsoffset 将音频 PTS 整体推迟 audio_early_offset 秒，
        # 让音频首样本的时间戳与视频首帧对齐；音频内容一字节不删，
        # 仅对时间戳做重贴标签，符合"只改时间戳不改内容"的原则。
        cmd_itsoffset_align_copy = [
            _ffmpeg_bin(),
            "-y",
            "-fflags",
            "+genpts",
            "-analyzeduration",
            "100M",
            "-probesize",
            "100M",
            "-i",
            input_path,
            "-itsoffset",
            f"{audio_early_offset:.6f}",
            "-i",
            input_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0?",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            str(out),
        ]
        try:
            _run(cmd_itsoffset_align_copy, cancel_check=cancel_check)
            _validate_audio_after_rebuild(0.0, preserve_audio_duration=True)
            _log.info(
                "rebuild_zero_based_timeline strategy=audio_earlier_than_video_timestamp_only mode=itsoffset_align_copy success offset=%.3f output=%s",
                audio_early_offset,
                output_path,
            )
            return
        except Exception as e:
            if str(e) in {"cancelled:pause", "cancelled:stop"}:
                raise
            _log.warning(
                "rebuild_zero_based_timeline strategy=audio_earlier_than_video_timestamp_only mode=itsoffset_align_copy failed input=%s offset=%.3f err=%s",
                input_path,
                audio_early_offset,
                e,
            )
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
        _log.info(
            "rebuild_zero_based_timeline strategy=audio_earlier_than_video_timestamp_only fallback=video_pts_offset_reencode offset=%.3f input=%s",
            audio_early_offset,
            input_path,
        )
    # 把"音频早于视频"(av_gap > 0) 也当作 audio 裁剪场景，走 copy-video + seek-audio 路径，
    # 避免对带 PTS 不连续的 MPEG-PS 源做整段 video 重编导致的插帧/丢帧漂移。
    effective_audio_trim = max(precise_trim, audio_early_content_trim)
    if has_audio and effective_audio_trim > 0.05 and (audio_early_content_trim > 0.05 or not timestamp_only_audio_late):
        _log.info(
            "rebuild_zero_based_timeline strategy=audio_content_trim mode=filter_rebuild trim=%.3f content_offset=%.3f early_trim=%.3f input=%s",
            effective_audio_trim,
            audio_content_offset,
            audio_early_content_trim,
            input_path,
        )
    applied_audio_trim = 0.0
    if has_audio and effective_audio_trim > 0.05 and not timestamp_only_audio_late:
        audio_filter = _trim_leading_audio_and_pad_filter(effective_audio_trim)
        applied_audio_trim = float(effective_audio_trim)
        _log.info(
            "rebuild_zero_based_timeline strategy=audio_content_trim mode=leading_trim_pad trim=%.3f content_offset=%.3f target_duration=%.3f input=%s",
            effective_audio_trim,
            audio_content_offset,
            target_duration,
            input_path,
        )
    elif has_audio and precise_trim > 0.05 and timestamp_only_audio_late:
        audio_filter = _reset_audio_filter(use_resample=bool(force_full_rebuild), trim_to_target=True, pad_to_target=False)
        _log.info(
            "rebuild_zero_based_timeline strategy=timestamp_only_audio_late_reset_to_video trim=%.3f content_offset=%.3f target_duration=%.3f force_full=%s input=%s",
            precise_trim,
            audio_content_offset,
            target_duration,
            force_full_rebuild,
            input_path,
        )
    elif has_audio and av_gap < -0.5:
        delay_seconds = abs(av_gap)
        delay_ms = int(round(delay_seconds * 1000.0))
        audio_filter = f"asetpts=PTS-STARTPTS,adelay={delay_ms}|{delay_ms}:all=1"
        _log.info(
            "rebuild_zero_based_timeline strategy=preserve_raw_start_gap_audio_delay av_gap=%.3f delay_ms=%d precise_trim=%.3f duration_delta=%.3f force_full=%s input=%s",
            av_gap,
            delay_ms,
            precise_trim,
            duration_delta,
            force_full_rebuild,
            input_path,
        )
    elif force_full_rebuild and has_audio:
        audio_filter = _reset_audio_filter(use_resample=True, trim_to_target=True, pad_to_target=True)
        _log.info(
            "rebuild_zero_based_timeline fallback=force_full_rebuild target_duration=%.3f trim=%.3f content_offset=%.3f input=%s",
            target_duration,
            precise_trim,
            audio_content_offset,
            input_path,
        )
    elif has_audio and audio_early_content_trim > 0.05:
        audio_filter = f"atrim=start={audio_early_content_trim:.3f},asetpts=PTS-STARTPTS"
        applied_audio_trim = float(audio_early_content_trim)
        _log.info(
            "rebuild_zero_based_timeline fallback=audio_early_trim_filter trim=%.3f input=%s",
            audio_early_content_trim,
            input_path,
        )
    elif has_audio and precise_trim > 0.05 and duration_delta > 0.2:
        audio_filter = _reset_audio_filter(use_resample=False, trim_to_target=True)
        _log.info(
            "rebuild_zero_based_timeline fallback=duration_delta_audio_trim_to_video trim=%.3f duration_delta=%.3f target_duration=%.3f input=%s",
            precise_trim,
            duration_delta,
            target_duration,
            input_path,
        )
    elif has_audio and precise_trim > 0.05:
        audio_filter = "aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS"
        _log.info(
            "rebuild_zero_based_timeline fallback=preserve_audio_content_reset_only trim=%.3f content_offset=%.3f input=%s",
            precise_trim,
            audio_content_offset,
            input_path,
        )
    elif has_audio and abs(av_gap) > 0.05:
        if av_gap > 0:
            video_filter = f"setpts=PTS-STARTPTS+{av_gap:.3f}/TB"
            _log.info(
                "rebuild_zero_based_timeline fallback=video_pts_offset offset=%.3f input=%s",
                av_gap,
                input_path,
            )
        else:
            # av_gap 在 (-0.5, -0.05) 区间的小幅"音频晚于视频"也按 adelay 处理，
            # 不再裁视频前段（保留 raw 完整内容；这部分视频在 raw 真实状态里就是无声画面）。
            delay_seconds = abs(av_gap)
            delay_ms = int(round(delay_seconds * 1000.0))
            audio_filter = f"asetpts=PTS-STARTPTS,adelay={delay_ms}|{delay_ms}:all=1"
            _log.info(
                "rebuild_zero_based_timeline fallback=audio_later_than_video_adelay offset=%.3f delay_ms=%d input=%s",
                delay_seconds,
                delay_ms,
                input_path,
            )
    def _rebuild_with_decoded_audio(rebuild_reason: str) -> bool:
        tmp_wav = out.with_name(out.stem + f".audio.{rebuild_reason}.wav")
        try:
            tmp_wav.unlink(missing_ok=True)
        except Exception:
            pass
        decode_cmd = [
            _ffmpeg_bin(),
            "-y",
            "-fflags",
            "+genpts",
            "-analyzeduration",
            "100M",
            "-probesize",
            "100M",
            "-i",
            input_path,
            "-vn",
            "-map",
            "0:a:0",
            "-af",
            "aresample=async=1:first_pts=0",
            "-c:a",
            "pcm_s16le",
        ]
        try:
            sample_rate = int(audio_info.get("sample_rate") or 0)
        except Exception:
            sample_rate = 0
        try:
            channels = int(audio_info.get("channels") or 0)
        except Exception:
            channels = 0
        if sample_rate > 0:
            decode_cmd.extend(["-ar", str(sample_rate)])
        if channels > 0:
            decode_cmd.extend(["-ac", str(channels)])
        decode_cmd.append(str(tmp_wav))
        bitrate = _audio_bitrate_candidates()[0]
        sample_audio_cmd = [
            _ffmpeg_bin(),
            "-y",
            "-fflags",
            "+genpts",
            "-analyzeduration",
            "100M",
            "-probesize",
            "100M",
            "-i",
            input_path,
            "-i",
            str(tmp_wav),
            "-map",
            "0:v:0",
            "-vf",
            video_filter,
            *_video_encode_args(),
            "-map",
            "1:a:0",
            *_audio_encode_args(bitrate),
            "-movflags",
            "+faststart",
            str(out),
        ]
        try:
            _log.info(
                "rebuild_zero_based_timeline strategy=%s mode=decoded_wav_rebuild trim=%.3f content_offset=%.3f input=%s",
                rebuild_reason,
                precise_trim,
                audio_content_offset,
                input_path,
            )
            _run(decode_cmd, cancel_check=cancel_check)
            _run(sample_audio_cmd, cancel_check=cancel_check)
            _validate_audio_after_rebuild(0.0, preserve_audio_duration=True)
            _log.info(
                "rebuild_zero_based_timeline strategy=%s mode=decoded_wav_rebuild success output=%s",
                rebuild_reason,
                output_path,
            )
            return True
        except Exception as e:
            if str(e) in {"cancelled:pause", "cancelled:stop"}:
                raise
            _log.warning(
                "rebuild_zero_based_timeline strategy=%s mode=decoded_wav_rebuild failed input=%s err=%s",
                rebuild_reason,
                input_path,
                e,
            )
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
            return False
        finally:
            try:
                tmp_wav.unlink(missing_ok=True)
            except Exception:
                pass
    if has_audio and audio_packet_timeline_disorder and precise_trim > 0.05 and not force_full_rebuild:
        _log.info(
            "rebuild_zero_based_timeline strategy=audio_packet_timeline_disorder fallback=decoded_wav_rebuild trim=%.3f first_back_pts=%.3f input=%s",
            precise_trim,
            first_backward_pts_time,
            input_path,
        )
        if _rebuild_with_decoded_audio("audio_packet_timeline_disorder"):
            return
    target_codec = _normalize_target_video_codec(target_video_codec)
    source_codec = _source_video_codec()
    can_copy_video = bool(
        has_audio
        and not force_full_rebuild
        and not audio_packet_timeline_disorder
        and video_filter == "setpts=PTS-STARTPTS"
        and (not target_codec or not source_codec or source_codec == target_codec)
    )
    if can_copy_video:
        audio_bitrate = _audio_bitrate_candidates()[0]
        copy_video_cmd = [
            _ffmpeg_bin(),
            "-y",
            "-fflags",
            "+genpts",
            "-analyzeduration",
            "100M",
            "-probesize",
            "100M",
            "-i",
            input_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "copy",
            "-af",
            audio_filter,
            *_audio_encode_args(audio_bitrate),
            "-movflags",
            "+faststart",
            str(out),
        ]
        try:
            _log.info(
                "rebuild_zero_based_timeline final_mux_copy_video video_codec=%s audio_filter=%s audio_bitrate=%s input=%s",
                source_codec or "unknown",
                audio_filter,
                audio_bitrate,
                input_path,
            )
            _run(copy_video_cmd, cancel_check=cancel_check)
            rebuilt_starts = _probe_stream_start_times(str(out))
            rebuilt_video_start = abs(float(rebuilt_starts.get("video") or 0.0))
            rebuilt_audio_start = abs(float(rebuilt_starts.get("audio") or 0.0))
            if rebuilt_video_start > 0.25 or rebuilt_audio_start > 0.25:
                raise RuntimeError(
                    f"copy_video_rebuild_start_not_zero:video_start={rebuilt_video_start:.3f}:audio_start={rebuilt_audio_start:.3f}"
                )
            _validate_audio_after_rebuild(applied_audio_trim, preserve_audio_duration=applied_audio_trim <= 0.05)
            _log.info("rebuild_zero_based_timeline final_mux_copy_video success output=%s", output_path)
            return
        except Exception as e:
            if str(e) in {"cancelled:pause", "cancelled:stop"}:
                try:
                    out.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            _log.warning(
                "rebuild_zero_based_timeline final_mux_copy_video failed, fallback to video reencode input=%s err=%s",
                input_path,
                e,
            )
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
    base_cmd[base_cmd.index("setpts=PTS-STARTPTS")] = video_filter
    cmd_with_audio = base_cmd + [
        "-map",
        "0:a:0?",
        "-af",
        audio_filter,
        "-c:a",
        "aac",
        "-b:a",
        "256k",
        "-movflags",
        "+faststart",
        str(out),
    ]
    try:
        _log.info(
            "rebuild_zero_based_timeline final_mux video_filter=%s audio_filter=%s input=%s",
            video_filter,
            audio_filter,
            input_path,
        )
        _run(cmd_with_audio, cancel_check=cancel_check)
        _validate_audio_after_rebuild(applied_audio_trim, preserve_audio_duration=applied_audio_trim <= 0.05)
        _log.info("rebuild_zero_based_timeline final_mux success output=%s", output_path)
        return
    except Exception as e:
        if str(e) in {"cancelled:pause", "cancelled:stop"}:
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        _log.warning(
            "rebuild_zero_based_timeline final_mux failed input=%s err=%s",
            input_path,
            e,
        )
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
        err_text = str(e or "")
        if has_audio and precise_trim > 0.05 and (
            "audio_duration_shrunk_after_timestamp_only_rebuild" in err_text
            or "audio_duration_far_too_short_after_rebuild" in err_text
        ):
            _log.info(
                "rebuild_zero_based_timeline fallback=decoded_audio_preserve_after_shrink trim=%.3f content_offset=%.3f timestamp_only=%s input=%s",
                precise_trim,
                audio_content_offset,
                timestamp_only_audio_late,
                input_path,
            )
            if _rebuild_with_decoded_audio("audio_preserve_after_shrink"):
                return
        if has_audio:
            _log.info(
                "rebuild_zero_based_timeline fallback=decoded_audio_after_final_mux_error trim=%.3f content_offset=%.3f force_full=%s err=%s input=%s",
                precise_trim,
                audio_content_offset,
                force_full_rebuild,
                err_text[:300],
                input_path,
            )
            if _rebuild_with_decoded_audio("final_mux_error"):
                return
    if has_audio:
        raise RuntimeError(
            f"audio_rebuild_preserve_failed:input={input_path}:trim={precise_trim:.3f}:av_gap={av_gap:.3f}"
        )
    cmd_without_audio = base_cmd + [
        "-an",
        "-movflags",
        "+faststart",
        str(out),
    ]
    _run(cmd_without_audio, cancel_check=cancel_check)


def extract_wav(input_path: str, output_path: str, sample_rate: int = 16000) -> None:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not has_audio_stream(input_path):
        _log.warning("输入文件无音频流，生成静音WAV: %s", input_path)
        # 获取视频时长，生成对应时长的静音wav
        dur = probe_duration_seconds(input_path)
        if dur <= 0:
            dur = 60.0  # fallback
        cmd = [
            _ffmpeg_bin(), "-y",
            "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
            "-t", str(dur),
            "-ar", str(sample_rate),
            "-ac", "1",
            "-f", "wav",
            str(out),
        ]
        _log.info("extract_wav ffmpeg cmd: %s", subprocess.list2cmdline(cmd))
        _run(cmd)
        return

    dur = probe_duration_seconds(input_path)
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-i",
        input_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-af",
        "aresample=async=1",  # 保持音频时间同步，修复 PTS 不连续导致的时间偏移
    ]
    if dur > 0:
        cmd += ["-t", str(dur)]
    cmd += [
        "-f",
        "wav",
        str(out),
    ]
    _log.info("extract_wav ffmpeg cmd: %s", subprocess.list2cmdline(cmd))
    _run(cmd)


def embed_srt_to_mp4(video_path: str, srt_path: str, output_path: str) -> None:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-i",
        video_path,
        "-i",
        srt_path,
        "-map",
        "0:v",
        "-map",
        "0:a",
        "-map",
        "1",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-c:s",
        "mov_text",
        "-metadata:s:s:0",
        "language=chi",
        "-movflags",
        "+faststart",
        str(out),
    ]
    _run(cmd)


def generate_hls_1080p(input_path: str, out_dir: str, segment_time: int = 6, on_line: Callable[[str], None] | None = None) -> str:
    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    playlist = out_root / "index.m3u8"
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-i",
        input_path,
        "-vf",
        "scale=-2:1080",
        "-c:v",
        "libx265",
        "-preset",
        "fast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-f",
        "hls",
        "-hls_time",
        str(int(segment_time)),
        "-hls_playlist_type",
        "vod",
        "-hls_segment_filename",
        str(out_root / "seg_%05d.ts"),
        str(playlist),
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    try:
        while True:
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                line = line.strip()
                if on_line:
                    on_line(line)
            if line == "" and proc.poll() is not None:
                break
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: exit={proc.returncode}")
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
        except Exception:
            pass
    return str(playlist)


def ffmpeg_exists() -> bool:
    b = _ffmpeg_bin()
    return bool(shutil.which(b) or Path(b).exists())


_TIME_RE = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})")
_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")


def parse_ffmpeg_time_seconds(line: str) -> float | None:
    m = _TIME_RE.search(line)
    if not m:
        return None
    h, mm, ss, cs = [int(x) for x in m.groups()]
    return h * 3600 + mm * 60 + ss + cs / 100.0


def _parse_ffmpeg_progress_time_seconds(raw: str) -> float | None:
    text = str(raw or "").strip()
    if not text or text in {"N/A", "-9223372036854775808"}:
        return None
    try:
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            micros = int(text)
            if micros <= 0:
                return 0.0
            return micros / 1_000_000.0
    except Exception:
        pass
    try:
        parts = text.split(":")
        if len(parts) == 3:
            h = int(parts[0])
            mm = int(parts[1])
            ss = float(parts[2])
            return h * 3600 + mm * 60 + ss
    except Exception:
        pass
    return None


def probe_video_info(path: str) -> dict:
    cache_key = _media_probe_cache_key(path)
    return _probe_video_info_cached(*cache_key)


@functools.lru_cache(maxsize=512)
def _probe_video_info_cached(path: str, _size: int, _mtime_ns: int) -> dict:
    """Probe video stream info: width, height, fps."""
    import json as _json
    p = _ffprobe_bin()
    cmd = [p, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,avg_frame_rate", "-of", "json", path]
    try:
        out = _check_output_with_timeout(cmd, stderr=subprocess.STDOUT)
        data = _json.loads(out)
        st = (data.get("streams") or [{}])[0]
        fr = st.get("avg_frame_rate", "0/1")
        try:
            num, den = fr.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 0.0
        except Exception:
            fps = 0.0
        return {"width": st.get("width") or 0, "height": st.get("height") or 0, "fps": fps}
    except Exception:
        return {"width": 0, "height": 0, "fps": 0.0}


def probe_bitrate_bps(path: str) -> int:
    cache_key = _media_probe_cache_key(path)
    return _probe_bitrate_bps_cached(*cache_key)


@functools.lru_cache(maxsize=512)
def _probe_bitrate_bps_cached(path: str, _size: int, _mtime_ns: int) -> int:
    p = _ffprobe_bin()
    cmd = [p, "-v", "error", "-show_entries", "format=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        out = _check_output_with_timeout(cmd, stderr=subprocess.STDOUT).strip()
        return int(out) if out and out.isdigit() else 0
    except Exception:
        return 0


def _has_encoder(name: str) -> bool:
    try:
        out = _check_output_with_timeout([_ffmpeg_bin(), "-hide_banner", "-encoders"], stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore", timeout=FFPROBE_TIMEOUT_SECONDS)
        return name in out
    except Exception:
        return False


def transcode_crf_progress(
    input_path: str,
    output_path: str,
    *,
    duration_sec: float = 0.0,
    crf: int = 28,
    preset: str = "fast",
    height: int = 1080,
    maxrate: str | None = None,
    bufsize: str | None = None,
    on_progress: Callable[[float, float], None] | None = None,
) -> None:
    """CRF-based transcode with progress callback(progress_0_1, speed_x)."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if duration_sec <= 0:
        duration_sec = probe_authoritative_duration_seconds(input_path)

    base_cmd = [
        _ffmpeg_bin(), "-y", "-i", input_path,
        "-vf", f"scale=-2:{height}",
    ]
    audio_tail = ["-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)]

    cmds: list[list[str]] = []
    # Try NVENC first
    if _has_encoder("h264_nvenc"):
        nvenc = base_cmd + ["-c:v", "h264_nvenc", "-preset", "p6", "-rc:v", "vbr"]
        if maxrate and bufsize:
            nvenc += ["-b:v", maxrate, "-maxrate", maxrate, "-bufsize", bufsize]
        else:
            nvenc += ["-b:v", "3M", "-maxrate", "3M", "-bufsize", "6M"]
        cmds.append(nvenc + audio_tail)
    # Intel QSV fallback (works on Intel CPUs with integrated graphics)
    if _has_encoder("h264_qsv"):
        qsv = base_cmd + ["-c:v", "h264_qsv", "-preset", "veryfast", "-look_ahead", "0"]
        if maxrate and bufsize:
            qsv += ["-b:v", maxrate, "-maxrate", maxrate, "-bufsize", bufsize]
        else:
            qsv += ["-global_quality", str(max(1, int(crf)))]
        cmds.append(qsv + audio_tail)
    # libx264 fallback
    x264 = base_cmd + ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    if maxrate and bufsize:
        x264 += ["-maxrate", maxrate, "-bufsize", bufsize]
    cmds.append(x264 + audio_tail)

    last_err: Exception | None = None
    for cmd in cmds:
        try:
            _run_with_progress(cmd, duration_sec, on_progress)
            return
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise last_err


def generate_hls_crf_progress(
    input_path: str,
    out_dir: str,
    *,
    duration_sec: float = 0.0,
    target_duration_sec: float | None = None,
    segment_time: int = 6,
    crf: int = 28,
    preset: str = "fast",
    height: int = 1080,
    maxrate: str | None = None,
    bufsize: str | None = None,
    on_progress: Callable[[float, float], None] | None = None,
    cancel_check: Callable[[], str | None] | None = None,
    resume_start_sec: float = 0.0,
    start_number: int = 0,
    append: bool = False,
    audio_copy: bool = False,
    audio_bitrate: str = "192k",
    audio_sample_rate: int | None = 48000,
    audio_channels: int | None = 2,
) -> str:
    """HLS segmented CRF transcode with progress callback(progress_0_1, speed_x). Returns m3u8 path."""
    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    playlist = out_root / "index.m3u8"
    if duration_sec <= 0:
        duration_sec = probe_authoritative_duration_seconds(input_path)
    input_duration_sec = float(duration_sec or 0.0)
    output_duration_sec = float(target_duration_sec or input_duration_sec or 0.0)
    if output_duration_sec <= 0:
        output_duration_sec = input_duration_sec

    seek_sec = 0.0
    try:
        seek_sec = max(0.0, float(resume_start_sec or 0.0))
    except Exception:
        seek_sec = 0.0

    vf = f"scale=-2:{height}"
    af: str | None = None
    pad_sec = 0.0
    if output_duration_sec > input_duration_sec and input_duration_sec > 0:
        pad_sec = max(0.0, float(output_duration_sec) - float(input_duration_sec))
        if pad_sec > 0:
            vf += f",tpad=stop_mode=clone:stop_duration={pad_sec:.3f}"
            af = f"apad=pad_dur={pad_sec:.3f},aformat=sample_rates=48000:channel_layouts=stereo"

    if seek_sec > 0:
        base_cmd = [_ffmpeg_bin(), "-y", "-ss", str(seek_sec), "-i", input_path, "-vf", vf]
    else:
        base_cmd = [_ffmpeg_bin(), "-y", "-i", input_path, "-vf", vf]
    if af:
        base_cmd += ["-af", af]

    run_duration_sec = float(output_duration_sec or input_duration_sec or 0.0)
    if seek_sec > 0 and output_duration_sec > 0:
        run_duration_sec = max(0.1, float(output_duration_sec) - float(seek_sec))

    audio_present = has_audio_stream(input_path)
    hls_common_tail: list[str] = [
        "-f", "hls", "-hls_time", str(int(segment_time)),
        "-hls_playlist_type", "vod",
        "-start_number", str(int(start_number or 0)),
    ]
    if run_duration_sec > 0:
        hls_common_tail += ["-t", f"{float(run_duration_sec):.3f}"]
    if append:
        hls_common_tail += ["-hls_flags", "append_list"]
    hls_common_tail += [
        "-hls_segment_filename", str(out_root / "seg_%05d.ts"),
        str(playlist),
    ]

    audio_tails: list[list[str]] = []
    if bool(audio_copy) and not af and audio_present:
        audio_tails.append(["-c:a", "copy"] + hls_common_tail)
    if audio_present:
        transcode_audio = ["-c:a", "aac", "-b:a", str(audio_bitrate or "192k")]
        if audio_sample_rate is not None and int(audio_sample_rate or 0) > 0:
            transcode_audio += ["-ar", str(int(audio_sample_rate))]
        if audio_channels is not None and int(audio_channels or 0) > 0:
            transcode_audio += ["-ac", str(int(audio_channels))]
        audio_tails.append(transcode_audio + hls_common_tail)
    else:
        audio_tails.append(["-an"] + hls_common_tail)

    cmds: list[list[str]] = []
    if _has_encoder("h264_nvenc"):
        nvenc = base_cmd + ["-c:v", "h264_nvenc", "-preset", "p6", "-rc:v", "vbr"]
        if maxrate and bufsize:
            nvenc += ["-b:v", maxrate, "-maxrate", maxrate, "-bufsize", bufsize]
        else:
            nvenc += ["-b:v", "3M", "-maxrate", "3M", "-bufsize", "6M"]
        for audio_tail in audio_tails:
            cmds.append(nvenc + audio_tail)
    if _has_encoder("h264_qsv"):
        qsv = base_cmd + ["-c:v", "h264_qsv", "-preset", "veryfast", "-look_ahead", "0"]
        if maxrate and bufsize:
            qsv += ["-b:v", maxrate, "-maxrate", maxrate, "-bufsize", bufsize]
        else:
            qsv += ["-global_quality", str(max(1, int(crf)))]
        for audio_tail in audio_tails:
            cmds.append(qsv + audio_tail)
    x264 = base_cmd + ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    if maxrate and bufsize:
        x264 += ["-maxrate", maxrate, "-bufsize", bufsize]
    for audio_tail in audio_tails:
        cmds.append(x264 + audio_tail)

    last_err: Exception | None = None
    for idx, cmd in enumerate(cmds, start=1):
        try:
            _run_with_progress(cmd, run_duration_sec, on_progress, cancel_check, log_error=False)
            return str(playlist)
        except Exception as e:
            last_err = e
            if idx < len(cmds):
                _log.warning(
                    "ffmpeg candidate failed attempt=%s/%s cmd=%s err=%s",
                    idx,
                    len(cmds),
                    " ".join(cmd[:6]) + " ...",
                    e,
                )
            continue
    if last_err:
        _log.error(
            "all ffmpeg candidates failed input=%s out=%s attempts=%s last_error=%s",
            input_path,
            out_dir,
            len(cmds),
            last_err,
        )
        raise last_err
    return str(playlist)


def _run_with_progress(
    cmd: list[str],
    duration_sec: float,
    on_progress: Callable[[float, float], None] | None,
    cancel_check: Callable[[], str | None] | None = None,
    log_error: bool = True,
) -> None:
    progress_cmd = list(cmd)
    if "-progress" not in progress_cmd:
        progress_cmd = [progress_cmd[0], "-progress", "pipe:1", "-nostats", *progress_cmd[1:]]
    proc = subprocess.Popen(
        progress_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )

    q: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            if proc.stdout is None:
                q.put(None)
                return
            for line in proc.stdout:
                q.put(line)
        finally:
            q.put(None)

    th = threading.Thread(target=_reader, daemon=True)
    th.start()

    last_lines: list[str] = []
    progress_state: dict[str, str] = {}
    heartbeat_interval_sec = 10.0
    stall_timeout_sec = 180.0
    output_idle_timeout_sec = 60.0
    started_ts = time.monotonic()
    last_output_ts = float(started_ts)
    last_progress_change_ts = float(started_ts)
    last_progress_sec: float | None = None
    last_speed = 0.0
    last_emit_ts = time.monotonic()

    def _emit_progress(progress_sec: float | None, speed: float, *, force: bool = False) -> None:
        nonlocal last_emit_ts, last_progress_sec, last_speed
        if progress_sec is None or duration_sec <= 0 or on_progress is None:
            return
        now = time.monotonic()
        if not force and (now - last_emit_ts) < heartbeat_interval_sec:
            return
        p = min(1.0, max(0.0, float(progress_sec) / float(duration_sec)))
        last_progress_sec = float(progress_sec)
        last_speed = float(speed)
        last_emit_ts = now
        on_progress(p, float(speed))

    def _fail_for_stall(reason: str) -> None:
        tail = "\n".join(last_lines[-15:]) if last_lines else "(no output)"
        last_progress_text = f"{float(last_progress_sec):.3f}" if last_progress_sec is not None else "none"
        _log.error(
            "ffmpeg stalled reason=%s last_progress=%s cmd=%s\n%s",
            reason,
            last_progress_text,
            " ".join(cmd[:6]) + " ...",
            tail,
        )
        with contextlib.suppress(Exception):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
        raise RuntimeError(f"ffmpeg transcode stalled:{reason}")

    def _check_stall() -> None:
        now = time.monotonic()
        output_idle_sec = float(now - last_output_ts)
        progress_idle_sec = float(now - last_progress_change_ts)
        if last_progress_sec is None:
            startup_idle_sec = float(now - started_ts)
            if startup_idle_sec >= stall_timeout_sec and output_idle_sec >= output_idle_timeout_sec:
                _fail_for_stall(f"startup_no_progress:{startup_idle_sec:.1f}s")
            return
        if progress_idle_sec >= stall_timeout_sec and output_idle_sec >= output_idle_timeout_sec:
            _fail_for_stall(f"no_progress:{progress_idle_sec:.1f}s")

    try:
        while True:
            if cancel_check:
                mode = cancel_check()
                if mode in {"pause", "stop"}:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    raise RuntimeError(f"cancelled:{mode}")

            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                _check_stall()
                _emit_progress(last_progress_sec, last_speed)
                continue

            if line is None:
                if proc.poll() is not None:
                    break
                _check_stall()
                _emit_progress(last_progress_sec, last_speed)
                continue

            last_output_ts = time.monotonic()
            last_lines.append(line.rstrip())
            if len(last_lines) > 30:
                last_lines.pop(0)

            stripped = line.strip()
            if stripped.startswith("out_time_ms="):
                progress_state["out_time_ms"] = stripped.split("=", 1)[1].strip()
            elif stripped.startswith("out_time="):
                progress_state["out_time"] = stripped.split("=", 1)[1].strip()
            elif stripped.startswith("speed="):
                progress_state["speed"] = stripped.split("=", 1)[1].strip()
            elif stripped == "progress=continue":
                t = _parse_ffmpeg_progress_time_seconds(progress_state.get("out_time_ms") or progress_state.get("out_time") or "")
                speed_raw = str(progress_state.get("speed") or "").strip().lower()
                try:
                    speed = float(speed_raw[:-1]) if speed_raw.endswith("x") else float(speed_raw or 0.0)
                except Exception:
                    speed = 0.0
                if t is not None and (last_progress_sec is None or abs(float(t) - float(last_progress_sec)) > 0.001):
                    last_progress_change_ts = time.monotonic()
                _emit_progress(t, speed, force=True)
                progress_state.clear()
                continue
            elif stripped == "progress=end":
                t = _parse_ffmpeg_progress_time_seconds(progress_state.get("out_time_ms") or progress_state.get("out_time") or "")
                speed_raw = str(progress_state.get("speed") or "").strip().lower()
                try:
                    speed = float(speed_raw[:-1]) if speed_raw.endswith("x") else float(speed_raw or 0.0)
                except Exception:
                    speed = 0.0
                if t is not None and (last_progress_sec is None or abs(float(t) - float(last_progress_sec)) > 0.001):
                    last_progress_change_ts = time.monotonic()
                _emit_progress(t, speed, force=True)
                progress_state.clear()
                continue

        try:
            code = proc.wait(timeout=3)
        except Exception:
            code = proc.poll()

        if code not in (0, None):
            tail = "\n".join(last_lines[-15:]) if last_lines else "(no output)"
            if log_error:
                _log.error("ffmpeg failed exit=%s cmd=%s\n%s", code, " ".join(cmd[:6]) + " ...", tail)
            raise RuntimeError(f"ffmpeg transcode failed: exit={code}")
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
