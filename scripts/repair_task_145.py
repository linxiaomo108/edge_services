"""
独立验证脚本：对 id=145 的 5 个分段执行 "raw vs systrans 通用对比 + 选优 + 合并"。
- 不修改 id=145/ 下任何文件；
- 所有 systrans 中间文件、归零后中间文件、最终合并文件都输出到 id=145_repair_out/；
- 用统一的"对齐健康度"评分比较两个候选，不引入绝对起点偏置；
- 若任一指标 systrans 比 raw 显著变差，且 raw 自身可接受，则选 raw；
- 选中候选若绝对起点>容差，做轻量 setpts 归零；
- 最后用 concat demuxer 合并，并对合并产物 ffprobe 校验。

使用：
    python scripts/repair_task_145.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

# 让脚本可以独立运行：把工程根加入 sys.path 后再 import 海康 SDK 封装
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from edge_service.video.hik.systrans import system_transform_file  # noqa: E402

SRC_DIR = ROOT / "id=145"
OUT_DIR = ROOT / "id=145_repair_out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- 容差（与主仓库一致） ----
START_GAP_TOL = 0.3       # |audio_start - video_start| 容差
END_GAP_TOL = 0.8         # |audio_end - video_end| 容差
DURATION_DELTA_TOL = 1.0  # |audio_duration - video_duration| 容差
ABS_START_REMUX_TOL = 2.0 # 绝对起点超过此值就走轻量 setpts 归零
MAX_AUDIO_PKT_WARN = 0.5
MAX_VIDEO_PKT_WARN = 1.0


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", **kw)


def ffprobe_streams(path: Path) -> dict:
    cp = run([
        "ffprobe", "-v", "quiet",
        "-analyzeduration", "100M", "-probesize", "100M",
        "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ])
    if cp.returncode != 0:
        return {}
    try:
        return json.loads(cp.stdout or "{}")
    except Exception:
        return {}


def probe_packet_end(path: Path, selector: str) -> tuple[float, float]:
    """返回 (end_time, max_packet_duration)。end_time 用最后一个包 pts+duration。"""
    cp = run([
        "ffprobe", "-v", "error",
        "-select_streams", selector,
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "csv=p=0", str(path),
    ])
    end = 0.0
    max_dur = 0.0
    for line in (cp.stdout or "").splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            pts = float(parts[0])
        except Exception:
            continue
        try:
            dur = float(parts[1]) if parts[1] not in ("", "N/A") else 0.0
        except Exception:
            dur = 0.0
        if dur > max_dur:
            max_dur = dur
        if pts + dur > end:
            end = pts + dur
    return end, max_dur


@dataclass
class Metrics:
    exists: bool
    size_bytes: int
    video_start: float
    audio_start: float
    video_duration: float
    audio_duration: float
    video_end: float
    audio_end: float
    max_video_pkt: float
    max_audio_pkt: float
    has_video: bool
    has_audio: bool

    @property
    def start_gap(self) -> float:
        return abs(self.audio_start - self.video_start)

    @property
    def end_gap(self) -> float:
        return abs(self.audio_end - self.video_end)

    @property
    def dur_delta(self) -> float:
        return abs(self.audio_duration - self.video_duration)

    @property
    def abs_start(self) -> float:
        return max(self.video_start, self.audio_start)

    def healthy(self) -> bool:
        """raw/systrans 是否结构与对齐都健康（不考虑绝对起点，绝对起点单独处理）。"""
        if not (self.has_video and self.has_audio):
            return False
        return (
            self.start_gap <= START_GAP_TOL
            and self.end_gap <= END_GAP_TOL
            and self.dur_delta <= DURATION_DELTA_TOL
            and self.max_audio_pkt <= MAX_AUDIO_PKT_WARN * 2  # 宽松
            and self.max_video_pkt <= MAX_VIDEO_PKT_WARN * 3
        )

    def alignment_badness(self) -> float:
        """对齐健康度评分（越小越好），不含绝对起点。"""
        if not self.exists:
            return float("inf")
        if not (self.has_video and self.has_audio):
            return float("inf")
        return (
            self.start_gap * 4.0
            + self.end_gap * 2.0
            + self.dur_delta * 4.0
            + max(0.0, self.max_audio_pkt - MAX_AUDIO_PKT_WARN) * 10.0
            + max(0.0, self.max_video_pkt - MAX_VIDEO_PKT_WARN) * 1.0
        )


def collect_metrics(path: Path) -> Metrics:
    if not path.exists() or path.stat().st_size <= 0:
        return Metrics(False, 0, 0, 0, 0, 0, 0, 0, 0, 0, False, False)
    data = ffprobe_streams(path)
    v_start = a_start = v_dur = a_dur = 0.0
    has_v = has_a = False
    for s in data.get("streams", []) or []:
        ct = str(s.get("codec_type") or "").lower()
        try:
            st = float(s.get("start_time") or 0.0)
        except Exception:
            st = 0.0
        try:
            du = float(s.get("duration") or 0.0)
        except Exception:
            du = 0.0
        if ct == "video" and not has_v:
            has_v = True
            v_start, v_dur = st, du
        elif ct == "audio" and not has_a:
            has_a = True
            a_start, a_dur = st, du
    v_end, v_maxpkt = probe_packet_end(path, "v:0") if has_v else (0.0, 0.0)
    a_end, a_maxpkt = probe_packet_end(path, "a:0") if has_a else (0.0, 0.0)
    return Metrics(
        exists=True, size_bytes=path.stat().st_size,
        video_start=v_start, audio_start=a_start,
        video_duration=v_dur, audio_duration=a_dur,
        video_end=v_end, audio_end=a_end,
        max_video_pkt=v_maxpkt, max_audio_pkt=a_maxpkt,
        has_video=has_v, has_audio=has_a,
    )


def fmt_metrics(m: Metrics) -> str:
    if not m.exists:
        return "MISSING"
    return (
        f"size={m.size_bytes/1048576:.1f}MB "
        f"v_start={m.video_start:.3f} a_start={m.audio_start:.3f} "
        f"v_dur={m.video_duration:.3f} a_dur={m.audio_duration:.3f} "
        f"v_end={m.video_end:.3f} a_end={m.audio_end:.3f} "
        f"start_gap={m.start_gap:.3f} end_gap={m.end_gap:.3f} dur_delta={m.dur_delta:.3f} "
        f"max_v_pkt={m.max_video_pkt:.3f} max_a_pkt={m.max_audio_pkt:.3f} "
        f"abs_start={m.abs_start:.3f} healthy={m.healthy()} badness={m.alignment_badness():.3f}"
    )


def choose_better(raw_m: Metrics, sys_m: Metrics) -> tuple[str, str]:
    """通用对比规则。返回 (winner, reason)。winner ∈ {'raw','systrans'}。"""
    # 1. systrans 结构性损坏 → raw
    if not sys_m.exists:
        return "raw", "systrans_missing"
    if raw_m.has_video and not sys_m.has_video:
        return "raw", "systrans_video_lost"
    if raw_m.has_audio and not sys_m.has_audio:
        return "raw", "systrans_audio_lost"
    # 2. raw 已经健康 → 检查 systrans 是否在关键指标上比 raw 显著变差
    if raw_m.healthy():
        worsened = []
        if sys_m.end_gap > raw_m.end_gap + END_GAP_TOL:
            worsened.append(f"end_gap raw={raw_m.end_gap:.3f}->sys={sys_m.end_gap:.3f}")
        if sys_m.dur_delta > raw_m.dur_delta + DURATION_DELTA_TOL:
            worsened.append(f"dur_delta raw={raw_m.dur_delta:.3f}->sys={sys_m.dur_delta:.3f}")
        if sys_m.start_gap > raw_m.start_gap + START_GAP_TOL:
            worsened.append(f"start_gap raw={raw_m.start_gap:.3f}->sys={sys_m.start_gap:.3f}")
        if worsened:
            return "raw", "raw_healthy_systrans_worsened:" + ";".join(worsened)
    # 3. 用对齐健康度评分比较；接近则优先 raw（更保守）
    raw_b = raw_m.alignment_badness()
    sys_b = sys_m.alignment_badness()
    if sys_b + 0.05 < raw_b:
        return "systrans", f"systrans_better:raw_b={raw_b:.3f} sys_b={sys_b:.3f}"
    return "raw", f"raw_preferred:raw_b={raw_b:.3f} sys_b={sys_b:.3f}"


def remux_zero_based(src: Path, dst: Path) -> bool:
    """轻量归零：copy 流，重置 PTS 起点；保持 mp4 风格比特流以便后续 concat。"""
    cp = run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-reset_timestamps", "1",
        "-fflags", "+genpts",
        "-movflags", "+faststart",
        str(dst),
    ])
    return cp.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def concat_parts(parts: list[Path], dst: Path) -> bool:
    """通过 MPEG-TS 中间体合并，绕过 mp4 hev1/hvc1 与 extradata 差异。"""
    ts_files: list[Path] = []
    for idx, p in enumerate(parts, start=1):
        ts = OUT_DIR / f"_concat_{idx:03d}.ts"
        cp = run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(p),
            "-c", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "mpegts",
            str(ts),
        ])
        if cp.returncode != 0 or not ts.exists() or ts.stat().st_size <= 0:
            print(f"[ts_intermediate] failed on {p.name}: {cp.stderr}")
            return False
        ts_files.append(ts)
    concat_arg = "concat:" + "|".join(str(t) for t in ts_files)
    cp = run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", concat_arg,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(dst),
    ])
    if cp.returncode != 0:
        print("[concat] failed:", cp.stderr)
        return False
    # 清理中间 ts
    for t in ts_files:
        try:
            t.unlink(missing_ok=True)
        except Exception:
            pass
    return dst.exists() and dst.stat().st_size > 0


def main() -> int:
    parts = sorted(SRC_DIR.glob("teacher_145.part*.mp4"))
    if not parts:
        print(f"未找到分段：{SRC_DIR}")
        return 1
    print(f"输入分段 {len(parts)} 个，输出目录：{OUT_DIR}")
    report: list[dict] = []
    merge_inputs: list[Path] = []

    for idx, raw in enumerate(parts, start=1):
        print(f"\n===== part {idx}/{len(parts)}: {raw.name} =====")
        raw_m = collect_metrics(raw)
        print(f"[raw] {fmt_metrics(raw_m)}")

        systrans_out = OUT_DIR / (raw.stem + ".systrans.mp4")
        sys_err = ""
        if systrans_out.exists() and systrans_out.stat().st_size > 0:
            print(f"[systrans] reuse existing {systrans_out.name}")
        else:
            try:
                system_transform_file(raw, systrans_out)
            except Exception as e:
                sys_err = f"{type(e).__name__}: {e}"
                print(f"[systrans] failed: {sys_err}")
        sys_m = collect_metrics(systrans_out)
        print(f"[systrans] {fmt_metrics(sys_m)}")

        winner, reason = choose_better(raw_m, sys_m)
        print(f"[choose] -> {winner}  reason={reason}")
        chosen_src = raw if winner == "raw" else systrans_out
        chosen_m = raw_m if winner == "raw" else sys_m

        merge_part = OUT_DIR / (raw.stem + ".merge.mp4")
        if chosen_m.abs_start > ABS_START_REMUX_TOL:
            ok = remux_zero_based(chosen_src, merge_part)
            print(f"[zero_based_remux] {chosen_src.name} -> {merge_part.name} ok={ok}")
            if not ok:
                print("[!] 归零失败，直接使用原候选作为合并输入")
                merge_part = chosen_src
        else:
            # 起点已经接近 0，直接复制（避免污染 raw，做硬链接/复制）
            import shutil
            shutil.copy2(chosen_src, merge_part)
            print(f"[copy] {chosen_src.name} -> {merge_part.name}")
        post_m = collect_metrics(merge_part)
        print(f"[merge_input] {fmt_metrics(post_m)}")
        merge_inputs.append(merge_part)
        report.append({
            "part": idx, "raw": raw.name,
            "raw_metrics": asdict(raw_m), "systrans_metrics": asdict(sys_m),
            "winner": winner, "reason": reason,
            "merge_input": merge_part.name,
            "merge_input_metrics": asdict(post_m),
            "systrans_error": sys_err,
        })

    print("\n===== merging =====")
    final_out = OUT_DIR / "teacher_145.merged.mp4"
    ok = concat_parts(merge_inputs, final_out)
    print(f"[merge] ok={ok} -> {final_out}")
    if ok:
        merged_m = collect_metrics(final_out)
        print(f"[merged] {fmt_metrics(merged_m)}")
        report.append({"final": final_out.name, "metrics": asdict(merged_m)})

    (OUT_DIR / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n报告：{OUT_DIR/'report.json'}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
