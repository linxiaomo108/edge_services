# 双模型ASR优化方案 - 分阶段实施计划

> 版本：v1.0  
> 日期：2026-03  
> 基于：`dual_model_asr_analysis.md` 分析报告 + `yh.md` 优化方案

---

## 一、方案核心思路

### 1.1 架构定位

**从"双模型投票融合"收敛为"职责拆分 + 条件调用"**

| 模型 | 职责 | 调用策略 |
|------|------|----------|
| **Whisper** | 时间戳基准 + 基础识别 | 全量调用 |
| **SenseVoice** | 中文文本增强 | 条件调用（低置信时） |
| **Silero VAD** | 语音分段 | 全量调用 |

### 1.2 核心原则

1. **职责拆分**：SenseVoice 负责文本准确率，Whisper 负责时间轴稳定
2. **条件调用**：只对低置信片段触发双模型，避免全量双跑
3. **渐进对齐**：先片段级对齐，再逐步增强到字符级
4. **可选降噪**：降噪做成开关模块，不强制处理
5. **轻量优先**：不引入重型外部语言模型或API

---

## 二、总体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                         视频文件输入                                  │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  阶段1：音频提取                                                      │
│  ├─ FFmpeg 提取音频                                                  │
│  └─ 输出 16kHz WAV                                                   │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  阶段2：音频预处理（可选）                                             │
│  ├─ 音质检测（SNR估算）                                               │
│  ├─ 轻量频段滤波（highpass 80Hz + lowpass 8kHz）                      │
│  └─ 可选降噪（RNNoise / ffmpeg anlmdn）                               │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  阶段3：VAD 语音分段                                                  │
│  ├─ Silero VAD 检测语音区间                                          │
│  ├─ 动态分段（30s为基准，按静音边界切分）                               │
│  └─ 输出：[(start, end), ...] 分段列表                                │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  阶段4：Whisper 基础识别                                              │
│  ├─ 对每个分段调用 Whisper                                            │
│  ├─ 输出：text + word_timestamps + avg_logprob + no_speech_prob       │
│  └─ 记录置信度指标                                                    │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  阶段5：置信度判断                                                    │
│  ├─ 高置信（avg_logprob > -0.8）→ 直接进入后处理                       │
│  └─ 低置信（avg_logprob ≤ -0.8）→ 触发 SenseVoice 二次识别             │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
                    ▼                               ▼
        ┌───────────────────┐           ┌───────────────────┐
        │  高置信路径        │           │  低置信路径        │
        │  直接使用Whisper   │           │  调用SenseVoice    │
        │  输出              │           │  进行文本增强      │
        └───────────────────┘           └───────────────────┘
                    │                               │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  阶段6：文本对齐                                                      │
│  ├─ 片段级对齐：SenseVoice文本 + Whisper时间戳                         │
│  └─ 字符级对齐（增强版）：编辑距离 + 时间插值                           │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  阶段7：后处理                                                        │
│  ├─ 幻觉过滤（黑名单 + 规则 + 置信度）                                 │
│  ├─ 教育词表纠错（correction_dict.json）                              │
│  └─ 重复内容过滤                                                      │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  阶段8：字幕生成                                                      │
│  ├─ 时间偏移修正（audio_stream_start + content_offset）               │
│  ├─ 输出 SRT 格式                                                    │
│  └─ 输出 VTT 格式                                                    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 三、分阶段实施计划

### 阶段一：MVP版本（稳定基础）

**目标**：在现有基础上优化，不引入新模型，先把质量稳定提升

**工期**：1-2周

**实施内容**：

| 序号 | 任务 | 说明 | 复用/新增 |
|------|------|------|-----------|
| 1.1 | 优化 Silero VAD 参数 | 调整阈值适配课堂场景 | 优化现有 |
| 1.2 | 增强幻觉过滤规则 | 补充课堂场景黑名单 | 优化现有 |
| 1.3 | 扩充教育词表 | 补充数学/语文/英语术语 | 优化现有 |
| 1.4 | 添加置信度日志 | 记录 avg_logprob 分布 | 新增 |
| 1.5 | 建立测试基准 | 选取10个典型视频作为测试集 | 新增 |

**代码改动范围**：

