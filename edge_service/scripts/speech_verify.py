#!/usr/bin/env python3
r"""
语音转写效果验证脚本 v2.0 (阶段一优化版)

用于验证语音转写功能，支持：
1. 对指定视频执行完整的语音转写流程
2. 将转写结果与已有的字幕文件进行对比
3. 输出详细的置信度分析报告

阶段一优化内容：
- 优化 Silero VAD 参数（适配课堂场景）
- 增强幻觉过滤规则（课堂场景黑名单）
- 添加置信度日志记录（avg_logprob 分布分析）
- 扩充教育词表纠错

流程包含：
- 音频提取
- VAD语音活动检测（优化参数）
- Whisper ASR转写
- 幻觉过滤（黑名单匹配、结构化规则、尾段过滤、课堂场景增强）
- 重复内容过滤
- 置信度分析与日志
- 字幕时间偏移修正

注意：此脚本不会被打包到客户端中，仅用于开发调试和方案验证。

配置参数（在脚本顶部CONFIG区域设置）：
- VIDEO_PATH: 要转写的视频文件路径
- COMPARE_SRT_PATH: 用于对比的已有字幕文件路径（可选）
- OUTPUT_DIR: 输出目录（可选，默认与视频同目录）
- ENABLE_PHASE1_OPTIMIZATIONS: 是否启用阶段一优化
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

# ============================================================
# 配置区域 - 在这里设置参数
# ============================================================
CONFIG = {
    # 要转写的视频文件路径（必填）
    "VIDEO_PATH": r"E:\Videos\2026-03-15\2033355621459091457\teacher_121.mp4",
    
    # 用于对比的已有字幕文件路径（可选，留空则不对比）
    "COMPARE_SRT_PATH": r"E:\Videos\2026-03-15\2033355621459091457\teacher_121.zh.srt",
    
    # 输出目录（可选，留空则输出到视频同目录）
    "OUTPUT_DIR": "",
    
    # 保存统计信息的JSON文件路径（可选）
    "STATS_PATH": "",
    
    # Whisper模型大小: small, medium, large-v3
    "MODEL_SIZE": "large-v3",
    
    # ========== 阶段一优化配置 ==========
    # 是否启用阶段一优化
    "ENABLE_PHASE1_OPTIMIZATIONS": True,
    
    # 是否输出置信度分析报告
    "ENABLE_CONFIDENCE_REPORT": True,
    
    # 低置信度阈值（低于此值的片段会被标记）
    "LOW_CONFIDENCE_THRESHOLD": -0.8,
    
    # 是否启用增强幻觉过滤
    "ENABLE_ENHANCED_HALLUCINATION_FILTER": True,
    
    # ========== 阶段二优化配置 ==========
    # 是否启用阶段二优化（SenseVoice双模型）
    "ENABLE_PHASE2_SENSEVOICE": True,
    
    # SenseVoice模型名称
    "SENSEVOICE_MODEL": "iic/SenseVoiceSmall",
    
    # 触发SenseVoice的置信度阈值（avg_logprob低于此值时调用）
    "SENSEVOICE_TRIGGER_THRESHOLD": -0.6,
    
    # 触发SenseVoice的no_speech_prob阈值
    "SENSEVOICE_NO_SPEECH_THRESHOLD": 0.35,
    
    # 是否对所有片段都使用双模型（调试用，会很慢）
    "SENSEVOICE_FORCE_ALL": False,
    
    # ========== 阶段三优化配置 ==========
    # 是否启用字符级对齐（需要先启用阶段二）
    "ENABLE_CHAR_ALIGNMENT": True,
    
    # ========== 阶段四优化配置 ==========
    # 是否启用音频预处理（SNR估算+可选降噪）
    "ENABLE_AUDIO_PREPROCESSING": True,
    
    # SNR阈值（低于此值时启用降噪，单位dB）
    "SNR_THRESHOLD_DB": 15.0,
    
    # 降噪强度（0-1，越大降噪越强，但可能损失语音细节）
    "DENOISE_STRENGTH": 0.5,
    
    # 是否强制降噪（忽略SNR判断）
    "FORCE_DENOISE": False,
    
    # ========== 阶段五优化配置 ==========
    # 是否启用并行处理（SenseVoice片段并行转写）
    "ENABLE_PARALLEL_PROCESSING": True,
    
    # 并行工作线程数（0=自动，根据CPU核心数决定）
    "PARALLEL_WORKERS": 0,
    
    # 批量处理大小（每批处理的片段数）
    "BATCH_SIZE": 10,
}
# ============================================================

# 添加项目根目录到路径
_script_dir = Path(__file__).resolve().parent
_project_root = _script_dir.parent
sys.path.insert(0, str(_project_root))

from edge_service.video.ffmpeg import extract_wav, get_audio_start_time, has_audio_stream, probe_duration_seconds
from edge_service.tasks.speech import (
    _detect_audio_activity_regions,
    _estimate_audio_activity_span,
    _wav_duration,
    _transcribe_wav,  # 核心转写函数，包含幻觉过滤等所有功能
    write_srt,
    write_vtt,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("speech_verify")


# ============================================================
# 阶段一优化模块
# ============================================================

# 课堂场景增强幻觉黑名单（补充现有黑名单）
CLASSROOM_HALLUCINATION_PATTERNS = [
    # 课堂场景常见幻觉
    r"^(好的?|嗯+|啊+|哦+|呃+)$",
    r"^(谢谢(大家|观看|收看|聆听)?[!！]?)$",
    r"^(请订阅|请点赞|请关注|欢迎订阅).*$",
    r"^(字幕|翻译|校对)[：:].+$",
    r"^(本视频|本节课|本期).*(到此结束|结束了?)$",
    r"^(感谢|谢谢).*(观看|收看|支持).*$",
    r"^(下[一期节].*再见|我们下[一期节].*见)$",
    r"^(音乐|掌声|笑声|欢呼声)$",
    r"^\[.*\]$",  # [音乐] [掌声] 等
    r"^【.*】$",  # 【音乐】【掌声】等
    # 重复无意义内容
    r"^(.)\1{3,}$",  # 同一字符重复4次以上
    r"^(\.{3,}|。{3,}|…+)$",  # 省略号
]

# 教育领域增强纠错词表（补充 correction_dict.json）
CLASSROOM_CORRECTIONS = {
    # 数学
    "成发": "乘法",
    "家发": "加法",
    "减发": "减法",
    "除发": "除法",
    "分之": "分之",
    "等于号": "等号",
    "大于号": "大于",
    "小于号": "小于",
    # 语文
    "比如说": "比如说",
    "举个例子": "举个例子",
    # 英语
    "A B C D": "ABCD",
    # 课堂用语
    "同学门": "同学们",
    "同学闷": "同学们",
    "老师门": "老师们",
    "大家号": "大家好",
    "好不好": "好不好",
    "对不队": "对不对",
    "是不是": "是不是",
    "懂不懂": "懂不懂",
    "会不会": "会不会",
    "能不能": "能不能",
    "行不行": "行不行",
    "要不要": "要不要",
    "想不想": "想不想",
}

# 编译正则表达式
_CLASSROOM_HALLUCINATION_REGEXES = [re.compile(p, re.IGNORECASE) for p in CLASSROOM_HALLUCINATION_PATTERNS]


def is_classroom_hallucination(text: str) -> bool:
    """检查文本是否为课堂场景幻觉内容"""
    if not text:
        return False
    text = text.strip()
    if len(text) <= 1:
        return text in {"嗯", "啊", "哦", "呃", "诶", "欸", "哎"}
    for regex in _CLASSROOM_HALLUCINATION_REGEXES:
        if regex.match(text):
            return True
    return False


def apply_classroom_corrections(text: str) -> str:
    """应用课堂场景增强纠错"""
    if not text:
        return text
    for wrong, right in sorted(CLASSROOM_CORRECTIONS.items(), key=lambda x: len(x[0]), reverse=True):
        if wrong in text:
            text = text.replace(wrong, right)
    return text


def analyze_confidence(segments: list[dict], threshold: float = -0.8) -> dict:
    """
    分析转写片段的置信度分布
    
    返回：
    {
        "total_segments": 总片段数,
        "low_confidence_count": 低置信度片段数,
        "low_confidence_ratio": 低置信度比例,
        "avg_logprob_mean": 平均 avg_logprob,
        "avg_logprob_min": 最小 avg_logprob,
        "avg_logprob_max": 最大 avg_logprob,
        "no_speech_prob_mean": 平均 no_speech_prob,
        "low_confidence_segments": 低置信度片段列表,
        "distribution": avg_logprob 分布统计,
    }
    """
    if not segments:
        return {
            "total_segments": 0,
            "low_confidence_count": 0,
            "low_confidence_ratio": 0.0,
            "avg_logprob_mean": 0.0,
            "avg_logprob_min": 0.0,
            "avg_logprob_max": 0.0,
            "no_speech_prob_mean": 0.0,
            "low_confidence_segments": [],
            "distribution": {},
        }
    
    logprobs = []
    no_speech_probs = []
    low_conf_segs = []
    
    for seg in segments:
        logprob = seg.get("avg_logprob", 0.0)
        no_speech = seg.get("no_speech_prob", 0.0)
        
        if logprob != 0.0:
            logprobs.append(logprob)
        if no_speech != 0.0:
            no_speech_probs.append(no_speech)
        
        if logprob < threshold:
            low_conf_segs.append({
                "start": seg.get("start", 0),
                "end": seg.get("end", 0),
                "text": seg.get("text", "")[:50],
                "avg_logprob": logprob,
                "no_speech_prob": no_speech,
            })
    
    # 计算分布
    distribution = {
        "excellent (> -0.3)": 0,
        "good (-0.3 ~ -0.5)": 0,
        "fair (-0.5 ~ -0.8)": 0,
        "poor (-0.8 ~ -1.0)": 0,
        "very_poor (< -1.0)": 0,
    }
    for lp in logprobs:
        if lp > -0.3:
            distribution["excellent (> -0.3)"] += 1
        elif lp > -0.5:
            distribution["good (-0.3 ~ -0.5)"] += 1
        elif lp > -0.8:
            distribution["fair (-0.5 ~ -0.8)"] += 1
        elif lp > -1.0:
            distribution["poor (-0.8 ~ -1.0)"] += 1
        else:
            distribution["very_poor (< -1.0)"] += 1
    
    return {
        "total_segments": len(segments),
        "low_confidence_count": len(low_conf_segs),
        "low_confidence_ratio": len(low_conf_segs) / max(len(segments), 1),
        "avg_logprob_mean": sum(logprobs) / max(len(logprobs), 1) if logprobs else 0.0,
        "avg_logprob_min": min(logprobs) if logprobs else 0.0,
        "avg_logprob_max": max(logprobs) if logprobs else 0.0,
        "no_speech_prob_mean": sum(no_speech_probs) / max(len(no_speech_probs), 1) if no_speech_probs else 0.0,
        "low_confidence_segments": low_conf_segs[:20],  # 只保留前20个
        "distribution": distribution,
    }


def post_process_segments_phase1(segments: list[dict], enable_enhanced_filter: bool = True) -> list[dict]:
    """
    阶段一后处理：增强幻觉过滤 + 教育词表纠错
    
    注意：这是在 _transcribe_wav 之后的额外处理，不影响核心流程
    """
    if not segments:
        return segments
    
    result = []
    filtered_count = 0
    corrected_count = 0
    
    for seg in segments:
        text = seg.get("text", "").strip()
        
        # 增强幻觉过滤
        if enable_enhanced_filter and is_classroom_hallucination(text):
            filtered_count += 1
            log.debug("阶段一过滤幻觉: %s", text[:30])
            continue
        
        # 教育词表纠错
        corrected_text = apply_classroom_corrections(text)
        if corrected_text != text:
            corrected_count += 1
            log.debug("阶段一纠错: %s -> %s", text[:30], corrected_text[:30])
        
        result.append({
            **seg,
            "text": corrected_text,
        })
    
    if filtered_count > 0 or corrected_count > 0:
        log.info("阶段一后处理: 过滤幻觉=%d, 纠错=%d", filtered_count, corrected_count)
    
    return result


# ============================================================
# 阶段二优化模块：SenseVoice 双模型集成
# ============================================================

# SenseVoice 模型缓存
_SENSEVOICE_MODEL = None
_SENSEVOICE_AVAILABLE = None


def check_sensevoice_available() -> bool:
    """检查 SenseVoice (FunASR) 是否可用"""
    global _SENSEVOICE_AVAILABLE
    if _SENSEVOICE_AVAILABLE is not None:
        return _SENSEVOICE_AVAILABLE
    try:
        import funasr
        _SENSEVOICE_AVAILABLE = True
        log.info("SenseVoice (FunASR) 可用")
    except ImportError:
        _SENSEVOICE_AVAILABLE = False
        log.warning("SenseVoice (FunASR) 不可用，请安装: pip install funasr")
    return _SENSEVOICE_AVAILABLE


def get_sensevoice_model(model_name: str = "iic/SenseVoiceSmall"):
    """获取 SenseVoice 模型（懒加载单例）"""
    global _SENSEVOICE_MODEL
    if _SENSEVOICE_MODEL is not None:
        return _SENSEVOICE_MODEL
    
    if not check_sensevoice_available():
        return None
    
    try:
        from funasr import AutoModel
        import torch
        
        device = "cuda" if torch.cuda.is_available() else "cpu"
        log.info("加载 SenseVoice 模型: %s (device=%s)", model_name, device)
        
        _SENSEVOICE_MODEL = AutoModel(
            model=model_name,
            trust_remote_code=True,
            device=device,
        )
        log.info("SenseVoice 模型加载完成")
        return _SENSEVOICE_MODEL
    except Exception as e:
        log.error("SenseVoice 模型加载失败: %s", e)
        return None


def transcribe_segment_sensevoice(
    wav_path: str,
    start_sec: float,
    end_sec: float,
    model_name: str = "iic/SenseVoiceSmall",
) -> str | None:
    """
    使用 SenseVoice 转写指定音频片段
    
    Args:
        wav_path: WAV 文件路径
        start_sec: 片段开始时间（秒）
        end_sec: 片段结束时间（秒）
        model_name: SenseVoice 模型名称
    
    Returns:
        转写文本，失败返回 None
    """
    model = get_sensevoice_model(model_name)
    if model is None:
        return None
    
    try:
        import tempfile
        import subprocess
        
        # 提取片段音频到临时文件
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        
        duration = end_sec - start_sec
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", wav_path,
            "-ss", str(start_sec),
            "-t", str(duration),
            "-ar", "16000",
            "-ac", "1",
            tmp_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        
        # 调用 SenseVoice 转写
        result = model.generate(input=tmp_path, language="zh")
        
        # 清理临时文件
        Path(tmp_path).unlink(missing_ok=True)
        
        if result and len(result) > 0:
            # SenseVoice 返回格式可能是 [{"text": "..."}] 或直接是文本
            if isinstance(result[0], dict):
                return result[0].get("text", "")
            elif isinstance(result[0], str):
                return result[0]
        return ""
    except Exception as e:
        log.warning("SenseVoice 转写片段失败 [%.1f-%.1f]: %s", start_sec, end_sec, e)
        return None


def normalize_dual_model_text(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"<\|.*?\|>", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"【[^】]*】", "", text)
    text = re.sub(r"\s+", "", text)
    return text.strip("，。！？；：,.!?;:")


def is_repeated_phrase_hallucination(text: str) -> bool:
    compact = normalize_dual_model_text(text)
    if len(compact) < 6:
        return False
    for unit_len in range(2, min(9, len(compact) // 2 + 1)):
        unit = compact[:unit_len]
        if unit and unit * 3 in compact:
            return True
    return False


def should_accept_sensevoice_result(whisper_seg: dict, sensevoice_text: str) -> bool:
    candidate = normalize_dual_model_text(sensevoice_text)
    if not candidate:
        return False
    if is_classroom_hallucination(candidate):
        return False
    if is_repeated_phrase_hallucination(candidate):
        return False

    whisper_text = normalize_dual_model_text(whisper_seg.get("text", ""))
    avg_logprob = float(whisper_seg.get("avg_logprob", 0.0) or 0.0)
    no_speech_prob = float(whisper_seg.get("no_speech_prob", 0.0) or 0.0)

    if whisper_text and candidate == whisper_text:
        return True
    if whisper_text and len(candidate) >= max(len(whisper_text) * 3, len(whisper_text) + 10) and avg_logprob > -0.9 and no_speech_prob < 0.7:
        return False
    return True


def should_use_sensevoice(
    segment: dict,
    trigger_threshold: float = -0.6,
    no_speech_threshold: float = 0.35,
    force_all: bool = False,
) -> bool:
    """
    判断是否需要对该片段调用 SenseVoice
    
    条件：
    1. force_all=True 时，所有片段都调用
    2. avg_logprob 低于阈值
    3. no_speech_prob 高于阈值但文本不为空（可能是幻觉）
    """
    if force_all:
        return True
    
    avg_logprob = segment.get("avg_logprob", 0.0)
    no_speech_prob = segment.get("no_speech_prob", 0.0)
    text = segment.get("text", "").strip()
    
    # 低置信度
    if avg_logprob < trigger_threshold:
        return True
    
    # 高无语音概率但有文本（可能是幻觉）
    if no_speech_prob > no_speech_threshold and len(text) > 1:
        return True

    if len(text) <= 10 and ("吗" in text or "？" in text or "?" in text) and avg_logprob < trigger_threshold + 0.2:
        return True
    
    return False


def align_segment_level(whisper_seg: dict, sensevoice_text: str) -> dict:
    """
    片段级对齐：使用 SenseVoice 文本，保留 Whisper 时间戳
    
    这是最简单的对齐策略，不做字符级映射
    """
    return {
        "start": whisper_seg.get("start", 0),
        "end": whisper_seg.get("end", 0),
        "text": sensevoice_text,
        "words": whisper_seg.get("words", []),  # 保留原始 word timestamps
        "avg_logprob": whisper_seg.get("avg_logprob", 0),
        "no_speech_prob": whisper_seg.get("no_speech_prob", 0),
        "source": "sensevoice",  # 标记来源
        "whisper_text": whisper_seg.get("text", ""),  # 保留原始 Whisper 文本用于对比
    }


# ============================================================
# 阶段三优化模块：字符级对齐
# ============================================================

def levenshtein_alignment(source: str, target: str) -> list[tuple[int, int, str]]:
    """
    计算 Levenshtein 编辑距离并返回对齐路径
    
    Args:
        source: 源字符串 (Whisper 文本)
        target: 目标字符串 (SenseVoice 文本)
    
    Returns:
        对齐操作列表: [(source_idx, target_idx, operation), ...]
        operation: 'M' (match), 'S' (substitute), 'I' (insert), 'D' (delete)
    """
    m, n = len(source), len(target)
    
    # DP 矩阵
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    
    # 初始化
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    
    # 填充 DP 矩阵
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if source[i - 1] == target[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]  # match
            else:
                dp[i][j] = 1 + min(
                    dp[i - 1][j],      # delete
                    dp[i][j - 1],      # insert
                    dp[i - 1][j - 1],  # substitute
                )
    
    # 回溯获取对齐路径
    alignment = []
    i, j = m, n
    
    while i > 0 or j > 0:
        if i > 0 and j > 0 and source[i - 1] == target[j - 1]:
            alignment.append((i - 1, j - 1, 'M'))  # match
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            alignment.append((i - 1, j - 1, 'S'))  # substitute
            i -= 1
            j -= 1
        elif j > 0 and dp[i][j] == dp[i][j - 1] + 1:
            alignment.append((-1, j - 1, 'I'))  # insert (target char not in source)
            j -= 1
        else:
            alignment.append((i - 1, -1, 'D'))  # delete (source char not in target)
            i -= 1
    
    alignment.reverse()
    return alignment


def interpolate_char_timestamps(
    whisper_words: list[dict],
    sensevoice_text: str,
    segment_start: float,
    segment_end: float,
) -> list[dict]:
    """
    基于 Whisper word timestamps 和 SenseVoice 文本，插值计算字符级时间戳
    
    Args:
        whisper_words: Whisper 的 word-level timestamps [{"word": "...", "start": ..., "end": ...}, ...]
        sensevoice_text: SenseVoice 转写的文本
        segment_start: 片段开始时间
        segment_end: 片段结束时间
    
    Returns:
        字符级时间戳列表 [{"char": "...", "start": ..., "end": ...}, ...]
    """
    if not sensevoice_text:
        return []
    
    # 如果没有 word timestamps，使用均匀分布
    if not whisper_words:
        duration = segment_end - segment_start
        char_duration = duration / max(len(sensevoice_text), 1)
        return [
            {
                "char": c,
                "start": segment_start + i * char_duration,
                "end": segment_start + (i + 1) * char_duration,
            }
            for i, c in enumerate(sensevoice_text)
        ]
    
    # 构建 Whisper 文本和字符时间映射
    whisper_text = ""
    whisper_char_times = []  # [(char, start, end), ...]
    
    for word_info in whisper_words:
        word = word_info.get("word", "").strip()
        word_start = word_info.get("start", segment_start)
        word_end = word_info.get("end", segment_end)
        
        if not word:
            continue
        
        # 均匀分配 word 内的字符时间
        char_duration = (word_end - word_start) / max(len(word), 1)
        for i, c in enumerate(word):
            char_start = word_start + i * char_duration
            char_end = word_start + (i + 1) * char_duration
            whisper_text += c
            whisper_char_times.append((c, char_start, char_end))
    
    if not whisper_text:
        # 没有有效的 word，使用均匀分布
        duration = segment_end - segment_start
        char_duration = duration / max(len(sensevoice_text), 1)
        return [
            {
                "char": c,
                "start": segment_start + i * char_duration,
                "end": segment_start + (i + 1) * char_duration,
            }
            for i, c in enumerate(sensevoice_text)
        ]
    
    # 计算对齐
    alignment = levenshtein_alignment(whisper_text, sensevoice_text)
    
    # 根据对齐结果插值时间戳
    result = []
    last_time = segment_start
    
    for src_idx, tgt_idx, op in alignment:
        if tgt_idx < 0:
            # Delete: Whisper 有但 SenseVoice 没有，跳过
            continue
        
        target_char = sensevoice_text[tgt_idx]
        
        if op == 'M' or op == 'S':
            # Match 或 Substitute: 使用 Whisper 的时间
            if 0 <= src_idx < len(whisper_char_times):
                _, char_start, char_end = whisper_char_times[src_idx]
                result.append({
                    "char": target_char,
                    "start": char_start,
                    "end": char_end,
                })
                last_time = char_end
            else:
                # 超出范围，使用插值
                result.append({
                    "char": target_char,
                    "start": last_time,
                    "end": last_time,
                })
        elif op == 'I':
            # Insert: SenseVoice 有但 Whisper 没有，需要插值
            # 使用前后字符的时间进行插值
            result.append({
                "char": target_char,
                "start": last_time,
                "end": last_time,  # 暂时设为相同，后续修正
            })
    
    # 修正插入字符的时间（使用相邻字符的时间进行插值）
    if len(result) > 1:
        for i in range(len(result)):
            if result[i]["start"] == result[i]["end"]:
                # 需要插值
                prev_end = result[i - 1]["end"] if i > 0 else segment_start
                next_start = result[i + 1]["start"] if i < len(result) - 1 else segment_end
                
                # 找到下一个有效时间
                for j in range(i + 1, len(result)):
                    if result[j]["start"] != result[j]["end"]:
                        next_start = result[j]["start"]
                        break
                
                # 计算插值时间
                gap = next_start - prev_end
                result[i]["start"] = prev_end
                result[i]["end"] = prev_end + gap / 2 if gap > 0 else prev_end
    
    return result


def align_character_level(
    whisper_seg: dict,
    sensevoice_text: str,
) -> dict:
    """
    字符级对齐：使用 SenseVoice 文本，基于 Whisper word timestamps 插值字符时间
    
    Args:
        whisper_seg: Whisper 转写的片段，包含 words 字段
        sensevoice_text: SenseVoice 转写的文本
    
    Returns:
        对齐后的片段，包含 char_timestamps 字段
    """
    segment_start = whisper_seg.get("start", 0)
    segment_end = whisper_seg.get("end", 0)
    whisper_words = whisper_seg.get("words", [])
    
    # 计算字符级时间戳
    char_timestamps = interpolate_char_timestamps(
        whisper_words,
        sensevoice_text,
        segment_start,
        segment_end,
    )
    
    return {
        "start": segment_start,
        "end": segment_end,
        "text": sensevoice_text,
        "words": whisper_words,
        "char_timestamps": char_timestamps,
        "avg_logprob": whisper_seg.get("avg_logprob", 0),
        "no_speech_prob": whisper_seg.get("no_speech_prob", 0),
        "source": "sensevoice_char_aligned",
        "whisper_text": whisper_seg.get("text", ""),
    }


# ============================================================
# 阶段四优化模块：音频预处理（SNR估算 + 可选降噪）
# ============================================================

def estimate_snr(wav_path: str, speech_regions: list[tuple] | None = None) -> dict:
    """
    估算音频的信噪比（SNR）
    
    方法：
    1. 如果有 speech_regions，使用语音区间作为信号，非语音区间作为噪声
    2. 如果没有 speech_regions，使用简单的能量分布估算
    
    Args:
        wav_path: WAV 文件路径
        speech_regions: 语音区间列表 [(start, end), ...]
    
    Returns:
        {
            "snr_db": 信噪比（dB）,
            "signal_rms": 信号 RMS,
            "noise_rms": 噪声 RMS,
            "method": 估算方法,
        }
    """
    import wave
    import struct
    import math
    
    try:
        with wave.open(wav_path, 'rb') as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frame_rate = wf.getframerate()
            n_frames = wf.getnframes()
            
            # 读取所有帧
            frames = wf.readframes(n_frames)
            
            # 解析为样本值
            if sample_width == 2:
                fmt = f"<{n_frames * n_channels}h"
                samples = list(struct.unpack(fmt, frames))
            else:
                # 不支持的格式，返回默认值
                return {
                    "snr_db": 20.0,
                    "signal_rms": 0,
                    "noise_rms": 0,
                    "method": "default",
                }
            
            # 如果是立体声，取平均
            if n_channels == 2:
                samples = [(samples[i] + samples[i + 1]) / 2 for i in range(0, len(samples), 2)]
            
            duration = n_frames / frame_rate
            
            if speech_regions and len(speech_regions) > 0:
                # 方法1：使用 speech_regions 区分信号和噪声
                signal_samples = []
                noise_samples = []
                
                for i, sample in enumerate(samples):
                    t = i / frame_rate
                    is_speech = False
                    for start, end in speech_regions:
                        if start <= t <= end:
                            is_speech = True
                            break
                    
                    if is_speech:
                        signal_samples.append(sample)
                    else:
                        noise_samples.append(sample)
                
                if signal_samples and noise_samples:
                    signal_rms = math.sqrt(sum(s ** 2 for s in signal_samples) / len(signal_samples))
                    noise_rms = math.sqrt(sum(s ** 2 for s in noise_samples) / len(noise_samples))
                    
                    if noise_rms > 0:
                        snr_db = 20 * math.log10(signal_rms / noise_rms)
                    else:
                        snr_db = 40.0  # 噪声为0，SNR很高
                    
                    return {
                        "snr_db": snr_db,
                        "signal_rms": signal_rms,
                        "noise_rms": noise_rms,
                        "method": "speech_regions",
                    }
            
            # 方法2：简单能量分布估算
            # 假设最低10%的能量帧为噪声
            frame_size = int(frame_rate * 0.02)  # 20ms 帧
            frame_energies = []
            
            for i in range(0, len(samples) - frame_size, frame_size):
                frame = samples[i:i + frame_size]
                energy = sum(s ** 2 for s in frame) / frame_size
                frame_energies.append(energy)
            
            if not frame_energies:
                return {
                    "snr_db": 20.0,
                    "signal_rms": 0,
                    "noise_rms": 0,
                    "method": "default",
                }
            
            frame_energies.sort()
            n_noise_frames = max(1, int(len(frame_energies) * 0.1))
            n_signal_frames = max(1, int(len(frame_energies) * 0.3))
            
            noise_energy = sum(frame_energies[:n_noise_frames]) / n_noise_frames
            signal_energy = sum(frame_energies[-n_signal_frames:]) / n_signal_frames
            
            noise_rms = math.sqrt(noise_energy)
            signal_rms = math.sqrt(signal_energy)
            
            if noise_rms > 0:
                snr_db = 20 * math.log10(signal_rms / noise_rms)
            else:
                snr_db = 40.0
            
            return {
                "snr_db": snr_db,
                "signal_rms": signal_rms,
                "noise_rms": noise_rms,
                "method": "energy_distribution",
            }
    
    except Exception as e:
        log.warning("SNR 估算失败: %s", e)
        return {
            "snr_db": 20.0,
            "signal_rms": 0,
            "noise_rms": 0,
            "method": "error",
        }


def denoise_audio(
    input_wav: str,
    output_wav: str,
    strength: float = 0.5,
) -> bool:
    """
    使用 ffmpeg 进行音频降噪
    
    Args:
        input_wav: 输入 WAV 文件路径
        output_wav: 输出 WAV 文件路径
        strength: 降噪强度 (0-1)
    
    Returns:
        是否成功
    """
    import subprocess
    
    try:
        # 使用 ffmpeg 的 afftdn 滤镜进行降噪
        # nr: 降噪强度 (0-97)
        # nf: 噪声底限 (-80 to -20)
        nr = int(strength * 50)  # 映射到 0-50
        nf = -40 + int(strength * 20)  # 映射到 -40 to -20
        
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", input_wav,
            "-af", f"afftdn=nr={nr}:nf={nf}",
            "-ar", "16000",
            "-ac", "1",
            output_wav,
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            return True
        else:
            log.warning("ffmpeg 降噪失败: %s", result.stderr)
            return False
    
    except Exception as e:
        log.warning("降噪处理异常: %s", e)
        return False


def preprocess_audio(
    wav_path: str,
    speech_regions: list[tuple] | None = None,
    snr_threshold_db: float = 15.0,
    denoise_strength: float = 0.5,
    force_denoise: bool = False,
) -> tuple[str, dict]:
    """
    音频预处理：SNR 估算 + 条件降噪
    
    Args:
        wav_path: 输入 WAV 文件路径
        speech_regions: 语音区间列表
        snr_threshold_db: SNR 阈值（低于此值时降噪）
        denoise_strength: 降噪强度
        force_denoise: 是否强制降噪
    
    Returns:
        (处理后的 WAV 路径, 统计信息)
    """
    stats = {
        "original_wav": wav_path,
        "processed_wav": wav_path,
        "snr_estimation": {},
        "denoise_applied": False,
        "denoise_reason": "",
    }
    
    # 1. 估算 SNR
    snr_info = estimate_snr(wav_path, speech_regions)
    stats["snr_estimation"] = snr_info
    
    snr_db = snr_info.get("snr_db", 20.0)
    log.info("       SNR 估算: %.1f dB (方法=%s)", snr_db, snr_info.get("method", "unknown"))
    
    # 2. 判断是否需要降噪
    need_denoise = False
    reason = ""
    
    if force_denoise:
        need_denoise = True
        reason = "强制降噪"
    elif snr_db < snr_threshold_db:
        need_denoise = True
        reason = f"SNR ({snr_db:.1f} dB) < 阈值 ({snr_threshold_db:.1f} dB)"
    
    # 3. 执行降噪
    if need_denoise:
        log.info("       执行降噪: %s", reason)
        
        # 生成降噪后的文件路径
        wav_dir = Path(wav_path).parent
        wav_stem = Path(wav_path).stem
        denoised_wav = str(wav_dir / f"{wav_stem}_denoised.wav")
        
        if denoise_audio(wav_path, denoised_wav, denoise_strength):
            stats["processed_wav"] = denoised_wav
            stats["denoise_applied"] = True
            stats["denoise_reason"] = reason
            log.info("       降噪完成: %s", denoised_wav)
            
            # 重新估算 SNR
            new_snr_info = estimate_snr(denoised_wav, speech_regions)
            log.info("       降噪后 SNR: %.1f dB", new_snr_info.get("snr_db", 0))
        else:
            log.warning("       降噪失败，使用原始音频")
            stats["denoise_reason"] = "降噪失败"
    else:
        log.info("       无需降噪: SNR (%.1f dB) >= 阈值 (%.1f dB)", snr_db, snr_threshold_db)
    
    return stats["processed_wav"], stats


# ============================================================
# 阶段五优化模块：并行处理
# ============================================================

def process_segments_parallel(
    segments: list[dict],
    wav_path: str,
    model_name: str = "iic/SenseVoiceSmall",
    trigger_threshold: float = -0.6,
    no_speech_threshold: float = 0.35,
    force_all: bool = False,
    enable_char_alignment: bool = False,
    num_workers: int = 0,
    batch_size: int = 10,
) -> tuple[list[dict], dict]:
    """
    使用并行处理优化 SenseVoice 转写（阶段五）
    
    Args:
        num_workers: 工作线程数（0=自动）
        batch_size: 批量处理大小
    
    Returns:
        (处理后的片段列表, 统计信息)
    """
    import concurrent.futures
    import os
    
    if not check_sensevoice_available():
        return segments, {"sensevoice_available": False, "parallel": False}
    
    # 确定工作线程数
    if num_workers <= 0:
        num_workers = min(4, os.cpu_count() or 2)
    
    stats = {
        "sensevoice_available": True,
        "parallel": True,
        "num_workers": num_workers,
        "batch_size": batch_size,
        "total_segments": len(segments),
        "triggered_count": 0,
        "replaced_count": 0,
        "char_aligned_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
    }
    
    # 筛选需要处理的片段
    to_process = []
    to_skip = []
    
    for i, seg in enumerate(segments):
        if should_use_sensevoice(seg, trigger_threshold, no_speech_threshold, force_all):
            to_process.append((i, seg))
            stats["triggered_count"] += 1
        else:
            to_skip.append((i, seg))
            stats["skipped_count"] += 1
    
    log.info("       并行处理: %d个片段需处理, %d个跳过, workers=%d",
             len(to_process), len(to_skip), num_workers)
    
    if not to_process:
        # 没有需要处理的片段
        return [{**seg, "source": "whisper"} for seg in segments], stats
    
    # 定义单个片段的处理函数
    def process_single(item: tuple) -> tuple:
        idx, seg = item
        start = seg.get("start", 0)
        end = seg.get("end", 0)
        
        sensevoice_text = transcribe_segment_sensevoice(wav_path, start, end, model_name)
        
        if sensevoice_text is not None and should_accept_sensevoice_result(seg, sensevoice_text):
            if enable_char_alignment:
                aligned_seg = align_character_level(seg, normalize_dual_model_text(sensevoice_text))
            else:
                aligned_seg = align_segment_level(seg, normalize_dual_model_text(sensevoice_text))
            return (idx, aligned_seg, True, enable_char_alignment)
        else:
            return (idx, {**seg, "source": "whisper"}, False, False)
    
    # 并行处理
    results = {}
    processed_count = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        # 分批提交任务
        for batch_start in range(0, len(to_process), batch_size):
            batch = to_process[batch_start:batch_start + batch_size]
            futures = {executor.submit(process_single, item): item for item in batch}
            
            for future in concurrent.futures.as_completed(futures):
                try:
                    idx, result_seg, success, char_aligned = future.result()
                    results[idx] = result_seg
                    
                    if success:
                        stats["replaced_count"] += 1
                        if char_aligned:
                            stats["char_aligned_count"] += 1
                    else:
                        stats["failed_count"] += 1
                    
                    processed_count += 1
                    if processed_count % 10 == 0 or processed_count == len(to_process):
                        log.info("       并行进度: %d/%d (替换=%d)",
                                 processed_count, len(to_process), stats["replaced_count"])
                
                except Exception as e:
                    item = futures[future]
                    idx, seg = item
                    results[idx] = {**seg, "source": "whisper"}
                    stats["failed_count"] += 1
                    log.warning("       并行处理异常 [%d]: %s", idx, e)
    
    # 添加跳过的片段
    for idx, seg in to_skip:
        results[idx] = {**seg, "source": "whisper"}
    
    # 按原始顺序排列结果
    final_results = [results[i] for i in range(len(segments))]
    
    return final_results, stats


def process_segments_with_sensevoice(
    segments: list[dict],
    wav_path: str,
    model_name: str = "iic/SenseVoiceSmall",
    trigger_threshold: float = -0.6,
    no_speech_threshold: float = 0.35,
    force_all: bool = False,
    enable_char_alignment: bool = False,  # 阶段三：字符级对齐
    enable_parallel: bool = False,  # 阶段五：并行处理
    num_workers: int = 0,
    batch_size: int = 10,
) -> tuple[list[dict], dict]:
    """
    使用 SenseVoice 处理低置信度片段
    
    Args:
        enable_char_alignment: 是否启用字符级对齐（阶段三）
        enable_parallel: 是否启用并行处理（阶段五）
        num_workers: 并行工作线程数
        batch_size: 批量处理大小
    
    Returns:
        (处理后的片段列表, 统计信息)
    """
    if not check_sensevoice_available():
        return segments, {"sensevoice_available": False}
    
    # 阶段五：如果启用并行处理，使用并行版本
    if enable_parallel:
        return process_segments_parallel(
            segments,
            wav_path,
            model_name=model_name,
            trigger_threshold=trigger_threshold,
            no_speech_threshold=no_speech_threshold,
            force_all=force_all,
            enable_char_alignment=enable_char_alignment,
            num_workers=num_workers,
            batch_size=batch_size,
        )
    
    # 串行处理（原有逻辑）
    stats = {
        "sensevoice_available": True,
        "parallel": False,
        "total_segments": len(segments),
        "triggered_count": 0,
        "replaced_count": 0,
        "char_aligned_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
    }
    
    result = []
    
    for i, seg in enumerate(segments):
        if should_use_sensevoice(seg, trigger_threshold, no_speech_threshold, force_all):
            stats["triggered_count"] += 1
            
            # 调用 SenseVoice
            start = seg.get("start", 0)
            end = seg.get("end", 0)
            sensevoice_text = transcribe_segment_sensevoice(wav_path, start, end, model_name)
            
            if sensevoice_text is not None and should_accept_sensevoice_result(seg, sensevoice_text):
                # 根据配置选择对齐方式
                normalized_sensevoice_text = normalize_dual_model_text(sensevoice_text)
                if enable_char_alignment:
                    # 阶段三：字符级对齐
                    aligned_seg = align_character_level(seg, normalized_sensevoice_text)
                    stats["char_aligned_count"] += 1
                else:
                    # 阶段二：片段级对齐
                    aligned_seg = align_segment_level(seg, normalized_sensevoice_text)
                
                result.append(aligned_seg)
                stats["replaced_count"] += 1
                
                whisper_text = seg.get("text", "")[:30]
                sv_text = normalized_sensevoice_text[:30]
                if whisper_text != sv_text:
                    log.debug("SenseVoice替换 [%.1f-%.1f]: '%s' -> '%s'", start, end, whisper_text, sv_text)
            else:
                # SenseVoice 失败，保留原始
                result.append({**seg, "source": "whisper"})
                stats["failed_count"] += 1
        else:
            # 不需要调用 SenseVoice
            result.append({**seg, "source": "whisper"})
            stats["skipped_count"] += 1
        
        # 进度显示
        if (i + 1) % 20 == 0 or i == len(segments) - 1:
            log.info("       SenseVoice处理进度: %d/%d (触发=%d, 替换=%d, 字符对齐=%d)",
                     i + 1, len(segments), stats["triggered_count"], stats["replaced_count"], stats["char_aligned_count"])
    
    return result, stats


def generate_sensevoice_report(
    segments: list[dict],
    stats: dict,
    output_path: str,
    video_path: str = "",
) -> None:
    """生成 SenseVoice 处理报告 HTML"""
    
    # 筛选被替换的片段
    replaced_segs = [s for s in segments if s.get("source") == "sensevoice"]
    
    def fmt_time(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"
    
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>SenseVoice 处理报告</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ color: #333; text-align: center; }}
        .card {{ background: #fff; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 15px; }}
        .stat-item {{ text-align: center; padding: 15px; background: #f8f9fa; border-radius: 6px; }}
        .stat-value {{ font-size: 24px; font-weight: bold; color: #333; }}
        .stat-label {{ color: #666; font-size: 12px; margin-top: 5px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px; }}
        th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f8f9fa; }}
        .whisper {{ color: #999; text-decoration: line-through; }}
        .sensevoice {{ color: #28a745; font-weight: bold; }}
        .diff {{ background: #fff3cd; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🎯 SenseVoice 双模型处理报告</h1>
        
        <div class="card">
            <h2>视频信息</h2>
            <p><strong>文件：</strong>{video_path}</p>
        </div>
        
        <div class="card">
            <h2>处理统计</h2>
            <div class="stats-grid">
                <div class="stat-item">
                    <div class="stat-value">{stats.get("total_segments", 0)}</div>
                    <div class="stat-label">总片段数</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{stats.get("triggered_count", 0)}</div>
                    <div class="stat-label">触发SenseVoice</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" style="color: #28a745;">{stats.get("replaced_count", 0)}</div>
                    <div class="stat-label">成功替换</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" style="color: #dc3545;">{stats.get("failed_count", 0)}</div>
                    <div class="stat-label">替换失败</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{stats.get("skipped_count", 0)}</div>
                    <div class="stat-label">跳过(高置信度)</div>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h2>替换详情（{len(replaced_segs)} 个片段）</h2>
            <table>
                <tr>
                    <th>时间</th>
                    <th>Whisper 原文</th>
                    <th>SenseVoice 替换</th>
                    <th>avg_logprob</th>
                </tr>
'''
    
    for seg in replaced_segs[:50]:  # 只显示前50个
        whisper_text = seg.get("whisper_text", "")
        sensevoice_text = seg.get("text", "")
        is_diff = whisper_text != sensevoice_text
        row_class = 'class="diff"' if is_diff else ""
        
        html += f'''                <tr {row_class}>
                    <td>{fmt_time(seg.get("start", 0))} - {fmt_time(seg.get("end", 0))}</td>
                    <td class="whisper">{whisper_text}</td>
                    <td class="sensevoice">{sensevoice_text}</td>
                    <td>{seg.get("avg_logprob", 0):.3f}</td>
                </tr>
'''
    
    if len(replaced_segs) > 50:
        html += f'                <tr><td colspan="4" style="text-align: center; color: #666;">... 还有 {len(replaced_segs) - 50} 个片段</td></tr>\n'
    
    if not replaced_segs:
        html += '                <tr><td colspan="4" style="text-align: center; color: #666;">无替换片段</td></tr>\n'
    
    html += '''            </table>
        </div>
    </div>
</body>
</html>
'''
    
    Path(output_path).write_text(html, encoding="utf-8")


def generate_confidence_report(
    confidence_analysis: dict,
    output_path: str,
    video_path: str = "",
) -> None:
    """生成置信度分析HTML报告"""
    
    dist = confidence_analysis.get("distribution", {})
    low_segs = confidence_analysis.get("low_confidence_segments", [])
    
    def fmt_time(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"
    
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>置信度分析报告</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 1000px; margin: 0 auto; }}
        h1 {{ color: #333; text-align: center; }}
        .card {{ background: #fff; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }}
        .stat-item {{ text-align: center; padding: 15px; background: #f8f9fa; border-radius: 6px; }}
        .stat-value {{ font-size: 24px; font-weight: bold; color: #333; }}
        .stat-label {{ color: #666; font-size: 14px; margin-top: 5px; }}
        .distribution {{ margin-top: 20px; }}
        .bar {{ height: 30px; margin: 5px 0; border-radius: 4px; display: flex; align-items: center; padding: 0 10px; color: #fff; font-size: 14px; }}
        .bar-excellent {{ background: #28a745; }}
        .bar-good {{ background: #5cb85c; }}
        .bar-fair {{ background: #f0ad4e; }}
        .bar-poor {{ background: #d9534f; }}
        .bar-very-poor {{ background: #c9302c; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
        th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f8f9fa; }}
        .low-conf {{ color: #d9534f; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 置信度分析报告</h1>
        
        <div class="card">
            <h2>视频信息</h2>
            <p><strong>文件：</strong>{video_path}</p>
        </div>
        
        <div class="card">
            <h2>统计概览</h2>
            <div class="stats-grid">
                <div class="stat-item">
                    <div class="stat-value">{confidence_analysis.get("total_segments", 0)}</div>
                    <div class="stat-label">总片段数</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" style="color: #d9534f;">{confidence_analysis.get("low_confidence_count", 0)}</div>
                    <div class="stat-label">低置信度片段</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{confidence_analysis.get("low_confidence_ratio", 0):.1%}</div>
                    <div class="stat-label">低置信度比例</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{confidence_analysis.get("avg_logprob_mean", 0):.3f}</div>
                    <div class="stat-label">平均 logprob</div>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h2>置信度分布</h2>
            <div class="distribution">
'''
    
    total = sum(dist.values()) or 1
    for label, count in dist.items():
        pct = count / total * 100
        width = max(5, pct)
        css_class = "bar-excellent" if "excellent" in label else \
                    "bar-good" if "good" in label else \
                    "bar-fair" if "fair" in label else \
                    "bar-poor" if "poor" in label and "very" not in label else "bar-very-poor"
        html += f'                <div class="bar {css_class}" style="width: {width}%;">{label}: {count} ({pct:.1f}%)</div>\n'
    
    html += '''            </div>
        </div>
        
        <div class="card">
            <h2>低置信度片段（前20个）</h2>
            <table>
                <tr>
                    <th>时间</th>
                    <th>文本</th>
                    <th>avg_logprob</th>
                    <th>no_speech_prob</th>
                </tr>
'''
    
    for seg in low_segs:
        html += f'''                <tr>
                    <td>{fmt_time(seg.get("start", 0))} - {fmt_time(seg.get("end", 0))}</td>
                    <td>{seg.get("text", "")}</td>
                    <td class="low-conf">{seg.get("avg_logprob", 0):.3f}</td>
                    <td>{seg.get("no_speech_prob", 0):.3f}</td>
                </tr>
'''
    
    if not low_segs:
        html += '                <tr><td colspan="4" style="text-align: center; color: #28a745;">无低置信度片段 ✓</td></tr>\n'
    
    html += '''            </table>
        </div>
    </div>
</body>
</html>
'''
    
    Path(output_path).write_text(html, encoding="utf-8")


# ============================================================
# 原有功能
# ============================================================

def parse_srt(srt_path: str) -> list[dict]:
    """解析SRT字幕文件，返回片段列表"""
    segments = []
    if not Path(srt_path).exists():
        return segments
    
    content = Path(srt_path).read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\n+", content.strip())
    
    time_pattern = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
    
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        
        time_line = lines[1] if lines[0].isdigit() else lines[0]
        text_lines = lines[2:] if lines[0].isdigit() else lines[1:]
        
        matches = time_pattern.findall(time_line)
        if len(matches) < 2:
            continue
        
        def to_seconds(m):
            return int(m[0]) * 3600 + int(m[1]) * 60 + int(m[2]) + int(m[3]) / 1000.0
        
        start = to_seconds(matches[0])
        end = to_seconds(matches[1])
        text = " ".join(text_lines).strip()
        
        if text:
            segments.append({"start": start, "end": end, "text": text})
    
    return segments


def compare_subtitles(segs1: list[dict], segs2: list[dict], label1: str = "新转写", label2: str = "原字幕") -> dict:
    """对比两个字幕的差异"""
    result = {
        "summary": {},
        "text_diff": [],
        "time_diff": [],
        "missing_in_new": [],
        "missing_in_old": [],
    }
    
    # 基本统计
    result["summary"] = {
        f"{label1}_segments": len(segs1),
        f"{label2}_segments": len(segs2),
        f"{label1}_total_text": sum(len(s.get("text", "")) for s in segs1),
        f"{label2}_total_text": sum(len(s.get("text", "")) for s in segs2),
        f"{label1}_duration": max((s.get("end", 0) for s in segs1), default=0) - min((s.get("start", 0) for s in segs1), default=0) if segs1 else 0,
        f"{label2}_duration": max((s.get("end", 0) for s in segs2), default=0) - min((s.get("start", 0) for s in segs2), default=0) if segs2 else 0,
    }
    
    # 文本对比
    text1 = "\n".join(s.get("text", "") for s in segs1)
    text2 = "\n".join(s.get("text", "") for s in segs2)
    
    diff = list(difflib.unified_diff(
        text2.splitlines(),
        text1.splitlines(),
        fromfile=label2,
        tofile=label1,
        lineterm="",
    ))
    result["text_diff"] = diff
    
    # 时间覆盖对比
    def get_time_ranges(segs):
        return [(s.get("start", 0), s.get("end", 0)) for s in segs]
    
    ranges1 = get_time_ranges(segs1)
    ranges2 = get_time_ranges(segs2)
    
    # 找出时间差异较大的片段
    for i, (s1, e1) in enumerate(ranges1):
        found = False
        for s2, e2 in ranges2:
            if abs(s1 - s2) < 5 and abs(e1 - e2) < 5:
                found = True
                break
        if not found:
            result["missing_in_old"].append({
                "index": i,
                "start": s1,
                "end": e1,
                "text": segs1[i].get("text", "")[:50],
            })
    
    for i, (s2, e2) in enumerate(ranges2):
        found = False
        for s1, e1 in ranges1:
            if abs(s1 - s2) < 5 and abs(e1 - e2) < 5:
                found = True
                break
        if not found:
            result["missing_in_new"].append({
                "index": i,
                "start": s2,
                "end": e2,
                "text": segs2[i].get("text", "")[:50],
            })
    
    return result


def generate_comparison_html(
    segs1: list[dict],
    segs2: list[dict],
    comparison: dict,
    output_path: str,
    video_path: str = "",
    label1: str = "新转写",
    label2: str = "原字幕",
) -> None:
    """生成HTML格式的对比报告"""
    
    def fmt_time(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"
    
    html_content = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>语音转写对比报告</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        h1 {{ color: #333; margin-bottom: 20px; text-align: center; }}
        h2 {{ color: #555; margin: 20px 0 10px; padding-bottom: 5px; border-bottom: 2px solid #ddd; }}
        h3 {{ color: #666; margin: 15px 0 10px; }}
        .summary {{ background: #fff; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }}
        .summary-item {{ background: #f8f9fa; padding: 15px; border-radius: 6px; text-align: center; }}
        .summary-item .label {{ color: #666; font-size: 14px; }}
        .summary-item .value {{ color: #333; font-size: 24px; font-weight: bold; margin-top: 5px; }}
        .comparison {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
        .panel {{ background: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow: hidden; }}
        .panel-header {{ background: #4a90d9; color: #fff; padding: 12px 15px; font-weight: bold; }}
        .panel-header.old {{ background: #6c757d; }}
        .panel-content {{ max-height: 600px; overflow-y: auto; }}
        .segment {{ padding: 10px 15px; border-bottom: 1px solid #eee; }}
        .segment:hover {{ background: #f8f9fa; }}
        .segment .time {{ color: #888; font-size: 12px; font-family: monospace; }}
        .segment .text {{ margin-top: 5px; line-height: 1.5; }}
        .missing {{ background: #fff3cd !important; }}
        .missing-section {{ background: #fff; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .missing-item {{ padding: 10px; margin: 5px 0; background: #fff3cd; border-radius: 4px; border-left: 4px solid #ffc107; }}
        .missing-item .time {{ color: #856404; font-family: monospace; font-size: 13px; }}
        .missing-item .text {{ color: #333; margin-top: 5px; }}
        .diff-section {{ background: #fff; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .diff-line {{ font-family: monospace; padding: 2px 10px; white-space: pre-wrap; word-break: break-all; }}
        .diff-add {{ background: #d4edda; color: #155724; }}
        .diff-del {{ background: #f8d7da; color: #721c24; }}
        .diff-header {{ color: #6c757d; }}
        .video-info {{ background: #e7f3ff; padding: 10px 15px; border-radius: 6px; margin-bottom: 20px; color: #004085; }}
        .tabs {{ display: flex; gap: 5px; margin-bottom: 15px; }}
        .tab {{ padding: 10px 20px; background: #e9ecef; border: none; border-radius: 6px 6px 0 0; cursor: pointer; font-size: 14px; }}
        .tab.active {{ background: #fff; font-weight: bold; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔍 语音转写对比报告</h1>
        
        <div class="video-info">
            <strong>视频文件：</strong>{video_path}
        </div>
        
        <div class="summary">
            <h2>📊 统计摘要</h2>
            <div class="summary-grid">
                <div class="summary-item">
                    <div class="label">{label1} 片段数</div>
                    <div class="value">{comparison["summary"].get(f"{label1}_segments", 0)}</div>
                </div>
                <div class="summary-item">
                    <div class="label">{label2} 片段数</div>
                    <div class="value">{comparison["summary"].get(f"{label2}_segments", 0)}</div>
                </div>
                <div class="summary-item">
                    <div class="label">{label1} 总字数</div>
                    <div class="value">{comparison["summary"].get(f"{label1}_total_text", 0)}</div>
                </div>
                <div class="summary-item">
                    <div class="label">{label2} 总字数</div>
                    <div class="value">{comparison["summary"].get(f"{label2}_total_text", 0)}</div>
                </div>
                <div class="summary-item">
                    <div class="label">{label1}新增片段</div>
                    <div class="value" style="color: #28a745;">{len(comparison.get("missing_in_old", []))}</div>
                </div>
                <div class="summary-item">
                    <div class="label">{label2}缺失片段</div>
                    <div class="value" style="color: #dc3545;">{len(comparison.get("missing_in_new", []))}</div>
                </div>
            </div>
        </div>
        
        <div class="tabs">
            <button class="tab active" onclick="showTab('side-by-side')">📋 并排对比</button>
            <button class="tab" onclick="showTab('missing-new')">➕ 新增片段 ({len(comparison.get("missing_in_old", []))})</button>
            <button class="tab" onclick="showTab('missing-old')">➖ 缺失片段 ({len(comparison.get("missing_in_new", []))})</button>
            <button class="tab" onclick="showTab('text-diff')">📝 文本差异</button>
        </div>
        
        <div id="side-by-side" class="tab-content active">
            <div class="comparison">
                <div class="panel">
                    <div class="panel-header">✨ {label1} ({len(segs1)} 片段)</div>
                    <div class="panel-content">
'''
    
    # 新转写片段
    missing_in_old_times = set((item["start"], item["end"]) for item in comparison.get("missing_in_old", []))
    for seg in segs1:
        is_missing = (seg.get("start", 0), seg.get("end", 0)) in missing_in_old_times
        css_class = "segment missing" if is_missing else "segment"
        html_content += f'''                        <div class="{css_class}">
                            <div class="time">{fmt_time(seg.get("start", 0))} → {fmt_time(seg.get("end", 0))}</div>
                            <div class="text">{seg.get("text", "")}</div>
                        </div>
'''
    
    html_content += '''                    </div>
                </div>
                <div class="panel">
                    <div class="panel-header old">📄 ''' + label2 + f''' ({len(segs2)} 片段)</div>
                    <div class="panel-content">
'''
    
    # 原字幕片段
    missing_in_new_times = set((item["start"], item["end"]) for item in comparison.get("missing_in_new", []))
    for seg in segs2:
        is_missing = (seg.get("start", 0), seg.get("end", 0)) in missing_in_new_times
        css_class = "segment missing" if is_missing else "segment"
        html_content += f'''                        <div class="{css_class}">
                            <div class="time">{fmt_time(seg.get("start", 0))} → {fmt_time(seg.get("end", 0))}</div>
                            <div class="text">{seg.get("text", "")}</div>
                        </div>
'''
    
    html_content += '''                    </div>
                </div>
            </div>
        </div>
        
        <div id="missing-new" class="tab-content">
            <div class="missing-section">
                <h3>➕ ''' + label1 + '''中有但''' + label2 + f'''中没有的片段 ({len(comparison.get("missing_in_old", []))}个)</h3>
'''
    
    for item in comparison.get("missing_in_old", []):
        html_content += f'''                <div class="missing-item">
                    <div class="time">[{fmt_time(item["start"])} → {fmt_time(item["end"])}]</div>
                    <div class="text">{segs1[item["index"]].get("text", "") if item["index"] < len(segs1) else item.get("text", "")}</div>
                </div>
'''
    
    if not comparison.get("missing_in_old"):
        html_content += '''                <p style="color: #666; padding: 20px; text-align: center;">无新增片段</p>
'''
    
    html_content += '''            </div>
        </div>
        
        <div id="missing-old" class="tab-content">
            <div class="missing-section">
                <h3>➖ ''' + label2 + '''中有但''' + label1 + f'''中没有的片段 ({len(comparison.get("missing_in_new", []))}个)</h3>
'''
    
    for item in comparison.get("missing_in_new", []):
        html_content += f'''                <div class="missing-item">
                    <div class="time">[{fmt_time(item["start"])} → {fmt_time(item["end"])}]</div>
                    <div class="text">{segs2[item["index"]].get("text", "") if item["index"] < len(segs2) else item.get("text", "")}</div>
                </div>
'''
    
    if not comparison.get("missing_in_new"):
        html_content += '''                <p style="color: #666; padding: 20px; text-align: center;">无缺失片段</p>
'''
    
    html_content += '''            </div>
        </div>
        
        <div id="text-diff" class="tab-content">
            <div class="diff-section">
                <h3>📝 文本差异（Unified Diff格式）</h3>
                <div style="max-height: 600px; overflow-y: auto; background: #f8f9fa; padding: 10px; border-radius: 4px;">
'''
    
    for line in comparison.get("text_diff", []):
        if line.startswith("+") and not line.startswith("+++"):
            html_content += f'                    <div class="diff-line diff-add">{line}</div>\n'
        elif line.startswith("-") and not line.startswith("---"):
            html_content += f'                    <div class="diff-line diff-del">{line}</div>\n'
        elif line.startswith("@@") or line.startswith("---") or line.startswith("+++"):
            html_content += f'                    <div class="diff-line diff-header">{line}</div>\n'
        else:
            html_content += f'                    <div class="diff-line">{line}</div>\n'
    
    if not comparison.get("text_diff"):
        html_content += '''                    <p style="color: #666; padding: 20px; text-align: center;">无文本差异</p>
'''
    
    html_content += '''                </div>
            </div>
        </div>
    </div>
    
    <script>
        function showTab(tabId) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.querySelector(`[onclick="showTab('${tabId}')"]`).classList.add('active');
            document.getElementById(tabId).classList.add('active');
        }
    </script>
</body>
</html>
'''
    
    Path(output_path).write_text(html_content, encoding="utf-8")


def transcribe_video(
    video_path: str,
    output_dir: str | None = None,
    model_size: str = "medium",
    enable_phase1: bool = True,
    enable_confidence_report: bool = True,
    low_confidence_threshold: float = -0.8,
    enable_enhanced_filter: bool = True,
    # 阶段二参数
    enable_phase2: bool = False,
    sensevoice_model: str = "iic/SenseVoiceSmall",
    sensevoice_trigger_threshold: float = -0.6,
    sensevoice_no_speech_threshold: float = 0.35,
    sensevoice_force_all: bool = False,
    # 阶段三参数
    enable_char_alignment: bool = False,
    # 阶段四参数
    enable_audio_preprocessing: bool = False,
    snr_threshold_db: float = 15.0,
    denoise_strength: float = 0.5,
    force_denoise: bool = False,
    # 阶段五参数
    enable_parallel: bool = False,
    parallel_workers: int = 0,
    batch_size: int = 10,
) -> tuple[list[dict], str, dict]:
    """
    对视频执行完整的语音转写流程 v2.4 (阶段一至五优化版)
    
    使用 _transcribe_wav 核心函数，包含：
    - 幻觉过滤（黑名单匹配、结构化规则）
    - 尾段可疑片段过滤
    - 重复内容过滤
    - VAD语音活动检测
    - 字幕时间偏移修正
    
    阶段一优化（enable_phase1=True时启用）：
    - 增强幻觉过滤（课堂场景黑名单）
    - 置信度分析与日志
    - 教育词表纠错
    
    阶段二优化（enable_phase2=True时启用）：
    - SenseVoice 双模型集成
    - 条件调用（低置信度片段触发）
    - 片段级对齐
    
    阶段四优化（enable_audio_preprocessing=True时启用）：
    - SNR 估算
    - 条件降噪（低 SNR 时自动降噪）
    
    阶段三优化（enable_char_alignment=True时启用，需先启用阶段二）：
    - Levenshtein 编辑距离对齐
    - 字符级时间戳插值
    
    阶段五优化（enable_parallel=True时启用，需先启用阶段二）：
    - SenseVoice 片段并行转写
    - 批量处理优化
    
    返回：(segments, srt_path, stats)
    """
    video = Path(video_path)
    if not video.exists():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")
    
    if output_dir:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = video.parent
    
    stats = {
        "video_path": str(video),
        "video_duration": 0.0,
        "audio_stream_start": 0.0,
        "wav_duration": 0.0,
        "speech_regions": [],
        "active_audio_sec": 0.0,
        "active_ratio": 0.0,
        "audio_offset": 0.0,
        "content_end_sec": 0.0,
        "subtitle_offset": 0.0,
        "asr_segments": 0,
        "elapsed_sec": 0.0,
        # 阶段一优化统计
        "phase1_enabled": enable_phase1,
        "phase1_filtered_count": 0,
        "phase1_corrected_count": 0,
        "confidence_analysis": {},
        # 阶段二优化统计
        "phase2_enabled": enable_phase2,
        "phase2_sensevoice_stats": {},
        # 阶段三优化统计
        "phase3_enabled": enable_char_alignment,
        # 阶段四优化统计
        "phase4_enabled": enable_audio_preprocessing,
        "phase4_preprocessing_stats": {},
        # 阶段五优化统计
        "phase5_enabled": enable_parallel,
        "phase5_parallel_workers": parallel_workers,
    }
    
    start_time = time.monotonic()
    
    # 进度辅助函数
    total_steps = 9
    def _log_step(step: int, message: str, *args):
        """输出带进度百分比的步骤日志"""
        progress = int((step / total_steps) * 100)
        prefix = f"[{step}/{total_steps}] ({progress:3d}%)"
        if args:
            log.info("%s " + message, prefix, *args)
        else:
            log.info("%s %s", prefix, message)
    
    # 1. 检查音频流
    log.info("=" * 60)
    log.info("开始语音转写验证 v2.4 (阶段一至五优化版)")
    log.info("视频文件: %s", video_path)
    log.info("模型大小: %s", model_size)
    log.info("阶段一优化: %s", "启用" if enable_phase1 else "禁用")
    log.info("阶段二优化(SenseVoice): %s", "启用" if enable_phase2 else "禁用")
    log.info("阶段三优化(字符级对齐): %s", "启用" if enable_char_alignment else "禁用")
    log.info("阶段四优化(音频预处理): %s", "启用" if enable_audio_preprocessing else "禁用")
    log.info("阶段五优化(并行处理): %s", "启用" if enable_parallel else "禁用")
    log.info("=" * 60)
    
    if not has_audio_stream(str(video)):
        raise RuntimeError("视频文件无音频流")
    
    # 2. 获取视频信息
    video_dur = probe_duration_seconds(str(video))
    audio_stream_start = get_audio_start_time(str(video))
    stats["video_duration"] = video_dur
    stats["audio_stream_start"] = audio_stream_start
    
    _log_step(1, "视频信息: 时长=%.1f秒, 音频流起始=%.3f秒", video_dur, audio_stream_start)
    
    # 3. 提取音频
    wav_path = str(out_dir / f"{video.stem}_verify.wav")
    _log_step(2, "提取音频中...")
    extract_wav(str(video), wav_path, sample_rate=16000)
    
    wav_dur = _wav_duration(wav_path)
    stats["wav_duration"] = wav_dur
    log.info("       音频提取完成: %s, 时长=%.1f秒", wav_path, wav_dur)
    
    # 4. 检测语音活动区间
    _log_step(3, "检测语音活动区间...")
    speech_regions = _detect_audio_activity_regions(wav_path)
    audio_offset, content_end_sec = _estimate_audio_activity_span(wav_path)
    
    stats["speech_regions"] = [(float(s), float(e)) for s, e in speech_regions]
    stats["audio_offset"] = audio_offset
    stats["content_end_sec"] = content_end_sec
    
    if speech_regions:
        active_audio_sec = sum(max(0.0, float(et) - float(st)) for st, et in speech_regions)
        active_ratio = (active_audio_sec / max(wav_dur, 1.0)) if wav_dur > 0 else 0.0
        stats["active_audio_sec"] = active_audio_sec
        stats["active_ratio"] = active_ratio
        
        log.info("       检测到 %d 个语音区间, 活跃=%.1f秒, 总=%.1f秒, 比例=%.1f%%",
                 len(speech_regions), active_audio_sec, wav_dur, active_ratio * 100.0)
        
        # 如果活跃比例过低，放弃VAD区间限制
        if active_ratio < 0.30 and wav_dur > 300:
            log.warning("       VAD检测活跃比例过低(%.1f%%)，放弃VAD区间限制改用全段转写", active_ratio * 100.0)
            speech_regions = []
    else:
        log.info("       未检测到明显语音区间，将使用全段转写")
    
    log.info("       音频偏移=%.3f秒, 内容结束=%.3f秒", audio_offset, content_end_sec)
    
    # 4.5 阶段四优化：音频预处理（SNR估算+可选降噪）
    transcribe_wav_path = wav_path  # 默认使用原始WAV
    if enable_audio_preprocessing:
        _log_step(4, "执行阶段四优化（音频预处理）...")
        
        transcribe_wav_path, preprocessing_stats = preprocess_audio(
            wav_path,
            speech_regions=speech_regions if speech_regions else None,
            snr_threshold_db=snr_threshold_db,
            denoise_strength=denoise_strength,
            force_denoise=force_denoise,
        )
        stats["phase4_preprocessing_stats"] = preprocessing_stats
    
    # 5. 执行ASR转写（使用核心函数，包含幻觉过滤等所有功能）
    _log_step(5, "执行ASR转写（包含幻觉过滤、黑名单匹配等）...")
    effective_dur = max(wav_dur, video_dur, content_end_sec)
    
    # 进度回调
    _last_progress = [0]
    def _on_seg_progress(seg_count: int, current_time: float):
        progress = min(100, int((current_time / max(effective_dur, 1.0)) * 100))
        if progress > _last_progress[0]:
            _last_progress[0] = progress
            print(f"\r       转写进度: {progress}% ({seg_count}片段, 当前={current_time:.1f}s)", end="", flush=True)
    
    def _on_stage_progress(stage: str, message: str):
        log.info("       [%s] %s", stage, message)
    
    # 调用核心转写函数（与监控页面完全一致）
    # 如果启用了阶段四预处理，使用降噪后的音频
    segs = _transcribe_wav(
        transcribe_wav_path,  # 可能是原始WAV或降噪后的WAV
        total_dur=effective_dur,
        expected_end_sec=content_end_sec if content_end_sec > 0 else effective_dur,
        speech_regions=speech_regions if speech_regions else None,
        raw={},  # 空的raw字典，使用默认配置
        on_seg_progress=_on_seg_progress,
        on_stage_progress=_on_stage_progress,
        model_size_override=model_size,
    )
    
    print()  # 换行
    
    stats["asr_segments"] = len(segs)
    _log_step(6, "转写完成: %d 个片段（已过滤幻觉和重复内容）", len(segs))
    
    # 6. 阶段一优化处理
    if enable_phase1:
        _log_step(7, "执行阶段一优化处理...")
        
        # 6.1 置信度分析
        confidence_analysis = analyze_confidence(segs, threshold=low_confidence_threshold)
        stats["confidence_analysis"] = confidence_analysis
        
        log.info("       置信度分析: 总片段=%d, 低置信度=%d (%.1f%%)",
                 confidence_analysis["total_segments"],
                 confidence_analysis["low_confidence_count"],
                 confidence_analysis["low_confidence_ratio"] * 100)
        log.info("       avg_logprob: 平均=%.3f, 最小=%.3f, 最大=%.3f",
                 confidence_analysis["avg_logprob_mean"],
                 confidence_analysis["avg_logprob_min"],
                 confidence_analysis["avg_logprob_max"])
        
        # 输出分布
        dist = confidence_analysis.get("distribution", {})
        if dist:
            log.info("       置信度分布:")
            for label, count in dist.items():
                if count > 0:
                    log.info("         - %s: %d", label, count)
        
        # 6.2 增强幻觉过滤 + 教育词表纠错
        original_count = len(segs)
        segs = post_process_segments_phase1(segs, enable_enhanced_filter=enable_enhanced_filter)
        filtered_count = original_count - len(segs)
        stats["phase1_filtered_count"] = filtered_count
        
        if filtered_count > 0:
            log.info("       阶段一过滤: 移除 %d 个幻觉片段", filtered_count)
        
        # 6.3 生成置信度分析报告
        if enable_confidence_report:
            confidence_report_path = str(out_dir / f"{video.stem}_confidence.html")
            generate_confidence_report(confidence_analysis, confidence_report_path, video_path)
            log.info("       置信度报告: %s", confidence_report_path)
    else:
        _log_step(7, "跳过阶段一优化（已禁用）")
    
    # 7. 阶段二+阶段三+阶段五优化：SenseVoice 双模型处理 + 字符级对齐 + 并行处理
    if enable_phase2:
        phase_desc = "SenseVoice双模型"
        if enable_char_alignment:
            phase_desc += " + 字符级对齐"
        if enable_parallel:
            phase_desc += " + 并行处理"
        _log_step(8, "执行阶段二/三/五优化（%s）...", phase_desc)
        
        if check_sensevoice_available():
            segs, sensevoice_stats = process_segments_with_sensevoice(
                segs,
                wav_path,
                model_name=sensevoice_model,
                trigger_threshold=sensevoice_trigger_threshold,
                no_speech_threshold=sensevoice_no_speech_threshold,
                force_all=sensevoice_force_all,
                enable_char_alignment=enable_char_alignment,  # 阶段三
                enable_parallel=enable_parallel,  # 阶段五
                num_workers=parallel_workers,
                batch_size=batch_size,
            )
            stats["phase2_sensevoice_stats"] = sensevoice_stats
            
            parallel_info = ""
            if sensevoice_stats.get("parallel"):
                parallel_info = f", 并行workers={sensevoice_stats.get('num_workers', 0)}"
            log.info("       SenseVoice处理完成: 触发=%d, 替换=%d, 字符对齐=%d, 失败=%d%s",
                     sensevoice_stats.get("triggered_count", 0),
                     sensevoice_stats.get("replaced_count", 0),
                     sensevoice_stats.get("char_aligned_count", 0),
                     sensevoice_stats.get("failed_count", 0),
                     parallel_info)
            
            # 生成SenseVoice处理报告
            sensevoice_report_path = str(out_dir / f"{video.stem}_sensevoice.html")
            generate_sensevoice_report(segs, sensevoice_stats, sensevoice_report_path, video_path)
            log.info("       SenseVoice报告: %s", sensevoice_report_path)
        else:
            log.warning("       SenseVoice不可用，跳过阶段二优化")
    else:
        _log_step(8, "跳过阶段二优化（已禁用）")
    
    # 8. 计算字幕偏移并生成字幕文件
    _log_step(9, "生成字幕文件...")
    
    subtitle_offset = max(0.0, float(audio_stream_start or 0.0))
    content_offset = 0.0
    if segs and audio_offset > 0:
        first_seg_start = max(0.0, float(segs[0].get("start", 0.0) or 0.0))
        candidate_offset = max(0.0, audio_offset - first_seg_start)
        if candidate_offset > 0 and effective_dur > 0:
            last_seg_end = max(float(s.get("end", 0.0) or 0.0) for s in segs)
            if last_seg_end + subtitle_offset + candidate_offset <= effective_dur:
                content_offset = candidate_offset
    subtitle_offset += max(0.0, content_offset)
    stats["subtitle_offset"] = subtitle_offset
    
    log.info("       字幕时间修正: audio_start=%.3f秒, content_offset=%.3f秒, subtitle_offset=%.3f秒",
             audio_stream_start, content_offset, subtitle_offset)
    
    # 生成字幕文件
    srt_path = str(out_dir / f"{video.stem}_verify.zh.srt")
    vtt_path = str(out_dir / f"{video.stem}_verify.zh.vtt")
    
    write_srt(segs, srt_path, offset=subtitle_offset)
    write_vtt(segs, vtt_path, offset=subtitle_offset)
    
    log.info("       SRT: %s", srt_path)
    log.info("       VTT: %s", vtt_path)
    
    # 清理临时WAV文件
    try:
        Path(wav_path).unlink(missing_ok=True)
    except Exception:
        pass
    
    elapsed = time.monotonic() - start_time
    stats["elapsed_sec"] = elapsed
    
    log.info("=" * 60)
    log.info("转写完成! 耗时=%.1f秒, 片段=%d, 字幕偏移=%.3f秒", elapsed, len(segs), subtitle_offset)
    log.info("=" * 60)
    
    # 应用偏移后的片段（用于对比）
    adjusted_segs = []
    for seg in segs:
        adjusted_segs.append({
            "start": seg["start"] + subtitle_offset,
            "end": seg["end"] + subtitle_offset,
            "text": seg["text"],
        })
    
    return adjusted_segs, srt_path, stats


def main():
    parser = argparse.ArgumentParser(
        description="语音转写效果验证脚本 v2.1 (阶段一+阶段二优化版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=r"""
