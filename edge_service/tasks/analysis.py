from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..utils import safe_name as _safe_name, get_lesson_dir as _get_lesson_dir, resolve_lesson_date as _resolve_lesson_date, task_type_prefix as _task_type_prefix, fmt_duration as _fmt_duration
from ..video.ffmpeg import (
    _ffmpeg_bin,
    _ffprobe_bin,
    probe_bitrate_bps,
    probe_duration_seconds,
    probe_video_info,
)

log = logging.getLogger("edge.analysis")

try:
    import cv2
except Exception:
    cv2 = None

try:
    import numpy as np
except Exception:
    np = None

try:
    import webrtcvad
except Exception:
    webrtcvad = None

def _resolve_template_path() -> str:
    current = Path(__file__).resolve()
    candidates = [
        current.parent / "templates" / "report_template.html",
        current.parents[1] / "tasks" / "templates" / "report_template.html",
        current.parents[2] / "tasks" / "templates" / "report_template.html",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(candidates[0])


_TEMPLATE_PATH = _resolve_template_path()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_sec(s: float) -> str:
    s = int(max(0, float(s or 0)))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _run_capture(cmd: list[str], timeout: int = 120) -> tuple[str, str, int]:
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding="utf-8", errors="ignore", timeout=timeout,
        )
        return p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        return "", str(e), -1
    except Exception as e:
        return "", str(e), -1


def _extract_wav(input_path: str, rate: int = 16000) -> str | None:
    tmp_name = ""
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        tmp_name = tmp.name
        tmp.close()
        cmd = [_ffmpeg_bin(), "-hide_banner", "-y", "-i", input_path, "-ac", "1", "-ar", str(rate), tmp_name]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=120)
        return tmp_name
    except Exception:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
        return None


# ---------------------------------------------------------------------------
# Audio analysis
# ---------------------------------------------------------------------------

def _analyze_audio(input_path: str) -> dict:
    cmd = [_ffmpeg_bin(), "-hide_banner", "-y", "-i", input_path, "-af", "volumedetect", "-f", "null", "-"]
    _, err, _ = _run_capture(cmd)
    mean = None
    maxv = None
    for line in (err or "").splitlines():
        line = line.strip()
        if line.startswith("mean_volume:"):
            try:
                mean = float(line.split(":")[1].split(" ")[1])
            except Exception:
                pass
        if line.startswith("max_volume:"):
            try:
                maxv = float(line.split(":")[1].split(" ")[1])
            except Exception:
                pass
    return {"mean": mean, "max": maxv}


def _analyze_audio_librosa(input_path: str) -> dict:
    wav = ""
    try:
        import librosa
        wav = _extract_wav(input_path, 22050)
        if not wav:
            return {}
        y, sr = librosa.load(wav, sr=22050, mono=True, duration=120)
        rms = float(librosa.feature.rms(y=y).mean()) if getattr(y, "size", 0) > 0 else 0.0
        zcr = float(librosa.feature.zero_crossing_rate(y=y).mean()) if getattr(y, "size", 0) > 0 else 0.0
        try:
            tempo = float(librosa.beat.beat_track(y=y, sr=sr)[0])
        except Exception:
            tempo = 0.0
        return {"rms": rms, "zcr": zcr, "tempo": tempo}
    except Exception:
        return {}
    finally:
        if wav:
            try:
                os.unlink(wav)
            except Exception:
                pass


def _speech_activity(input_path: str) -> dict:
    wav_path = _extract_wav(input_path, 16000)
    if not wav_path:
        return {"speech_sec": 0.0}
    try:
        wf = wave.open(wav_path, "rb")
        rate = wf.getframerate()
        width = wf.getsampwidth()
        frame_ms = 20
        speech_frames = 0
        total_frames = 0
        th = 0
        import audioop
        use_vad = (webrtcvad is not None)
        vad = webrtcvad.Vad(2) if use_vad else None
        rms_hist: list[int] = []
        while True:
            nfr = int(rate * frame_ms / 1000)
            buf = wf.readframes(nfr)
            if not buf:
                break
            total_frames += 1
            ok = False
            if use_vad:
                try:
                    ok = vad.is_speech(buf, rate)
                except Exception:
                    ok = False
            else:
                try:
                    r = audioop.rms(buf, width)
                    rms_hist.append(r)
                    if len(rms_hist) > 50:
                        avg = sum(rms_hist) / float(len(rms_hist))
                        th = max(200, int(avg * 1.2))
                    ok = (r >= max(200, th))
                except Exception:
                    ok = False
            if ok:
                speech_frames += 1
        wf.close()
        try:
            os.unlink(wav_path)
        except Exception:
            pass
        speech_sec = speech_frames * frame_ms / 1000.0
        ratio = (speech_frames / float(total_frames)) if total_frames > 0 else 0.0
        return {"speech_sec": speech_sec, "ratio": ratio}
    except Exception:
        try:
            os.unlink(wav_path)
        except Exception:
            pass
        return {"speech_sec": 0.0}