```
edge_service/tasks/speech.py
├─ _detect_audio_activity_regions()  # 优化VAD参数
├─ _is_hallucination()               # 补充黑名单
├─ _is_structured_hallucination()    # 补充规则
└─ _collect_segments()               # 添加置信度日志

docs/correction_dict.json            # 扩充词表
docs/hallucination_blacklist.json    # 补充黑名单
```

**验收标准**：
- [ ] VAD 分段准确率 ≥ 95%
- [ ] 幻觉内容减少 30%
- [ ] 建立测试基准数据集

---

### 阶段二：SenseVoice 集成（条件调用）

**目标**：引入 SenseVoice 作为中文增强模型，只对低置信片段调用

**工期**：2-3周

**实施内容**：

| 序号 | 任务 | 说明 | 优先级 |
|------|------|------|--------|
| 2.1 | SenseVoice 模型集成 | 安装 FunASR，下载模型 | 高 |
| 2.2 | 置信度阈值确定 | 分析日志确定触发阈值 | 高 |
| 2.3 | 条件调用逻辑 | 低置信时调用 SenseVoice | 高 |
| 2.4 | 片段级对齐实现 | SenseVoice文本 + Whisper时间 | 高 |
| 2.5 | 文本差异比对 | 记录两模型输出差异 | 中 |
| 2.6 | 性能监控 | 记录双模型调用耗时 | 中 |

**核心代码结构**：

```python
# edge_service/tasks/speech.py 新增函数

def _get_sensevoice_model():
    """加载 SenseVoice 模型（懒加载单例）"""
    global _SENSEVOICE_MODEL
    if _SENSEVOICE_MODEL is None:
        from funasr import AutoModel
        _SENSEVOICE_MODEL = AutoModel(
            model="iic/SenseVoiceSmall",
            device="cuda" if torch.cuda.is_available() else "cpu"
        )
    return _SENSEVOICE_MODEL


def _transcribe_with_sensevoice(wav_path: str, start: float, end: float) -> str:
    """使用 SenseVoice 转写指定片段"""
    model = _get_sensevoice_model()
    # 提取片段音频
    segment_wav = _extract_segment(wav_path, start, end)
    result = model.generate(input=segment_wav)
    return result[0]["text"]


def _should_use_sensevoice(segment: dict) -> bool:
    """判断是否需要调用 SenseVoice"""
    avg_logprob = segment.get("avg_logprob", 0)
    no_speech_prob = segment.get("no_speech_prob", 0)
    
    # 低置信度条件
    if avg_logprob < -0.8:
        return True
    # 高无语音概率
    if no_speech_prob > 0.5 and len(segment.get("text", "")) > 5:
        return True
    # 命中幻觉风险
    if _is_hallucination_risk(segment.get("text", "")):
        return True
    
    return False


def _align_segment_level(whisper_seg: dict, sensevoice_text: str) -> dict:
    """片段级对齐：使用 SenseVoice 文本，保留 Whisper 时间戳"""
    return {
        "start": whisper_seg["start"],
        "end": whisper_seg["end"],
        "text": sensevoice_text,
        "words": whisper_seg.get("words", []),  # 保留原始 word timestamps
        "source": "sensevoice",  # 标记来源
    }
```

**模型部署**：

```bash
# 安装 FunASR
pip install funasr

# 模型会自动下载到 ~/.cache/modelscope/
# 或手动下载后放到指定目录
# 模型大小：SenseVoiceSmall 约 500MB
```

**验收标准**：
- [ ] SenseVoice 模型成功集成
- [ ] 条件调用逻辑正常工作
- [ ] 低置信片段准确率提升 15%
- [ ] 总处理时间增加不超过 30%

---

### 阶段三：字符级对齐（精度增强）

**目标**：实现字符级时间戳对齐，提升字幕显示精度

**工期**：2-3周

**实施内容**：

| 序号 | 任务 | 说明 | 复杂度 |
|------|------|------|--------|
| 3.1 | 编辑距离算法实现 | Levenshtein 距离计算 | 低 |
| 3.2 | 操作路径回溯 | 记录 match/replace/insert/delete | 中 |
| 3.3 | 时间分配策略 | 处理字数不等情况 | 高 |
| 3.4 | 时间插值算法 | 插入字符的时间分配 | 高 |
| 3.5 | 边界情况处理 | 空文本、极短片段等 | 中 |
| 3.6 | 单元测试 | 覆盖各种对齐场景 | 中 |

