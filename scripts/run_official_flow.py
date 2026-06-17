"""Utility to re-run camera.py normalization + merge flow on local segments.

Example:
    python scripts/run_official_flow.py \
        --src-dir id=6835 \
        --pattern "teacher_6835.part*.mp4"
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from edge_service.tasks.camera import (  # noqa: E402
    DownloadPartNormalizeResult,
    _collect_structural_media_metrics,
    _concat_parts,
    _format_structural_media_metrics,
    _normalize_download_part,
)


def _stage_inputs(src_dir: Path, pattern: str, work_dir: Path, *, refresh: bool) -> list[Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for src in sorted(src_dir.glob(pattern)):
        if not src.is_file():
            continue
        dst = work_dir / src.name
        if refresh or not dst.exists() or dst.stat().st_size != src.stat().st_size:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        staged.append(dst)
    return staged


def _cleanup_work_dir(work_dir: Path, keep: set[str]) -> None:
    if not work_dir.exists():
        return
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


def run_flow(args: argparse.Namespace) -> int:
    src_dir = args.src_dir
    work_dir = args.work_dir or (src_dir.parent / f"{src_dir.name}_flow_out")
    work_dir.mkdir(parents=True, exist_ok=True)
    staged_parts = _stage_inputs(src_dir, args.pattern, work_dir, refresh=args.refresh_inputs)
    if not staged_parts:
        print(f"No inputs matched {args.pattern} under {src_dir}")
        return 1
    print(f"Staged {len(staged_parts)} part(s) into {work_dir}")
    keep = {p.name for p in staged_parts} | {args.output_name}
    if args.clean_intermediates:
        _cleanup_work_dir(work_dir, keep)
    merge_inputs: list[Path] = []

    def on_status(msg: str) -> None:
        print(f"    [status] {msg}")

    for idx, part in enumerate(staged_parts, start=1):
        print(f"\n===== Normalizing part {idx}/{len(staged_parts)} -> {part.name} =====")
        result: DownloadPartNormalizeResult = _normalize_download_part(
            part,
            task_type=args.task_type,
            on_status=on_status,
            total_elapsed_supplier=lambda: 0.0,
            cancel_check=lambda: None,
            batch_av_policy=None,
        )
        merge_part = Path(result.merge_part_path) if result.merge_part_path else part
        merge_inputs.append(merge_part)
        metrics = _collect_structural_media_metrics(merge_part)
        print(f"  normalize_reason={result.normalize_reason}")
        print(f"  classification={result.classification}")
        print(f"  merge_part={merge_part}")
        print(f"  metrics={_format_structural_media_metrics(metrics)}")

    final_out = work_dir / args.output_name
    if final_out.exists():
        final_out.unlink()
    print("\n===== Merging via _concat_parts =====")
    temp_files = _concat_parts(
        merge_inputs,
        final_out,
        force_canonical_merge=args.force_canonical,
        canonicalize_part_indexes=None,
        on_observation=None,
        skip_adjacent_preflight=args.skip_adjacent_preflight,
    )
    print(f"Generated {len(temp_files)} intermediate files")
    if not final_out.exists() or final_out.stat().st_size <= 0:
        print(f"[FAIL] merged output missing: {final_out}")
        return 2
    final_metrics = _collect_structural_media_metrics(final_out)
    expected_v = sum(
        float(_collect_structural_media_metrics(p).get("video_duration") or 0.0)
        for p in merge_inputs
    )
    got_v = float(final_metrics.get("video_duration") or 0.0)
    got_a = float(final_metrics.get("audio_duration") or 0.0)
    print(f"\n[merged] {final_out} size={final_out.stat().st_size/1048576:.1f} MB")
    print(f"  metrics={_format_structural_media_metrics(final_metrics)}")
    print(f"  expected_v={expected_v:.3f}s got_v={got_v:.3f}s got_a={got_a:.3f}s")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Re-run camera.py normalization + merge on local segments")
    parser.add_argument("--src-dir", type=Path, default=Path("id=6835"))
    parser.add_argument("--pattern", type=str, default="teacher_*.part*.mp4")
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--output-name", type=str, default="merged.flow.mp4")
    parser.add_argument("--task-type", type=int, default=0)
    parser.add_argument("--refresh-inputs", action="store_true")
    parser.add_argument("--clean-intermediates", action="store_true")
    parser.add_argument("--force-canonical", action="store_true")
    parser.add_argument("--skip-adjacent-preflight", action="store_true")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return run_flow(args)


if __name__ == "__main__":
    sys.exit(main())
