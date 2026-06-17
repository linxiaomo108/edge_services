from __future__ import annotations

import asyncio
import audioop
import html
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import wave
from pathlib import Path
from typing import Any, Callable

from ..monitor_config import load_monitor_cfg as _load_monitor_cfg
from ..utils import safe_name as _safe_name, get_lesson_dir as _get_lesson_dir, load_download_path as _load_download_path, resolve_lesson_date as _resolve_lesson_date, fmt_duration as _fmt_duration
from ..video.ffmpeg import extract_wav, get_audio_start_time, has_audio_stream, probe_duration_seconds

log = logging.getLogger("edge.speech")

_whisper_models: dict[tuple[str, str], Any] = {}
_whisper_model_lock = threading.Lock()
_whisper_device: str = "cpu"


def _load_speech_model_size(model_size_override: str | None = None) -> str:
    if model_size_override:
        v = str(model_size_override).strip().lower()
        if v in {"small", "medium", "large", "large-v3"}:
            return v
    try:
        data = _load_monitor_cfg()
        v = str(data.get("speechModel") or "medium").strip().lower()
        return v if v in {"small", "medium", "large", "large-v3"} else "medium"
    except Exception:
        return "medium"


def _resolve_model_name(size: str) -> str:
    s = str(size or "medium").strip().lower()
    if s == "small":
        return "small"
    if s == "large-v3":
        return "large-v3"
    if s == "large":
        return "large-v3"
    return "medium"


def _candidate_model_roots() -> list[Path]:
    roots: list[Path] = []
    env_root = str(os.getenv("WHISPER_MODEL_DIR") or "").strip()
    if env_root:
        roots.append(Path(env_root))
    hf_root = str(os.getenv("HF_HOME") or "").strip()
    if hf_root:
        roots.append(Path(hf_root))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent.parent / "models")
    else:
        project_root = Path(__file__).resolve().parents[2]
        roots.append(project_root / "models")
        roots.append(project_root / "packaging_resources" / "models")
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _resolve_local_model_dir(model_name: str) -> Path | None:
    for root in _candidate_model_roots():
        for candidate in [root / f"faster-whisper-{model_name}", root / model_name, root]:
            if (candidate / "model.bin").exists() and (candidate / "config.json").exists():
                return candidate
    return None


def _resolve_whisper_model_source(model_name: str) -> tuple[str, dict[str, Any]]:
    local_model_dir = _resolve_local_model_dir(model_name)
    if local_model_dir is not None:
        return str(local_model_dir), {"local_files_only": True}
    if getattr(sys, "frozen", False):
        expected = _candidate_model_roots()[0] / f"faster-whisper-{model_name}"
        raise RuntimeError(f"本地Whisper模型缺失: {expected}")
    download_root = _candidate_model_roots()[0] if _candidate_model_roots() else None
    opts: dict[str, Any] = {}
    if download_root is not None:
        opts["download_root"] = str(download_root)
    return model_name, opts


def _expected_whisper_feature_size(model_name: str) -> int | None:
    normalized = str(model_name or "").strip().lower()
    if normalized in {"large-v3", "large"}:
        return 128
    return None


def _current_whisper_feature_size(model: Any) -> int | None:
    feat_kwargs = getattr(model, "feat_kwargs", None)
    if isinstance(feat_kwargs, dict):
        try:
            value = int(feat_kwargs.get("feature_size") or 0)
            if value > 0:
                return value
        except Exception:
            pass
    extractor = getattr(model, "feature_extractor", None)
    mel_filters = getattr(extractor, "mel_filters", None)
    if mel_filters is not None:
        try:
            value = int(len(mel_filters))
            if value > 0:
                return value
        except Exception:
            pass
    return None


def _ensure_whisper_feature_compat(model: Any, model_name: str, model_source: str) -> Any:
    expected = _expected_whisper_feature_size(model_name)
    if expected is None:
        return model
    actual = _current_whisper_feature_size(model)
    if actual == expected:
        return model
    try:
        from faster_whisper.feature_extractor import FeatureExtractor
    except Exception as exc:
        raise RuntimeError(f"Whisper特征提取器加载失败: {exc}") from exc
    extractor = getattr(model, "feature_extractor", None)
    sampling_rate = int(getattr(extractor, "sampling_rate", 16000) or 16000)
    hop_length = int(getattr(extractor, "hop_length", 160) or 160)
    chunk_length = int(getattr(extractor, "chunk_length", 30) or 30)
    n_fft = int(getattr(extractor, "n_fft", 400) or 400)
    feat_kwargs = {
        "feature_size": expected,
        "sampling_rate": sampling_rate,
        "hop_length": hop_length,
        "chunk_length": chunk_length,
        "n_fft": n_fft,
    }
    model.feat_kwargs = feat_kwargs
    model.feature_extractor = FeatureExtractor(**feat_kwargs)
    try:
        model.num_samples_per_token = model.feature_extractor.hop_length * model.input_stride
        model.frames_per_second = model.feature_extractor.sampling_rate // model.feature_extractor.hop_length
        model.tokens_per_second = model.feature_extractor.sampling_rate // model.num_samples_per_token
    except Exception:
        pass
    preprocessor_cfg = Path(str(model_source)) / "preprocessor_config.json"
    if not preprocessor_cfg.exists():
        log.warning("Whisper模型目录缺少 preprocessor_config.json，已按 %s 所需 feature_size=%s 自动修正: %s", model_name, expected, model_source)
    else:
        log.warning("Whisper特征维度与模型不匹配，已按 %s 所需 feature_size=%s 自动修正: actual=%s source=%s", model_name, expected, actual, model_source)
    return model


_HALLUCINATION_RULES_CACHE: dict[str, Any] | None = None
_HALLUCINATION_RULES_LOCK = threading.Lock()

_CORRECTION_DICT_CACHE: dict[str, str] | None = None
_CORRECTION_DICT_LOCK = threading.Lock()

_SUBJECT_SPECIAL_MAP: dict[str, str] = {
    "信息学算法": "math",
    "信息学语言传播": "english",
    "信息学实验p": "physics",
    "信息学实验c": "chemistry",
    "国文素养": "chinese",
}

_SUBJECT_DIRECT_MAP: dict[str, str] = {
    "数学": "math",
    "英语": "english",
    "语文": "chinese",
    "物理": "physics",
    "化学": "chemistry",
    "生物": "biology",
    "历史": "history_politics",
    "政治": "history_politics",
    "道法": "history_politics",
    "思想品德": "history_politics",
}

_SUBJECT_LABELS = {
    "math": "数学",
    "english": "英语",
    "chinese": "语文",
    "physics": "物理",
    "chemistry": "化学",
    "biology": "生物",
    "history_politics": "历史政治",
}

_SUBJECT_KEYWORDS = {
    "math": ["数学", "函数", "方程", "几何", "概率", "排列组合", "圆锥曲线", "导数", "数列"],
    "english": ["英语", "英文", "单词", "语法", "阅读理解", "完形填空", "听力", "作文"],
    "chinese": ["语文", "古诗", "文言文", "阅读", "作文", "修辞", "字词", "课文"],
    "physics": ["物理", "速度", "加速度", "力学", "电路", "电压", "电流", "功率"],
    "chemistry": ["化学", "方程式", "离子", "溶液", "酸碱", "氧化", "还原", "元素"],
    "biology": ["生物", "细胞", "遗传", "光合作用", "呼吸作用", "染色体"],
    "history_politics": ["历史", "政治", "道法", "思想品德", "改革开放", "朝代", "宪法"],
}

_SUBJECT_PROMPT_TERMS = {
    "math": ["加减乘除", "三角形", "平行四边形", "排列组合", "概率", "方程", "根号", "分式", "导数", "抛物线"],
    "english": ["listen", "repeat", "dialogue", "vocabulary", "grammar", "reading", "writing", "sentence", "pronunciation"],
    "chinese": ["课文", "段落大意", "中心思想", "古诗词", "文言文", "修辞手法", "生字词", "朗读"],
    "physics": ["受力分析", "速度", "位移", "电流", "电压", "电阻", "功率", "实验"],
    "chemistry": ["化学方程式", "离子方程式", "酸碱盐", "氧化还原", "实验现象", "元素符号"],
    "biology": ["细胞结构", "遗传", "变异", "光合作用", "呼吸作用", "生态系统"],
    "history_politics": ["历史事件", "时间线", "制度", "宪法", "核心价值观", "国家治理"],
}


