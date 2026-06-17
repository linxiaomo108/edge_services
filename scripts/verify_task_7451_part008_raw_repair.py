from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from edge_service.tasks.camera import (  # noqa: E402
    _collect_structural_media_metrics,
    _format_structural_media_metrics,
    _probe_precise_av_sync,
    _validate_download_part_rebuild_output,
)
from edge_service.video import ffmpeg as ffmpeg_mod  # noqa: E402
from edge_service.video.ffmpeg import rebuild_zero_based_timeline  # noqa: E402


DEFAULT_INPUT = ROOT / "id=7451" / "teacher_7451.part008.mp4"
DEFAULT_OUT_DIR = ROOT / "id=7451_part008_raw_repair_out"


class Tee:
    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        written = 0
        for stream in self._streams:
            stream.write(data)
            stream.flush()
            written = max(written, len(data))
        return written

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_path.open("w", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log_fp)
    sys.stderr = Tee(sys.__stderr__, log_fp)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def fmt_elapsed(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    minutes = int(seconds // 60)
    remain = seconds - minutes * 60
    return f"{minutes}m{remain:.1f}s"


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def probe_bundle(path: Path) -> dict[str, object]:
    precise = _probe_precise_av_sync(path)
    metrics = _collect_structural_media_metrics(path, include_packet_metrics=False)
    validation_issue = _validate_download_part_rebuild_output(path, include_packet_metrics=False)
    return {
        "path": str(path),
        "precise": precise,
        "metrics": metrics,
        "metrics_text": _format_structural_media_metrics(metrics),
        "validation_issue": validation_issue or "",
    }


def print_probe(label: str, bundle: dict[str, object]) -> None:
    precise = bundle["precise"]
    print(
        f"[{label}] path={bundle['path']}\n"
        f"  classification={precise.get('classification')} reason={precise.get('reason')}\n"
        f"  video_start={float(precise.get('video_start') or 0.0):.3f}s "
        f"audio_start={float(precise.get('audio_start') or 0.0):.3f}s "
        f"coarse_gap={float(precise.get('coarse_gap') or 0.0):.3f}s "
        f"precise_trim={float(precise.get('precise_trim') or 0.0):.3f}s "
        f"audio_content_offset={float(precise.get('audio_content_offset') or 0.0):.3f}s\n"
        f"  metrics={bundle['metrics_text']}\n"
        f"  validation_issue={bundle['validation_issue'] or 'ok'}"
    )


def stage_input(src: Path, work_dir: Path, *, refresh: bool) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    dst = work_dir / src.name
    if refresh and dst.exists():
        dst.unlink()
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dst)
    return dst


def _safe_stem_label(src: Path) -> str:
    return src.stem.replace("=", "_").replace(" ", "_")


def attempt_official_raw_rebuild(src: Path, out_dir: Path) -> dict[str, object]:
    out = out_dir / f"{src.stem}.official_raw.fixed.mp4"
    out.unlink(missing_ok=True)
    started = time.perf_counter()
    try:
        rebuild_zero_based_timeline(str(src), str(out))
        elapsed = time.perf_counter() - started
        bundle = probe_bundle(out)
        return {
            "name": "official_raw_rebuild",
            "status": "ok",
            "elapsed_sec": elapsed,
            "output": str(out),
            "probe": bundle,
        }
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return {
            "name": "official_raw_rebuild",
            "status": "failed",
            "elapsed_sec": elapsed,
            "error": str(exc),
            "output": str(out),
        }


def _zero_based_video_only(src: Path, dst: Path) -> None:
    cmd = [
        ffmpeg_mod._ffmpeg_bin(),
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-an",
        "-avoid_negative_ts",
        "make_zero",
        "-reset_timestamps",
        "1",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    _run(cmd)


def _probe_video_duration(path: Path) -> float:
    metrics = _collect_structural_media_metrics(path, include_packet_metrics=False)
    duration = float(metrics.get("video_duration") or 0.0)
    if duration <= 0.0:
        raise RuntimeError(f"video_duration_missing:{path}")
    return duration


def _mux_zero_based_video_with_trimmed_audio(
    *,
    zero_video: Path,
    raw_src: Path,
    dst: Path,
    trim_sec: float,
    target_duration: float,
) -> None:
    audio_filter = (
        f"atrim=start={max(0.0, trim_sec):.3f},"
        f"asetpts=PTS-STARTPTS,"
        f"apad=whole_dur={target_duration:.3f},"
        f"atrim=end={target_duration:.3f},"
        f"asetpts=PTS-STARTPTS"
    )
    cmd = [
        ffmpeg_mod._ffmpeg_bin(),
        "-y",
        "-fflags",
        "+genpts",
        "-analyzeduration",
        "100M",
        "-probesize",
        "100M",
        "-i",
        str(zero_video),
        "-i",
        str(raw_src),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-c:v",
        "copy",
        "-af",
        audio_filter,
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-avoid_negative_ts",
        "make_zero",
        "-reset_timestamps",
        "1",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    _run(cmd)


def _mux_zero_based_video_with_decoded_audio(
    *,
    zero_video: Path,
    raw_src: Path,
    dst: Path,
    trim_sec: float,
    target_duration: float,
) -> None:
    wav_path = dst.with_suffix(".audio.tmp.wav")
    wav_path.unlink(missing_ok=True)
    try:
        decode_cmd = [
            ffmpeg_mod._ffmpeg_bin(),
            "-y",
            "-fflags",
            "+genpts",
            "-analyzeduration",
            "100M",
            "-probesize",
            "100M",
            "-i",
            str(raw_src),
            "-vn",
            "-map",
            "0:a:0",
            "-af",
            "aresample=async=1:first_pts=0",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ]
        _run(decode_cmd)
        audio_filter = (
            f"atrim=start={max(0.0, trim_sec):.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"apad=whole_dur={target_duration:.3f},"
            f"atrim=end={target_duration:.3f},"
            f"asetpts=PTS-STARTPTS"
        )
        mux_cmd = [
            ffmpeg_mod._ffmpeg_bin(),
            "-y",
            "-fflags",
            "+genpts",
            "-analyzeduration",
            "100M",
            "-probesize",
            "100M",
            "-i",
            str(zero_video),
            "-i",
            str(wav_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-af",
            audio_filter,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-avoid_negative_ts",
            "make_zero",
            "-reset_timestamps",
            "1",
            "-movflags",
            "+faststart",
            str(dst),
        ]
        _run(mux_cmd)
    finally:
        wav_path.unlink(missing_ok=True)


def attempt_videozero_experiment(src: Path, out_dir: Path) -> dict[str, object]:
    precise = _probe_precise_av_sync(src)
    trim_sec = float(precise.get("precise_trim") or 0.0)
    zero_video = out_dir / f"{src.stem}.videozero.tmp.mp4"
    out_copy = out_dir / f"{src.stem}.videozero_audio_trim.fixed.mp4"
    out_decoded = out_dir / f"{src.stem}.videozero_audio_decoded.fixed.mp4"
    for path in (zero_video, out_copy, out_decoded):
        path.unlink(missing_ok=True)

    started = time.perf_counter()
    result: dict[str, object] = {
        "name": "videozero_audio_trim_experiment",
        "status": "failed",
        "elapsed_sec": 0.0,
        "trim_sec": trim_sec,
    }
    try:
        _zero_based_video_only(src, zero_video)
        target_duration = _probe_video_duration(zero_video)
        result["videozero_duration_sec"] = target_duration
        try:
            _mux_zero_based_video_with_trimmed_audio(
                zero_video=zero_video,
                raw_src=src,
                dst=out_copy,
                trim_sec=trim_sec,
                target_duration=target_duration,
            )
            bundle = probe_bundle(out_copy)
            result.update(
                {
                    "status": "ok",
                    "path_used": "copy_video_plus_trimmed_audio",
                    "output": str(out_copy),
                    "probe": bundle,
                }
            )
        except Exception as exc_copy:
            result["copy_path_error"] = str(exc_copy)
            _mux_zero_based_video_with_decoded_audio(
                zero_video=zero_video,
                raw_src=src,
                dst=out_decoded,
                trim_sec=trim_sec,
                target_duration=target_duration,
            )
            bundle = probe_bundle(out_decoded)
            result.update(
                {
                    "status": "ok",
                    "path_used": "copy_video_plus_decoded_audio",
                    "output": str(out_decoded),
                    "probe": bundle,
                }
            )
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        result["elapsed_sec"] = time.perf_counter() - started
    return result


def print_attempt_result(result: dict[str, object]) -> None:
    name = result["name"]
    print(f"\n=== {name} ===")
    print(f"status={result['status']} elapsed={fmt_elapsed(float(result.get('elapsed_sec') or 0.0))}")
    if result.get("trim_sec") is not None:
        print(f"trim_sec={float(result.get('trim_sec') or 0.0):.3f}")
    if result.get("videozero_duration_sec") is not None:
        print(f"videozero_duration_sec={float(result.get('videozero_duration_sec') or 0.0):.3f}")
    if result.get("path_used"):
        print(f"path_used={result['path_used']}")
    if result.get("output"):
        print(f"output={result['output']}")
    if result.get("error"):
        print(f"error={result['error']}")
    if result.get("copy_path_error"):
        print(f"copy_path_error={result['copy_path_error']}")
    probe = result.get("probe")
    if isinstance(probe, dict):
        print_probe(name + ":output", probe)


def main() -> None:
    parser = argparse.ArgumentParser(description="Experiment for abnormal raw part repair without systrans")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Raw part mp4 path")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Output directory")
    parser.add_argument("--refresh-input", action="store_true", help="Restage input even if staged file already exists")
    parser.add_argument("--log-file", type=Path, default=None, help="Log file path")
    args = parser.parse_args()

    src = args.input if args.input.is_absolute() else (ROOT / args.input)
    if not src.exists():
        raise SystemExit(f"input not found: {src}")

    out_dir = args.out_dir if args.out_dir.is_absolute() else (ROOT / args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    label = _safe_stem_label(src)
    log_file = args.log_file
    if log_file is None:
        log_file = out_dir / f"{label}.raw_repair.log"
    elif not log_file.is_absolute():
        log_file = ROOT / log_file
    setup_logging(log_file)

    print(f"[start] input={src}")
    print(f"[start] out_dir={out_dir}")
    print(f"[start] log_file={log_file}")

    staged_input = stage_input(src, out_dir / "staged_input", refresh=args.refresh_input)
    print(f"[stage] staged_input={staged_input}")

    source_bundle = probe_bundle(staged_input)
    print_probe("source", source_bundle)

    official_result = attempt_official_raw_rebuild(staged_input, out_dir)
    print_attempt_result(official_result)

    experiment_result = attempt_videozero_experiment(staged_input, out_dir)
    print_attempt_result(experiment_result)

    summary = {
        "input": str(staged_input),
        "source": source_bundle,
        "official_raw_rebuild": official_result,
        "videozero_experiment": experiment_result,
    }
    summary_path = out_dir / f"{label}.raw_repair.summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] summary={summary_path}")


if __name__ == "__main__":
    main()
