from __future__ import annotations

import logging
import hashlib
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

TOOL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TOOL_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TOOL_SDK_DOWNLOAD_DIR = TOOL_ROOT / "sdk" / "download"
TOOL_SDK_SYSTRANS_DIR = TOOL_ROOT / "sdk" / "systrans"
TOOL_FFMPEG_DIR = TOOL_ROOT / "ffmpeg"
TOOL_STATE_DIR = TOOL_ROOT / "state"
TOOL_OUTPUT_DIR = TOOL_ROOT / "output"

def _bootstrap_sdk_dir() -> Path:
    override = str(os.getenv("DIY_HIK_SDK_DIR") or os.getenv("ADHOC_HIK_SDK_DIR") or os.getenv("EDGE_HIK_SDK_DIR") or "").strip()
    if override:
        return Path(override).resolve()
    candidates = [
        TOOL_SDK_DOWNLOAD_DIR,
        PROJECT_ROOT / "edge_service" / "sdk" / "download",
        PROJECT_ROOT / "sdk" / "download",
    ]
    for candidate in candidates:
        if (candidate / "HCNetSDK.dll").exists():
            return candidate.resolve()
    return candidates[0].resolve()


def _bootstrap_client_dll_environment(sdk_dir: Path) -> list[Path]:
    dirs: list[Path] = []
    candidates = [
        sdk_dir,
        sdk_dir / "ClientDemoDll",
        TOOL_SDK_SYSTRANS_DIR,
        TOOL_FFMPEG_DIR,
    ]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if not resolved.exists() or not resolved.is_dir():
            continue
        if resolved in dirs:
            continue
        dirs.append(resolved)
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(resolved))
            except Exception:
                pass
    if dirs:
        current_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join([str(p) for p in dirs] + ([current_path] if current_path else []))
    return dirs


_BOOTSTRAP_SDK_DIR = _bootstrap_sdk_dir()
os.environ["EDGE_HIK_SDK_DIR"] = str(_BOOTSTRAP_SDK_DIR)
if TOOL_SDK_SYSTRANS_DIR.exists():
    os.environ["EDGE_HIK_SYSTRANS_SDK_DIR"] = str(TOOL_SDK_SYSTRANS_DIR.resolve())
if (TOOL_FFMPEG_DIR / "ffmpeg.exe").exists():
    os.environ["EDGE_FFMPEG_BIN"] = str((TOOL_FFMPEG_DIR / "ffmpeg.exe").resolve())
_BOOTSTRAP_DLL_DIRS = _bootstrap_client_dll_environment(_BOOTSTRAP_SDK_DIR)
os.chdir(TOOL_ROOT)

from edge_service.tasks.camera import (  # noqa: E402
    _collect_structural_media_metrics,
    _concat_parts,
    _detect_download_batch_av_policy,
    _download_part_merge_ready_issue_for_batch_policy,
    _format_structural_media_metrics,
    _normalize_download_part,
    _rebuild_nvr_skew_merged_audio_from_raw_parts,
    _rebuild_nvr_skew_merged_by_raw_video_duration,
    _segment_estimated_bps,
    _segment_estimated_bytes_per_sec,
    _segment_ranges,
    _segment_seconds,
    _segment_target_mb,
    _estimate_next_segment_seconds,
)
from edge_service.video.nvr import close_download_session, download_by_time_with_session, open_download_session  # noqa: E402
from edge_service.db import Db, DbConfig  # noqa: E402
from edge_service.video.hik.download import (  # noqa: E402
    DownloadResult as HikDownloadResult,
    _get_channel_candidates,
    _probe_media_duration_seconds,
    _probe_sdk_download_candidate,
    _sdk_download_file,
)


# =============================================================================
# 手动下载参数区：有临时下载任务时，优先改这里；南京36.152.15.42
# =============================================================================
DOWNLOAD_PROVIDER = "hikvision"
DEVICE_IP = "117.144.207.90"
DEVICE_PORT = 18081
LOGIN_USERNAME = "admin"
LOGIN_PASSWORD = "wlyn6688"
WEB_CHANNEL_NUM = 2
TIME_RANGE_TEXT = "2026-06-27 08:00 ~ 2026-06-27 11:30"