**核心代码结构**：

```python
# edge_service/tasks/alignment.py 新增模块

from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple


class EditOp(Enum):
    MATCH = "match"
    REPLACE = "replace"
    INSERT = "insert"
    DELETE = "delete"


@dataclass
class AlignedChar:
    char: str
    start: float
    end: float
    source: str  # "whisper" or "sensevoice"


def compute_edit_operations(
    whisper_text: str,
    sensevoice_text: str
) -> List[Tuple[EditOp, int, int, str]]:
    """
    计算从 whisper_text 到 sensevoice_text 的编辑操作序列
    返回: [(操作类型, whisper索引, sensevoice索引, 字符), ...]
    """
    m, n = len(whisper_text), len(sensevoice_text)
    
    # 动态规划计算编辑距离
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if whisper_text[i-1] == sensevoice_text[j-1]:
                dp[i][j] = dp[i-1][j-1]
            else:
                dp[i][j] = 1 + min(dp[i-1][j], dp[i][j-1], dp[i-1][j-1])
    
    # 回溯操作路径
    operations = []
    i, j = m, n
    while i > 0 or j > 0:
        if i > 0 and j > 0 and whisper_text[i-1] == sensevoice_text[j-1]:
            operations.append((EditOp.MATCH, i-1, j-1, sensevoice_text[j-1]))
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i-1][j-1] + 1:
            operations.append((EditOp.REPLACE, i-1, j-1, sensevoice_text[j-1]))
            i -= 1
            j -= 1
        elif j > 0 and dp[i][j] == dp[i][j-1] + 1:
            operations.append((EditOp.INSERT, i, j-1, sensevoice_text[j-1]))
            j -= 1
        else:
            operations.append((EditOp.DELETE, i-1, j, whisper_text[i-1]))
            i -= 1
    
    return list(reversed(operations))


def align_character_level(
    whisper_words: List[dict],
    sensevoice_text: str
) -> List[AlignedChar]:
    """
    字符级对齐：将 SenseVoice 文本对齐到 Whisper 时间轴
    
    Args:
        whisper_words: Whisper 输出的 word timestamps
                       [{"word": "你", "start": 0.5, "end": 0.8}, ...]
        sensevoice_text: SenseVoice 输出的文本
    
    Returns:
        对齐后的字符列表，每个字符带时间戳
    """
    # 展开 Whisper words 为字符级
    whisper_chars = []
    for word in whisper_words:
        text = word.get("word", "")
        start = word.get("start", 0)
        end = word.get("end", start)
        if not text:
            continue
        # 均匀分配时间给每个字符
        char_duration = (end - start) / len(text)
        for i, char in enumerate(text):
            whisper_chars.append({
                "char": char,
                "start": start + i * char_duration,
                "end": start + (i + 1) * char_duration,
            })
    
    whisper_text = "".join(c["char"] for c in whisper_chars)
    
    # 计算编辑操作
    operations = compute_edit_operations(whisper_text, sensevoice_text)
    
    # 根据操作分配时间
    result = []
    whisper_idx = 0
    
    for op, w_idx, s_idx, char in operations:
        if op == EditOp.MATCH:
            # 直接映射
            result.append(AlignedChar(
                char=char,
                start=whisper_chars[w_idx]["start"],
                end=whisper_chars[w_idx]["end"],
                source="match"
            ))
            whisper_idx = w_idx + 1
        
        elif op == EditOp.REPLACE:
            # 继承被替换字符的时间
            result.append(AlignedChar(
                char=char,
                start=whisper_chars[w_idx]["start"],
                end=whisper_chars[w_idx]["end"],
                source="replace"
            ))
            whisper_idx = w_idx + 1
        
        elif op == EditOp.INSERT:
            # 从相邻字符借用时间
            if result:
                prev_end = result[-1].end
            else:
                prev_end = whisper_chars[0]["start"] if whisper_chars else 0
            
            if w_idx < len(whisper_chars):
                next_start = whisper_chars[w_idx]["start"]
            else:
                next_start = prev_end + 0.1
            
            # 插入字符占用相邻间隙的一半
            gap = max(0.05, (next_start - prev_end) / 2)
            result.append(AlignedChar(
                char=char,
                start=prev_end,
                end=prev_end + gap,
                source="insert"
            ))
        
        elif op == EditOp.DELETE:
            # 跳过，不输出
            whisper_idx = w_idx + 1
    
    return result
```

