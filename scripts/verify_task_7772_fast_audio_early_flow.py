from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
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
    _detect_download_batch_av_policy,
    _download_part_merge_ready_issue_for_batch_policy,
    _download_part_merge_ready_issue_with_options,
    _ffmpeg_bin,
    _format_structural_media_metrics,
    _normalize_download_part,
    _probe_precise_av_sync,
    _probe_structural_media_issue,
    _probe_timeline_state,
)


DEFAULT_TASK_DIR = ROOT / "id7772"
DEFAULT_WORK_DIR = ROOT / "id7772_fast_audio_early_out"
AV_SYNC_TOLERANCE_SECONDS = 0.30
AUDIO_EARLY_SCENE_MIN_SECONDS = 0.30
AUDIO_EARLY_SCENE_MAX_SECONDS = 8.0
ABSOLUTE_START_MIN_SECONDS = 60.0


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


def fmt_elapsed(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    minutes = int(seconds // 60)
    remain = seconds - minutes * 60
    return f"{minutes}m{remain:.1f}s"


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


def detect_prefix(task_dir: Path) -> str:
    parts = sorted(task_dir.glob("*.part*.mp4"))
    if not parts:
        raise RuntimeError(f"no part mp4 files in {task_dir}")
    marker = ".part"
    name = parts[0].name
    if marker not in name:
        raise RuntimeError(f"bad part name: {name}")
    return name.split(marker, 1)[0]


def copy_part(src: Path, dst: Path, *, refresh: bool) -> str:
    src_size = src.stat().st_size
    if not refresh and dst.exists() and dst.stat().st_size == src_size:
        return "reused"
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            shutil.copy2(src, dst)
            return "copied"
        except OSError as exc:
            last_err = exc
            winerror = int(getattr(exc, "winerror", 0) or 0)
            if winerror in {32, 33, 1224} and attempt < 3:
                print(f"[warn] staged file busy, retrying ({attempt}/3): {dst}")
                time.sleep(float(attempt))
                continue
            raise
    raise RuntimeError(f"copy_failed:{src}->{dst}:{last_err}")


def stage_inputs(src_dir: Path, work_dir: Path, prefix: str, *, refresh: bool) -> list[Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for src in sorted(src_dir.glob(f"{prefix}.part*.mp4")):
        dst = work_dir / src.name
        status = copy_part(src, dst, refresh=refresh)
        print(f"[stage] {status}: {src.name} -> {dst}")
        staged.append(dst)
    if not staged:
        raise RuntimeError(f"no staged parts found in {src_dir}")
    return staged


def _run_ffmpeg(cmd: list[str], label: str) -> tuple[bool, str, float]:
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
    except Exception as exc:
        return False, str(exc), time.perf_counter() - start
    elapsed = time.perf_counter() - start
    output = str(proc.stdout or "")
    if proc.returncode != 0:
        print(f"[{label}] ffmpeg failed rc={proc.returncode} elapsed={fmt_elapsed(elapsed)} tail={output[-1000:]}")
        return False, output, elapsed
    return True, output, elapsed


def _duration_value(metrics: dict[str, float], key: str) -> float:
    try:
        return float(metrics.get(key) or 0.0)
    except Exception:
        return 0.0


def _classify_download_part_scene(part: Path) -> dict[str, object]:
    """Detect the source structure before choosing a repair branch.

    This mirrors the production idea: the branch is selected from observed media
    facts, not from task id or a pre-known offset.
    """
    state = _probe_timeline_state(part)
    precise = _probe_precise_av_sync(part, include_packet_timeline=False)
    metrics = _collect_structural_media_metrics(part, include_packet_metrics=False)
    video_start = float(state.get("video_start") or 0.0)
    audio_start = float(state.get("audio_start") or 0.0)
    media_start = float(state.get("media_start") or 0.0)
    video_duration = _duration_value(metrics, "video_duration")
    audio_duration = _duration_value(metrics, "audio_duration")
    start_gap = audio_start - video_start
    audio_early_by = max(0.0, -start_gap)
    classification = str(precise.get("classification") or state.get("classification") or "").strip()
    duration_delta = abs(video_duration - audio_duration) if video_duration and audio_duration else 999999.0
    packet_disorder = "packet_disorder" in classification.lower()
    stable_audio_early = (
        classification == "audio_early"
        and AUDIO_EARLY_SCENE_MIN_SECONDS < audio_early_by <= AUDIO_EARLY_SCENE_MAX_SECONDS
        and media_start >= ABSOLUTE_START_MIN_SECONDS
        and duration_delta <= max(1.0, audio_early_by + 0.5)
        and not packet_disorder
    )
    if stable_audio_early:
        scene = "stable_absolute_audio_early"
        route = "fast_audio_early"
    else:
        scene = classification or "official_default"
        route = "official_normalize"
    reason = (
        f"classification={classification or '-'} media_start={media_start:.3f} "
        f"video_start={video_start:.3f} audio_start={audio_start:.3f} "
        f"start_gap={start_gap:.3f} audio_early_by={audio_early_by:.3f} "
        f"video_duration={video_duration:.3f} audio_duration={audio_duration:.3f} "
        f"duration_delta={duration_delta:.3f} packet_disorder={packet_disorder}"
    )
    return {
        "scene": scene,
        "route": route,
        "reason": reason,
        "audio_early_by": audio_early_by,
        "source_video_duration": video_duration,
        "classification": classification,
    }


def _validate_shifted_audio_output(
    out_path: Path,
    *,
    expected_audio_delay: float,
    source_video_duration: float,
    allow_audio_start_delay: bool,
    require_concat_duration_tight: bool = False,
    expect_video_delayed_from_zero: bool = False,
) -> str:
    if not out_path.exists() or out_path.stat().st_size <= 0:
        return "output_missing"
    structural_issue = _probe_structural_media_issue(out_path)
    if structural_issue:
        return f"structural_invalid:{structural_issue}"
    metrics = _collect_structural_media_metrics(out_path, include_packet_metrics=False)
    video_duration = _duration_value(metrics, "video_duration")
    audio_duration = _duration_value(metrics, "audio_duration")
    format_duration = _duration_value(metrics, "format_duration")
    authoritative_duration = _duration_value(metrics, "authoritative_duration")
    video_start = _duration_value(metrics, "video_start")
    audio_start = _duration_value(metrics, "audio_start")
    if video_duration <= 0:
        return "video_duration_invalid"
    if source_video_duration > 0 and abs(video_duration - source_video_duration) > 1.0:
        return f"video_duration_changed:src={source_video_duration:.3f}:out={video_duration:.3f}"
    if audio_duration <= 0:
        return "audio_duration_invalid"
    if allow_audio_start_delay:
        actual_delay = audio_start - video_start
        if abs(actual_delay - expected_audio_delay) > AV_SYNC_TOLERANCE_SECONDS:
            return f"audio_delay_unexpected:expected={expected_audio_delay:.3f}:actual={actual_delay:.3f}"
    elif expect_video_delayed_from_zero:
        if abs(audio_start) > AV_SYNC_TOLERANCE_SECONDS:
            return f"audio_start_not_zero:audio={audio_start:.3f}"
        if abs(video_start - expected_audio_delay) > max(AV_SYNC_TOLERANCE_SECONDS, 0.15):
            return f"video_delay_unexpected:expected={expected_audio_delay:.3f}:video={video_start:.3f}"
    else:
        if abs(audio_start - video_start) > AV_SYNC_TOLERANCE_SECONDS:
            return f"start_gap_unexpected:video={video_start:.3f}:audio={audio_start:.3f}"
    if abs(audio_duration - video_duration) > max(1.0, expected_audio_delay + 0.5):
        return f"duration_delta_large:video={video_duration:.3f}:audio={audio_duration:.3f}"
    if require_concat_duration_tight:
        container_duration = max(format_duration, authoritative_duration, video_duration)
        media_end = max(video_start + video_duration, audio_start + audio_duration)
        if abs(container_duration - media_end) > 0.75:
            return f"container_duration_mismatch:container={container_duration:.3f}:media_end={media_end:.3f}:video_start={video_start:.3f}:audio_start={audio_start:.3f}"
    return ""


def _repair_audio_early_copy(part: Path, out_path: Path, *, audio_early_by: float, source_video_duration: float) -> tuple[bool, str, float]:
    tmp = out_path.with_name(out_path.stem + ".audioearly.copy.tmp.mp4")
    tmp.unlink(missing_ok=True)
    cmd = [
        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(part),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy",
        "-bsf:v", "setts=pts=PTS-STARTPTS:dts=DTS-STARTDTS",
        "-bsf:a", "setts=pts=PTS-STARTPTS:dts=DTS-STARTDTS",
        "-avoid_negative_ts", "make_zero",
        "-reset_timestamps", "1",
        "-fflags", "+genpts",
        "-movflags", "+faststart",
        str(tmp),
    ]
    ok, output, elapsed = _run_ffmpeg(cmd, "audio-copy")
    if not ok:
        tmp.unlink(missing_ok=True)
        return False, f"ffmpeg_failed:{output[-300:]}", elapsed
    issue = _validate_shifted_audio_output(
        tmp,
        expected_audio_delay=0.0,
        source_video_duration=source_video_duration,
        allow_audio_start_delay=False,
        require_concat_duration_tight=True,
    )
    if issue:
        tmp.unlink(missing_ok=True)
        return False, issue, elapsed
    tmp.replace(out_path)
    return True, "", elapsed


def _repair_audio_early_audio_only(part: Path, out_path: Path, *, audio_early_by: float, source_video_duration: float) -> tuple[bool, str, float]:
    tmp = out_path.with_name(out_path.stem + ".audioearly.aac.tmp.mp4")
    tmp.unlink(missing_ok=True)
    cmd = [
        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(part),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "copy",
        "-bsf:v", "setts=pts=PTS-STARTPTS:dts=DTS-STARTDTS",
        "-af", "asetpts=N/SR/TB",
        "-c:a", "aac",
        "-b:a", "256k",
        "-ar", "44100",
        "-ac", "2",
        "-avoid_negative_ts", "make_zero",
        "-reset_timestamps", "1",
        "-fflags", "+genpts",
        "-movflags", "+faststart",
    ]
    cmd.append(str(tmp))
    ok, output, elapsed = _run_ffmpeg(cmd, "audio-only")
    if not ok:
        tmp.unlink(missing_ok=True)
        return False, f"ffmpeg_failed:{output[-300:]}", elapsed
    issue = _validate_shifted_audio_output(
        tmp,
        expected_audio_delay=0.0,
        source_video_duration=source_video_duration,
        allow_audio_start_delay=False,
        require_concat_duration_tight=True,
        expect_video_delayed_from_zero=False,
    )
    if issue:
        tmp.unlink(missing_ok=True)
        return False, issue, elapsed
    tmp.replace(out_path)
    return True, "", elapsed


def normalize_audio_early_fast(part: Path, out_path: Path, scene: dict[str, object]) -> tuple[Path, str, float, float]:
    start = time.perf_counter()
    if str(scene.get("route") or "") != "fast_audio_early":
        raise RuntimeError(f"not_target_audio_early_scene:{part.name}:{scene.get('reason')}")
    audio_early_by = float(scene.get("audio_early_by") or 0.0)
    source_video_duration = float(scene.get("source_video_duration") or 0.0)

    ok, reason, copy_elapsed = _repair_audio_early_copy(
        part,
        out_path,
        audio_early_by=audio_early_by,
        source_video_duration=source_video_duration,
    )
    if ok:
        elapsed = time.perf_counter() - start
        print(f"[part-ok] {part.name} branch=timeline_rebase_copy elapsed={fmt_elapsed(elapsed)} copy_step={fmt_elapsed(copy_elapsed)} original_audio_early_by={audio_early_by:.3f}s out={out_path}")
        return out_path, "timeline_rebase_copy", elapsed, 0.0
    print(f"[part-fallback] {part.name} copy path rejected reason={reason}; retry audio timestamp rebuild with video copy")

    ok, reason, audio_elapsed = _repair_audio_early_audio_only(
        part,
        out_path,
        audio_early_by=audio_early_by,
        source_video_duration=source_video_duration,
    )
    if ok:
        elapsed = time.perf_counter() - start
        print(f"[part-ok] {part.name} branch=audio_timestamp_rebuild_video_copy elapsed={fmt_elapsed(elapsed)} audio_step={fmt_elapsed(audio_elapsed)} original_audio_early_by={audio_early_by:.3f}s out={out_path}")
        return out_path, "audio_timestamp_rebuild_video_copy", elapsed, 0.0
    raise RuntimeError(f"audio_early_fast_repair_failed:{part.name}:{reason}")


def normalize_by_detected_scene(
    part: Path,
    out_path: Path,
    *,
    policy: object | None,
    total_start: float,
    learned_routes: dict[str, str],
) -> tuple[Path, str, float, float | None, bool]:
    start = time.perf_counter()
    scene = _classify_download_part_scene(part)
    print(f"[scene] {part.name} scene={scene['scene']} route={scene['route']} {scene['reason']}")
    if scene["route"] == "fast_audio_early":
        learned_route = learned_routes.get(str(scene["scene"]))
        if learned_route == "audio_timestamp_rebuild_video_copy":
            audio_early_by = float(scene.get("audio_early_by") or 0.0)
            source_video_duration = float(scene.get("source_video_duration") or 0.0)
            print(
                f"[shortcut] {part.name} scene={scene['scene']} "
                f"reuse_branch=audio_timestamp_rebuild_video_copy reason=pilot_parts_confirmed_copy_path_invalid"
            )
            branch_start = time.perf_counter()
            ok, reason, audio_elapsed = _repair_audio_early_audio_only(
                part,
                out_path,
                audio_early_by=audio_early_by,
                source_video_duration=source_video_duration,
            )
            if not ok:
                raise RuntimeError(f"audio_early_fast_shortcut_failed:{part.name}:{reason}")
            elapsed = time.perf_counter() - branch_start
            print(f"[part-ok] {part.name} branch=audio_timestamp_rebuild_video_copy elapsed={fmt_elapsed(elapsed)} audio_step={fmt_elapsed(audio_elapsed)} original_audio_early_by={audio_early_by:.3f}s out={out_path}")
            return out_path, "audio_timestamp_rebuild_video_copy", elapsed, 0.0, True
        merge_part, branch, elapsed, expected_delay = normalize_audio_early_fast(part, out_path, scene)
        if branch == "audio_timestamp_rebuild_video_copy":
            learned_routes[str(scene["scene"])] = "audio_timestamp_rebuild_video_copy"
        return merge_part, branch, elapsed, expected_delay, True

    print(f"[official] {part.name} route=official_normalize reason=scene_not_fast_audio_early")

    def on_status(msg: str) -> None:
        print(f"  status: {msg}")

    result = _normalize_download_part(
        part,
        task_type=0,
        on_status=on_status,
        total_elapsed_supplier=lambda: time.perf_counter() - total_start,
        cancel_check=lambda: None,
        batch_av_policy=policy,
    )
    elapsed = time.perf_counter() - start
    merge_part = Path(result.merge_part_path) if result.merge_part_path else part
    branch = f"official:{result.normalize_reason or result.classification or 'default'}"
    print(
        f"[part-ok] {part.name} branch={branch} elapsed={fmt_elapsed(elapsed)} "
        f"classification={result.classification} force_canonical_merge={result.force_canonical_merge}"
    )
    return merge_part, branch, elapsed, None, False


def _audio_early_merge_ready_issue(video_path: Path, *, expected_audio_delay: float) -> str:
    """Experiment-specific validation for 7772-like outputs.

    The formal merge-ready probe treats a positive audio start as suspicious.
    For this experiment, positive audio start is the intended result: it is the
    silence inserted to delay originally-early audio back to the video timeline.
    """
    metrics = _collect_structural_media_metrics(video_path, include_packet_metrics=False)
    video_duration = _duration_value(metrics, "video_duration")
    audio_duration = _duration_value(metrics, "audio_duration")
    video_start = _duration_value(metrics, "video_start")
    audio_start = _duration_value(metrics, "audio_start")
    if video_duration <= 0:
        return "video_duration_invalid"
    if audio_duration <= 0:
        return "audio_duration_invalid"
    actual_delay = audio_start - video_start
    if abs(actual_delay - expected_audio_delay) > AV_SYNC_TOLERANCE_SECONDS:
        return f"audio_delay_unexpected:expected={expected_audio_delay:.3f}:actual={actual_delay:.3f}"
    if abs(video_duration - audio_duration) > max(1.0, expected_audio_delay + 0.5):
        return f"duration_delta_large:video={video_duration:.3f}:audio={audio_duration:.3f}"
    structural_issue = _probe_structural_media_issue(video_path)
    if structural_issue:
        return f"structural_invalid:{structural_issue}"
    return ""


def main() -> int:
    total_start = time.perf_counter()
    parser = argparse.ArgumentParser(description="Fast experimental repair flow for task 7772 audio_early scene")
    parser.add_argument("--src-dir", type=Path, default=DEFAULT_TASK_DIR)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--output-name", default="student_7772.fast-audio-early.mp4")
    parser.add_argument("--refresh-inputs", action="store_true")
    parser.add_argument("--clean-intermediates", action="store_true")
    parser.add_argument("--keep-merge-temp", action="store_true")
    parser.add_argument("--detect-batch-policy", action="store_true", help="Run the slower official batch policy probe before per-part repair.")
    parser.add_argument("--log-file", type=Path, default=None)
    args = parser.parse_args()

    src_dir = args.src_dir.resolve()
    work_dir = args.work_dir.resolve()
    log_path = (args.log_file.resolve() if args.log_file else work_dir / f"verify_task_7772_fast_audio_early.{datetime.now().strftime('%Y%m%d-%H%M%S')}.log")
    setup_logging(log_path)

    print(f"source_dir={src_dir}")
    print(f"work_dir={work_dir}")
    print(f"log_file={log_path}")
    prefix = detect_prefix(src_dir)
    print(f"prefix={prefix}")
    parts = stage_inputs(src_dir, work_dir, prefix, refresh=args.refresh_inputs)
    print(f"part_count={len(parts)}")

    if args.clean_intermediates:
        keep = {p.name for p in parts} | {log_path.name}
        for path in work_dir.iterdir():
            if path.name in keep:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    policy = None
    if args.detect_batch_policy:
        try:
            policy = _detect_download_batch_av_policy(parts)
            print(
                "batch_policy="
                f"{policy.name} median_gap={policy.start_gap_median:.3f}s "
                f"range={policy.start_gap_range:.3f}s reason={policy.reason}"
            )
        except Exception as exc:
            print(f"[warn] batch policy detection failed: {exc}")
    else:
        print("batch_policy=skipped reason=7772_fast_audio_early_experiment_uses_per_part_scene_detection")

    merge_inputs: list[Path] = []
    timings: list[tuple[str, str, float, float]] = []
    expected_total = 0.0
    expected_delays: list[float] = []
    learned_routes: dict[str, str] = {}
    for idx, part in enumerate(parts, start=1):
        print(f"\n===== fast normalize part {idx}/{len(parts)}: {part.name} =====")
        out = work_dir / f"{part.stem}.fastfixed.mp4"
        merge_part, branch, elapsed, expected_delay, is_fast_audio_early = normalize_by_detected_scene(
            part,
            out,
            policy=policy,
            total_start=total_start,
            learned_routes=learned_routes,
        )
        metrics = _collect_structural_media_metrics(merge_part, include_packet_metrics=False)
        if is_fast_audio_early and expected_delay is not None:
            issue = _audio_early_merge_ready_issue(merge_part, expected_audio_delay=expected_delay)
            issue_label = "audio_early_scene_issue"
        else:
            batch_policy_name = str(getattr(policy, "name", "") or "")
            issue = _download_part_merge_ready_issue_for_batch_policy(
                merge_part,
                batch_policy_name,
                include_packet_metrics=False,
            )
            issue_label = "official_merge_ready_issue"
        print(f"  metrics: {_format_structural_media_metrics(metrics)}")
        print(f"  {issue_label}={issue or 'ok'}")
        if issue:
            raise RuntimeError(f"merge_ready_failed:{merge_part.name}:{issue}")
        expected_total += _duration_value(metrics, "video_duration")
        merge_inputs.append(merge_part)
        if expected_delay is not None:
            expected_delays.append(expected_delay)
        timings.append((part.name, branch, elapsed, expected_delay))

    final_out = work_dir / args.output_name
    final_out.unlink(missing_ok=True)
    print("\n===== merge via official _concat_parts, no forced canonical =====")
    merge_start = time.perf_counter()
    temp_files = _concat_parts(
        merge_inputs,
        final_out,
        force_canonical_merge=False,
        canonicalize_part_indexes=None,
        on_observation=lambda data: print(f"  merge_observation={data}"),
        skip_adjacent_preflight=True,
        boundary_video_gap_action="observe",
        force_ts_bridge=False,
    )
    merge_elapsed = time.perf_counter() - merge_start
    print(f"merge_elapsed={fmt_elapsed(merge_elapsed)} temp_files={len(temp_files)}")
    if temp_files and not args.keep_merge_temp:
        cleaned = 0
        for temp in temp_files:
            try:
                temp.unlink(missing_ok=True)
                cleaned += 1
            except Exception as exc:
                print(f"[warn] temp cleanup failed: {temp}: {exc}")
        print(f"merge_temp_cleaned={cleaned}")

    if not final_out.exists() or final_out.stat().st_size <= 0:
        raise RuntimeError(f"final_output_missing:{final_out}")
    final_metrics = _collect_structural_media_metrics(final_out, include_packet_metrics=False)
    if expected_delays and len(expected_delays) == len(merge_inputs):
        expected_final_delay = sorted(expected_delays)[len(expected_delays) // 2]
        final_issue = _audio_early_merge_ready_issue(final_out, expected_audio_delay=expected_final_delay)
        final_issue_label = "audio_early_scene_issue"
    else:
        final_issue = _download_part_merge_ready_issue_with_options(
            final_out,
            include_packet_metrics=False,
            defer_precise_probe_until_suspicious=True,
            deferred_start_gap_tolerance_seconds=0.3,
        )
        final_issue_label = "official_merge_ready_issue"
    video_duration = _duration_value(final_metrics, "video_duration")
    audio_duration = _duration_value(final_metrics, "audio_duration")
    duration_match = abs(video_duration - expected_total) < 5.0 and abs(audio_duration - expected_total) < 5.0
    print(f"\n[merged] {final_out}")
    print(f"  size_mb={final_out.stat().st_size / 1048576:.1f}")
    print(f"  metrics: {_format_structural_media_metrics(final_metrics)}")
    print(f"  {final_issue_label}={final_issue or 'ok'}")
    print(f"  expected_total={expected_total:.3f}s video_duration={video_duration:.3f}s audio_duration={audio_duration:.3f}s duration_match={duration_match}")
    print("\n[timing]")
    for name, branch, elapsed, expected_delay in timings:
        delay_text = f" expected_audio_delay={expected_delay:.3f}s" if expected_delay is not None else ""
        print(f"  {name}: {fmt_elapsed(elapsed)} branch={branch}{delay_text}")
    print(f"  merge: {fmt_elapsed(merge_elapsed)}")
    print(f"  total: {fmt_elapsed(time.perf_counter() - total_start)}")
    if final_issue:
        raise RuntimeError(f"final_merge_ready_failed:{final_issue}")
    if not duration_match:
        raise RuntimeError("final_duration_mismatch")
    print("[OK] fast audio_early verification succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
