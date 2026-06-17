#!/usr/bin/env python3
"""Standalone audio-late repair utility.

This script was built to validate a safer alternative to CAM-DL-NORM-010 for
segments where audio timestamps lag video by ~1-2 s but the underlying content
is intact.  It keeps the original files untouched, applies a pure timestamp
shift (stream copy) via ffmpeg, and produces per-file verification metrics.

Typical usage (case 6835 sample):
    python scripts/audio_late_align.py \
        --inputs id=6835 \
        --output-dir output/audio_late_fix/6835 \
        --min-gap-sec 0.5 --target-gap-sec 0.05
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from edge_service.video import ffmpeg as ffmpeg_utils  # noqa: E402

FFMPEG_BIN = ffmpeg_utils.ffmpeg_bin()
FFPROBE_BIN = ffmpeg_utils._ffprobe_bin()


@dataclass
class StreamTiming:
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration if self.duration > 0 else self.start


@dataclass
class ProbeResult:
    path: Path
    video: StreamTiming
    audio: StreamTiming

    @property
    def audio_late_by(self) -> float:
        return max(0.0, self.audio.start - self.video.start)

    @property
    def has_audio(self) -> bool:
        return self.audio.duration > 0.0 or self.audio.start > 0.0


def _run_ffprobe(path: Path, selector: str) -> StreamTiming:
    cmd = [
        FFPROBE_BIN,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-select_streams",
        selector,
        str(path),
    ]
    proc = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed rc={proc.returncode}: {proc.stderr.strip()}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"ffprobe json decode error: {exc}") from exc
    streams = payload.get("streams") or []
    if not streams:
        return StreamTiming(start=0.0, duration=0.0)
    stream = streams[0] or {}
    try:
        start = float(stream.get("start_time") or 0.0)
    except Exception:
        start = 0.0
    try:
        duration = float(stream.get("duration") or 0.0)
    except Exception:
        duration = 0.0
    return StreamTiming(start=start, duration=duration)


def probe_media(path: Path) -> ProbeResult:
    video = _run_ffprobe(path, "v:0")
    audio = _run_ffprobe(path, "a:0")
    return ProbeResult(path=path, video=video, audio=audio)


def iter_target_files(sources: Iterable[Path]) -> Iterable[Path]:
    for src in sources:
        if src.is_dir():
            yield from sorted(p for p in src.glob("*.mp4") if p.is_file())
        elif src.suffix.lower() == ".mp4" and src.is_file():
            yield src


def align_audio_late(src: Path, dst: Path, gap_sec: float, *, dry_run: bool = False) -> None:
    if gap_sec <= 0:
        raise ValueError("gap_sec must be positive for audio-late alignment")
    dst.parent.mkdir(parents=True, exist_ok=True)
    offset = -gap_sec  # advance audio to match video start
    cmd: List[str] = [
        FFMPEG_BIN,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-itsoffset",
        f"{offset:.6f}",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-c",
        "copy",
        "-reset_timestamps",
        "1",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    if dry_run:
        print("DRY-RUN:", " ".join(cmd))
        return
    subprocess.run(cmd, check=True)


def build_default_output_dir(inputs: list[Path]) -> Path:
    if len(inputs) == 1 and inputs[0].is_dir():
        return inputs[0] / "aligned"
    return Path("output/audio_late_fix")


def main() -> None:
    parser = argparse.ArgumentParser(description="Align audio-late MP4 segments via stream-copy timestamp shift")
    parser.add_argument("--inputs", nargs="+", type=Path, default=[Path("id=6835")], help="Source MP4 files or directories")
    parser.add_argument("--output-dir", type=Path, default=None, help="Destination directory for aligned files")
    parser.add_argument("--min-gap-sec", type=float, default=0.5, help="Only align when audio lags video by >= this many seconds")
    parser.add_argument("--target-gap-sec", type=float, default=0.1, help="Post-processing acceptable residual gap")
    parser.add_argument("--dry-run", action="store_true", help="Print ffmpeg commands without executing")
    args = parser.parse_args()

    inputs = [p if p.is_absolute() else (Path.cwd() / p) for p in args.inputs]
    output_dir = args.output_dir
    if output_dir is None:
        output_dir = build_default_output_dir(inputs)
    output_dir.mkdir(parents=True, exist_ok=True)

    sources = list(iter_target_files(inputs))
    if not sources:
        print("No MP4 files found under:", ", ".join(str(p) for p in inputs))
        sys.exit(1)

    summary: list[dict[str, object]] = []
    for src in sources:
        probe_before = probe_media(src)
        if not probe_before.has_audio:
            summary.append({
                "path": str(src),
                "status": "skipped",
                "reason": "no_audio_stream",
            })
            continue
        gap = probe_before.audio_late_by
        if gap < args.min_gap_sec:
            summary.append({
                "path": str(src),
                "status": "skipped",
                "reason": f"audio_gap={gap:.3f}s < min_gap",
            })
            continue
        dst = output_dir / f"{src.stem}.aligned.mp4"
        try:
            align_audio_late(src, dst, gap, dry_run=args.dry_run)
        except subprocess.CalledProcessError as exc:
            summary.append({
                "path": str(src),
                "status": "failed",
                "reason": f"ffmpeg_exit_{exc.returncode}",
            })
            continue
        if args.dry_run:
            summary.append({
                "path": str(src),
                "status": "dry_run",
                "suggested_command": str(dst),
            })
            continue
        probe_after = probe_media(dst)
        residual = probe_after.audio_late_by
        summary.append({
            "path": str(src),
            "output": str(dst),
            "status": "aligned" if residual <= args.target_gap_sec else "needs_review",
            "original_gap_sec": round(gap, 3),
            "residual_gap_sec": round(residual, 3),
            "video_duration_sec": round(probe_after.video.duration, 3),
            "audio_duration_sec": round(probe_after.audio.duration, 3),
        })
        print(
            f"[aligned] {src.name}: gap {gap:.3f}s -> {residual:.3f}s -> {dst}"
            if residual <= args.target_gap_sec
            else f"[needs_review] {src.name}: gap {gap:.3f}s -> {residual:.3f}s (target {args.target_gap_sec:.3f}s)"
        )

    report_path = output_dir / "audio_late_align_report.json"
    with report_path.open("w", encoding="utf-8") as fp:
        json.dump(summary, fp, indent=2, ensure_ascii=False)
    print(f"Report written to {report_path}")


if __name__ == "__main__":
    main()
