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
    ABSOLUTE_TIMELINE_TS_BRIDGE_POLICY,
    _collect_structural_media_metrics,
    _concat_parts,
    _detect_download_batch_av_policy,
    _download_part_merge_ready_issue_for_batch_policy,
    _download_part_merge_ready_issue_with_options,
    _format_structural_media_metrics,
    _is_expected_tail_silence_output,
    _normalize_download_part,
    _part_relative_alignment_healthy,
    _source_part_has_no_audio_stream,
)


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


def stage_selected_inputs(src_dir: Path, work_dir: Path, prefix: str, selected_indexes: list[int], *, refresh: bool) -> list[Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for idx in selected_indexes:
        src = src_dir / f"{prefix}.part{int(idx):03d}.mp4"
        if not src.exists():
            raise RuntimeError(f"selected part not found: {src}")
        dst = work_dir / src.name
        _copy_part_with_retry(src, dst, refresh=refresh)
        staged.append(dst)
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


def main() -> int:
    total_start = time.perf_counter()
    parser = argparse.ArgumentParser(description="Run official camera repair + merge flow on local source parts")
    parser.add_argument("--src-dir", type=Path, default=DEFAULT_TASK_DIR)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--output-name", type=str, default="student_7094.official-flow.mp4")
    parser.add_argument("--task-type", type=int, default=0)
    parser.add_argument("--refresh-inputs", action="store_true")
    parser.add_argument("--clean-intermediates", action="store_true")
    parser.add_argument("--keep-merge-temp", action="store_true", help="keep _concat_parts temporary files for inspection")
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--skip-batch-policy", action="store_true", help="skip download batch AV policy detection")
    parser.add_argument("--part-index", type=int, action="append", default=[], help="only run the specified 1-based part index; can be used multiple times")
    parser.add_argument("--force-merge-single", action="store_true", help="when only one part is selected, still run _concat_parts")
    args = parser.parse_args()

    src_dir = args.src_dir.resolve()
    if not src_dir.exists():
        print(f"[error] source dir not found: {src_dir}")
        return 1

    prefix = detect_prefix(src_dir)
    work_dir = (args.work_dir or (src_dir.parent / f"{src_dir.name}_official_flow_out")).resolve()
    default_log_name = f"verify_task_7904_official_flow.{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    log_path = (args.log_file.resolve() if args.log_file else (work_dir / default_log_name))
    setup_run_logging(log_path)
    selected_indexes = [int(x) for x in args.part_index]
    staged_parts = (
        stage_selected_inputs(src_dir, work_dir, prefix, selected_indexes, refresh=args.refresh_inputs)
        if selected_indexes
        else stage_inputs(src_dir, work_dir, prefix, refresh=args.refresh_inputs)
    )
    parts = staged_parts if selected_indexes else select_parts(staged_parts, selected_indexes)

    print(f"source_dir={src_dir}")
    print(f"work_dir={work_dir}")
    print(f"log_file={log_path}")
    print(f"prefix={prefix}")
    print(f"part_count={len(parts)}")
    if args.part_index:
        print(f"selected_parts={','.join(str(int(x)) for x in args.part_index)}")

    policy = None
    if args.skip_batch_policy:
        print("batch_policy=skipped")
    else:
        policy = _detect_download_batch_av_policy(staged_parts)
        print(
            "batch_policy="
            f"{policy.name} median_gap={policy.start_gap_median:.3f}s "
            f"range={policy.start_gap_range:.3f}s part_count={policy.part_count} "
            f"reason={policy.reason} target_video_codec={policy.target_video_codec or ''} "
            f"video_codec_mix={bool(policy.video_codec_mix)}"
        )

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

    merge_inputs: list[Path] = []
    expected_total = 0.0
    part_elapsed: list[tuple[str, float]] = []
    for idx, part in enumerate(parts, start=1):
        print(f"\n===== official normalize part {idx}/{len(parts)}: {part.name} =====")

        def on_status(msg: str) -> None:
            print(f"  status: {msg}")

        part_start = time.perf_counter()
        result = _normalize_download_part(
            part,
            task_type=args.task_type,
            on_status=on_status,
            total_elapsed_supplier=lambda: time.perf_counter() - total_start,
            cancel_check=lambda: None,
            batch_av_policy=policy,
        )
        elapsed = time.perf_counter() - part_start
        part_elapsed.append((part.name, elapsed))
        merge_part = Path(result.merge_part_path) if result.merge_part_path else part
        metrics = _collect_structural_media_metrics(merge_part, include_packet_metrics=False)
        batch_policy_name = str(getattr(result, "classification", "") or (policy.name if policy else ""))
        if batch_policy_name == ABSOLUTE_TIMELINE_TS_BRIDGE_POLICY and _part_relative_alignment_healthy(metrics):
            issue = ""
        else:
            issue = _download_part_merge_ready_issue_for_batch_policy(
                merge_part,
                batch_policy_name,
                include_packet_metrics=False,
            )
        print(f"  elapsed={fmt_elapsed(elapsed)}")
        print(f"  normalize_reason={result.normalize_reason}")
        print(f"  classification={result.classification}")
        print(f"  force_canonical_merge={result.force_canonical_merge}")
        print(f"  canonicalize_merge_part={result.canonicalize_merge_part}")
        print(f"  merge_risk_level={result.merge_risk_level}")
        print(f"  merge_part_path={merge_part}")
        print(f"  metrics: {_format_structural_media_metrics(metrics)}")
        print(f"  merge_ready_issue={issue or 'ok'}")
        if issue:
            print(f"[FAIL] official normalized part not merge-ready: {merge_part}")
            return 2
        merge_inputs.append(merge_part)
        expected_total += float(metrics.get("video_duration") or 0.0)

    if len(merge_inputs) == 1 and not args.force_merge_single:
        only_part = merge_inputs[0]
        print("\n[single-part]")
        print("  merge_skipped=True")
        print(f"  output={only_part}")
        print(f"  total={fmt_elapsed(time.perf_counter() - total_start)}")
        print("[OK] single part official flow verification succeeded")
        return 0

    final_out = work_dir / args.output_name
    try:
        final_out.unlink(missing_ok=True)
    except Exception:
        pass

    force_ts_bridge = bool(policy and policy.name == ABSOLUTE_TIMELINE_TS_BRIDGE_POLICY)
    # Keep the heavyweight adjacent-boundary probe disabled for verification runs,
    # but the formal _concat_parts path now still performs lightweight family checks
    # (e.g. systrans/fixed mixed boundaries) before attempting direct concat.
    skip_adjacent_preflight = True
    boundary_video_gap_action = "observe" if skip_adjacent_preflight else "repair"

    print("\n===== merge via official _concat_parts =====")
    merge_start = time.perf_counter()
    temp_files = _concat_parts(
        merge_inputs,
        final_out,
        force_canonical_merge=False,
        canonicalize_part_indexes=None,
        on_observation=None,
        skip_adjacent_preflight=skip_adjacent_preflight,
        boundary_video_gap_action=boundary_video_gap_action,
        force_ts_bridge=force_ts_bridge,
    )
    merge_elapsed = time.perf_counter() - merge_start
    print(f"_concat_parts temp_files={len(temp_files)}")
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
    if (
        final_issue.startswith("stream_end_gap")
        and _is_expected_tail_silence_output(final_out, include_packet_metrics=False)
        and any(_is_expected_tail_silence_output(p, include_packet_metrics=False) for p in merge_inputs)
    ):
        final_issue = ""
    all_inputs_no_audio = bool(merge_inputs) and all(_source_part_has_no_audio_stream(p) for p in merge_inputs)
    if final_issue == "missing_audio_timing" and all_inputs_no_audio and _source_part_has_no_audio_stream(final_out):
        final_issue = ""
    video_duration = float(final_metrics.get("video_duration") or 0.0)
    audio_duration = float(final_metrics.get("audio_duration") or 0.0)
    has_expected_tail_silence = any(_is_expected_tail_silence_output(p, include_packet_metrics=False) for p in merge_inputs)
    has_source_no_audio = all_inputs_no_audio and _source_part_has_no_audio_stream(final_out)
    duration_match = abs(video_duration - expected_total) < 5.0 and (
        abs(audio_duration - expected_total) < 5.0 or has_expected_tail_silence or has_source_no_audio
    )

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
    print("[OK] official flow verification succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