使用方式1 - 直接修改脚本顶部CONFIG区域的参数，然后运行:
    python scripts/speech_verify.py
    
使用方式2 - 通过命令行参数（会覆盖CONFIG中的设置）:
    python scripts/speech_verify.py --video "E:\Videos\xxx\teacher_121.mp4"
    python scripts/speech_verify.py --video "E:\Videos\xxx\teacher_121.mp4" --compare "E:\Videos\xxx\teacher_121.zh.srt"
    python scripts/speech_verify.py --video "E:\Videos\xxx\teacher_121.mp4" --no-phase1  # 禁用阶段一优化
    python scripts/speech_verify.py --video "E:\Videos\xxx\teacher_121.mp4" --phase2    # 启用阶段二(SenseVoice)
    python scripts/speech_verify.py --video "E:\Videos\xxx\teacher_121.mp4" --phase2 --char-align  # 启用阶段三(字符级对齐)
        """,
    )
    parser.add_argument("--video", "-v", help="要转写的视频文件路径（覆盖CONFIG.VIDEO_PATH）")
    parser.add_argument("--compare", "-c", help="用于对比的已有字幕文件路径（覆盖CONFIG.COMPARE_SRT_PATH）")
    parser.add_argument("--output", "-o", help="输出目录（覆盖CONFIG.OUTPUT_DIR）")
    parser.add_argument("--stats", "-s", help="保存统计信息的JSON文件路径（覆盖CONFIG.STATS_PATH）")
    parser.add_argument("--model", "-m", help="Whisper模型大小: small, medium, large-v3（覆盖CONFIG.MODEL_SIZE）")
    parser.add_argument("--no-phase1", action="store_true", help="禁用阶段一优化")
    parser.add_argument("--no-confidence-report", action="store_true", help="禁用置信度分析报告")
    parser.add_argument("--phase2", action="store_true", help="启用阶段二优化(SenseVoice双模型)")
    parser.add_argument("--sensevoice-all", action="store_true", help="对所有片段使用SenseVoice（调试用）")
    parser.add_argument("--char-align", action="store_true", help="启用阶段三优化(字符级对齐，需配合--phase2)")
    parser.add_argument("--preprocess", action="store_true", help="启用阶段四优化(音频预处理，SNR估算+降噪)")
    parser.add_argument("--force-denoise", action="store_true", help="强制降噪（忽略SNR判断）")
    parser.add_argument("--parallel", action="store_true", help="启用阶段五优化(并行处理，需配合--phase2)")
    parser.add_argument("--workers", type=int, default=0, help="并行工作线程数（0=自动）")
    
    args = parser.parse_args()
    
    # 合并CONFIG和命令行参数（命令行优先）
    video_path = args.video or CONFIG.get("VIDEO_PATH", "")
    compare_path = args.compare or CONFIG.get("COMPARE_SRT_PATH", "")
    output_dir = args.output or CONFIG.get("OUTPUT_DIR", "")
    stats_path = args.stats or CONFIG.get("STATS_PATH", "")
    model_size = args.model or CONFIG.get("MODEL_SIZE", "medium")
    
    # 阶段一优化配置
    enable_phase1 = CONFIG.get("ENABLE_PHASE1_OPTIMIZATIONS", True) and not args.no_phase1
    enable_confidence_report = CONFIG.get("ENABLE_CONFIDENCE_REPORT", True) and not args.no_confidence_report
    low_confidence_threshold = CONFIG.get("LOW_CONFIDENCE_THRESHOLD", -0.8)
    enable_enhanced_filter = CONFIG.get("ENABLE_ENHANCED_HALLUCINATION_FILTER", True)
    
    # 阶段二优化配置
    enable_phase2 = CONFIG.get("ENABLE_PHASE2_SENSEVOICE", False) or args.phase2
    sensevoice_model = CONFIG.get("SENSEVOICE_MODEL", "iic/SenseVoiceSmall")
    sensevoice_trigger_threshold = CONFIG.get("SENSEVOICE_TRIGGER_THRESHOLD", -0.6)
    sensevoice_no_speech_threshold = CONFIG.get("SENSEVOICE_NO_SPEECH_THRESHOLD", 0.35)
    sensevoice_force_all = CONFIG.get("SENSEVOICE_FORCE_ALL", False) or args.sensevoice_all
    
    # 阶段三优化配置
    enable_char_alignment = CONFIG.get("ENABLE_CHAR_ALIGNMENT", False) or args.char_align
    
    # 阶段四优化配置
    enable_audio_preprocessing = CONFIG.get("ENABLE_AUDIO_PREPROCESSING", False) or args.preprocess
    snr_threshold_db = CONFIG.get("SNR_THRESHOLD_DB", 15.0)
    denoise_strength = CONFIG.get("DENOISE_STRENGTH", 0.5)
    force_denoise = CONFIG.get("FORCE_DENOISE", False) or args.force_denoise
    
    # 阶段五优化配置
    enable_parallel = CONFIG.get("ENABLE_PARALLEL_PROCESSING", False) or args.parallel
    parallel_workers = args.workers if args.workers > 0 else CONFIG.get("PARALLEL_WORKERS", 0)
    batch_size = CONFIG.get("BATCH_SIZE", 10)
    
    if not video_path:
        log.error("错误: 未指定视频文件路径。请在CONFIG.VIDEO_PATH中设置或通过--video参数指定。")
        return 1
    
    try:
        # 执行转写（带阶段一至五优化）
        segs, srt_path, stats = transcribe_video(
            video_path,
            output_dir or None,
            model_size,
            enable_phase1=enable_phase1,
            enable_confidence_report=enable_confidence_report,
            low_confidence_threshold=low_confidence_threshold,
            enable_enhanced_filter=enable_enhanced_filter,
            # 阶段二参数
            enable_phase2=enable_phase2,
            sensevoice_model=sensevoice_model,
            sensevoice_trigger_threshold=sensevoice_trigger_threshold,
            sensevoice_no_speech_threshold=sensevoice_no_speech_threshold,
            sensevoice_force_all=sensevoice_force_all,
            # 阶段三参数
            enable_char_alignment=enable_char_alignment,
            # 阶段四参数
            enable_audio_preprocessing=enable_audio_preprocessing,
            snr_threshold_db=snr_threshold_db,
            denoise_strength=denoise_strength,
            force_denoise=force_denoise,
            # 阶段五参数
            enable_parallel=enable_parallel,
            parallel_workers=parallel_workers,
            batch_size=batch_size,
        )
        
        # 保存统计信息
        if stats_path:
            stats_file = Path(stats_path)
            stats_file.parent.mkdir(parents=True, exist_ok=True)
            stats_file.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("统计信息已保存: %s", stats_path)
        
        # 对比字幕
        if compare_path:
            log.info("")
            log.info("=" * 60)
            log.info("开始字幕对比")
            log.info("=" * 60)
            
            old_segs = parse_srt(compare_path)
            if not old_segs:
                log.warning("原字幕文件为空或解析失败: %s", compare_path)
            else:
                comparison = compare_subtitles(segs, old_segs, "新转写", "原字幕")
                
                log.info("")
                log.info("【统计对比】")
                for k, v in comparison["summary"].items():
                    log.info("  %s: %s", k, v)
                
                log.info("")
                log.info("【新转写中有但原字幕中没有的片段】(%d个)", len(comparison["missing_in_old"]))
                for item in comparison["missing_in_old"][:10]:
                    log.info("  [%.1f-%.1f] %s...", item["start"], item["end"], item["text"])
                if len(comparison["missing_in_old"]) > 10:
                    log.info("  ... 还有 %d 个", len(comparison["missing_in_old"]) - 10)
                
                log.info("")
                log.info("【原字幕中有但新转写中没有的片段】(%d个)", len(comparison["missing_in_new"]))
                for item in comparison["missing_in_new"][:10]:
                    log.info("  [%.1f-%.1f] %s...", item["start"], item["end"], item["text"])
                if len(comparison["missing_in_new"]) > 10:
                    log.info("  ... 还有 %d 个", len(comparison["missing_in_new"]) - 10)
                
                if comparison["text_diff"]:
                    log.info("")
                    log.info("【文本差异】(前50行)")
                    for line in comparison["text_diff"][:50]:
                        log.info("  %s", line)
                
                # 生成HTML对比报告
                html_report_path = Path(srt_path).with_suffix(".compare.html")
                generate_comparison_html(
                    segs, old_segs, comparison,
                    str(html_report_path),
                    video_path=video_path,
                )
                log.info("")
                log.info("HTML对比报告已生成: %s", html_report_path)
                
                # 同时保存JSON（便于程序处理）
                json_report_path = Path(srt_path).with_suffix(".compare.json")
                json_report_path.write_text(
                    json.dumps(comparison, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                log.info("JSON对比数据已保存: %s", json_report_path)
        
        log.info("")
        log.info("验证完成!")
        return 0
        
    except Exception as e:
        log.error("转写失败: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