def _motion_index(input_path: str, max_frames: int = 1000000000, max_process_seconds: int = 120,
                  sample_every: int = 8) -> dict:
    """计算视频运动指数。每隔 sample_every 帧采样一帧，避免大视频逐帧处理超时。"""
    if cv2 is None or np is None:
        return {"avg": 0.0, "peaks": 0}
    try:
        cap = cv2.VideoCapture(input_path)
        prev = None
        total = 0.0
        cnt = 0
        peaks = 0
        i = 0
        start_ts = time.time()
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            i += 1
            if i % sample_every != 0:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, (320, 180))
            if prev is not None:
                diff = cv2.absdiff(gray, prev)
                val = float(np.mean(diff))
                total += val
                cnt += 1
                if val > 20.0:
                    peaks += 1
            prev = gray
            if cnt >= max_frames or (time.time() - start_ts) >= max_process_seconds:
                break
        cap.release()
        avg = (total / cnt) if cnt > 0 else 0.0
        return {"avg": avg, "peaks": peaks}
    except Exception as e:
        log.error("运动分析异常: %s", e, exc_info=True)
        return {"avg": 0.0, "peaks": 0}


def _detect_scenes(input_path: str, thresh: float = 0.35) -> list[float]:
    try:
        vf = f"select=gt(scene,{thresh}),showinfo"
        cmd = [_ffmpeg_bin(), "-hide_banner", "-y", "-i", input_path, "-vf", vf, "-f", "null", "-"]
        _, err, _ = _run_capture(cmd, timeout=300)
        times: list[float] = []
        for line in (err or "").splitlines():
            line = line.strip()
            if "showinfo" in line and "pts_time:" in line:
                try:
                    t = float(line.split("pts_time:")[1].split(" ")[0])
                    times.append(t)
                except Exception:
                    pass
        return times
    except Exception:
        return []


def _make_thumbs(input_path: str, out_dir: str, prefix: str, duration: float) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    d = max(1.0, float(duration or 0.0))
    ts = [max(1.0, d * 0.1), max(1.0, d * 0.5), max(1.0, d * 0.9)]
    outs: list[str] = []
    for i, t in enumerate(ts, start=1):
        out = os.path.join(out_dir, f"{prefix}_{i}.jpg")
        cmd = [_ffmpeg_bin(), "-hide_banner", "-y", "-ss", str(round(t, 2)),
               "-i", input_path, "-frames:v", "1", "-q:v", "2", out]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
            outs.append(out)
        except Exception:
            pass
    return outs


# ---------------------------------------------------------------------------
# VAD series (per-second speech activity)
# ---------------------------------------------------------------------------

def _compute_vad_series(input_path: str, frame_ms: int = 20) -> tuple[list[float], list[float]]:
    wav_path = _extract_wav(input_path, 16000)
    if not wav_path:
        dur = probe_duration_seconds(input_path) or 0
        return [0.0] * int(dur), [0.0] * int(dur)
    try:
        wf = wave.open(wav_path, "rb")
        rate = wf.getframerate()
        width = wf.getsampwidth()
        seconds: list[float] = []
        cur_speech_frames = 0
        cur_frames = 0
        import audioop
        use_vad = (webrtcvad is not None)
        vad = webrtcvad.Vad(2) if use_vad else None
        rms_hist: list[int] = []
        while True:
            buf = wf.readframes(int(rate * frame_ms / 1000))
            if not buf:
                break
            cur_frames += 1
            ok = False
            if use_vad:
                try:
                    ok = vad.is_speech(buf, rate)
                except Exception:
                    ok = False
            else:
                try:
                    r = audioop.rms(buf, width)
                    rms_hist.append(r)
                    th = max(200, int((sum(rms_hist) / float(len(rms_hist))) * 1.2)) if len(rms_hist) > 50 else 200
                    ok = (r >= th)
                except Exception:
                    ok = False
            if ok:
                cur_speech_frames += 1
            if (cur_frames * frame_ms) >= 1000:
                seconds.append(cur_speech_frames * frame_ms / 1000.0)
                cur_frames = 0
                cur_speech_frames = 0
        wf.close()
        try:
            os.unlink(wav_path)
        except Exception:
            pass
        return seconds, [1.0 if s > 0 else 0.0 for s in seconds]
    except Exception:
        try:
            if wav_path:
                os.unlink(wav_path)
        except Exception:
            pass
        dur = probe_duration_seconds(input_path) or 0
        return [0.0] * int(dur), [0.0] * int(dur)


