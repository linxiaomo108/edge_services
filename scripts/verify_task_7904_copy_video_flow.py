from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from edge_service.tasks.camera import (  # noqa: E402
    _collect_structural_media_metrics,
    _concat_parts,
    _download_part_merge_ready_issue_with_options,
    _format_structural_media_metrics,
    _has_abnormal_absolute_timeline_start,
    _probe_precise_av_sync,
    _validate_download_part_rebuild_output,
)
from edge_service.video import ffmpeg as ffmpeg_mod  # noqa: E402
from edge_service.video.ffmpeg import probe_audio_stream_info, probe_stream_timings  # noqa: E402


DEFAULT_TASK_DIR = ROOT / "id=7904"


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


def detect_prefix(task_dir: Path) -> str:
    parts = sorted(task_dir.glob("*.part*.mp4"))
    if not parts:
        raise RuntimeError(f"no part mp4 files in {task_dir}")
    name = parts[0].name
    marker = ".part"
    if marker not in name:
        raise RuntimeError(f"bad part name: {name}")
    return name.split(marker, 1)[0]


def fmt_elapsed(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    minutes = int(seconds // 60)
    remain = seconds - minutes * 60
    return f"{minutes}m{remain:.1f}s"


def setup_run_logging(log_path: Path) -> None:
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


def _copy_part_with_retry(src: Path, dst: Path, *, refresh: bool) -> str:
    src_size = src.stat().st_size
    if not refresh and dst.exists() and dst.stat().st_size == src_size:
        return "reused"
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            shutil.copy2(src, dst)
            return "copied"
        except OSError as e:
            last_err = e
            winerror = int(getattr(e, "winerror", 0) or 0)
            locked = winerror in {32, 33, 1224}
            same_size = dst.exists() and dst.stat().st_size == src_size
            if locked and same_size:
                print(f"[warn] staged file locked, reusing existing copy: {dst}")
                return "reused_locked"
            if locked and attempt < 3:
                print(f"[warn] staged file busy, retrying ({attempt}/3): {dst}")
                time.sleep(1.0 * attempt)
                continue
            raise
    raise RuntimeError(f"copy_failed:{src}->{dst}:{last_err}")


def stage_inputs(src_dir: Path, work_dir: Path, prefix: str, *, refresh: bool) -> list[Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for src in sorted(src_dir.glob(f"{prefix}.part*.mp4")):
        dst = work_dir / src.name
        _copy_part_with_retry(src, dst, refresh=refresh)
        staged.append(dst)
    if not staged:
        raise RuntimeError(f"no staged parts found in {src_dir}")
    return staged


def select_parts(parts: list[Path], selected_indexes: list[int]) -> list[Path]:
    if not selected_indexes:
        return parts
    part_map = {idx: part for idx, part in enumerate(parts, start=1)}
    selected: list[Path] = []
    for idx in selected_indexes:
        part = part_map.get(int(idx))
        if part is None:
            raise RuntimeError(f"part index out of range: {idx} (total={len(parts)})")
        selected.append(part)
    return selected


def _audio_filter_for_target_duration(target_duration: float) -> str:
    return (
        f"aresample=async=1:first_pts=0,"
        f"asetpts=PTS-STARTPTS,"
        f"atrim=end={target_duration:.3f},"
        f"apad=whole_dur={target_duration:.3f},"
        f"atrim=end={target_duration:.3f},"
        f"asetpts=PTS-STARTPTS"
    )


def _is_target_family(
    precise_state: dict[str, float | bool | str],
    audio_info: dict[str, object],
) -> tuple[bool, str]:
    classification = str(precise_state.get("classification") or "").strip()
    coarse_gap = float(precise_state.get("coarse_gap") or 0.0)
    audio_content_offset = float(precise_state.get("audio_content_offset") or 0.0)
    audio_packet_timeline_disorder = bool(precise_state.get("audio_packet_timeline_disorder"))
    video_start = float(precise_state.get("video_start") or 0.0)
    audio_start = float(precise_state.get("audio_start") or 0.0)
    codec_name = str(audio_info.get("codec_name") or "").strip().lower()
    if classification != "audio_early":
        return False, f"classification={classification or 'unknown'}"
    if coarse_gap <= 0.5:
        return False, f"coarse_gap_too_small={coarse_gap:.3f}"
    if audio_content_offset > 0.2:
        return False, f"content_offset_too_large={audio_content_offset:.3f}"
    if audio_packet_timeline_disorder:
        return False, "audio_packet_timeline_disorder"
    if codec_name not in {"pcm_alaw", "pcm_mulaw"}:
        return False, f"audio_codec={codec_name or 'unknown'}"
    if not _has_abnormal_absolute_timeline_start(video_start, audio_start):
        return False, f"absolute_start_not_abnormal:video={video_start:.3f}:audio={audio_start:.3f}"
    return True, "matched_audio_early_pcm_zero_based_copy_video"


def rebuild_part_copy_video(part: Path, output: Path, *, keep_temp: bool = False) -> dict[str, object]:
    log = logging.getLogger("verify.7904.copyvideo")
    overall_start = time.perf_counter()
    output.unlink(missing_ok=True)
    video_zero_tmp = output.with_name(output.stem + ".videozero.tmp.mp4")
    video_zero_tmp.unlink(missing_ok=True)

    analyze_start = time.perf_counter()
    precise_state = _probe_precise_av_sync(part)
    audio_info = probe_audio_stream_info(str(part))
    source_timings = probe_stream_timings(str(part))
    source_metrics = _collect_structural_media_metrics(part, include_packet_metrics=False)
    analyze_elapsed = time.perf_counter() - analyze_start

    eligible, reason = _is_target_family(precise_state, audio_info)
    if not eligible:
        raise RuntimeError(f"part_not_supported_by_copy_video_experiment:{reason}")

    target_duration = float(source_metrics.get("video_duration") or 0.0)
    if target_duration <= 0.0:
        raise RuntimeError("missing_video_duration")

    log.info(
        "copy_video_experiment start input=%s classification=%s coarse_gap=%.3f codec=%s target_duration=%.3f reason=%s",
        part,
        precise_state.get("classification"),
        float(precise_state.get("coarse_gap") or 0.0),
        str(audio_info.get("codec_name") or ""),
        target_duration,
        reason,
    )

    video_zero_start = time.perf_counter()
    video_zero_cmd = [
        ffmpeg_mod._ffmpeg_bin(),
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        str(part),
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
        str(video_zero_tmp),
    ]
    ffmpeg_mod._run(video_zero_cmd)
    video_zero_elapsed = time.perf_counter() - video_zero_start

    video_zero_metrics = _collect_structural_media_metrics(video_zero_tmp, include_packet_metrics=False)
    video_zero_issue = _validate_download_part_rebuild_output(
        video_zero_tmp,
        include_packet_metrics=False,
    )

    audio_filter = _audio_filter_for_target_duration(target_duration)
    mux_start = time.perf_counter()
    copy_video_cmd = [
        ffmpeg_mod._ffmpeg_bin(),
        "-y",
        "-fflags",
        "+genpts",
        "-analyzeduration",
        "100M",
        "-probesize",
        "100M",
        "-i",
        str(video_zero_tmp),
        "-i",
        str(part),
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
        str(output),
    ]
    ffmpeg_mod._run(copy_video_cmd)
    mux_elapsed = time.perf_counter() - mux_start

    validate_start = time.perf_counter()
    output_metrics = _collect_structural_media_metrics(output, include_packet_metrics=False)
    merge_ready_issue = _download_part_merge_ready_issue_with_options(
        output,
        include_packet_metrics=False,
        defer_precise_probe_until_suspicious=True,
        deferred_start_gap_tolerance_seconds=0.2,
    )
    strict_issue = _validate_download_part_rebuild_output(output, include_packet_metrics=False)
    validate_elapsed = time.perf_counter() - validate_start

    total_elapsed = time.perf_counter() - overall_start
    if not keep_temp:
        video_zero_tmp.unlink(missing_ok=True)

    return {
        "source_metrics": source_metrics,
        "source_timings": source_timings,
        "precise_state": precise_state,
        "audio_info": audio_info,
        "eligibility_reason": reason,
        "video_zero_metrics": video_zero_metrics,
        "video_zero_issue": video_zero_issue,
        "output_metrics": output_metrics,
        "merge_ready_issue": merge_ready_issue,
        "strict_issue": strict_issue,
        "audio_filter": audio_filter,
        "analyze_elapsed": analyze_elapsed,
        "video_zero_elapsed": video_zero_elapsed,
        "mux_elapsed": mux_elapsed,
        "validate_elapsed": validate_elapsed,
        "total_elapsed": total_elapsed,
        "output_path": output,
        "video_zero_tmp": video_zero_tmp,
    }


def main() -> int:
    total_start = time.perf_counter()
    parser = argparse.ArgumentParser(
        description="Experiment repair flow for task 7904 family using optimized judgment + copy-video final mux"
    )
    parser.add_argument("--src-dir", type=Path, default=DEFAULT_TASK_DIR)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--output-name", type=str, default="student_7094.copy-video-flow.mp4")
    parser.add_argument("--refresh-inputs", action="store_true")
    parser.add_argument("--clean-intermediates", action="store_true")
    parser.add_argument("--keep-temp", action="store_true", help="keep per-part temporary video-only zero-based files")
    parser.add_argument("--keep-merge-temp", action="store_true", help="keep _concat_parts temporary files for inspection")
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--part-index", type=int, action="append", default=[], help="only run the specified 1-based part index; can be used multiple times")
    parser.add_argument("--force-merge-single", action="store_true", help="when only one part is selected, still run _concat_parts")
    parser.add_argument("--full-merge-preflight", action="store_true", help="do not skip adjacent merge preflight")
    args = parser.parse_args()

    src_dir = args.src_dir.resolve()
    if not src_dir.exists():
        print(f"[error] source dir not found: {src_dir}")
        return 1

    prefix = detect_prefix(src_dir)
    work_dir = (args.work_dir or (src_dir.parent / f"{src_dir.name}_copy_video_repair_out")).resolve()
    default_log_name = f"verify_task_7904_copy_video_flow.{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    log_path = (args.log_file.resolve() if args.log_file else (work_dir / default_log_name))
    setup_run_logging(log_path)

    staged_parts = stage_inputs(src_dir, work_dir, prefix, refresh=args.refresh_inputs)
    parts = select_parts(staged_parts, [int(x) for x in args.part_index])

    if args.clean_intermediates:
        keep = {p.name for p in parts} | {args.output_name, log_path.name}
        for path in work_dir.iterdir():
            if path.name in keep:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink()
                except Exception:
                    pass

    print(f"source_dir={src_dir}")
    print(f"work_dir={work_dir}")
    print(f"log_file={log_path}")
    print(f"prefix={prefix}")
    print(f"part_count={len(parts)}")
    if args.part_index:
        print(f"selected_parts={','.join(str(int(x)) for x in args.part_index)}")
    print("strategy=optimized_judgment + video_zero_copy + final_mux_copy_video")
    print("formal_flow_modified=False")

    merge_inputs: list[Path] = []
    expected_total = 0.0
    part_elapsed: list[tuple[str, float]] = []
    for idx, part in enumerate(parts, start=1):
        repaired = work_dir / f"{part.stem}.copyvideo.fixed.mp4"
        print(f"\n===== copy-video repair part {idx}/{len(parts)}: {part.name} =====")
        result = rebuild_part_copy_video(part, repaired, keep_temp=args.keep_temp)
        part_elapsed.append((part.name, float(result["total_elapsed"] or 0.0)))

        source_metrics = result["source_metrics"]
        source_timings = result["source_timings"]
        precise_state = result["precise_state"]
        audio_info = result["audio_info"]
        video_zero_metrics = result["video_zero_metrics"]
        output_metrics = result["output_metrics"]
        merge_ready_issue = str(result["merge_ready_issue"] or "")
        strict_issue = str(result["strict_issue"] or "")

        print(f"  source_metrics: {_format_structural_media_metrics(source_metrics)}")
        print(
            "  source_starts:"
            f" video={float((source_timings.get('video') or {}).get('start_time') or 0.0):.3f}"
            f" audio={float((source_timings.get('audio') or {}).get('start_time') or 0.0):.3f}"
        )
        print(
            "  precise_state:"
            f" classification={precise_state.get('classification')}"
            f" coarse_gap={float(precise_state.get('coarse_gap') or 0.0):.3f}"
            f" content_offset={float(precise_state.get('audio_content_offset') or 0.0):.3f}"
            f" packet_disorder={bool(precise_state.get('audio_packet_timeline_disorder'))}"
        )
        print(
            "  audio_info:"
            f" codec={audio_info.get('codec_name')}"
            f" sample_rate={audio_info.get('sample_rate')}"
            f" channels={audio_info.get('channels')}"
        )
        print(f"  eligibility={result['eligibility_reason']}")
        print(f"  video_zero_metrics: {_format_structural_media_metrics(video_zero_metrics)}")
        print(f"  video_zero_issue={result['video_zero_issue'] or 'ok'}")
        print(f"  audio_filter={result['audio_filter']}")
        print(f"  output_metrics: {_format_structural_media_metrics(output_metrics)}")
        print(f"  strict_issue={strict_issue or 'ok'}")
        print(f"  merge_ready_issue={merge_ready_issue or 'ok'}")
        print(
            "  elapsed:"
            f" analyze={fmt_elapsed(float(result['analyze_elapsed'] or 0.0))}"
            f" video_zero={fmt_elapsed(float(result['video_zero_elapsed'] or 0.0))}"
            f" mux={fmt_elapsed(float(result['mux_elapsed'] or 0.0))}"
            f" validate={fmt_elapsed(float(result['validate_elapsed'] or 0.0))}"
            f" total={fmt_elapsed(float(result['total_elapsed'] or 0.0))}"
        )
        if strict_issue or merge_ready_issue:
            print(f"[FAIL] repaired part not merge-ready: {repaired}")
            return 2
        merge_inputs.append(repaired)
        expected_total += float(output_metrics.get("video_duration") or 0.0)

    if len(merge_inputs) == 1 and not args.force_merge_single:
        only_part = merge_inputs[0]
        print("\n[single-part]")
        print("  merge_skipped=True")
        print(f"  output={only_part}")
        print(f"  total={fmt_elapsed(time.perf_counter() - total_start)}")
        print("[OK] single part copy-video verification succeeded")
        return 0

    final_out = work_dir / args.output_name
    final_out.unlink(missing_ok=True)

    print("\n===== merge via _concat_parts =====")
    merge_start = time.perf_counter()
    temp_files = _concat_parts(
        merge_inputs,
        final_out,
        force_canonical_merge=False,
        canonicalize_part_indexes=None,
        on_observation=None,
        skip_adjacent_preflight=not args.full_merge_preflight,
    )
    merge_elapsed = time.perf_counter() - merge_start
    print(f"_concat_parts temp_files={len(temp_files)}")
    print(f"skip_adjacent_preflight={not args.full_merge_preflight}")
    print(f"merge_elapsed={fmt_elapsed(merge_elapsed)}")
    if temp_files and not args.keep_merge_temp:
        cleaned_temp = 0
        for temp in temp_files:
            try:
                temp.unlink(missing_ok=True)
                cleaned_temp += 1
            except Exception as e:
                print(f"[warn] failed to remove merge temp: {temp}: {e}")
        print(f"merge_temp_cleaned={cleaned_temp}")

    if not final_out.exists() or final_out.stat().st_size <= 0:
        print(f"[FAIL] merged output missing: {final_out}")
        return 3

    final_metrics = _collect_structural_media_metrics(final_out, include_packet_metrics=False)
    final_issue = _download_part_merge_ready_issue_with_options(
        final_out,
        include_packet_metrics=False,
        defer_precise_probe_until_suspicious=True,
        deferred_start_gap_tolerance_seconds=0.2,
    )
    video_duration = float(final_metrics.get("video_duration") or 0.0)
    audio_duration = float(final_metrics.get("audio_duration") or 0.0)
    duration_match = abs(video_duration - expected_total) < 5.0 and abs(audio_duration - expected_total) < 5.0

    print(f"\n[merged] {final_out}")
    print(f"  size_mb={final_out.stat().st_size / 1048576:.1f}")
    print(f"  metrics: {_format_structural_media_metrics(final_metrics)}")
    print(f"  merge_ready_issue={final_issue or 'ok'}")
    print(f"  expected_total={expected_total:.3f}s")
    print(f"  video_duration={video_duration:.3f}s")
    print(f"  audio_duration={audio_duration:.3f}s")
    print(f"  duration_match={duration_match}")

    print("\n[timing]")
    for name, elapsed in part_elapsed:
        print(f"  {name}: {fmt_elapsed(elapsed)}")
    print(f"  merge: {fmt_elapsed(merge_elapsed)}")
    print(f"  total: {fmt_elapsed(time.perf_counter() - total_start)}")

    if final_issue:
        print("[FAIL] merged output still suspicious")
        return 4
    if not duration_match:
        print("[FAIL] merged duration mismatch")
        return 5

    print("[OK] copy-video experiment flow verification succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