# 可选参数：一般不用改
TASK_TYPE = 0
DEVICE_MODEL = ""
NVR_DEVICE_ID = None
DB_PATH = str(TOOL_STATE_DIR / "diy_download.db")
SDK_DIR = ""
OUTPUT_LABEL = "adhoc"
OUTPUT_ROOT = TOOL_OUTPUT_DIR
REFRESH_OUTPUT_DIR = False
CLEAN_INTERMEDIATES = False
KEEP_MERGE_TEMP = False
ALLOW_SAME_CHANNEL_DIRECT_DOWNLOAD_RECOVERY = True
ENABLE_DIAGNOSTIC_CANDIDATE_SCAN = True
DIAGNOSTIC_SAMPLE_SECONDS = 90
DIAGNOSTIC_MAX_CANDIDATES = 24


class Tee:
    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        written = 0
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        written = len(data)
        return written

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


@dataclass
class DownloadPartRecord:
    index: int
    requested_start: datetime
    requested_end: datetime
    requested_sec: float
    output_path: Path
    channel_used: int
    record_type_used: int
    mapping_source: str
    actual_size_bytes: int
    actual_duration_sec: float
    accepted_as_tail: bool
    elapsed_sec: float


def _parse_time_range(text: str) -> tuple[datetime, datetime]:
    raw = str(text or "").strip()
    if "~" not in raw:
        raise ValueError(f"time range format invalid: {raw!r}")
    start_raw, end_raw = [item.strip() for item in raw.split("~", 1)]
    start_dt = datetime.strptime(start_raw, "%Y-%m-%d %H:%M")
    end_dt = datetime.strptime(end_raw, "%Y-%m-%d %H:%M")
    if end_dt <= start_dt:
        raise ValueError(f"time range invalid: end <= start ({raw})")
    return start_dt, end_dt