# ---------------------------------------------------------------------------
# FIAC / S-T / Engagement / Radar / Rt-Ch computation
# ---------------------------------------------------------------------------

def _compute_flanders(teacher_sec: list[float], student_sec: list[float], total_dur: float) -> dict:
    n = min(len(teacher_sec), len(student_sec))
    if n <= 0:
        return {
            "chart1": {"categories": ["9.主动发言", "5.讲授说明", "8.回应教师"], "counts": [0, 0, 0], "colors": ["#45B7D1", "#4ECDC4", "#45B7D1"]},
            "chart2": {"names": ["学生参与度", "间接/直接比", "教师话语比", "沉默比例"], "values": [0.0, 0.0, 0.0, 0.0]},
        }
    t_speech = s_speech = both = silent = 0
    for t, s in zip(teacher_sec[:n], student_sec[:n]):
        t_on = t >= 0.5
        s_on = s >= 0.5
        if t_on:
            t_speech += 1
        if s_on:
            s_speech += 1
        if t_on and s_on:
            both += 1
        if (not t_on) and (not s_on):
            silent += 1
    return {
        "chart1": {"categories": ["9.主动发言", "5.讲授说明", "8.回应教师"], "counts": [s_speech, t_speech - both, both], "colors": ["#45B7D1", "#4ECDC4", "#45B7D1"]},
        "chart2": {"names": ["学生参与度", "间接/直接比", "教师话语比", "沉默比例"], "values": [round(100.0 * s_speech / n, 2), 0.0, round(100.0 * t_speech / n, 2), round(100.0 * silent / n, 2)]},
    }


def _compute_st(teacher_sec: list[float], student_sec: list[float], bin_len: int = 10) -> dict:
    n = min(len(teacher_sec), len(student_sec))
    points: list[list[float]] = []
    t_total = s_total = 0.0
    seq: list[str] = []
    for i in range(0, n, bin_len):
        tbin = sum(1 for x in teacher_sec[i:i + bin_len] if x >= 0.5)
        sbin = sum(1 for x in student_sec[i:i + bin_len] if x >= 0.5)
        t_total += tbin
        s_total += sbin
        points.append([round(t_total, 2), round(s_total, 2)])
        if tbin > 0 or sbin > 0:
            seq.append("T" if tbin > sbin else ("S" if sbin > tbin else "TS"))
        else:
            seq.append("-")
    pre = present = practice = evalp = 0
    for i in range(n):
        t_on = teacher_sec[i] >= 0.5
        s_on = student_sec[i] >= 0.5
        if not t_on and not s_on:
            pre += 1
        elif t_on and not s_on:
            present += 1
        elif s_on and not t_on:
            practice += 1
        else:
            evalp += 1
    total = max(1, pre + present + practice + evalp)
    actual = [round(100.0 * pre / total, 1), round(100.0 * present / total, 1), round(100.0 * practice / total, 1), round(100.0 * evalp / total, 1)]
    return {
        "chart1": {"points": points, "student_total": round(s_total, 2), "teacher_total": round(t_total, 2), "total_duration": n, "sequence": seq},
        "chart2": {"phase_names": ["准备阶段", "呈现阶段", "练习阶段", "评价阶段"], "actual_ratios": actual, "expected_ratios": [15.0, 45.0, 30.0, 10.0]},
    }


def _compute_engagement(teacher_sec: list[float], student_sec: list[float]) -> dict:
    n = min(len(teacher_sec), len(student_sec))
    engagements = [round(min(1.0, (teacher_sec[i] + student_sec[i]) / 1.0), 3) for i in range(n)]
    return {"timestamps": list(range(n)), "engagements": engagements}


