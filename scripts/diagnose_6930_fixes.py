"""Ad-hoc repair flow for task 6930.

Steps:
1. 对每个分段检查音频编码，若是 pcm_alaw/pcm_mulaw/mp2 则转封装到 AAC；
2. 将处理后的 MP4 统一转为 TS，写入 concat 列表；
3. 执行 TS concat copy 生成 mp4；
4. 若最终视频时长与各段视频时长之和差值超过阈值，则对 TS 列表做 CFR 重编码兜底。

运行示例：
    python scripts/diagnose_6930_fixes.py --src-dir id=6930 --work-dir id=6930_diag
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
AUDIO_NEEDS_AAC = {"pcm_alaw", "pcm_mulaw", "mp2"}
DURATION_MISMATCH_THRESHOLD = 1.0  # seconds


def run(cmd: list[str]) -> None:
    print("[cmd]", " ".join(cmd))
    subprocess.run(cmd, check=True)


def probe_audio_codec(path: Path) -> str:
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        return ""
    return str(streams[0].get("codec_name") or "").lower()


def probe_video_duration(path: Path) -> float:
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=duration",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    try:
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def probe_format_duration(path: Path) -> float:
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    try:
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def ensure_aac_audio(src: Path, work_dir: Path) -> Path:
    codec = probe_audio_codec(src)
    if not codec or codec not in AUDIO_NEEDS_AAC:
        print(f"[audio] {src.name} codec={codec or 'unknown'} -> reuse")
        return src
    dst = work_dir / f"{src.stem}.aacfix.mp4"
    cmd = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    run(cmd)
    return dst


def convert_to_ts(src: Path, ts_dir: Path, index: int) -> Path:
    ts_dir.mkdir(parents=True, exist_ok=True)
    dst = ts_dir / f"part{index:03d}.ts"
    cmd = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-bsf:v",
        "hevc_mp4toannexb",
        "-f",
        "mpegts",
        str(dst),
    ]
    run(cmd)
    return dst


def write_concat_file(paths: Iterable[Path], concat_path: Path) -> None:
    lines = [f"file '{p.resolve().as_posix()}'" for p in paths]
    concat_path.write_text("\n".join(lines), encoding="utf-8")


def concat_ts_copy(concat_file: Path, output: Path) -> None:
    cmd = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart",
        str(output),
    ]
    run(cmd)


def concat_ts_cfr(concat_file: Path, output: Path) -> None:
    cmd = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-filter_complex",
        "[0:v]setpts=PTS-STARTPTS,fps=25[v];[0:a]aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    run(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose and repair task 6930 segments")
    parser.add_argument("--src-dir", type=Path, default=ROOT / "id=6930")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "id=6930_diag")
    parser.add_argument("--threshold", type=float, default=DURATION_MISMATCH_THRESHOLD)
    args = parser.parse_args()

    src_dir: Path = args.src_dir
    work_dir: Path = args.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    ts_dir = work_dir / "ts"
    aac_dir = work_dir / "aac"
    outputs_dir = work_dir / "outputs"
    for folder in (ts_dir, aac_dir, outputs_dir):
        folder.mkdir(parents=True, exist_ok=True)

    parts = sorted(src_dir.glob("student_6930.part*.mp4"))
    if not parts:
        print("[error] no parts found")
        return 1

    processed_parts: list[Path] = []
    total_expected = 0.0
    for idx, src in enumerate(parts, start=1):
        print(f"\n=== processing {src.name} ===")
        normalized = ensure_aac_audio(src, aac_dir)
        processed_parts.append(normalized)
        total_expected += probe_video_duration(normalized)

    ts_paths: list[Path] = []
    for idx, part in enumerate(processed_parts, start=1):
        ts_paths.append(convert_to_ts(part, ts_dir, idx))

    concat_file = work_dir / "concat.txt"
    write_concat_file(ts_paths, concat_file)
    ts_copy_mp4 = outputs_dir / "student_6930.tsbridge.mp4"
    concat_ts_copy(concat_file, ts_copy_mp4)

    merged_duration = probe_video_duration(ts_copy_mp4)
    duration_gap = abs(merged_duration - total_expected)
    print(f"[probe] expected_total={total_expected:.3f}s merged={merged_duration:.3f}s gap={duration_gap:.3f}s")

    final_output = ts_copy_mp4
    if duration_gap > args.threshold:
        print("[warn] duration mismatch exceeds threshold, running CFR repair ...")
        final_output = outputs_dir / "student_6930.tsbridge.cfr.mp4"
        concat_ts_cfr(concat_file, final_output)
        merged_duration = probe_format_duration(final_output)
        print(f"[cfr] final_duration={merged_duration:.3f}s")

    print(f"\n[done] final output: {final_output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