def _fmt_elapsed(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    minutes = int(seconds // 60)
    remain = seconds - minutes * 60
    return f"{minutes}m{remain:.1f}s"


def _fmt_size(size_bytes: int) -> str:
    mb = max(0.0, float(size_bytes or 0.0)) / 1048576.0
    if mb >= 1024:
        return f"{mb / 1024.0:.2f}GB"
    return f"{mb:.1f}MB"


def _safe_name(text: str) -> str:
    out = []
    for ch in str(text or ""):
        if ch.isalnum() or ch in {"-", "_", "."}:
            out.append(ch)
        else:
            out.append("_")
    return "".join(out).strip("_") or "adhoc"


def _build_prefix() -> str:
    return f"{OUTPUT_LABEL}_ch{int(WEB_CHANNEL_NUM):02d}"


def _build_work_dir(start_dt: datetime, end_dt: datetime) -> Path:
    stamp = f"{start_dt.strftime('%Y%m%d-%H%M')}_{end_dt.strftime('%H%M')}"
    ip_tail = _safe_name(DEVICE_IP.replace(".", "-"))
    name = f"{OUTPUT_LABEL}_{ip_tail}_ch{int(WEB_CHANNEL_NUM):02d}_{stamp}"
    return OUTPUT_ROOT / name


def _effective_nvr_device_id() -> int:
    try:
        explicit = int(NVR_DEVICE_ID or 0)
    except Exception:
        explicit = 0
    if explicit > 0:
        return explicit
    key = f"{DEVICE_IP}:{int(DEVICE_PORT)}".encode("utf-8", errors="ignore")
    return int(hashlib.sha1(key).hexdigest()[:8], 16)


def _resolve_sdk_dir() -> Path:
    if str(SDK_DIR or "").strip():
        return Path(str(SDK_DIR).strip()).resolve()
    candidates = [
        TOOL_SDK_DOWNLOAD_DIR,
        PROJECT_ROOT / "edge_service" / "sdk" / "download",
        PROJECT_ROOT / "sdk" / "download",
    ]
    for candidate in candidates:
        if (candidate / "HCNetSDK.dll").exists():
            return candidate.resolve()
    return candidates[0].resolve()


def _resolve_ffmpeg_bin() -> Path:
    configured = str(os.getenv("EDGE_FFMPEG_BIN") or "").strip()
    if configured:
        return Path(configured).resolve()
    return (TOOL_FFMPEG_DIR / "ffmpeg.exe").resolve()


def _validate_runtime_files() -> None:
    sdk_dir = _resolve_sdk_dir()
    required_sdk = [
        sdk_dir / "HCNetSDK.dll",
        sdk_dir / "HCCore.dll",
        sdk_dir / "PlayCtrl.dll",
    ]
    missing = [str(path) for path in required_sdk if not path.exists()]
    ffmpeg_bin = _resolve_ffmpeg_bin()
    ffprobe_bin = ffmpeg_bin.with_name("ffprobe.exe")
    if not ffmpeg_bin.exists():
        missing.append(str(ffmpeg_bin))
    if not ffprobe_bin.exists():
        missing.append(str(ffprobe_bin))
    if missing:
        raise FileNotFoundError("diy_download runtime missing files: " + "; ".join(missing))


def _init_local_state_db() -> None:
    if not str(DB_PATH or "").strip():
        return
    db = Db(DbConfig(path=str(DB_PATH)))
    db.init_schema()


def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fp = log_path.open("w", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, fp)
    sys.stderr = Tee(sys.__stderr__, fp)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def _cleanup_dir(work_dir: Path, keep: set[str]) -> None:
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


def _print_config(start_dt: datetime, end_dt: datetime, work_dir: Path, log_path: Path) -> None:
    requested_sec = (end_dt - start_dt).total_seconds()
    seg_sec = _segment_seconds()
    initial_ranges = _segment_ranges(start_dt, end_dt, seg_sec)
    est_bps = _segment_estimated_bps()
    est_bytes_sec = _segment_estimated_bytes_per_sec()
    est_total_size = int(est_bytes_sec * requested_sec)
    print("===== diy download official-flow config =====")
    print(f"tool_root={TOOL_ROOT}")
    print(f"project_root={PROJECT_ROOT}")
    print(f"provider={DOWNLOAD_PROVIDER}")
    print(f"device_ip={DEVICE_IP}")
    print(f"device_port={DEVICE_PORT}")
    print(f"username={LOGIN_USERNAME}")
    print(f"channel={WEB_CHANNEL_NUM}")
    print(f"time_range={start_dt.isoformat()} ~ {end_dt.isoformat()}")
    print(f"requested_duration={requested_sec:.1f}s")
    print(f"segment_target_mb={_segment_target_mb()}")
    print(f"segment_estimated_bps={est_bps}")
    print(f"segment_initial_sec={seg_sec}")
    print(f"segment_initial_count={len(initial_ranges)}")
    print(f"estimated_total_size={_fmt_size(est_total_size)}")
    print(f"sdk_dir={_resolve_sdk_dir()}")
    print(f"systrans_sdk_dir={os.getenv('EDGE_HIK_SYSTRANS_SDK_DIR') or ''}")
    print(f"ffmpeg_bin={_resolve_ffmpeg_bin()}")
    print(f"dll_search_dirs={[str(p) for p in _BOOTSTRAP_DLL_DIRS]}")
    print(f"runtime_cwd={Path.cwd()}")
    print(f"nvr_device_id={_effective_nvr_device_id()} explicit={NVR_DEVICE_ID}")
    print(f"db_path={DB_PATH}")
    print(f"work_dir={work_dir}")
    print(f"log_file={log_path}")
    print("======================================")


def _make_download_status_callback(part_idx: int, total_hint_supplier: Callable[[], int]) -> Callable[[float, str], None]:
    last_status = {"text": ""}

    def _cb(_progress: float, status: str) -> None:
        text = str(status or "").strip()
        if not text or text == last_status["text"]:
            return
        last_status["text"] = text
        print(f"  [part{part_idx:03d}/{total_hint_supplier()} status] {text}")

    return _cb


def _make_progress_ex_callback(part_idx: int, total_hint_supplier: Callable[[], int]) -> Callable[[float, int, float], None]:
    last_bucket = {"value": -1}

    def _cb(progress: float, size_bytes: int, speed_bps: float) -> None:
        pct = max(0, min(100, int(round(float(progress or 0.0) * 100.0))))
        if pct == last_bucket["value"] and pct not in {0, 100}:
            return
        last_bucket["value"] = pct
        speed_mb = max(0.0, float(speed_bps or 0.0)) / 1048576.0
        print(
            f"  [part{part_idx:03d}/{total_hint_supplier()} progress] "
            f"{pct:>3d}% size={_fmt_size(int(size_bytes or 0))} speed={speed_mb:.2f}MB/s"
        )

    return _cb


def _run_downloads(
    work_dir: Path,
    prefix: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[DownloadPartRecord]:
    total_seconds = max(1.0, (end_dt - start_dt).total_seconds())
    current_start = start_dt
    current_seg_sec = _segment_seconds()
    adaptive_segmenting = True
    records: list[DownloadPartRecord] = []
    locked_channel: int | None = None
    locked_record_type: int | None = None
    hint_uid: int | None = None
    hint_channel: int | None = None
    hint_record_type: int | None = None
    disabled_history_channels: set[int] = set()

    sdk_dir = _resolve_sdk_dir()
    os.environ["EDGE_HIK_SDK_DIR"] = str(sdk_dir)
    print(f"[sdk] using download sdk dir: {sdk_dir}")

    session = open_download_session(
        provider=DOWNLOAD_PROVIDER,
        sdk_dir=str(sdk_dir),
        db_path=DB_PATH,
        nvr_device_id=_effective_nvr_device_id(),
        ip=DEVICE_IP,
        port=DEVICE_PORT,
        username=LOGIN_USERNAME,
        password=LOGIN_PASSWORD,
        channel=WEB_CHANNEL_NUM,
        device_model=DEVICE_MODEL or None,
    )

    def _diagnostic_candidate_scan(
        *,
        requested_start: datetime,
        requested_end: datetime,
        failed_message: str,
    ) -> None:
        if not ENABLE_DIAGNOSTIC_CANDIDATE_SCAN:
            return
        diag_dir = work_dir / "diagnostic_candidate_scan"
        diag_dir.mkdir(parents=True, exist_ok=True)
        sample_end = min(requested_start + timedelta(seconds=int(max(30, DIAGNOSTIC_SAMPLE_SECONDS))), requested_end)
        sample_sec = max(1.0, (sample_end - requested_start).total_seconds())
        user_id = int(getattr(session, "preferred_download_uid", None) or getattr(session, "login_uid", 0) or 0)
        record_type = 0

        candidates = _get_channel_candidates(
            WEB_CHANNEL_NUM,
            getattr(session, "device_model", None),
            getattr(session, "sdk_start_channel", None),
            getattr(session, "sdk_start_dchan", None),
            getattr(session, "ip_chan_num", None),
            getattr(session, "persisted_sdk_channel", None),
            getattr(session, "channel_offset", None),
            excluded_channels=set(),
            emit_log=True,
        )
        sdk_start_dchan = int(getattr(session, "sdk_start_dchan", 0) or 0)
        ip_chan_num = int(getattr(session, "ip_chan_num", 0) or 0)
        if sdk_start_dchan > 0 and ip_chan_num > 0:
            candidates.extend(range(sdk_start_dchan, sdk_start_dchan + ip_chan_num))
        # script-only broadened inspection, still only for diagnosis
        deduped: list[int] = []
        seen: set[int] = set()
        for ch in candidates:
            try:
                sdk_ch = int(ch)
            except Exception:
                continue
            if sdk_ch <= 0 or sdk_ch in seen:
                continue
            deduped.append(sdk_ch)
            seen.add(sdk_ch)
        candidates = deduped[: max(1, int(DIAGNOSTIC_MAX_CANDIDATES))]

        print("\n===== diagnostic candidate scan =====")
        print(f"  reason={failed_message}")
        print(f"  sample_window={requested_start.isoformat()} ~ {sample_end.isoformat()} ({sample_sec:.1f}s)")
        print(f"  candidates={candidates}")

        for order, sdk_ch in enumerate(candidates, start=1):
            print(f"\n  --- diagnostic candidate {order}/{len(candidates)} sdk_channel={sdk_ch} ---")
            probe = _probe_sdk_download_candidate(
                session.sdk,
                user_id,
                sdk_ch,
                requested_start,
                sample_end,
                record_type,
                probe_seconds=10,
            )
            print(
                f"    probe_ok={probe.ok} status={probe.status} "
                f"pos={probe.pos} size={probe.size_bytes}"
            )
            sample_path = diag_dir / f"candidate_{sdk_ch:03d}_{requested_start.strftime('%Y%m%d_%H%M%S')}.mp4"
            try:
                sample_path.unlink(missing_ok=True)
            except Exception:
                pass
            ok, err = _sdk_download_file(
                session.sdk,
                user_id,
                sdk_ch,
                requested_start,
                sample_end,
                str(sample_path),
                record_type=record_type,
                on_progress=None,
                on_progress_ex=None,
                cancel_check=lambda: None,
            )
            if not ok:
                try:
                    sample_path.unlink(missing_ok=True)
                except Exception:
                    pass
                print(f"    download_ok=False err={err}")
                continue
            size_bytes = sample_path.stat().st_size if sample_path.exists() else 0
            duration = _probe_media_duration_seconds(str(sample_path))
            print(
                f"    download_ok=True size={_fmt_size(size_bytes)} "
                f"duration={duration:.3f}s path={sample_path}"
            )

    def _direct_same_channel_download_recovery(
        *,
        sdk_channel: int,
        requested_start: datetime,
        requested_end: datetime,
        requested_sec: float,
        output_path: Path,
    ) -> HikDownloadResult:
        user_id = int(getattr(session, "preferred_download_uid", None) or getattr(session, "login_uid", 0) or 0)
        record_type = int(locked_record_type or 0)
        expected_duration = int(max(1.0, requested_sec))
        duration_tolerance_sec = max(30.0, expected_duration * 0.10)
        tmp_output = str(output_path) + ".same_channel_direct.attempt"
        try:
            Path(tmp_output).unlink(missing_ok=True)
        except Exception:
            pass
        print(
            f"  [recovery] same-channel direct download test "
            f"sdk_channel={sdk_channel} uid={user_id} record_type={record_type} "
            f"window={requested_start.isoformat()} ~ {requested_end.isoformat()}"
        )
        ok, err = _sdk_download_file(
            session.sdk,
            user_id,
            int(sdk_channel),
            requested_start,
            requested_end,
            tmp_output,
            record_type=record_type,
            on_progress=_make_download_status_callback(part_idx, lambda: max(part_idx, remaining_parts + part_idx - 1)),
            on_progress_ex=_make_progress_ex_callback(part_idx, lambda: max(part_idx, remaining_parts + part_idx - 1)),
            cancel_check=lambda: None,
        )
        if err and str(err).startswith("cancelled:"):
            raise RuntimeError(str(err))
        if not ok:
            try:
                Path(tmp_output).unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"same_channel_direct_download_failed:{err or 'unknown'}")
        file_size = Path(tmp_output).stat().st_size if Path(tmp_output).exists() else 0
        actual_duration = _probe_media_duration_seconds(tmp_output)
        accepted_as_tail = False
        if actual_duration < expected_duration - duration_tolerance_sec - 0.001:
            is_last_part = requested_end >= end_dt
            if is_last_part and file_size >= 1024 and actual_duration > 1.0:
                accepted_as_tail = True
                print(
                    f"  [recovery] tail partial accepted actual_duration={actual_duration:.3f}s "
                    f"expected={expected_duration}s tolerance={duration_tolerance_sec:.1f}s"
                )
            else:
                try:
                    Path(tmp_output).unlink(missing_ok=True)
                except Exception:
                    pass
                raise RuntimeError(
                    f"same_channel_direct_duration_short:actual={actual_duration:.3f}:"
                    f"expected={expected_duration}:tol={duration_tolerance_sec:.1f}"
                )
        # nearby-recovery 那条路径会裁齐；这里我们只测相同窗口，不做扩窗裁齐
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        Path(tmp_output).replace(output_path)
        print(
            f"  [recovery] direct download succeeded size={_fmt_size(file_size)} "
            f"actual_duration={actual_duration:.3f}s"
        )
        return HikDownloadResult(
            path=str(output_path),
            size_bytes=int(file_size),
            channel_used=int(sdk_channel),
            hint_uid=int(user_id),
            hint_record_type=int(record_type),
            hint_reusable=False,
            mapping_source="same_channel_direct_probe_bypass",
            persisted_channel=0,
            persisted_failed=False,
            actual_duration_sec=float(actual_duration),
            accepted_as_tail=bool(accepted_as_tail),
        )

    try:
        part_idx = 1
        while current_start < end_dt:
            remaining_parts = len(_segment_ranges(current_start, end_dt, max(30, int(current_seg_sec))))
            requested_end = min(current_start + timedelta(seconds=int(current_seg_sec)), end_dt)
            if requested_end <= current_start:
                break

            out_path = work_dir / f"{prefix}.part{part_idx:03d}.mp4"
            part_duration = max(1.0, (requested_end - current_start).total_seconds())
            print(
                f"\n===== download part {part_idx}/{max(part_idx, remaining_parts + part_idx - 1)} =====\n"
                f"range={current_start.isoformat()} ~ {requested_end.isoformat()} "
                f"requested={part_duration:.1f}s seg_sec={current_seg_sec} locked_channel={locked_channel or 0}"
            )

            if REFRESH_OUTPUT_DIR:
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass

            part_start = time.perf_counter()
            try:
                res = download_by_time_with_session(
                    session,
                    provider=DOWNLOAD_PROVIDER,
                    start_time=current_start,
                    end_time=requested_end,
                    output_path=str(out_path),
                    on_progress=_make_download_status_callback(part_idx, lambda: max(part_idx, remaining_parts + part_idx - 1)),
                    on_progress_ex=_make_progress_ex_callback(part_idx, lambda: max(part_idx, remaining_parts + part_idx - 1)),
                    cancel_check=lambda: None,
                    hint_uid=hint_uid,
                    hint_channel=hint_channel,
                    hint_record_type=hint_record_type,
                    locked_channel=locked_channel,
                    locked_record_type=locked_record_type,
                    excluded_channels=sorted(disabled_history_channels),
                    apply_start_padding=False,
                    apply_end_padding=False,
                    allow_tail_partial=requested_end >= end_dt,
                )
            except RuntimeError as e:
                msg = str(e or "")
                direct_sdk_channel = (
                    int(session.sdk_start_dchan or 0) + max(1, int(WEB_CHANNEL_NUM or 1)) - 1
                    if int(getattr(session, "sdk_start_dchan", 0) or 0) > 0 and int(getattr(session, "ip_chan_num", 0) or 0) > 0
                    else 0
                )
                can_try_direct_recovery = (
                    ALLOW_SAME_CHANNEL_DIRECT_DOWNLOAD_RECOVERY
                    and locked_channel is None
                    and direct_sdk_channel > 0
                    and (
                        "无可下载视频" in msg
                        or "sdk_candidate_probe_failed:" in msg
                    )
                )
                if not can_try_direct_recovery:
                    if "无可下载视频" in msg or "sdk_candidate_probe_failed:" in msg:
                        _diagnostic_candidate_scan(
                            requested_start=current_start,
                            requested_end=requested_end,
                            failed_message=msg,
                        )
                    raise
                print(
                    f"  [recovery] probe path failed, trying direct download on same mapped sdk channel={direct_sdk_channel}"
                )
                try:
                    res = _direct_same_channel_download_recovery(
                        sdk_channel=direct_sdk_channel,
                        requested_start=current_start,
                        requested_end=requested_end,
                        requested_sec=part_duration,
                        output_path=out_path,
                    )
                except Exception as recovery_error:
                    _diagnostic_candidate_scan(
                        requested_start=current_start,
                        requested_end=requested_end,
                        failed_message=str(recovery_error),
                    )
                    raise
            elapsed = time.perf_counter() - part_start

            actual_channel_used = int(res.channel_used or 0)
            if actual_channel_used > 0:
                if locked_channel is None:
                    locked_channel = actual_channel_used
                    locked_record_type = int(res.hint_record_type or 0)
                    print(
                        f"  [lock] first successful sdk channel locked: "
                        f"sdk_channel={locked_channel} record_type={locked_record_type}"
                    )
                elif locked_channel != actual_channel_used:
                    raise RuntimeError(
                        f"same task downloaded different sdk channels: locked={locked_channel} actual={actual_channel_used}"
                    )
                hint_uid = None
                hint_channel = locked_channel
                hint_record_type = locked_record_type

            if bool(getattr(res, "persisted_failed", False)) and int(getattr(res, "persisted_channel", 0) or 0) > 0:
                disabled_history_channels.add(int(getattr(res, "persisted_channel", 0) or 0))

            print(
                "  [done] "
                f"elapsed={_fmt_elapsed(elapsed)} size={_fmt_size(int(res.size_bytes or 0))} "
                f"sdk_channel={actual_channel_used} mapping={str(getattr(res, 'mapping_source', '') or '')} "
                f"actual_duration={float(getattr(res, 'actual_duration_sec', 0.0) or 0.0):.3f}s "
                f"tail_partial={bool(getattr(res, 'accepted_as_tail', False))}"
            )

            records.append(
                DownloadPartRecord(
                    index=part_idx,
                    requested_start=current_start,
                    requested_end=requested_end,
                    requested_sec=part_duration,
                    output_path=out_path,
                    channel_used=actual_channel_used,
                    record_type_used=int(res.hint_record_type or 0),
                    mapping_source=str(getattr(res, "mapping_source", "") or ""),
                    actual_size_bytes=int(res.size_bytes or 0),
                    actual_duration_sec=float(getattr(res, "actual_duration_sec", 0.0) or 0.0),
                    accepted_as_tail=bool(getattr(res, "accepted_as_tail", False)),
                    elapsed_sec=elapsed,
                )
            )

            next_seg_sec = _estimate_next_segment_seconds(
                int(res.size_bytes or 0),
                float(part_duration),
                int(current_seg_sec),
            ) if adaptive_segmenting else int(current_seg_sec)
            current_start = requested_end
            current_seg_sec = max(30, int(next_seg_sec)) if adaptive_segmenting else int(current_seg_sec)
            part_idx += 1

        total_size = sum(item.actual_size_bytes for item in records)
        total_elapsed = sum(item.elapsed_sec for item in records)
        print(
            f"\n[download-summary] parts={len(records)} total_size={_fmt_size(total_size)} "
            f"elapsed={_fmt_elapsed(total_elapsed)} requested={total_seconds:.1f}s locked_channel={locked_channel or 0}"
        )
        return records
    finally:
        close_download_session(session, provider=DOWNLOAD_PROVIDER)


def _process_and_merge(
    work_dir: Path,
    prefix: str,
    downloaded_parts: list[DownloadPartRecord],
    total_start: float,
) -> Path:
    parts = [item.output_path for item in downloaded_parts]
    batch_policy = _detect_download_batch_av_policy(parts)
    print(
        "\n[batch-policy] "
        f"policy={batch_policy.name} median_gap={batch_policy.start_gap_median:.3f}s "
        f"range={batch_policy.start_gap_range:.3f}s part_count={batch_policy.part_count} "
        f"reason={batch_policy.reason} target_video_codec={batch_policy.target_video_codec or ''} "
        f"video_codec_mix={bool(batch_policy.video_codec_mix)}"
    )

    merge_inputs: list[Path] = []
    part_process_elapsed = 0.0
    for order, part in enumerate(parts, start=1):
        print(f"\n===== normalize part {order}/{len(parts)}: {part.name} =====")

        def on_status(msg: str) -> None:
            print(f"  [normalize-status] {msg}")

        start = time.perf_counter()
        result = _normalize_download_part(
            part,
            TASK_TYPE,
            on_status,
            lambda: time.perf_counter() - total_start,
            lambda: None,
            batch_policy,
        )
        elapsed = time.perf_counter() - start
        part_process_elapsed += elapsed
        merge_part = Path(result.merge_part_path) if result.merge_part_path else part
        metrics = _collect_structural_media_metrics(merge_part, include_packet_metrics=False)
        issue = _download_part_merge_ready_issue_for_batch_policy(
            merge_part,
            str(getattr(result, "classification", "") or batch_policy.name),
            include_packet_metrics=False,
        )
        print(f"  elapsed={_fmt_elapsed(elapsed)}")
        print(f"  normalize_reason={result.normalize_reason}")
        print(f"  classification={result.classification}")
        print(f"  force_canonical_merge={result.force_canonical_merge}")
        print(f"  canonicalize_merge_part={result.canonicalize_merge_part}")
        print(f"  merge_risk_level={result.merge_risk_level}")
        print(f"  merge_part={merge_part}")
        print(f"  metrics={_format_structural_media_metrics(metrics)}")
        print(f"  merge_ready_issue={issue or 'ok'}")
        if issue:
            raise RuntimeError(f"normalized part not merge-ready: {merge_part} issue={issue}")
        merge_inputs.append(merge_part)

    output_name = f"{prefix}.final.mp4"
    final_out = work_dir / output_name
    try:
        final_out.unlink(missing_ok=True)
    except Exception:
        pass

    print("\n===== merge via official _concat_parts =====")
    merge_start = time.perf_counter()
    temp_files = _concat_parts(
        merge_inputs,
        final_out,
        force_canonical_merge=False,
        canonicalize_part_indexes=None,
        on_observation=lambda data: print(f"  [merge-observation] {data}"),
        skip_adjacent_preflight=False,
        boundary_video_gap_action="repair",
        force_ts_bridge=bool(batch_policy.name == "absolute_timeline_same_origin_ts_bridge"),
        cancel_check=lambda: None,
    )
    merge_elapsed = time.perf_counter() - merge_start
    print(f"  merge_elapsed={_fmt_elapsed(merge_elapsed)}")
    print(f"  merge_temp_files={len(temp_files)}")

    if batch_policy.name == "nvr_pts_skew_same_origin":
        print("\n===== rebuild merged audio from raw parts =====")
        merged_base_out = final_out
        nvr_audio_merged = final_out.with_name(final_out.stem + ".nvrskew_audio.mp4")
        audio_rebuild_start = time.perf_counter()
        audio_rebuild_ok = _rebuild_nvr_skew_merged_audio_from_raw_parts(final_out, parts, nvr_audio_merged)
        audio_rebuild_elapsed = time.perf_counter() - audio_rebuild_start
        print(f"  audio_rebuild_ok={audio_rebuild_ok}")
        print(f"  audio_rebuild_elapsed={_fmt_elapsed(audio_rebuild_elapsed)}")
        if not audio_rebuild_ok or not nvr_audio_merged.exists() or nvr_audio_merged.stat().st_size <= 0:
            print("  audio rebuild failed, fallback to raw-video-duration rebuild")
            fallback_out = final_out.with_name(final_out.stem + ".nvrskew_video_duration.mp4")
            fallback_start = time.perf_counter()
            fallback_ok, fallback_temp_files = _rebuild_nvr_skew_merged_by_raw_video_duration(parts, fallback_out)
            fallback_elapsed = time.perf_counter() - fallback_start
            print(f"  fallback_ok={fallback_ok}")
            print(f"  fallback_elapsed={_fmt_elapsed(fallback_elapsed)}")
            temp_files.extend(fallback_temp_files)
            if not fallback_ok or not fallback_out.exists() or fallback_out.stat().st_size <= 0:
                raise RuntimeError(f"nvr_skew_audio_rebuild_failed:{nvr_audio_merged}")
            nvr_audio_merged = fallback_out
        temp_files.append(final_out)
        try:
            merged_base_out.unlink(missing_ok=True)
        except Exception:
            pass
        nvr_audio_merged.replace(merged_base_out)
        final_out = merged_base_out

    if not KEEP_MERGE_TEMP:
        cleaned = 0
        for path in temp_files:
            try:
                Path(path).unlink(missing_ok=True)
                cleaned += 1
            except Exception:
                pass
        print(f"  merge_temp_cleaned={cleaned}")

    if not final_out.exists() or final_out.stat().st_size <= 0:
        raise RuntimeError(f"merged output missing: {final_out}")

    final_metrics = _collect_structural_media_metrics(final_out, include_packet_metrics=False)
    print(f"\n[merge-result] {final_out}")
    print(f"  size={_fmt_size(final_out.stat().st_size)}")
    print(f"  metrics={_format_structural_media_metrics(final_metrics)}")
    print(f"  repair_elapsed={_fmt_elapsed(part_process_elapsed)}")
    print(f"  merge_elapsed={_fmt_elapsed(merge_elapsed)}")
    return final_out


def main() -> int:
    total_start = time.perf_counter()
    start_dt, end_dt = _parse_time_range(TIME_RANGE_TEXT)
    prefix = _build_prefix()
    work_dir = _build_work_dir(start_dt, end_dt)
    work_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / "diy_download.latest.log"
    _setup_logging(log_path)
    _validate_runtime_files()
    _init_local_state_db()
    _print_config(start_dt, end_dt, work_dir, log_path)

    if CLEAN_INTERMEDIATES:
        _cleanup_dir(work_dir, keep={log_path.name})

    try:
        download_records = _run_downloads(work_dir, prefix, start_dt, end_dt)
        if not download_records:
            raise RuntimeError("no parts downloaded")
        final_out = _process_and_merge(work_dir, prefix, download_records, total_start)
        total_elapsed = time.perf_counter() - total_start
        total_download_elapsed = sum(item.elapsed_sec for item in download_records)
        total_size = sum(item.actual_size_bytes for item in download_records)
        print("\n===== final summary =====")
        print(f"download_parts={len(download_records)}")
        print(f"download_elapsed={_fmt_elapsed(total_download_elapsed)}")
        print(f"download_total_size={_fmt_size(total_size)}")
        print(f"total_elapsed={_fmt_elapsed(total_elapsed)}")
        print(f"final_output={final_out}")
        print(f"log_file={log_path}")
        print("[OK] diy download official flow succeeded")
        return 0
    except Exception as e:
        print(f"\n[FAIL] {e}")
        print(f"log_file={log_path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