def _compute_radar(fiac: dict, st: dict, engagement: dict) -> dict:
    try:
        student_participation = (fiac.get("chart2") or {}).get("values", [0, 0, 0])[0]
    except Exception:
        student_participation = 0.0
    expected = (st.get("chart2") or {}).get("expected_ratios") or [15.0, 45.0, 30.0, 10.0]
    actual = (st.get("chart2") or {}).get("actual_ratios") or [0, 0, 0, 0]
    bal = max(0.0, 100.0 - sum(abs(float(a) - float(e)) for a, e in zip(actual, expected)))
    try:
        teacher_ratio = (fiac.get("chart2") or {}).get("values", [0, 0, 0])[2]
        fiac_score = round(100.0 - abs(50.0 - float(teacher_ratio or 0)), 2)
    except Exception:
        fiac_score = 80.0
    engs = engagement.get("engagements", [])
    avg_eng = (sum(engs) / float(len(engs) or 1)) * 100.0
    fm = actual
    try:
        sum_dev = sum(abs((float(v) if v is not None else 0.0) - 25.0) for v in fm)
        score4mat = max(0.0, 100.0 - (min(150.0, sum_dev) / 150.0) * 100.0)
    except Exception:
        score4mat = 60.0
    try:
        pres = float(fm[1] if len(fm) > 1 else 0.0)
        prac = float(fm[2] if len(fm) > 2 else 0.0)
        evalp2 = float(fm[3] if len(fm) > 3 else 0.0)
        ana = 0.3 * prac + 0.3 * evalp2
        eva = 0.5 * evalp2
        cre = 0.2 * evalp2
        mem = 0.5 * pres
        und = 0.5 * pres
        app = 0.7 * prac
        total_bloom = mem + und + app + ana + eva + cre
        scale = (100.0 / total_bloom) if total_bloom > 0 else 0.0
        bloom_high = (ana + eva + cre) * scale
    except Exception:
        bloom_high = 50.0
    cats = ["弗兰德斯\n互动分析", "S-T模型\n阶段平衡", "学生\n参与度", "4MAT\n分布平衡", "布鲁姆\n高阶思维"]
    scores = [fiac_score, round(bal, 2), round(avg_eng, 2), round(score4mat, 1), round(bloom_high, 1)]
    return {"categories": cats, "scores": scores, "ideal_score": 80}


def _compute_rtch(teacher_sec: list[float], student_sec: list[float]) -> dict:
    n = max(1, min(len(teacher_sec), len(student_sec)))
    t_speech = sum(1 for s in teacher_sec[:n] if s >= 0.5)
    s_speech = sum(1 for s in student_sec[:n] if s >= 0.5)
    tr = t_speech / float(n)
    sr = s_speech / float(n)
    label = "Rt-Ch平衡"
    if tr >= 0.65:
        label = "Rt主导"
    elif sr >= 0.65:
        label = "Ch主导"
    return {"label": label, "teacher_ratio": round(tr * 100, 2), "student_ratio": round(sr * 100, 2)}


# ---------------------------------------------------------------------------
# Pack single video analysis
# ---------------------------------------------------------------------------

def _pack_video(path: str, log_cb: Callable[[str], None] | None = None) -> dict:
    def _cb(msg: str) -> None:
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    _cb(f"分析视频: {os.path.basename(path)}")
    info = probe_video_info(path)
    dur = probe_duration_seconds(path)
    br = probe_bitrate_bps(path)

    _cb("分析音频...")
    a1 = _analyze_audio(path)
    a2 = _analyze_audio_librosa(path)

    _cb("检测语音活动...")
    sp = _speech_activity(path)

    _cb("检测场景变化...")
    sc = _detect_scenes(path)

    _cb("分析运动指数...")
    mo = _motion_index(path)

    _cb(f"视频分析完成: {os.path.basename(path)}")
    return {"info": info, "duration": dur, "bitrate": br, "audio": {**a1, **a2}, "speech": sp, "scene_times": sc, "motion": mo}


# ---------------------------------------------------------------------------
# HTML report generation
# ---------------------------------------------------------------------------