**验收标准**：
- [ ] 字符级对齐算法实现完成
- [ ] 单元测试覆盖率 ≥ 80%
- [ ] 字幕时间精度提升（误差 < 0.3s）
- [ ] 无明显性能退化

---

### 阶段四：音频预处理（可选增强）

**目标**：添加可选的音频预处理模块，提升低质量音频的识别效果

**工期**：1-2周

**实施内容**：

| 序号 | 任务 | 说明 | 优先级 |
|------|------|------|--------|
| 4.1 | SNR 估算 | 评估音频信噪比 | 中 |
| 4.2 | 轻量频段滤波 | highpass + lowpass | 高 |
| 4.3 | RNNoise 集成 | 可选深度降噪 | 低 |
| 4.4 | 自动降噪决策 | 根据 SNR 决定是否降噪 | 中 |
| 4.5 | 降噪效果回退 | 降噪后效果变差则回退 | 中 |

**核心代码结构**：

```python
# edge_service/video/audio_preprocess.py 新增模块

import subprocess
from pathlib import Path


def estimate_snr(wav_path: str) -> float:
    """
    估算音频信噪比（简化版）
    返回 SNR 估计值（dB）
    """
    # 使用 ffmpeg 获取音频统计信息
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        wav_path
    ]
    # ... 实现细节
    return snr_db


def apply_light_filter(input_wav: str, output_wav: str) -> bool:
    """
    应用轻量频段滤波
    - highpass: 去除 80Hz 以下低频噪声
    - lowpass: 去除 8kHz 以上高频噪声
    """
    cmd = [
        "ffmpeg", "-y", "-i", input_wav,
        "-af", "highpass=f=80,lowpass=f=8000",
        "-ar", "16000",
        output_wav
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def apply_denoise(input_wav: str, output_wav: str, method: str = "anlmdn") -> bool:
    """
    应用降噪处理
    
    Args:
        method: "anlmdn" (ffmpeg内置) 或 "rnnoise" (深度学习)
    """
    if method == "anlmdn":
        cmd = [
            "ffmpeg", "-y", "-i", input_wav,
            "-af", "anlmdn=s=7:p=0.002:r=0.002",
            "-ar", "16000",
            output_wav
        ]
    elif method == "rnnoise":
        # RNNoise 需要单独安装
        cmd = ["rnnoise", input_wav, output_wav]
    else:
        return False
    
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def preprocess_audio(
    input_wav: str,
    output_wav: str,
    enable_denoise: bool = False,
    snr_threshold: float = 20.0
) -> str:
    """
    音频预处理主函数
    
    Args:
        enable_denoise: 是否启用降噪
        snr_threshold: SNR 低于此值时自动启用降噪
    
    Returns:
        处理后的音频路径
    """
    # 1. 估算 SNR
    snr = estimate_snr(input_wav)
    
    # 2. 决定是否降噪
    should_denoise = enable_denoise or (snr < snr_threshold)
    
    # 3. 应用轻量滤波（始终执行）
    temp_wav = input_wav.replace(".wav", "_filtered.wav")
    apply_light_filter(input_wav, temp_wav)
    
    # 4. 可选降噪
    if should_denoise:
        apply_denoise(temp_wav, output_wav)
    else:
        Path(output_wav).write_bytes(Path(temp_wav).read_bytes())
    
    return output_wav
```

**验收标准**：
- [ ] 轻量滤波不损伤语音质量
- [ ] 降噪开关可配置
- [ ] 低 SNR 音频识别率提升

---

### 阶段五：性能优化（生产就绪）

**目标**：优化整体性能，确保 3 小时视频处理时间 ≤ 3 小时

**工期**：1-2周

**实施内容**：

