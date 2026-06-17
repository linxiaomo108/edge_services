from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..video.ffmpeg import extract_wav
from ..device_detector import get_whisper_device


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


def _get_model_params() -> tuple[str, str]:
    """获取模型设备和计算类型"""
    device = get_whisper_device()
    if device == "cuda":
        compute_type = os.getenv("EDGE_WHISPER_COMPUTE_TYPE", "float16")
    else:
        compute_type = os.getenv("EDGE_WHISPER_COMPUTE_TYPE", "int8")
    return device, compute_type


def transcribe_to_segments(wav_path: str) -> list[Segment]:
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        raise RuntimeError("faster_whisper not available") from e

    device, compute_type = _get_model_params()
    model = WhisperModel("medium", device=device, compute_type=compute_type)
    segments, _ = model.transcribe(wav_path, language="zh", vad_filter=False, beam_size=3, initial_prompt="简体中文")
    out: list[Segment] = []
    for s in segments:
        txt = str(getattr(s, "text", "")).strip()
        if not txt:
            continue
        st = float(getattr(s, "start", 0.0) or 0.0)
        et = float(getattr(s, "end", st + 2.0) or (st + 2.0))
        out.append(Segment(start=st, end=et, text=txt))
    return out


def write_srt(segments: list[Segment], out_path: str) -> None:
    def _ts(sec: float) -> str:
        sec = max(0.0, float(sec))
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int(round((sec - int(sec)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{_ts(seg.start)} --> {_ts(seg.end)}")
        lines.append(seg.text)
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")


def transcribe_video_to_srt(video_path: str, srt_path: str, sample_rate: int = 16000) -> None:
    wav_path = str(Path(srt_path).with_suffix(".tmp.wav"))
    extract_wav(video_path, wav_path, sample_rate=sample_rate)
    segs = transcribe_to_segments(wav_path)
    try:
        Path(wav_path).unlink(missing_ok=True)
    except Exception:
        pass
    if not segs:
        raise RuntimeError("empty transcription")
    write_srt(segs, srt_path)