def _build_html_report(data: dict, mode: str = "web") -> str:
    """Build HTML report from analysis data using template."""
    try:
        with open(_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            tpl = f.read()
    except Exception as e:
        log.error("加载分析模板失败: %s", e)
        return f"<h1>加载分析模板失败: {e}</h1>"

    try:
        styles = re.findall(r"<style[^>]*>([\s\S]*?)</style>", tpl, flags=re.IGNORECASE)
        style_css = styles[-1] if styles else ""
        if str(mode).lower() == "h5":
            mobile_css = (
                "html,body{width:100%;max-width:100%;overflow-x:hidden}"
                ".container{width:100%;max-width:100%;margin:0 auto;padding:0 12px}"
                ".header{padding:20px 16px;border-radius:12px;margin-bottom:0;text-align:left}"
                ".header h1{font-size:20px;line-height:1.4}"
                ".summary{grid-template-columns:1fr;gap:12px;margin-top:20px;margin-bottom:16px;padding:0}"
                ".summary-card{padding:16px;border-radius:12px;box-shadow:none;border:1px solid #e2e8f0}"
                ".summary-card h3{font-size:12px}"
                ".summary-card .value{font-size:22px}"
                ".content{padding:0 12px}"
                ".section{padding:16px;border-radius:12px;margin-bottom:16px}"
                ".section h2{font-size:18px;padding-left:10px}"
                ".chart-container,.chart-row{padding:12px;margin-bottom:12px;width:100%}"
                ".chart-row{grid-template-columns:1fr !important}"
                ".chart-box{height:260px;min-height:220px}"
                ".model-description,.analysis-text,.teaching-suggestions{font-size:14px;padding:12px}"
                ".teaching-suggestions ul{padding-left:16px}"
                ".footer{padding:16px;margin-top:16px;font-size:12px}"
            )
            style_css = f"{style_css}\n{mobile_css}"

        bodies = re.findall(r"<body[^>]*>([\s\S]*?)</body>", tpl, flags=re.IGNORECASE)
        body_html = bodies[-1] if bodies else tpl

        head_scripts = (
            "<script>(function(){"
            "function load(src){return new Promise(function(resolve,reject){var s=document.createElement('script');s.src=src;s.onload=resolve;s.onerror=function(){reject(new Error('load fail: '+src));};document.head.appendChild(s);});}"
            "var cdns=['https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js','https://fastly.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js','https://unpkg.com/echarts@5.4.3/dist/echarts.min.js'];"
            "window.__echartsReady__=function(fn){if(window.echarts&&window.echarts.init){fn();return;}var i=0;function next(){if(window.echarts&&window.echarts.init){fn();return;}if(i>=cdns.length){console.error('ECharts 加载失败');return;}load(cdns[i++]).then(function(){setTimeout(next,20);}).catch(function(){next();});}next();};"
            "})();</script>"
        )

        tpl = (
            "<!DOCTYPE html><html lang=zh-CN>"
            "<head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<style>{style_css}</style>{head_scripts}</head>"
            f"<body>{body_html}</body></html>"
        )

        # Inject data variables
        def _inject_var(name: str, payload: dict) -> None:
            nonlocal tpl
            pat = re.compile(rf"var\s+{name}\s*=\s*\{{[\s\S]*?\}};", re.MULTILINE)
            repl = f"var {name} = {json.dumps(payload, ensure_ascii=False)};"
            tpl = pat.sub(repl, tpl)

        _inject_var("flandersData", data.get("fiac") or {})
        _inject_var("stData", data.get("st") or {})
        _inject_var("engagementData", data.get("engagement") or {})
        _inject_var("radarData", data.get("radar") or {})

        try:
            rt = float((data.get("rtch") or {}).get("teacher_ratio") or 0) / 100.0
            ch = float((data.get("rtch") or {}).get("student_ratio") or 0) / 100.0
            _inject_var("rtchPoint", {"rt": round(rt, 4), "ch": round(ch, 4)})
        except Exception:
            pass

        try:
            scores = (data.get("radar") or {}).get("scores") or [0, 0, 0]
            fiac_s = round(scores[0] if len(scores) > 0 else 0, 1)
            st_s = round(scores[1] if len(scores) > 1 else 0, 1)
            eng_s = round(scores[2] if len(scores) > 2 else 0, 1)
            tpl = re.sub(
                r"(整体得分表现为：FIAC\s*)[0-9.]+分、S‑T\s*[0-9.]+分、参与度\s*[0-9.]+%",
                rf"\g<1>{fiac_s}分、S‑T {st_s}分、参与度 {eng_s}%", tpl,
            )
        except Exception:
            pass

        try:
            tpl = tpl.replace("专业教室视频分析报告（演示）", "专业教室视频分析报告")
        except Exception:
            pass

        # Inject advice
        try:
            adv_text = (data.get("advice") or {}).get("text") or ""
            adv_items = [x.strip() for x in adv_text.split("\n") if x.strip()]
            if adv_items:
                def _esc(s: str) -> str:
                    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                newlis = "".join([f"<li>{_esc(it)}</li>" for it in adv_items])
                m = re.search(r"<div class=\"teaching-suggestions\">\s*<h4>\s*综合改进建议\s*</h4>[\s\S]*?<ul>", tpl)
                if m:
                    s = m.end()
                    tpl = tpl[:s] + newlis + tpl[s:]
        except Exception:
            pass

        return tpl
    except Exception as e:
        log.error("报告生成失败: %s", e)
        return f"<h1>报告生成失败: {e}</h1>"


# ---------------------------------------------------------------------------
# Full report generation
# ---------------------------------------------------------------------------

def _generate_report_sync(
    teacher_mp4: str,
    student_mp4: str,
    out_dir: str,
    details: dict | None = None,
    log_cb: Callable[[str], None] | None = None,
    cancel_check: Callable[[], str | None] | None = None,
) -> dict:
    """Synchronous report generation (runs in thread)."""
    os.makedirs(out_dir, exist_ok=True)

    def _check_cancel() -> None:
        if cancel_check is None:
            return
        mode = str(cancel_check() or "").strip().lower()
        if mode in {"pause", "stop"}:
            raise RuntimeError(f"cancelled:{mode}")

    def _cb(msg: str) -> None:
        _check_cancel()
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    _cb("开始视频分析")
    timeout = 300

    with ThreadPoolExecutor(max_workers=4) as ex:
        _cb("并行分析教师和学员视频...")
        f_t = ex.submit(_pack_video, teacher_mp4, log_cb)
        f_s = ex.submit(_pack_video, student_mp4, log_cb)

        try:
            t_view = f_t.result(timeout=timeout)
            _check_cancel()
            _cb("教师视频分析完成")
        except Exception as e:
            log.error("教师视频分析失败: %s", e, exc_info=True)
            t_view = {"info": {}, "duration": 0, "bitrate": 0, "audio": {}, "speech": {}, "scene_times": [], "motion": {"avg": 0.0, "peaks": 0}}

        try:
            s_view = f_s.result(timeout=timeout)
            _check_cancel()
            _cb("学员视频分析完成")
        except Exception as e:
            log.error("学员视频分析失败: %s", e, exc_info=True)
            s_view = {"info": {}, "duration": 0, "bitrate": 0, "audio": {}, "speech": {}, "scene_times": [], "motion": {"avg": 0.0, "peaks": 0}}

        # Generate thumbnails
        _cb("生成缩略图...")
        assets = os.path.join(out_dir, "report_assets")
        ft = ex.submit(_make_thumbs, teacher_mp4, assets, "teacher", t_view["duration"])
        fs = ex.submit(_make_thumbs, student_mp4, assets, "student", s_view["duration"])
        try:
            t_thumbs = ft.result(timeout=60)
            _check_cancel()
        except Exception:
            t_thumbs = []
        try:
            s_thumbs = fs.result(timeout=60)
            _check_cancel()
        except Exception:
            s_thumbs = []

    _cb("计算教学分析指标...")
    _check_cancel()
    td = float(t_view.get("duration") or 0)
    sd = float(s_view.get("duration") or 0)

    t_sec, _ = _compute_vad_series(teacher_mp4)
    s_sec, _ = _compute_vad_series(student_mp4)

    fiac = _compute_flanders(t_sec, s_sec, max(td, sd))
    st = _compute_st(t_sec, s_sec, 10)
    eng = _compute_engagement(t_sec, s_sec)
    radar = _compute_radar(fiac, st, eng)
    rtch = _compute_rtch(t_sec, s_sec)

    # Generate advice
    adv: list[str] = []
    try:
        tr = fiac["chart2"]["values"][2]
        sp = fiac["chart2"]["values"][0]
        if tr > 70:
            adv.append("教师讲授偏高，建议增加学生主体发言与同伴互动。")
        if sp < 30:
            adv.append("学生发言比例偏低，鼓励学生更多参与。")
        ar = st["chart2"]["actual_ratios"]
        er = st["chart2"]["expected_ratios"]
        if ar[0] > 40:
            adv.append("课堂准备阶段占比较高，可适当缩短。")
        if ar[2] < 30:
            adv.append("练习阶段占比较低，建议增加练习环节。")
        bal = radar.get("scores", [0, 0, 0])[1]
        if bal < 60:
            if ar[1] < er[1] - 10:
                adv.append("呈现阶段不足，建议结构化讲解并辅以板书/可视化。")
            if ar[2] < er[2] - 10:
                adv.append("练习阶段不足，增加分层任务与即时反馈。")
            if ar[3] < er[3] - 5:
                adv.append("评价阶段偏弱，引入形成性评价与同伴互评。")
        mi_t = (t_view.get("motion") or {}).get("avg") or 0.0
        mi_s = (s_view.get("motion") or {}).get("avg") or 0.0
        if mi_t < 5 and mi_s < 5:
            adv.append("画面运动变化较低，建议引入实物演示或板书走位增强关注。")
        avg_eng = (sum(eng.get("engagements", []) or []) / float(len(eng.get("engagements", []) or [1]))) * 100.0
        if avg_eng < 50:
            adv.append("学生平均参与度偏低，采用积分激励与随机点名提升参与。")
    except Exception:
        pass

    data = {
        "class_name": (details or {}).get("class_name"),
        "lesson": (details or {}).get("lesson"),
        "date": str((details or {}).get("date") or "").split(" ")[0],
        "teacher": {**t_view, "thumbs": t_thumbs},
        "student": {**s_view, "thumbs": s_thumbs},
        "teacher_path": teacher_mp4,
        "student_path": student_mp4,
        "fiac": fiac,
        "st": st,
        "engagement": eng,
        "radar": radar,
        "rtch": rtch,
        "advice": {"text": "\n".join(adv)},
    }

    # Interaction analysis
    tsc = t_view.get("scene_times") or []
    ssc = s_view.get("scene_times") or []
    inter_count = 0
    inter_dur = 0.0
    if tsc and ssc:
        i = j = 0
        while i < len(tsc) and j < len(ssc):
            dt = tsc[i] - ssc[j]
            if abs(dt) <= 2.0:
                inter_count += 1
                inter_dur += 5.0
                i += 1
                j += 1
            elif dt < 0:
                i += 1
            else:
                j += 1
    data["interaction"] = {"count": inter_count, "duration": inter_dur}

    # Generate HTML reports
    _cb("生成HTML报告...")
    for fmt in ["web", "h5"]:
        _check_cancel()
        fname = os.path.join(out_dir, f"report_{fmt}.html")
        html = _build_html_report(data, fmt)
        with open(fname, "w", encoding="utf-8-sig") as f:
            f.write(html)
        _cb(f"报告已生成: report_{fmt}.html")

    # Save data as JSON
    _check_cancel()
    json_path = os.path.join(out_dir, "report_data.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass

    _cb("报告生成完成")
    return data


# ---------------------------------------------------------------------------
# Async entry point for ANALYSIS step
# ---------------------------------------------------------------------------

def _find_video_by_type(lesson_dir: Path, task_type: int) -> Path | None:
    """Find a video file in lesson dir by task type prefix."""
    prefix = _task_type_prefix(task_type)
    if not lesson_dir.exists():
        return None
    for f in sorted(lesson_dir.glob(f"{prefix}_*.mp4")):
        name = f.name.lower()
        if ".part" in name or "_nosub" in name or "_sub_tmp" in name or "_temp_embed" in name:
            continue
        if f.stat().st_size > 0:
            return f
    return None


async def run_analysis_task(
    raw: dict[str, Any],
    on_progress: Callable,
    db=None,
) -> list[dict[str, Any]]:
    """Execute ANALYSIS step: analyze teacher+student videos and generate report."""
    _task_start = time.monotonic()

    def _cancel_check() -> str | None:
        mode = str(raw.get("__cancel") or "").strip().lower()
        return mode if mode in {"pause", "stop"} else None

    def _raise_if_cancelled() -> None:
        mode = _cancel_check()
        if mode:
            raise RuntimeError(f"cancelled:{mode}")

    def _est_total(elapsed: float, pct: int) -> float:
        if pct <= 0 or elapsed <= 0:
            return -1.0
        return elapsed / (pct / 100.0)

    def _progress_msg(action: str, pct: int, elapsed: float) -> str:
        parts = [action, f"已耗时{_fmt_duration(elapsed)}"]
        est = _est_total(elapsed, pct)
        if est > 0:
            parts.append(f"预计时长{_fmt_duration(est)}")
        return "，".join(parts)

    on_progress("ANALYSIS", 0.0, _progress_msg("准备视频分析", 0, 0))
    log.info("ANALYSIS 0%% 准备视频分析")
    _raise_if_cancelled()

    # Resolve lesson directory
    from ..utils import load_download_path as _load_download_path
    download_root = Path(_load_download_path())
    lesson_id = str(raw.get("lessonId") or "").strip()
    lesson_date = _resolve_lesson_date(raw.get("lessonDate"), raw.get("lessonStartAt"))

    lesson_dir = _get_lesson_dir(lesson_date, lesson_id, download_root)

    # Find teacher and student videos
    teacher_mp4 = _find_video_by_type(lesson_dir, 1)
    student_mp4 = _find_video_by_type(lesson_dir, 2)

    if teacher_mp4 is None:
        raise RuntimeError(f"找不到教师视频（teacher_*.mp4）: {lesson_dir}")
    if student_mp4 is None:
        log.warning("找不到学员视频（student_*.mp4），将仅分析教师视频")
        student_mp4 = teacher_mp4  # fallback: use teacher for both

    elapsed = time.monotonic() - _task_start
    on_progress("ANALYSIS", 0.05, _progress_msg(f"开始分析（教师: {teacher_mp4.name}, 学员: {student_mp4.name}）", 5, elapsed))
    log.info("ANALYSIS 5%% 教师=%s 学员=%s", teacher_mp4.name, student_mp4.name)
    _raise_if_cancelled()

    out_dir = str(lesson_dir / "report")

    # Progress tracking for the sync function
    _last_pct = [5]

    def _log_cb(msg: str) -> None:
        _raise_if_cancelled()
        elapsed_now = time.monotonic() - _task_start
        # Estimate progress: video analysis ~60%, VAD ~20%, report gen ~15%, done 5%
        pct = _last_pct[0]
        if "分析视频" in msg:
            pct = min(30, pct + 5)
        elif "视频分析完成" in msg:
            pct = min(60, pct + 10)
        elif "缩略图" in msg:
            pct = 65
        elif "教学分析指标" in msg:
            pct = 70
        elif "生成HTML" in msg:
            pct = 90
        elif "报告生成完成" in msg:
            pct = 95
        else:
            pct = min(90, pct + 2)
        _last_pct[0] = pct
        on_progress("ANALYSIS", pct / 100.0, _progress_msg(msg, pct, elapsed_now))
        log.info("ANALYSIS %d%% %s", pct, msg)

    # Run the analysis in a thread
    details = {
        "class_name": str(raw.get("relate_class") or ""),
        "lesson": str(raw.get("relate_lesson") or ""),
        "date": lesson_date,
    }

    try:
        data = await asyncio.to_thread(
            _generate_report_sync,
            str(teacher_mp4),
            str(student_mp4),
            out_dir,
            details,
            _log_cb,
            _cancel_check,
        )
    except RuntimeError as e:
        msg = str(e)
        if msg in {"cancelled:pause", "cancelled:stop"}:
            log.info("ANALYSIS cooperative cancel: %s", msg)
        raise

    _raise_if_cancelled()
    elapsed = time.monotonic() - _task_start
    on_progress("ANALYSIS", 1.0, f"视频分析已完成，共耗时{_fmt_duration(elapsed)}")
    log.info("ANALYSIS 100%% 视频分析报告完成, 总耗时=%.0f秒", elapsed)

    # Collect artifacts
    artifacts: list[dict[str, Any]] = []
    report_dir = Path(out_dir)
    for fname in ["report_web.html", "report_h5.html", "report_data.json"]:
        fp = report_dir / fname
        if fp.exists():
            file_type = "report_data"
            if fname == "report_web.html":
                file_type = "report_web_html"
            elif fname == "report_h5.html":
                file_type = "report_h5_html"
            artifacts.append({
                "path": str(fp),
                "sizeBytes": int(fp.stat().st_size),
                "stepCode": "ANALYSIS",
                "fileType": file_type,
            })

    # Thumbnails
    assets_dir = report_dir / "report_assets"
    if assets_dir.exists():
        for fp in sorted(assets_dir.glob("*.jpg")):
            artifacts.append({
                "path": str(fp),
                "sizeBytes": int(fp.stat().st_size),
                "stepCode": "ANALYSIS",
                "fileType": "report_thumbnail",
            })

    return artifacts