| 序号 | 任务 | 说明 | 优先级 |
|------|------|------|--------|
| 5.1 | 并行调用优化 | 低置信片段并行调用双模型 | 高 |
| 5.2 | 批量处理 | 减少模型加载开销 | 中 |
| 5.3 | GPU 内存优化 | 避免 OOM | 高 |
| 5.4 | 进度监控 | 实时显示处理进度 | 中 |
| 5.5 | 性能基准测试 | 建立性能测试套件 | 中 |

**核心代码结构**：

```python
# edge_service/tasks/speech.py 优化

import concurrent.futures
from typing import List


def _process_low_confidence_segments_parallel(
    segments: List[dict],
    wav_path: str,
    max_workers: int = 2
) -> List[dict]:
    """
    并行处理低置信片段
    同时调用 Whisper 和 SenseVoice
    """
    results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        
        for seg in segments:
            if _should_use_sensevoice(seg):
                # 提交 SenseVoice 任务
                future = executor.submit(
                    _transcribe_with_sensevoice,
                    wav_path,
                    seg["start"],
                    seg["end"]
                )
                futures[future] = seg
            else:
                results.append(seg)
        
        # 收集结果
        for future in concurrent.futures.as_completed(futures):
            seg = futures[future]
            try:
                sensevoice_text = future.result()
                aligned_seg = _align_segment_level(seg, sensevoice_text)
                results.append(aligned_seg)
            except Exception as e:
                # 回退到 Whisper 结果
                results.append(seg)
    
    # 按时间排序
    results.sort(key=lambda x: x["start"])
    return results
```

**验收标准**：
- [ ] 3 小时视频处理时间 ≤ 3 小时
- [ ] GPU 内存使用 < 8GB
- [ ] 无 OOM 崩溃

---

## 四、里程碑与时间线

```
┌─────────────────────────────────────────────────────────────────────┐
│  Week 1-2: 阶段一 MVP版本                                            │
│  ├─ 优化 VAD 参数                                                    │
│  ├─ 增强幻觉过滤                                                     │
│  ├─ 扩充教育词表                                                     │
│  └─ 建立测试基准                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  Week 3-5: 阶段二 SenseVoice集成                                     │
│  ├─ 模型集成与部署                                                   │
│  ├─ 条件调用逻辑                                                     │
│  ├─ 片段级对齐                                                       │
│  └─ 性能监控                                                         │
├─────────────────────────────────────────────────────────────────────┤
│  Week 6-8: 阶段三 字符级对齐                                         │
│  ├─ 编辑距离算法                                                     │
│  ├─ 时间分配策略                                                     │
│  ├─ 边界情况处理                                                     │
│  └─ 单元测试                                                         │
├─────────────────────────────────────────────────────────────────────┤
│  Week 9-10: 阶段四 音频预处理                                        │
│  ├─ SNR 估算                                                         │
│  ├─ 轻量滤波                                                         │
│  └─ 可选降噪                                                         │
├─────────────────────────────────────────────────────────────────────┤
│  Week 11-12: 阶段五 性能优化                                         │
│  ├─ 并行调用                                                         │
│  ├─ 批量处理                                                         │
│  └─ 性能测试                                                         │
└─────────────────────────────────────────────────────────────────────┘

总工期：10-12周
```

---

## 五、风险与缓解措施

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| SenseVoice 模型兼容性 | 部署失败 | 中 | 提前在目标环境测试 |
| 字符级对齐准确率不足 | 字幕时间错位 | 中 | 先用片段级对齐兜底 |
| 处理时间超标 | 无法满足性能要求 | 中 | 条件调用 + 并行优化 |
| 降噪损伤语音 | ASR 准确率下降 | 低 | 降噪设为可选 |
| GPU 内存不足 | OOM 崩溃 | 中 | 分批处理 + 内存监控 |

---

## 六、可复用模块清单

基于现有代码 `edge_service/tasks/speech.py`：