def _as_bool(v: object, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    text = str(v).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _load_speech_runtime_options() -> dict[str, Any]:
    try:
        data = _load_monitor_cfg() or {}
    except Exception:
        data = {}
    prompt_mode = str(data.get("speechPromptMode") or "off").strip().lower()
    if prompt_mode not in {"off", "auto", "custom"}:
        prompt_mode = "off"
    try:
        speech_temperature = float(data.get("speechTemperature", 0.0) or 0.0)
    except Exception:
        speech_temperature = 0.0
    try:
        speech_retry_temperature = float(data.get("speechRetryTemperature", 0.4) or 0.4)
    except Exception:
        speech_retry_temperature = 0.4
    vad_mode = str(data.get("speechVadMode") or "builtin").strip().lower()
    if vad_mode not in {"builtin", "silero", "off"}:
        vad_mode = "builtin"
    return {
        "word_timestamps": _as_bool(data.get("speechWordTimestamps"), True),
        "prompt_mode": prompt_mode,
        "prompt_text": str(data.get("speechPromptText") or "").strip(),
        "hallucination_filter": _as_bool(data.get("speechHallucinationFilter"), True),
        "speech_temperature": min(1.0, max(0.0, speech_temperature)),
        "speech_retry_temperature": min(1.0, max(0.0, speech_retry_temperature)),
        "vad_mode": vad_mode,
    }


def _load_correction_dict() -> dict[str, str]:
    global _CORRECTION_DICT_CACHE
    if _CORRECTION_DICT_CACHE is not None:
        return _CORRECTION_DICT_CACHE
    with _CORRECTION_DICT_LOCK:
        if _CORRECTION_DICT_CACHE is not None:
            return _CORRECTION_DICT_CACHE
        path = Path(__file__).resolve().parents[2] / "docs" / "correction_dict.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            raw = {}
        merged: dict[str, str] = {}
        for section in raw.values():
            if isinstance(section, dict) and not section.get("_meta"):
                for wrong, right in section.items():
                    if isinstance(wrong, str) and isinstance(right, str) and wrong and right and wrong != right:
                        merged[wrong] = right
        _CORRECTION_DICT_CACHE = merged
        return _CORRECTION_DICT_CACHE


def _apply_corrections(text: str) -> str:
    """应用术语纠错转换库，按 key 长度从长到短优先匹配，防止短 key 提前替换。"""
    if not text:
        return text
    corrections = _load_correction_dict()
    if not corrections:
        return text
    for wrong in sorted(corrections, key=len, reverse=True):
        if wrong in text:
            text = text.replace(wrong, corrections[wrong])
    return text


def _load_hallucination_rules() -> dict[str, Any]:
    global _HALLUCINATION_RULES_CACHE
    if _HALLUCINATION_RULES_CACHE is not None:
        return _HALLUCINATION_RULES_CACHE
    with _HALLUCINATION_RULES_LOCK:
        if _HALLUCINATION_RULES_CACHE is not None:
            return _HALLUCINATION_RULES_CACHE
        path = Path(__file__).resolve().parents[2] / "docs" / "hallucination_blacklist.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        _HALLUCINATION_RULES_CACHE = data if isinstance(data, dict) else {}
        return _HALLUCINATION_RULES_CACHE


def _collect_task_texts(raw: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("subjectName", "courseName", "lessonName", "className", "gradeName", "relate_class", "relate_lesson"):
        text = str(raw.get(key) or "").strip()
        if text:
            values.append(text)
    return values


def _detect_subject_code(raw: dict[str, Any]) -> str:
    subject_field = str(raw.get("subject") or "").strip()
    if subject_field:
        lower_sf = subject_field.lower()
        if lower_sf in _SUBJECT_SPECIAL_MAP:
            return _SUBJECT_SPECIAL_MAP[lower_sf]
        for display, code in _SUBJECT_DIRECT_MAP.items():
            if display in subject_field:
                return code
    merged = " ".join(_collect_task_texts(raw)).lower()
    if not merged:
        return ""
    for lower_sf, code in _SUBJECT_SPECIAL_MAP.items():
        if lower_sf in merged:
            return code
    for subject_code, keywords in _SUBJECT_KEYWORDS.items():
        if any(keyword.lower() in merged for keyword in keywords):
            return subject_code
    return ""


def _is_weak_prompt_piece(piece: str) -> bool:
    compact = re.sub(r"\s+", "", str(piece or "").strip())
    if len(compact) < 2:
        return True
    weak_patterns = [
        r"^第?\d+[课次讲节章单元课]?$",
        r"^第[一二三四五六七八九十百零两]+[课次讲节章单元课]?$",
        r"^[一二三四五六七八九十两0-9]+年级$",
        r"^[一二三四五六七八九十两0-9]+班$",
        r"^高[一二三]$",
        r"^初[一二三]$",
        r"^[A-Za-z0-9_-]{1,8}$",
    ]
    if any(re.fullmatch(pattern, compact) for pattern in weak_patterns):
        return True
    if compact in {"班级", "课程", "课次", "课堂", "班级信息", "课程信息"}:
        return True
    return False


def _extract_grade_label(raw: dict[str, Any]) -> str:
    """从 grade/gradeName/className 提取可读年级描述。
    返回如"高一"/"初二"/"六年级"等；无法确认时返回空字符串。
    """
    for key in ("grade", "gradeName", "className", "relate_class", "relateClass"):
        text = str(raw.get(key) or "").strip()
        if not text:
            continue
        grade_patterns = [
            (r"小学[\u4e00二三四五六1-6]+年级", lambda m: m.group()),
            (r"(高[一二三]|初[一二三]|[\u4e00二三四五六七八九十][\u4e00二三四五六]?年级)", lambda m: m.group()),
            (r"高[一二三]", lambda m: m.group() + "年级"),
            (r"初[一二三]", lambda m: m.group() + "年级"),
        ]
        for pattern, formatter in grade_patterns:
            m = re.search(pattern, text)
            if m:
                return formatter(m)
    return ""


def _extract_prompt_terms(raw: dict[str, Any], subject_code: str) -> list[str]:
    terms: list[str] = []
    for piece in _SUBJECT_PROMPT_TERMS.get(subject_code, []):
        if piece not in terms:
            terms.append(piece)
    return terms[:16]


def _build_initial_prompt(raw: dict[str, Any], runtime_opts: dict[str, Any]) -> str:
    prompt_mode = str(runtime_opts.get("prompt_mode") or "auto").strip().lower()
    prompt_text = str(runtime_opts.get("prompt_text") or "").strip()
    if prompt_mode == "off":
        return ""
    if prompt_mode == "custom":
        return prompt_text[:400]
    subject_code = _detect_subject_code(raw)
    subject_label = _SUBJECT_LABELS.get(subject_code, "")
    grade_label = _extract_grade_label(raw)
    terms = _extract_prompt_terms(raw, subject_code)
    if subject_label or grade_label:
        grade_part = grade_label if grade_label else ""
        subject_part = subject_label if subject_label else "课堂"
        context_desc = (grade_part + subject_part).strip() if grade_part else subject_part
        role_prompt = f"这是{context_desc}课程的课堂录音转写。请准确识别{subject_part}术语，忽略无意义重复噪音。"
    else:
        role_prompt = "这是课堂录音转写。请准确识别术语，忽略无意义重复噪音。"
    extra_terms: list[str] = []
    for piece in terms:
        t = str(piece or "").strip()
        if t and not _is_weak_prompt_piece(t) and t not in extra_terms:
            extra_terms.append(t)
    if prompt_text:
        t = prompt_text[:120].strip()
        if t and not _is_weak_prompt_piece(t) and t not in extra_terms:
            extra_terms.append(t)
    result = role_prompt
    if extra_terms:
        result += "，".join(extra_terms[:8])
    return result[:400]


def _segment_words(raw_words: object) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for w in list(raw_words or []):
        text = _to_simplified(str(getattr(w, "word", "") or "").strip())
        if not text:
            continue
        try:
            st = float(getattr(w, "start", 0.0) or 0.0)
        except Exception:
            st = 0.0
        try:
            et = float(getattr(w, "end", st) or st)
        except Exception:
            et = st
        if et <= st:
            et = st + 0.05
        words.append({"start": st, "end": et, "word": text})
    return words


def _slice_wav_file(wav_path: str, start_sec: float, end_sec: float) -> tuple[str, float]:
    start_sec = max(0.0, float(start_sec or 0.0))
    end_sec = max(start_sec, float(end_sec or 0.0))
    with wave.open(wav_path, "rb") as src:
        channels = src.getnchannels()
        sample_width = src.getsampwidth()
        frame_rate = src.getframerate()
        frame_count = src.getnframes()
        start_frame = min(frame_count, max(0, int(start_sec * frame_rate)))
        end_frame = min(frame_count, max(start_frame, int(end_sec * frame_rate)))
        src.setpos(start_frame)
        frames = src.readframes(end_frame - start_frame)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp_path = tmp.name
    tmp.close()
    with wave.open(tmp_path, "wb") as dst:
        dst.setnchannels(channels)
        dst.setsampwidth(sample_width)
        dst.setframerate(frame_rate)
        dst.writeframes(frames)
    return tmp_path, max(0.0, (end_frame - start_frame) / float(frame_rate or 16000))


def _detect_audio_activity_regions(
    wav_path: str,
    *,
    window_ms: int = 50,
    rms_threshold: int = 400,  # 降低阈值，对低音量语音更敏感
    consecutive_windows: int = 3,  # 减少连续窗口要求，更快响应
    max_gap_sec: float = 1.5,  # 增加允许的静音间隔，避免过度分割
    pad_sec: float = 0.5,  # 增加padding，确保语音边界完整
    min_region_sec: float = 0.5,  # 降低最小区间要求
    min_active_ratio: float = 0.08,  # 降低活跃比例要求
    tail_guard_sec: float = 180.0,
    tail_min_active_ratio: float = 0.15,  # 降低尾部活跃比例要求
) -> list[tuple[float, float]]:
    try:
        with wave.open(wav_path, "rb") as wf:
            frame_rate = int(wf.getframerate() or 16000)
            sample_width = int(wf.getsampwidth() or 2)
            total_frames = int(wf.getnframes() or 0)
            if frame_rate <= 0 or total_frames <= 0:
                return []
            chunk_frames = max(1, int(frame_rate * window_ms / 1000))
            total_dur = total_frames / float(frame_rate)
            max_gap_windows = max(1, int(round(max_gap_sec * frame_rate / chunk_frames)))
            regions: list[tuple[float, float]] = []
            active_streak = 0
            silent_streak = 0
            idx = 0
            region_start_idx: int | None = None
            last_active_idx: int | None = None
            region_active_windows = 0

            def _finalize(end_idx: int | None) -> None:
                nonlocal region_start_idx, last_active_idx, region_active_windows, silent_streak
                if region_start_idx is None or last_active_idx is None:
                    region_start_idx = None
                    last_active_idx = None
                    region_active_windows = 0
                    silent_streak = 0
                    return
                raw_start = max(0.0, region_start_idx * chunk_frames / float(frame_rate) - pad_sec)
                raw_end = min(total_dur, ((end_idx if end_idx is not None else last_active_idx) + 1) * chunk_frames / float(frame_rate) + pad_sec)
                dur = max(0.0, raw_end - raw_start)
                region_windows = max(1, (last_active_idx - region_start_idx) + 1)
                active_ratio = region_active_windows / float(region_windows)
                near_tail = raw_end >= max(0.0, total_dur - tail_guard_sec)
                required_ratio = tail_min_active_ratio if near_tail else min_active_ratio
                if dur >= min_region_sec and active_ratio >= required_ratio:
                    if regions and raw_start - regions[-1][1] <= max_gap_sec:
                        prev_start, prev_end = regions[-1]
                        regions[-1] = (prev_start, max(prev_end, raw_end))
                    else:
                        regions.append((raw_start, raw_end))
                region_start_idx = None
                last_active_idx = None
                region_active_windows = 0
                silent_streak = 0

            while True:
                frames = wf.readframes(chunk_frames)
                if not frames:
                    break
                rms = audioop.rms(frames, sample_width)
                is_active = rms >= rms_threshold
                if is_active:
                    active_streak += 1
                    silent_streak = 0
                    if region_start_idx is None and active_streak >= consecutive_windows:
                        region_start_idx = max(0, idx - consecutive_windows + 1)
                        region_active_windows = consecutive_windows
                    elif region_start_idx is not None:
                        region_active_windows += 1
                    last_active_idx = idx
                else:
                    active_streak = 0
                    if region_start_idx is not None:
                        silent_streak += 1
                        if silent_streak >= max_gap_windows:
                            _finalize(last_active_idx)
                idx += 1
            _finalize(last_active_idx)
            return [(max(0.0, st), min(total_dur, max(st, et))) for st, et in regions if et > st + 0.05]
    except Exception:
        return []


def _silero_vad_regions(
    wav_path: str,
    *,
    threshold: float = 0.5,
    min_speech_ms: int = 250,
    min_silence_ms: int = 800,
    pad_ms: int = 200,
) -> list[tuple[float, float]]:
    """使用 Silero VAD 模型检测有声区间，返回 [(start_sec, end_sec), ...]。
    若 silero_vad 未安装或推理失败，返回空列表（调用方应回退到内置 VAD）。"""
    try:
        import torch
        from silero_vad import load_silero_vad, get_speech_timestamps, read_audio
    except ImportError:
        log.warning("silero_vad 未安装，Silero VAD 回退到内置 VAD (pip install silero-vad)")
        return []
    try:
        model = load_silero_vad()
        wav = read_audio(wav_path, sampling_rate=16000)
        speech_ts = get_speech_timestamps(
            wav,
            model,
            threshold=threshold,
            min_speech_duration_ms=min_speech_ms,
            min_silence_duration_ms=min_silence_ms,
            speech_pad_ms=pad_ms,
            return_seconds=True,
        )
        regions = [(float(t["start"]), float(t["end"])) for t in (speech_ts or [])]
        log.info("Silero VAD: 检测到 %d 个有声区间，总活跃=%.1fs", len(regions), sum(e - s for s, e in regions))
        return regions
    except Exception as exc:
        log.warning("Silero VAD 推理失败，回退到内置 VAD: %s", exc)
        return []


def _estimate_audio_activity_span(
    wav_path: str,
    *,
    window_ms: int = 50,
    rms_threshold: int = 400,
    consecutive_windows: int = 3,
) -> tuple[float, float]:
    regions = _detect_audio_activity_regions(
        wav_path,
        window_ms=window_ms,
        rms_threshold=max(int(rms_threshold), 400),  # 与_detect_audio_activity_regions保持一致
        consecutive_windows=max(int(consecutive_windows), 3),
    )
    if not regions:
        return 0.0, 0.0
    return float(regions[0][0]), float(regions[-1][1])


def _get_hardware_profile() -> str:
    """检测当前硬件资源，返回 'high' / 'mid' / 'low' 档位。
    - high: GPU VRAM >= 8 GB
    - mid:  GPU VRAM < 8 GB 或 CPU >= 8 核
    - low:  CPU < 8 核（或 GPU 不可用且核心数少）
    结果无副作用，每次调用重新检测（模型加载时调用一次即可）。
    """
    try:
        import torch
        if torch.cuda.is_available():
            vram_bytes = torch.cuda.get_device_properties(0).total_memory
            vram_gb = vram_bytes / (1024 ** 3)
            if vram_gb >= 8.0:
                return "high"
            return "mid"
    except Exception:
        pass
    try:
        import os as _os
        cpu_count = int(_os.cpu_count() or 0)
        if cpu_count >= 8:
            return "mid"
    except Exception:
        pass
    return "low"


def _get_whisper_model(model_size_override: str | None = None):
    global _whisper_device
    model_size = _load_speech_model_size(model_size_override)
    model_name = _resolve_model_name(model_size)
    model_source, model_opts = _resolve_whisper_model_source(model_name)
    with _whisper_model_lock:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            log.error("faster_whisper 未安装，请执行: pip install faster-whisper")
            return None
        try:
            key = (model_source, "cuda")
            if key in _whisper_models:
                _whisper_device = "cuda"
                return _whisper_models[key]
            model = WhisperModel(
                model_source,
                device="cuda",
                compute_type="float16",
                **model_opts,
            )
            model = _ensure_whisper_feature_compat(model, model_name, model_source)
            _whisper_models[key] = model
            _whisper_device = "cuda"
            log.info("Whisper %s 已加载 (GPU/float16) source=%s", model_name, model_source)
            return model
        except Exception as e:
            log.warning("GPU 加载失败，回退到 CPU: %s", e)
            key = (model_source, "cpu")
            if key in _whisper_models:
                _whisper_device = "cpu"
                return _whisper_models[key]
            model = WhisperModel(
                model_source,
                device="cpu",
                compute_type="int8",
                **model_opts,
            )
            model = _ensure_whisper_feature_compat(model, model_name, model_source)
            _whisper_models[key] = model
            _whisper_device = "cpu"
            log.info("Whisper %s 已加载 (CPU/int8) source=%s", model_name, model_source)
            return model


def _transcribe_wav(
    wav_path: str,
    total_dur: float = 0.0,
    expected_end_sec: float | None = None,
    speech_regions: list[tuple[float, float]] | None = None,
    raw: dict[str, Any] | None = None,
    on_seg_progress: Callable | None = None,
    on_stage_progress: Callable | None = None,
    cancel_check: Callable[[], str | None] | None = None,
    on_chunk_complete: Callable[[int, int, list[dict], float, float], None] | None = None,
    on_partial_chunk_progress: Callable[[int, int, list[dict], float, float], None] | None = None,
    existing_segments: list[dict] | None = None,
    resume_next_chunk_index: int = 0,
    model_size_override: str | None = None,
    heartbeat_sec: float = 20.0,
    first_segment_warn_sec: float = 180.0,
    first_segment_stack_sec: float = 600.0,
    segment_idle_warn_sec: float = 180.0,
    segment_idle_stack_sec: float = 600.0,
) -> list[dict]:
    owner_thread_id = threading.get_ident()
    transcribe_start = time.monotonic()
    heartbeat_stop = threading.Event()
    state: dict[str, Any] = {
        "phase": "init",
        "phase_started_at": transcribe_start,
        "first_segment_seen": False,
        "last_segment_wall": transcribe_start,
        "last_segment_audio": 0.0,
    }

    def _emit_stage(message: str, pct_hint: int) -> None:
        if on_stage_progress:
            try:
                on_stage_progress(message, pct_hint, time.monotonic() - transcribe_start)
            except Exception:
                pass

    def _set_phase(name: str, message: str, pct_hint: int) -> None:
        state["phase"] = name
        state["phase_started_at"] = time.monotonic()
        _emit_stage(message, pct_hint)

    def _dump_owner_stack(reason: str) -> None:
        frame = sys._current_frames().get(owner_thread_id)
        if frame is None:
            return
        stack_text = "".join(traceback.format_stack(frame)[-20:])
        log.warning("ASR阶段保护日志：%s\n%s", reason, stack_text)

    _warned: set[tuple[str, str]] = set()

    def _heartbeat_loop() -> None:
        while not heartbeat_stop.wait(heartbeat_sec):
            now = time.monotonic()
            phase = str(state.get("phase") or "")
            phase_elapsed = now - float(state.get("phase_started_at") or now)
            total_elapsed = now - transcribe_start
            if phase in {"model_load", "transcribe_init", "waiting_first_segment"}:
                _emit_stage(f"{phase_messages.get(phase, '语音识别准备中')}（阶段耗时{_fmt_duration(phase_elapsed)}）", phase_progress.get(phase, 18))
                if phase_elapsed >= first_segment_warn_sec and (phase, "warn") not in _warned:
                    _warned.add((phase, "warn"))
                    log.warning("ASR阶段耗时较长: phase=%s 阶段耗时=%.0fs 总耗时=%.0fs wav=%s", phase, phase_elapsed, total_elapsed, os.path.basename(wav_path))
                if phase_elapsed >= first_segment_stack_sec and (phase, "stack") not in _warned:
                    _warned.add((phase, "stack"))
                    _dump_owner_stack(f"phase={phase} 阶段耗时{phase_elapsed:.0f}s")
            elif phase == "decoding":
                idle_elapsed = now - float(state.get("last_segment_wall") or now)
                audio_done = float(state.get("last_segment_audio") or 0.0)
                _emit_stage(
                    f"语音识别中（已转写{_fmt_duration(audio_done)}/{_fmt_duration(total_dur)}，阶段空闲{_fmt_duration(idle_elapsed)}）",
                    max(18, min(89, int(10 + (audio_done / max(total_dur, 1.0)) * 80))) if total_dur > 0 else 18,
                )
                if idle_elapsed >= segment_idle_warn_sec and (phase, "warn") not in _warned:
                    _warned.add((phase, "warn"))
                    log.warning("ASR分段输出停滞: 空闲=%.0fs 已转写=%.1fs/%0.1fs wav=%s", idle_elapsed, audio_done, total_dur, os.path.basename(wav_path))
                if idle_elapsed >= segment_idle_stack_sec and (phase, "stack") not in _warned:
                    _warned.add((phase, "stack"))
                    _dump_owner_stack(f"phase=decoding 分段空闲{idle_elapsed:.0f}s 已转写{audio_done:.1f}s")

    def _fmt_log_dur(sec: float) -> str:
        sec = int(max(0, sec))
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    content_end_sec = min(total_dur, max(0.0, float(expected_end_sec or 0.0))) if expected_end_sec is not None else total_dur
    coverage_slack_sec = 3.0 if total_dur >= 300.0 else 1.5
    runtime_opts = _load_speech_runtime_options()
    initial_prompt = _build_initial_prompt(raw or {}, runtime_opts)
    subject_code = _detect_subject_code(raw or {})
    hallucination_rules = _load_hallucination_rules()

    def _covered_dur(seg_list: list[dict]) -> float:
        if not seg_list:
            return 0.0
        return max(float(s.get("end", 0.0)) for s in seg_list)

    def _merge_regions(regions: list[tuple[float, float]], max_gap_sec: float = 1.2) -> list[tuple[float, float]]:
        merged: list[tuple[float, float]] = []
        for st, et in sorted(regions, key=lambda item: (float(item[0]), float(item[1]))):
            cur_st = max(0.0, float(st))
            cur_et = max(cur_st, float(et))
            if not merged:
                merged.append((cur_st, cur_et))
                continue
            prev_st, prev_et = merged[-1]
            if cur_st <= prev_et + max_gap_sec:
                merged[-1] = (prev_st, max(prev_et, cur_et))
            else:
                merged.append((cur_st, cur_et))
        return merged

    # 已知幻觉短语黑名单（Whisper在低信噪比/静音时的固定输出）
    _HALLUCINATION_PHRASES = [
        "请不吝点赞", "订阅", "转发", "打赏支持", "明镜与点点",
        "感谢观看", "谢谢观看", "欢迎订阅", "关注我们", "点击订阅",
        "字幕由", "本视频由", "翻译", "校对", "字幕组", "字幕志愿者",
        "字幕提供", "字幕制作", "字幕校对", "字幕编辑", "字幕支持",
        "杨茜茜", "Amara.org", "Amara",
        "未经作者授权", "未经许可", "请勿转载", "点赞关注收藏", "一键三连",
        "关注点赞", "课代表", "小铃铛", "下期见", "下次再见", "片尾",
    ]

    _TAIL_HALLUCINATION_PHRASES = [
        "谢谢大家", "谢谢老师", "感谢大家", "拜拜", "再见", "下课", "辛苦了",
        "课程结束", "字幕志愿者", "字幕提供", "字幕制作",
    ]

    def _is_hallucination(txt: str) -> bool:
        """判断是否为已知幻觉短语（黑名单匹配）"""
        return any(phrase in txt for phrase in _HALLUCINATION_PHRASES)

    def _match_rule_patterns(patterns: object, txt: str) -> bool:
        for pattern in list(patterns or []):
            if str(pattern or "") and str(pattern) in txt:
                return True
        return False

    def _match_rule_regex(patterns: object, txt: str) -> bool:
        for pattern in list(patterns or []):
            try:
                if re.search(str(pattern or ""), txt):
                    return True
            except Exception:
                continue
        return False

    def _is_structured_hallucination(txt: str, compact_txt: str, avg_logprob: float, compression_ratio: float, global_st: float) -> tuple[bool, str]:
        if not runtime_opts.get("hallucination_filter", True):
            return False, ""
        exact_match = {str(v or "").strip() for v in list(hallucination_rules.get("exact_match") or []) if str(v or "").strip()}
        if compact_txt in exact_match or txt in exact_match:
            return True, "exact_match"
        contains_match = hallucination_rules.get("contains_match") or {}
        for rule_name, rule_cfg in dict(contains_match).items():
            if _match_rule_patterns((rule_cfg or {}).get("patterns") or [], txt):
                return True, str(rule_name)
        subject_specific = (hallucination_rules.get("subject_specific") or {}).get(subject_code) or {}
        if _match_rule_patterns(subject_specific.get("patterns") or [], txt):
            return True, f"subject_specific:{subject_code or 'generic'}"
        tail_patterns = ((hallucination_rules.get("tail_segment_patterns") or {}).get("patterns") or [])
        if global_st >= max(0.0, content_end_sec - 300.0) and _match_rule_patterns(tail_patterns, compact_txt):
            return True, "tail_segment_patterns"
        regex_patterns = ((hallucination_rules.get("regex_patterns") or {}).get("patterns") or [])
        if _match_rule_regex(regex_patterns, txt) or _match_rule_regex(regex_patterns, compact_txt):
            return True, "regex_patterns"
        if len(compact_txt) <= 6 and compression_ratio > 2.4 and avg_logprob < -2.1:
            return True, "low_confidence_repetition"
        return False, ""

    def _is_suspicious_tail_segment(txt: str, global_st: float, global_et: float) -> bool:
        compact = re.sub(r"\s+", "", str(txt or ""))
        if not compact:
            return True
        if content_end_sec <= 0:
            return False
        if global_st < max(0.0, content_end_sec - 300.0):
            return False
        if any(phrase in compact for phrase in _TAIL_HALLUCINATION_PHRASES):
            return True
        if len(compact) <= 1:
            return True
        if len(compact) <= 4 and compact in {"好", "嗯", "哦", "啊", "你", "我", "诶", "欸", "哎", "呀"}:
            return True
        if len(compact) >= 6 and len(set(compact)) <= 3:
            return True
        return False

    def _collect_segments(seg_iter, *, offset_sec: float = 0.0, keep_from: float = 0.0, keep_to: float | None = None, on_segment_collected: Callable[[list[dict], float], None] | None = None) -> list[dict]:
        collected: list[dict] = []
        sc = 0
        repeat_count: dict[str, int] = {}
        for s in seg_iter:
            if cancel_check:
                mode = str(cancel_check() or "")
                if mode in {"pause", "stop"}:
                    raise RuntimeError(f"cancelled:{mode}")
            sc += 1
            if not state["first_segment_seen"]:
                state["first_segment_seen"] = True
                _warned.discard(("waiting_first_segment", "warn"))
                _warned.discard(("waiting_first_segment", "stack"))
                log.info("ASR: 已产出首个识别片段，开始进入稳定解码阶段")
                _emit_stage("已产出首个识别片段，进入稳定解码", 20)
            state["phase"] = "decoding"
            state["phase_started_at"] = time.monotonic()
            txt = _to_simplified(str(getattr(s, "text", "")).strip())
            et = float(getattr(s, "end", 0.0))
            st = float(getattr(s, "start", 0.0))
            avg_logprob = float(getattr(s, "avg_logprob", 0.0) or 0.0)
            compression_ratio = float(getattr(s, "compression_ratio", 0.0) or 0.0)
            words = _segment_words(getattr(s, "words", None)) if runtime_opts.get("word_timestamps", True) else []
            if words:
                st = min(st, float(words[0].get("start", st) or st)) if st > 0 else float(words[0].get("start", 0.0) or 0.0)
                et = max(et, float(words[-1].get("end", et) or et))
            no_speech_prob = float(getattr(s, "no_speech_prob", 0.0) or 0.0)
            compact_txt = re.sub(r"\s+", "", txt)
            if no_speech_prob > 0.97 and len(compact_txt) <= 1:
                log.info("ASR: 跳过高no_speech_prob片段 no_speech_prob=%.3f", no_speech_prob)
                continue
            if et <= 0:
                et = st + 2.0
            global_st = st + offset_sec
            global_et = et + offset_sec
            state["last_segment_wall"] = time.monotonic()
            state["last_segment_audio"] = max(float(state.get("last_segment_audio") or 0.0), global_et)
            if not txt:
                continue
            # 过滤已知幻觉短语黑名单
            if _is_hallucination(txt):
                log.info("ASR: 跳过幻觉短语")
                continue
            matched, reason = _is_structured_hallucination(txt, compact_txt, avg_logprob, compression_ratio, global_st)
            if matched:
                log.info("ASR: 跳过结构化幻觉片段 rule=%s text=%s", reason, txt[:40])
                continue
            # 过滤尾段可疑片段
            if _is_suspicious_tail_segment(txt, global_st, global_et):
                log.info("ASR: 跳过尾段可疑片段 text=%s", txt[:40])
                continue
            repeat_count[txt] = repeat_count.get(txt, 0) + 1
            if repeat_count[txt] > 3 and len(compact_txt) <= 6 and avg_logprob < -0.8:
                log.info("ASR: 跳过重复幻觉内容(出现%d次)", repeat_count[txt])
                continue
            if global_et <= keep_from + 0.15:
                continue
            if keep_to is not None and global_st >= keep_to - 0.15:
                continue
            if global_st < keep_from:
                global_st = keep_from
            if keep_to is not None and global_et > keep_to:
                global_et = keep_to
            if global_et <= global_st + 0.05:
                continue
            global_words = []
            for word in words:
                word_st = float(word.get("start", 0.0) or 0.0) + offset_sec
                word_et = float(word.get("end", word_st) or word_st) + offset_sec
                if word_et <= keep_from + 0.02:
                    continue
                if keep_to is not None and word_st >= keep_to - 0.02:
                    continue
                if word_st < keep_from:
                    word_st = keep_from
                if keep_to is not None and word_et > keep_to:
                    word_et = keep_to
                if word_et <= word_st + 0.01:
                    continue
                global_words.append({"start": word_st, "end": word_et, "word": str(word.get("word") or "")})
            txt = _apply_corrections(txt)
            collected.append({
                "start": global_st,
                "end": global_et,
                "text": txt,
                "words": global_words,
                "avg_logprob": avg_logprob,
                "no_speech_prob": no_speech_prob,
                "compression_ratio": compression_ratio,
                "source": "whisper",
            })
            if sc <= 3 or sc % 20 == 0:
                log.info("ASR segment: idx=%s end=%.2fs text_len=%s total_segments=%s", sc, global_et, len(txt), len(collected))
            if on_seg_progress and total_dur > 0:
                on_seg_progress(global_et, total_dur)
            if on_segment_collected:
                on_segment_collected(list(collected), global_et)
            if cancel_check:
                mode = str(cancel_check() or "")
                if mode in {"pause", "stop"}:
                    raise RuntimeError(f"cancelled:{mode}")
        return collected

    def _transcribe_once(source_wav: str, *, offset_sec: float = 0.0, keep_from: float = 0.0, keep_to: float | None = None, decode_opts: dict[str, Any] | None = None, stage_message: str = "", on_segment_collected: Callable[[list[dict], float], None] | None = None) -> list[dict]:
        opts = dict(decode_opts or {})
        if stage_message:
            _set_phase("transcribe_init", stage_message, 15)
        segments_iter, _ = model.transcribe(source_wav, **opts)
        _set_phase("waiting_first_segment", "等待首个识别片段（VAD/首段推理）", 18)
        log.info("ASR: 推理引擎已就绪，开始逐段识别")
        return _collect_segments(segments_iter, offset_sec=offset_sec, keep_from=keep_from, keep_to=keep_to, on_segment_collected=on_segment_collected)

    phase_messages = {
        "model_load": "加载Whisper模型中",
        "transcribe_init": "初始化转写器中（提交large-v3任务）",
        "waiting_first_segment": "等待首个识别片段（VAD/首段推理）",
    }
    phase_progress = {
        "model_load": 12,
        "transcribe_init": 15,
        "waiting_first_segment": 18,
    }

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    try:
        _set_phase("model_load", "加载Whisper模型中", 12)
        model = _get_whisper_model(model_size_override)
        if model is None:
            raise RuntimeError("Whisper 模型加载失败")
        _emit_stage(f"Whisper模型已就绪（device={_whisper_device}）", 14)
        common_temperature = float(runtime_opts.get("speech_temperature", 0.0) or 0.0)
        retry_temperature = float(runtime_opts.get("speech_retry_temperature", 0.4) or 0.4)
        hw_profile = _get_hardware_profile()
        if hw_profile == "high":
            _beam_size, _best_of = 5, 5
        elif hw_profile == "low":
            _beam_size, _best_of = 1, 1
        else:
            _beam_size, _best_of = 3, 3
        log.info("硬件档位=%s beam_size=%s best_of=%s device=%s", hw_profile, _beam_size, _best_of, _whisper_device)
        vad_mode = str(runtime_opts.get("vad_mode") or "builtin").strip().lower()
        if vad_mode == "silero":
            _silero_regions = _silero_vad_regions(wav_path)
            if _silero_regions:
                if not speech_regions:
                    speech_regions = _silero_regions
                log.info("VAD模式=silero，将使用 Silero 检测区间驱动分块转写")
            else:
                vad_mode = "builtin"
                log.info("Silero VAD 无结果，自动回退到内置 VAD")
        _builtin_vad = vad_mode != "off"
        common = dict(
            language="zh",
            beam_size=_beam_size,
            best_of=_best_of,
            temperature=common_temperature,
            vad_filter=_builtin_vad,
            vad_parameters=dict(
                threshold=0.42,
                min_speech_duration_ms=180,
                max_speech_duration_s=45,
                min_silence_duration_ms=900,
                speech_pad_ms=420,
            ) if _builtin_vad else {},
            condition_on_previous_text=False,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.98,
            word_timestamps=bool(runtime_opts.get("word_timestamps", True)),
        )
        if not _builtin_vad:
            common.pop("vad_parameters", None)
        retry_common = dict(
            language="zh",
            beam_size=max(1, _beam_size - 1),
            best_of=max(1, _best_of - 1),
            temperature=retry_temperature,
            vad_filter=False,
            condition_on_previous_text=False,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.98,
            word_timestamps=bool(runtime_opts.get("word_timestamps", True)),
        )
        if initial_prompt:
            common["initial_prompt"] = initial_prompt
            retry_common["initial_prompt"] = initial_prompt
        log.info("开始 ASR 识别: %s (device=%s), 音频时长=%.0f秒", os.path.basename(wav_path), _whisper_device, total_dur)
        chunk_sec = 1800.0
        overlap_sec = 2.0
        active_regions = [(max(0.0, float(st)), min(total_dur, float(et))) for st, et in (speech_regions or []) if float(et) > float(st) + 0.2]
        active_audio_sec = sum(max(0.0, et - st) for st, et in active_regions)
        active_ratio = (active_audio_sec / max(total_dur, 1.0)) if total_dur > 0 else 1.0
        strict_coverage_mode = active_ratio >= 0.7
        if active_regions:
            log.info("ASR: 仅转写有效有声区间 region_count=%s active=%.0fs total=%.0fs ratio=%.1f%%", len(active_regions), active_audio_sec, total_dur, active_ratio * 100.0)
        if total_dur <= chunk_sec + 30.0 and not active_regions:
            log.info("ASR: 使用整段转写模式")
            segs = _transcribe_once(wav_path, decode_opts=common, stage_message="初始化转写器中（整段转写）")
            covered_end = _covered_dur(segs)
            if strict_coverage_mode and content_end_sec > 10.0 and covered_end < content_end_sec - coverage_slack_sec:
                log.warning("ASR 覆盖不足(covered=%s target=%s)，整段重试一次（关闭VAD）", _fmt_log_dur(covered_end), _fmt_log_dur(content_end_sec))
                state["first_segment_seen"] = False
                state["last_segment_wall"] = time.monotonic()
                state["last_segment_audio"] = 0.0
                _warned.clear()
                retry_segs = _transcribe_once(wav_path, decode_opts=retry_common, stage_message="重试转写（整段，关闭VAD）")
                if _covered_dur(retry_segs) > _covered_dur(segs):
                    segs = retry_segs
            final_end = _covered_dur(segs)
            if content_end_sec > 10.0 and final_end < content_end_sec - coverage_slack_sec:
                log.warning("ASR 覆盖不足（可能为噪声/幻觉已过滤）：已到%s，有声内容估算到%s", _fmt_log_dur(final_end), _fmt_log_dur(content_end_sec))
            return segs

        all_segments: list[dict] = list(existing_segments or [])
        if active_regions:
            work_span = _merge_regions(active_regions)
        else:
            work_span = [(0.0, total_dur)]
        chunk_plan: list[tuple[float, float]] = []
        for region_start, region_end in work_span:
            cursor = float(region_start)
            region_end = float(region_end)
            while cursor < region_end - 0.05:
                logical_end = min(region_end, cursor + chunk_sec)
                chunk_plan.append((cursor, logical_end))
                cursor = logical_end
        chunk_count = len(chunk_plan)
        if resume_next_chunk_index > 0:
            log.info("ASR: 从断点继续，跳过前 %s/%s 个分块", resume_next_chunk_index, chunk_count)
        log.info("ASR: 使用分块转写模式 chunk_sec=%.0f overlap=%.1f count=%s", chunk_sec, overlap_sec, chunk_count)
        for idx, (logical_start, logical_end) in enumerate(chunk_plan):
            if idx < int(resume_next_chunk_index):
                continue
            if cancel_check:
                mode = str(cancel_check() or "")
                if mode in {"pause", "stop"}:
                    raise RuntimeError(f"cancelled:{mode}")
            slice_start = max(0.0, logical_start - overlap_sec)
            slice_end = min(total_dur, logical_end + overlap_sec)
            state["first_segment_seen"] = False
            state["last_segment_wall"] = time.monotonic()
            state["last_segment_audio"] = logical_start
            _warned.clear()
            chunk_wav = ""
            try:
                chunk_wav, chunk_dur = _slice_wav_file(wav_path, slice_start, slice_end)
                log.info(
                    "ASR chunk %s/%s: logical=%s-%s slice=%s-%s dur=%.1fs",
                    idx + 1,
                    chunk_count,
                    _fmt_log_dur(logical_start),
                    _fmt_log_dur(logical_end),
                    _fmt_log_dur(slice_start),
                    _fmt_log_dur(slice_end),
                    chunk_dur,
                )
                chunk_segments = _transcribe_once(
                    chunk_wav,
                    offset_sec=slice_start,
                    keep_from=logical_start,
                    keep_to=logical_end,
                    decode_opts=common,
                    stage_message=f"初始化转写器中（分块 {idx + 1}/{chunk_count}）",
                    on_segment_collected=(lambda partial_segments, partial_audio_end, _idx=idx + 1: on_partial_chunk_progress(_idx, chunk_count, _dedupe_segments(all_segments + partial_segments), partial_audio_end, total_dur)) if on_partial_chunk_progress else None,
                )
                chunk_covered = 0.0
                if chunk_segments:
                    chunk_covered = max(0.0, max(float(s.get("end", logical_start)) for s in chunk_segments) - logical_start)
                logical_dur = max(1.0, logical_end - logical_start)
                if strict_coverage_mode and chunk_covered / logical_dur < 0.95:
                    log.warning(
                        "ASR chunk %s/%s 覆盖率不足(%.1f%%)，重试一次（关闭VAD）",
                        idx + 1,
                        chunk_count,
                        (chunk_covered / logical_dur) * 100,
                    )
                    state["first_segment_seen"] = False
                    state["last_segment_wall"] = time.monotonic()
                    state["last_segment_audio"] = logical_start
                    _warned.clear()
                    retry_segments = _transcribe_once(
                        chunk_wav,
                        offset_sec=slice_start,
                        keep_from=logical_start,
                        keep_to=logical_end,
                        decode_opts=retry_common,
                        stage_message=f"重试转写（分块 {idx + 1}/{chunk_count}，关闭VAD）",
                        on_segment_collected=(lambda partial_segments, partial_audio_end, _idx=idx + 1: on_partial_chunk_progress(_idx, chunk_count, _dedupe_segments(all_segments + partial_segments), partial_audio_end, total_dur)) if on_partial_chunk_progress else None,
                    )
                    retry_covered = 0.0
                    if retry_segments:
                        retry_covered = max(0.0, max(float(s.get("end", logical_start)) for s in retry_segments) - logical_start)
                    if retry_covered > chunk_covered:
                        chunk_segments = retry_segments
                all_segments.extend(chunk_segments)
                if on_seg_progress:
                    on_seg_progress(min(total_dur, logical_end), total_dur)
                if on_chunk_complete:
                    on_chunk_complete(idx + 1, chunk_count, _dedupe_segments(all_segments), logical_end, total_dur)
                if cancel_check:
                    mode = str(cancel_check() or "")
                    if mode in {"pause", "stop"}:
                        raise RuntimeError(f"cancelled:{mode}")
            finally:
                if chunk_wav:
                    try:
                        Path(chunk_wav).unlink(missing_ok=True)
                    except Exception:
                        pass

        deduped = _dedupe_segments(all_segments)
        final_end = _covered_dur(deduped)
        if content_end_sec > 10.0 and final_end < content_end_sec - coverage_slack_sec:
            log.warning("ASR 覆盖不足（可能为噪声/幻觉已过滤）：已到%s，有声内容估算到%s", _fmt_log_dur(final_end), _fmt_log_dur(content_end_sec))
        final_coverage = (final_end / total_dur * 100) if total_dur > 0 else 100.0
        log.info("ASR 最终结果: %d 片段，覆盖率=%.1f%%（分块模式）", len(deduped), final_coverage)
        return deduped
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)


_T2S_MAP = {
    "臺": "台", "萬": "万", "與": "与", "專": "专", "業": "业", "報": "报",
    "學": "学", "習": "习", "數": "数", "據": "据", "錄": "录", "視": "视",
    "頻": "频", "課": "课", "師": "师", "員": "员", "網": "网", "頁": "页",
    "顯": "显", "電": "电", "腦": "脑", "終": "终", "開": "开", "關": "关",
    "畫": "画", "規": "规", "範": "范", "較": "较", "並": "并", "將": "将",
    "為": "为", "從": "从", "國": "国", "內": "内", "裏": "里", "裡": "里",
    "區": "区", "門": "门", "書": "书", "語": "语", "雲": "云", "標": "标",
    "準": "准", "題": "题", "說": "说", "載": "载", "現": "现", "時": "时",
    "間": "间", "體": "体", "寫": "写", "轉": "转", "選": "选", "動": "动",
    "長": "长", "後": "后", "這": "这", "個": "个", "們": "们", "來": "来",
    "對": "对", "點": "点", "還": "还", "會": "会", "嗎": "吗", "麼": "么",
    "經": "经", "過": "过", "讓": "让", "應": "应", "當": "当", "實": "实",
    "覺": "觉", "樣": "样", "進": "进", "於": "于", "發": "发", "無": "无",
    "聲": "声", "讀": "读", "愛": "爱", "滿": "满", "難": "难", "幾": "几",
    "車": "车", "鐘": "钟", "兩": "两", "級": "级", "聽": "听", "請": "请",
    "記": "记", "號": "号", "變": "变", "總": "总", "貝": "贝", "庫": "库",
    "線": "线", "輸": "输", "優": "优", "講": "讲", "認": "认", "證": "证",
    "價": "价", "觀": "观", "測": "测", "試": "试", "遠": "远", "連": "连",
    "斷": "断", "續": "续", "務": "务", "資": "资", "產": "产", "務": "务",
    "壓": "压", "縮": "缩", "廣": "广", "錄": "录", "備": "备", "註": "注",
    "達": "达", "參": "参", "算": "算", "該": "该", "剛": "刚", "嗎": "吗",
    "陣": "阵", "際": "际", "嗎": "吗", "營": "营", "處": "处", "學": "学",
}


def _to_simplified(s: str) -> str:
    out = s
    try:
        import opencc
        c = opencc.OpenCC("t2s")
        out = c.convert(out)
    except Exception:
        try:
            from zhconv import convert
            out = convert(out, "zh-cn")
        except Exception:
            pass
    for k, v in _T2S_MAP.items():
        out = out.replace(k, v)
    return out


def _srt_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _vtt_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def write_srt(segments: list[dict], path: str, offset: float = 0.0) -> None:
    lines: list[str] = []
    idx = 0
    for seg in segments:
        st = float(seg.get("start", 0.0) or 0.0) + offset
        et = float(seg.get("end", st + 2.0) or (st + 2.0)) + offset
        txt = str(seg.get("text", "") or "").strip()
        if not txt:
            continue
        idx += 1
        lines.append(str(idx))
        lines.append(f"{_srt_time(st)} --> {_srt_time(et)}")
        lines.append(txt)
        lines.append("")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_vtt(segments: list[dict], path: str, offset: float = 0.0) -> None:
    lines: list[str] = ["WEBVTT", ""]
    for seg in segments:
        st = float(seg.get("start", 0.0) or 0.0) + offset
        et = float(seg.get("end", st + 2.0) or (st + 2.0)) + offset
        txt = str(seg.get("text", "") or "").strip()
        if not txt:
            continue
        lines.append(f"{_vtt_time(st)} --> {_vtt_time(et)}")
        lines.append(txt)
        lines.append("")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_transcript_html(segments: list[dict], path: str, offset: float = 0.0) -> None:
    parts = [
        "<html><head><meta charset=\"utf-8\"></head><body>",
        "<div class=\"transcript\">",
    ]
    for seg in segments:
        st = float(seg.get("start", 0.0) or 0.0) + offset
        et = float(seg.get("end", st + 2.0) or (st + 2.0)) + offset
        txt = html.escape(str(seg.get("text", "") or "").strip())
        if not txt:
            continue
        parts.append(
            f"<p><span>{html.escape(_vtt_time(st))} --> {html.escape(_vtt_time(et))}</span><br>{txt}</p>"
        )
    parts.extend(["</div>", "</body></html>"])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def _dedupe_segments(segments: list[dict]) -> list[dict]:
    ordered = [dict(seg or {}) for seg in (segments or [])]
    ordered.sort(key=lambda x: (float(x.get("start", 0.0)), float(x.get("end", 0.0))))
    deduped: list[dict] = []
    for seg in ordered:
        if deduped:
            prev = deduped[-1]
            prev_end = float(prev.get("end", 0.0))
            cur_start = float(seg.get("start", 0.0))
            cur_end = float(seg.get("end", 0.0))
            prev_text = str(prev.get("text", "") or "").strip()
            cur_text = str(seg.get("text", "") or "").strip()
            if cur_start <= prev_end and cur_text == prev_text:
                if cur_end > prev_end:
                    prev["end"] = cur_end
                continue
            if cur_start < prev_end and cur_text != prev_text:
                seg["start"] = prev_end
                if float(seg.get("end", 0.0)) <= float(seg.get("start", 0.0)) + 0.05:
                    continue
        deduped.append(seg)
    return deduped


def _speech_paths(teacher_mp4: Path) -> dict[str, Path]:
    return {
        "wav": teacher_mp4.parent / f"{teacher_mp4.stem}.wav",
        "srt": teacher_mp4.parent / f"{teacher_mp4.stem}.zh.srt",
        "vtt": teacher_mp4.parent / f"{teacher_mp4.stem}.zh.vtt",
        "html": teacher_mp4.parent / f"{teacher_mp4.stem}.speech.html",
        "checkpoint": teacher_mp4.parent / f"{teacher_mp4.stem}.speech.checkpoint.json",
    }


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _load_speech_checkpoint(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_speech_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def has_speech_resume_checkpoint(raw: dict[str, Any]) -> bool:
    teacher_mp4 = _resolve_teacher_video(raw)
    if teacher_mp4 is None:
        return False
    paths = _speech_paths(teacher_mp4)
    return paths["checkpoint"].exists() and paths["wav"].exists()


def cleanup_speech_task_files(raw: dict[str, Any], remove_outputs: bool = True) -> None:
    teacher_mp4 = _resolve_teacher_video(raw)
    if teacher_mp4 is None:
        return
    paths = _speech_paths(teacher_mp4)
    _remove_if_exists(paths["checkpoint"])
    _remove_if_exists(paths["wav"])
    if remove_outputs:
        _remove_if_exists(paths["srt"])
        _remove_if_exists(paths["vtt"])
        _remove_if_exists(paths["html"])
        hls_vtt = teacher_mp4.parent / f"{teacher_mp4.stem}_1080P" / "subtitles.zh.vtt"
        _remove_if_exists(hls_vtt)


def _resolve_teacher_video(raw: dict[str, Any]) -> Path | None:
    lesson_id = str(raw.get("lessonId") or "").strip()
    lesson_date = _resolve_lesson_date(raw.get("lessonDate"), raw.get("lessonStartAt"))

    root = Path(_load_download_path())
    out_dir = _get_lesson_dir(lesson_date, lesson_id, root)
    if not out_dir.exists():
        return None
    # 在课次目录中查找 teacher_*.mp4（CameraTask 的 server_task_id 可能与 CourseTask 不同）
    for f in sorted(out_dir.glob("teacher_*.mp4")):
        if f.stat().st_size > 0:
            return f
    return None


def _wav_duration(wav_path: str) -> float:
    try:
        wf = wave.open(wav_path, "rb")
        dur = wf.getnframes() / float(wf.getframerate() or 16000)
        wf.close()
        return dur
    except Exception:
        return 0.0


async def run_speech_task(
    raw: dict[str, Any],
    on_progress: Callable,
) -> list[dict[str, Any]]:
    task_type = int(raw.get("taskType") or 0)
    log.info("SPEECH 开始: taskType=%s", task_type)

    _task_start = time.monotonic()

    def _est_total(elapsed: float, pct: int) -> float:
        """根据已耗时和当前进度估算总时长"""
        if pct <= 0 or elapsed <= 0:
            return -1.0
        return elapsed / (pct / 100.0)

    def _progress_msg(action: str, pct: int, elapsed: float) -> str:
        parts = [action, f"已耗时{_fmt_duration(elapsed)}"]
        est = _est_total(elapsed, pct)
        if est > 0:
            parts.append(f"预计时长{_fmt_duration(est)}")
        return "，".join(parts)

    # ---------- 定位老师视频 ----------
    on_progress("SPEECH", 0.0, _progress_msg("定位老师视频", 0, 0))
    log.info("SPEECH 0%% 定位老师视频")

    teacher_mp4 = await asyncio.to_thread(_resolve_teacher_video, raw)
    if teacher_mp4 is None or not teacher_mp4.exists():
        raise RuntimeError("找不到老师视频文件，无法执行语音转写")
    speech_paths = _speech_paths(teacher_mp4)

    # ---------- 检测音频流 ----------
    elapsed = time.monotonic() - _task_start
    on_progress("SPEECH", 0.02, _progress_msg("检测音频流", 2, elapsed))
    log.info("SPEECH 2%% 检测音频流")

    has_audio = await asyncio.to_thread(has_audio_stream, str(teacher_mp4))
    if not has_audio:
        log.warning("视频无音频流，跳过语音转写: %s", teacher_mp4)
        on_progress("SPEECH", 0.5, "视频无音频流，生成空字幕")
        srt_path = str(speech_paths["srt"])
        vtt_path = str(speech_paths["vtt"])
        html_path = str(speech_paths["html"])
        await asyncio.to_thread(write_srt, [], srt_path)
        await asyncio.to_thread(write_vtt, [], vtt_path)
        await asyncio.to_thread(write_transcript_html, [], html_path)
        await asyncio.to_thread(_remove_if_exists, speech_paths["checkpoint"])
        on_progress("SPEECH", 1.0, "视频无音频流，已生成空字幕文件")
        return [
            {"path": srt_path, "sizeBytes": 0, "stepCode": "SPEECH", "fileType": "subtitle_srt"},
            {"path": vtt_path, "sizeBytes": 0, "stepCode": "SPEECH", "fileType": "subtitle_vtt"},
            {"path": html_path, "sizeBytes": int(Path(html_path).stat().st_size) if Path(html_path).exists() else 0, "stepCode": "SPEECH", "fileType": "speech_report_html"},
        ]

    # ---------- 提取音频 ----------
    elapsed = time.monotonic() - _task_start
    on_progress("SPEECH", 0.05, _progress_msg("提取音频中", 5, elapsed))
    log.info("SPEECH 5%% 提取音频中")

    wav_dir = teacher_mp4.parent
    wav_path = str(speech_paths["wav"])
    if not speech_paths["wav"].exists():
        await asyncio.to_thread(extract_wav, str(teacher_mp4), wav_path, 16000)
    else:
        log.info("SPEECH 复用已存在的音频中间文件: %s", wav_path)
    if not Path(wav_path).exists():
        raise RuntimeError("音频提取失败")

    wav_dur = await asyncio.to_thread(_wav_duration, wav_path)
    video_dur = await asyncio.to_thread(probe_duration_seconds, str(teacher_mp4))
    audio_stream_start = await asyncio.to_thread(get_audio_start_time, str(teacher_mp4))
    speech_regions = await asyncio.to_thread(_detect_audio_activity_regions, wav_path)
    audio_offset, content_end_sec = await asyncio.to_thread(_estimate_audio_activity_span, wav_path)
    elapsed = time.monotonic() - _task_start
    log.info(
        "SPEECH 10%% 音频提取完成: %s, wav时长=%.1f秒, 视频时长=%.1f秒, audio_start=%.3f秒, audio_offset=%.3f秒, content_end=%.3f秒, 已耗时%.0f秒",
        wav_path,
        wav_dur,
        video_dur,
        audio_stream_start,
        audio_offset,
        content_end_sec,
        elapsed,
    )

    if speech_regions:
        active_audio_sec = sum(max(0.0, float(et) - float(st)) for st, et in speech_regions)
        active_ratio = (active_audio_sec / max(wav_dur, 1.0)) if wav_dur > 0 else 0.0
        log.info(
            "SPEECH 音频活动区间统计: region_count=%s active=%.1f秒 total=%.1f秒 ratio=%.1f%%",
            len(speech_regions),
            active_audio_sec,
            wav_dur,
            active_ratio * 100.0,
        )
        # 如果检测到的活跃语音比例过低（<30%），可能是VAD误判，放弃区间限制改用全段转写
        if active_ratio < 0.30 and wav_dur > 300:
            log.warning(
                "SPEECH VAD检测活跃比例过低(%.1f%%)，可能存在误判，放弃VAD区间限制改用全段转写",
                active_ratio * 100.0,
            )
            speech_regions = []

    # ---------- 无有效声音内容检测 ----------
    if content_end_sec <= 0:
        log.warning("音频无有效声音内容，跳过语音转写: %s", teacher_mp4)
        try:
            Path(wav_path).unlink(missing_ok=True)
        except Exception:
            pass
        await asyncio.to_thread(_remove_if_exists, speech_paths["checkpoint"])
        on_progress("SPEECH", 0.5, "音频无有效声音内容，生成空字幕")
        srt_path = str(speech_paths["srt"])
        vtt_path = str(speech_paths["vtt"])
        html_path = str(speech_paths["html"])
        await asyncio.to_thread(write_srt, [], srt_path)
        await asyncio.to_thread(write_vtt, [], vtt_path)
        await asyncio.to_thread(write_transcript_html, [], html_path)
        on_progress("SPEECH", 1.0, "音频无有效声音内容，已生成空字幕文件")
        return [
            {"path": srt_path, "sizeBytes": 0, "stepCode": "SPEECH", "fileType": "subtitle_srt"},
            {"path": vtt_path, "sizeBytes": 0, "stepCode": "SPEECH", "fileType": "subtitle_vtt"},
            {"path": html_path, "sizeBytes": int(Path(html_path).stat().st_size) if Path(html_path).exists() else 0, "stepCode": "SPEECH", "fileType": "speech_report_html"},
        ]

    # ---------- 语音识别 ----------
    effective_dur = video_dur if video_dur > 0 else wav_dur
    on_progress("SPEECH", 0.1, _progress_msg(f"语音识别中（音频时长{_fmt_duration(effective_dur)}）", 10, elapsed))

    _asr_start = time.monotonic()

    def _on_seg_progress(transcribed_sec: float, total_sec: float) -> None:
        ratio = min(transcribed_sec / max(total_sec, 1.0), 1.0)
        p = 0.1 + ratio * 0.8  # 映射到 0.1~0.9
        pct = int(p * 100)
        elapsed_total = time.monotonic() - _task_start
        msg = _progress_msg(
            f"语音识别中（{_fmt_duration(transcribed_sec)}/{_fmt_duration(total_sec)}）",
            pct, elapsed_total,
        )
        on_progress("SPEECH", p, msg)

    def _on_stage_progress(message: str, pct_hint: int, _elapsed_asr: float):
        pct = max(10, min(89, int(pct_hint)))
        progress = pct / 100.0
        elapsed_total = time.monotonic() - _task_start
        on_progress("SPEECH", progress, _progress_msg(message, pct, elapsed_total))

    def _cancel_check() -> str | None:
        m = str(raw.get("__cancel") or "")
        return m if m in {"pause", "stop"} else None

    checkpoint = await asyncio.to_thread(_load_speech_checkpoint, speech_paths["checkpoint"])
    resume_next_chunk_index = 0
    existing_segments: list[dict] = []
    if checkpoint and speech_paths["wav"].exists():
        try:
            resume_next_chunk_index = max(0, int(checkpoint.get("next_chunk_index") or 0))
        except Exception:
            resume_next_chunk_index = 0
        existing_segments = _dedupe_segments(list(checkpoint.get("segments") or []))
        if resume_next_chunk_index > 0 or existing_segments:
            log.info(
                "SPEECH 命中断点恢复: next_chunk=%s segments=%s wav=%s",
                resume_next_chunk_index,
                len(existing_segments),
                wav_path,
            )

    def _on_chunk_complete(done_chunk_index: int, chunk_count: int, segments_so_far: list[dict], transcribed_sec: float, total_sec: float) -> None:
        payload = {
            "version": 1,
            "wav_path": wav_path,
            "next_chunk_index": int(done_chunk_index),
            "chunk_count": int(chunk_count),
            "transcribed_sec": float(transcribed_sec),
            "total_sec": float(total_sec),
            "segments": segments_so_far,
        }
        _write_speech_checkpoint(speech_paths["checkpoint"], payload)

    def _on_partial_chunk_progress(done_chunk_index: int, chunk_count: int, segments_so_far: list[dict], transcribed_sec: float, total_sec: float) -> None:
        payload = {
            "version": 1,
            "wav_path": wav_path,
            "next_chunk_index": max(0, int(done_chunk_index) - 1),
            "chunk_count": int(chunk_count),
            "transcribed_sec": float(transcribed_sec),
            "total_sec": float(total_sec),
            "segments": segments_so_far,
        }
        _write_speech_checkpoint(speech_paths["checkpoint"], payload)

    def _do_transcribe():
        return _transcribe_wav(
            wav_path,
            total_dur=effective_dur,
            expected_end_sec=content_end_sec if content_end_sec > 0 else effective_dur,
            speech_regions=speech_regions,
            raw=raw,
            on_seg_progress=_on_seg_progress,
            on_stage_progress=_on_stage_progress,
            cancel_check=_cancel_check,
            on_chunk_complete=_on_chunk_complete,
            on_partial_chunk_progress=_on_partial_chunk_progress,
            existing_segments=existing_segments,
            resume_next_chunk_index=resume_next_chunk_index,
        )

    try:
        segs = await asyncio.to_thread(_do_transcribe)
    except RuntimeError as e:
        msg = str(e)
        if msg == "cancelled:pause":
            log.info("SPEECH 已暂停，保留断点与中间文件: %s", speech_paths["checkpoint"])
            raise
        if msg == "cancelled:stop":
            await asyncio.to_thread(cleanup_speech_task_files, raw, True)
            log.info("SPEECH 已终止，已清理中间文件")
            raise
        raise

    try:
        Path(wav_path).unlink(missing_ok=True)
    except Exception:
        pass
    await asyncio.to_thread(_remove_if_exists, speech_paths["checkpoint"])

    if not segs:
        log.warning("ASR 识别无结果（可能音频为静音或无语音内容）")

    subtitle_offset = max(0.0, float(audio_stream_start or 0.0))
    content_offset = 0.0
    if segs and audio_offset > 0:
        first_seg_start = max(0.0, float(segs[0].get("start", 0.0) or 0.0))
        candidate_offset = max(0.0, audio_offset - first_seg_start)
        if candidate_offset > 0 and effective_dur > 0:
            last_seg_end = max(float(s.get("end", 0.0) or 0.0) for s in segs)
            if last_seg_end + subtitle_offset + candidate_offset <= effective_dur:
                content_offset = candidate_offset
            else:
                log.warning("SPEECH content_offset=%.3f秒会导致末时间戳(%.3f+%.3f+%.3f=%.3f秒)超出视频时长(%.3f秒)，放弃内容级偏移",
                            candidate_offset, last_seg_end, subtitle_offset, candidate_offset, last_seg_end + subtitle_offset + candidate_offset, effective_dur)
        else:
            content_offset = candidate_offset
    subtitle_offset += max(0.0, content_offset)
    log.info("SPEECH 字幕时间修正: audio_start=%.3f秒, audio_offset=%.3f秒, content_offset=%.3f秒, subtitle_offset=%.3f秒", audio_stream_start, audio_offset, content_offset, subtitle_offset)

    asr_elapsed = time.monotonic() - _asr_start
    elapsed = time.monotonic() - _task_start
    log.info("SPEECH 90%% ASR完成: %d片段, ASR耗时=%.0f秒, 总耗时=%.0f秒", len(segs), asr_elapsed, elapsed)

    # ---------- 生成字幕文件 ----------
    on_progress("SPEECH", 0.9, _progress_msg("生成字幕文件中", 90, elapsed))
    log.info("SPEECH 90%% 生成字幕文件中")

    srt_path = str(speech_paths["srt"])
    vtt_path = str(speech_paths["vtt"])
    html_path = str(speech_paths["html"])
    await asyncio.to_thread(write_srt, segs, srt_path, offset=subtitle_offset)
    await asyncio.to_thread(write_vtt, segs, vtt_path, offset=subtitle_offset)
    await asyncio.to_thread(write_transcript_html, segs, html_path, offset=subtitle_offset)

    hls_dir = teacher_mp4.parent / f"{teacher_mp4.stem}_1080P"
    if hls_dir.exists() and hls_dir.is_dir():
        hls_vtt = str(hls_dir / "subtitles.zh.vtt")
        try:
            import shutil
            shutil.copy(vtt_path, hls_vtt)
        except Exception:
            pass

    elapsed = time.monotonic() - _task_start
    _e_h = int(elapsed) // 3600
    _e_m = (int(elapsed) % 3600) // 60
    _e_s = int(elapsed) % 60
    _elapsed_str = f"{_e_h}时{_e_m}分{_e_s}秒" if _e_h > 0 else (f"{_e_m}分{_e_s}秒" if _e_m > 0 else f"{_e_s}秒")
    on_progress("SPEECH", 1.0, f"语音转写完成，共耗时{_elapsed_str}")
    log.info("SPEECH 100%% 语音转写完成: %d片段, 总耗时=%.0f秒", len(segs), elapsed)

    srt_size = int(Path(srt_path).stat().st_size) if Path(srt_path).exists() else 0
    vtt_size = int(Path(vtt_path).stat().st_size) if Path(vtt_path).exists() else 0
    html_size = int(Path(html_path).stat().st_size) if Path(html_path).exists() else 0

    return [
        {"path": srt_path, "sizeBytes": srt_size, "stepCode": "SPEECH", "fileType": "subtitle_srt"},
        {"path": vtt_path, "sizeBytes": vtt_size, "stepCode": "SPEECH", "fileType": "subtitle_vtt"},
        {"path": html_path, "sizeBytes": html_size, "stepCode": "SPEECH", "fileType": "speech_report_html"},
    ]