| 模块 | 函数 | 复用方式 |
|------|------|----------|
| VAD检测 | `_detect_audio_activity_regions` | 直接复用 |
| Silero VAD | `_silero_vad_regions` | 直接复用 |
| Whisper转写 | `_transcribe_wav` | 直接复用 |
| 幻觉过滤 | `_is_hallucination` | 直接复用 |
| 结构化幻觉过滤 | `_is_structured_hallucination` | 直接复用 |
| 规则纠错 | `_apply_corrections` | 直接复用 |
| 字幕输出 | `write_srt`, `write_vtt` | 直接复用 |
| 音频提取 | `extract_wav` | 直接复用 |
| 时间戳获取 | `get_audio_start_time` | 直接复用 |

---

## 七、新增模块清单

| 模块 | 文件 | 功能 |
|------|------|------|
| SenseVoice集成 | `speech.py` | 模型加载、推理 |
| 置信度判断 | `speech.py` | 决定是否调用双模型 |
| 片段级对齐 | `speech.py` | 文本替换 + 时间保留 |
| 字符级对齐 | `alignment.py` | 编辑距离 + 时间插值 |
| 音频预处理 | `audio_preprocess.py` | SNR估算、滤波、降噪 |
| 并行处理 | `speech.py` | 双模型并行调用 |

---

## 八、测试策略

### 8.1 单元测试

```python
# tests/test_alignment.py

def test_edit_operations_match():
    """测试完全匹配"""
    ops = compute_edit_operations("你好", "你好")
    assert all(op[0] == EditOp.MATCH for op in ops)


def test_edit_operations_replace():
    """测试替换"""
    ops = compute_edit_operations("红方蓝", "红黄蓝")
    assert ops[1][0] == EditOp.REPLACE


def test_edit_operations_insert():
    """测试插入"""
    ops = compute_edit_operations("今天气好", "今天天气好")
    assert any(op[0] == EditOp.INSERT for op in ops)


def test_align_character_level():
    """测试字符级对齐"""
    whisper_words = [
        {"word": "红", "start": 0.5, "end": 0.8},
        {"word": "方", "start": 0.8, "end": 1.1},
        {"word": "蓝", "start": 1.1, "end": 1.4},
    ]
    result = align_character_level(whisper_words, "红黄蓝")
    assert len(result) == 3
    assert result[0].char == "红"
    assert result[1].char == "黄"
    assert result[2].char == "蓝"
```

### 8.2 集成测试

```python
# tests/test_dual_model.py

def test_conditional_sensevoice_call():
    """测试条件调用逻辑"""
    # 高置信片段不应调用 SenseVoice
    high_conf_seg = {"avg_logprob": -0.5, "text": "这是测试"}
    assert not _should_use_sensevoice(high_conf_seg)
    
    # 低置信片段应调用 SenseVoice
    low_conf_seg = {"avg_logprob": -1.2, "text": "这是测试"}
    assert _should_use_sensevoice(low_conf_seg)


def test_segment_level_alignment():
    """测试片段级对齐"""
    whisper_seg = {
        "start": 0.0,
        "end": 2.0,
        "text": "红方蓝",
        "words": [...]
    }
    sensevoice_text = "红黄蓝"
    
    result = _align_segment_level(whisper_seg, sensevoice_text)
    assert result["text"] == "红黄蓝"
    assert result["start"] == 0.0
    assert result["end"] == 2.0
```

### 8.3 性能测试

```python
# tests/test_performance.py

def test_3hour_video_processing_time():
    """测试3小时视频处理时间"""
    video_path = "test_data/3hour_video.mp4"
    
    start_time = time.time()
    result = transcribe_video(video_path)
    elapsed = time.time() - start_time
    
    # 处理时间应 ≤ 3小时
    assert elapsed <= 3 * 3600
```

---

## 九、总结

本方案将双模型ASR优化拆分为5个阶段：

1. **MVP版本**：优化现有模块，建立测试基准
2. **SenseVoice集成**：条件调用，片段级对齐
3. **字符级对齐**：精细化时间戳映射
4. **音频预处理**：可选降噪增强
5. **性能优化**：并行处理，生产就绪

**核心设计原则**：
- 职责拆分而非投票融合
- 条件调用而非全量双跑
- 渐进对齐而非一步到位
- 可选降噪而非强制处理

**预期收益**：
- 中文识别准确率提升 15-25%
- 幻觉内容减少 50%
- 字幕时间精度提升
- 处理时间可控

**总工期**：10-12周
