# 先下载全部分段、后集中处理 — 完整实现方案与阶段拆分

> 版本: 2026-04-24  
> 适用范围: `edge_services` 中 `CameraTask` 的 `DOWNLOAD` 链路  
> 文档定位: 面向“**先把一个任务下的分段都下载下来，下载完成后再对这些分段进行集中处理**”这一需求，给出完整实现思路、约束、修改点、阶段拆分与未决问题。  
> 原则: **根本修复优先**。不依赖末尾兜底掩盖前面的错误识别或错误路径选择。

---

## 一、初始沟通的真实需求

这轮需求的起点，不是泛化的“调度优化”，而是当前单个 `CameraTask` 的下载链路效率过低。

### 1.1 当前链路的实际执行方式

当前 `run_camera_task` 的下载链路是：

1. 下载第 1 个分段
2. 立刻检测第 1 个分段
3. 第 1 个分段有问题就立刻修复
4. 再下载第 2 个分段
5. 再检测 / 修复第 2 个分段
6. 所有分段都完成后再合并
7. 合并后如果发现问题，还会继续做额外修复 / 兜底

也就是说，当前是：

**下载一个分段 → 立刻处理一个分段 → 全部分段处理完再合并 → 合并后还可能继续修**

### 1.2 你明确提出的目标

目标是改成：

**先把一个任务下的所有分段都下载完成，再对这些分段做集中处理。**

也就是目标链路应变成：

1. 只做分段下载
2. 所有 raw 分段下载完成
3. 进入集中处理阶段
4. 逐个分段做检测 / 分类 / 修复
5. 全部分段处理完成后再合并
6. 最后生成源视频并进入后续转码

这套模型的直接收益还包括：

- 尽快释放 NVR / SDK session
- 让新的下载型任务更早进入下载阶段
- 把后处理收敛成本地 CPU / 磁盘任务，便于排队和限流

### 1.3 你明确给出的约束

这部分是方案必须遵守的硬约束：

#### 约束 A：清理规则

- 只有**最终文件产出完成**后，才能删除分段文件和中间状态文件
- 任何异常时，**源分段文件不能删除**
- 暂停任务后，**不能删除源分段文件**
- 终止任务后，**需要删除源分段文件和中间状态文件**

#### 约束 B：集中处理阶段的进度与耗时

- 所有分段下载完成后，进入集中处理时，进度提示要清晰显示：
  - 当前处理到第几个文件
  - 一共有几个文件
- 文件处理耗时要单独统计
- 处理耗时要并入最终总耗时 / 任务踪迹耗时

#### 约束 C：集中处理阶段的生命周期能力

- 集中处理阶段必须支持**暂停**
- 集中处理阶段必须支持**继续执行**

#### 约束 D：重新处理能力

- 集中处理很长，一旦处理逻辑修复后，不要重新下载已经下载好的分段
- 需要新增一个按钮 icon：**【重新处理】**
- 这个动作只针对**已下载的分段视频**重新执行集中处理
- 点击后要：
  - 清掉已产生的中间状态文件
  - 只保留 raw 分段视频
  - 按已有处理逻辑重新执行集中处理

### 1.4 你随后又给出的 4 个关键纠偏点

这 4 点决定了方案不能只写“怎么拆流程”，还必须回答“为什么这么拆、哪些旧逻辑不该继续保留”。

#### 纠偏 1：不要再写“末尾兜底一轮时长修正”作为主方案

你的观点是正确的：

- 都已经进入集中处理了
- 如果处理完、合并完，还要在末尾再兜一轮
- 说明前面的问题探测 / 分类 / 修复路径不够精确

因此主方案必须是：

**把问题识别、分类、修复路径做精确，而不是把末尾兜底当成标准流程。**

#### 纠偏 2：“单段归一化”必须说清楚是什么意思

这不是一个抽象概念，必须在方案里明确：

- 它不是“每个分段都一定要重编码”
- 它的真实含义应是：
  - 对 raw 分段做检测
  - 只有异常分段才生成派生产物用于后续合并
  - 正常分段直接复用 raw 分段本身

#### 纠偏 3：不能把“通道不一致”留到 merge 前再发现

你的判断也是对的：

- 如果下载时没有限制好通道
- 让错误分段都下载成功了
- 最后 merge 前才报“channel_used 不一致”
- 本质上是在用后置失败掩盖前置约束缺失

因此真正正确的方向是：

**在下载阶段就把通道限制好。merge 前的通道校验只能是断言，不应是主防线。**

#### 纠偏 4：要先把异常样本识别和修复路径修精准

这是整个方案能否落地的前提：

- 如果下载异常样本的识别不准
- 如果 `ffmpeg` 修复路径分流不准
- 那集中处理只是把错误更系统地执行一遍

所以最终方案必须同时包含：

1. **先下载全部分段，再集中处理** 的执行架构
2. **下载异常样本识别 / 修复路径精确化** 的前置改造

---

## 二、当前实现现状与真实缺口

## 2.1 当前 `CameraTask` 下载阶段的真实行为

当前核心逻辑在：

- `edge_service/tasks/camera.py`
  - `run_camera_task`
  - `_normalize_download_part`
  - `_reconcile_download_resume_parts`
  - `_normalize_source_video`
  - `_concat_parts`
- `edge_service/video/hik/download.py`
  - `_sdk_download_with_retry`
  - `download_by_time_with_session`

当前 `run_camera_task` 中的关键事实：

- 分段下载循环里，**每个分段下载完就立即调用** `_normalize_download_part`
- `.done` 元数据里已经写入：
  - `channel_used`
  - `record_type_used`
  - `merge_part_path`
  - `normalize_reason`
  - `normalize_classification`
- 所有分段下载并即时处理后，再执行 merge
- merge 完成后，当前代码**会直接清理 `part*.mp4` 和派生产物**
- merge 之后还会调用 `_normalize_source_video` 对源视频继续做结构 / 时长 / faststart 相关处理

### 2.2 这和目标方案的冲突点

当前实现与目标需求存在这些直接冲突：

#### 冲突 1：分段是“边下边修”，不是“先全下后处理”

这会导致：

- 下载阶段夹杂大量本地 `ffmpeg` 处理
- NVR 下载窗口占用时间被本地处理拉长
- 整体吞吐和定位效率都差

#### 冲突 2：raw 分段在 merge 后就会被删除

这和你的硬约束不一致。

你要求的是：

- **最终文件真正产出完成前，不删 raw 分段**
- **任何异常不删 raw 分段**

而当前代码在 merge 成功后就开始清理 `part*.mp4`，这会带来两个问题：

- 后续 `_normalize_source_video` 如果失败，raw 分段已丢失
- 后续如果要“重新处理”，已经没有原始分段可用

#### 冲突 3：当前 `.done` 语义混合了“下载完成”和“处理完成”

这在现方案里还能勉强工作，但在目标方案里会出问题。

因为目标方案里必须区分：

- raw 分段是否下载完成
- 该分段是否已进入集中处理
- 该分段是否已处理完成
- 该分段的 merge 输入文件是什么

更准确地说，当前 `.done` 更适合只承担“raw 源分段已下载完成”的职责；
集中处理进度、`merge_part_path`、处理耗时、处理状态更适合独立放进 `process_state.json`。

#### 冲突 3.1：当前链路仍存在“修复产物覆盖 raw 分段”的风险

目标方案里必须坚持一条硬规则：

- **raw `part*.mp4` 只作为原始证据，不允许被就地覆盖**

原因很直接：

- 后处理 bug 修复后，需要基于同一批 raw 分段重跑
- 异常复盘需要保留原始证据
- “重新处理”动作的前提就是 raw 分段仍然存在

所以后处理产物必须和 raw 分段分离，例如：

- raw：`partNNN.mp4`
- 派生处理产物：`partNNN.fixed.mp4`、`partNNN.norm.mp4`
- 集中处理状态：`process_state.json`

#### 冲突 4：当前没有“只重处理、不重下载”的独立动作

当前现有动作是：

- `try_retry_task`
- `try_rerun_task`

其中：

- `DOWNLOAD` 的 `retry` / `rerun` 会走 `_stop_and_reset_task`
- `_stop_and_reset_task` 会删除 `part*.mp4`、`.done`、merged、norm、aligned、faststart 等

这不满足“保留 raw 分段、只重新处理”的需求。

#### 冲突 5：当前 pause/resume 是围绕步骤做的，不是围绕“下载子阶段”做的

当前 `CameraTask` 只有两个显式 step：

- `DOWNLOAD`
- `TRANSCODE`

如果要实现集中处理，必须明确：

- 是在 `DOWNLOAD` 步骤内部增加子阶段
- 还是新增一个独立的 `PROCESS` 步骤

这是方案设计的关键点。

---

## 三、推荐的总体架构

## 3.1 推荐方案：先不改 DB 大步骤，先在 `DOWNLOAD` 内部拆子阶段

推荐第一版不要把 `CameraTask` 大步骤从 `DOWNLOAD -> TRANSCODE` 改成 `DOWNLOAD -> PROCESS -> MERGE -> TRANSCODE`。

原因：

- 当前 pause / resume / stop / rerun / retry / follow-up CourseTask 都围绕现有 step 模型实现
- 如果直接新增 DB 大步骤，改动面太大
- 第一版完全可以把“集中处理”做成 `DOWNLOAD` 内部的**子阶段**

### 3.2 推荐的 `DOWNLOAD` 子阶段模型

第一版建议把 `DOWNLOAD` 步骤拆成这些运行时子阶段：

1. `SEGMENT_DOWNLOAD`
2. `SEGMENT_PROCESS`
3. `MERGE`
4. `SOURCE_FINALIZE`

说明：

- **`SEGMENT_DOWNLOAD`**
  - 只负责把 raw `partNNN.mp4` 下载下来
  - 不做即时修复
- **`SEGMENT_PROCESS`**
  - 所有分段下载完成后，逐个做检测 / 分类 / 修复
- **`MERGE`**
  - 使用集中处理后的 merge 输入文件合并
- **`SOURCE_FINALIZE`**
  - 只做必要的最终提交 / 封装 / 最小必要校验
  - 不再把大范围兜底修复当成常规流程

### 3.3 为什么推荐先保持在 `DOWNLOAD` 步骤内部

这样做的好处：

- 现有 `try_pause_task(server_id, step=DOWNLOAD)` 可以继续沿用
- 现有 `try_resume_task(server_id, step=DOWNLOAD)` 可以继续沿用
- 现有 `StepStatus` / `TaskStatus` 基本不用大改
- 现有进度上报模型能复用
- 现有 `CourseTask` 启动依赖不用重写

### 3.4 长期是否要拆成独立大步骤

长期可以讨论，但第一版不建议。

如果后续要做，也应在第一版稳定后再评估。

---

## 四、目标执行流程

## 4.1 目标流程总览

```text
CameraTask.DOWNLOAD
  ├─ Phase A: 全部分段下载
  ├─ Phase B: 集中处理所有已下载分段
  ├─ Phase C: 合并
  └─ Phase D: 最终源视频提交 / 最小必要封装

CameraTask.TRANSCODE
  └─ 维持现有 HLS 转码
```

## 4.2 Phase A：全部分段下载

这一阶段只做：

- 分段时间范围计算
- 通道选择 / 锁定
- raw 分段下载
- 下载级基础校验
- 记录“源分段已就绪”的最小必要元数据

这一阶段**不做**：

- `_normalize_download_part`
- 分段时间线修复
- 分段对齐修复
- merge 输入派生产物生成

### Phase A 结束条件

满足以下条件后，才进入集中处理：

- 计划内所有 raw `partNNN.mp4` 已下载完成
- 每个分段至少通过下载级基础可用性校验
- 通道锁定一致性已经在下载过程中成立

### Phase A 的产物

每个分段至少有：

- raw 文件：`partNNN.mp4`
- 下载完成标记：`.done` 或等价 metadata 中记录
  - `range_start`
  - `range_end`
  - `size_bytes`
  - `channel_used`
  - `record_type_used`
  - `mapping_source`
  - `download_state=success`

这里建议明确约束 `.done` 的职责：

- **只表示 raw 源分段已下载完成**
- 不再把集中处理结果继续混写进 `.done`

集中处理阶段另设：

- `process_state.json`
  - `phase`
  - `total_parts`
  - `processed_parts`
  - `merge_part_paths`
  - 每个 part 的处理状态
  - 下载耗时 / 处理耗时累计值

### 4.2.1 什么是 raw 分段视频

这里把概念明确一下，避免后续实现时口径不统一。

`raw` 分段视频指的是：

- 按任务时间范围切出来的**原始下载结果**
- 文件名形态通常是：`partNNN.mp4`
- 它代表 NVR / SDK 在该时间片段内返回的原始视频文件
- 它允许带有原始时间线、原始音视频起始偏差、原始封装问题
- 它不是“已修复视频”，也不是“最终 merge 输入”这个概念本身

也就是说，`raw part*.mp4` 的职责是：

- 作为原始证据保留
- 作为集中处理阶段的输入样本
- 作为后续重新处理、异常复盘、修复回归验证的基础数据

它**不应该**承担：

- 被就地覆盖为修复后版本
- 被直接当作“该分段一定已经可安全 merge”的承诺

### 4.2.2 从下载完成到处理结束，一共会出现哪些视频文件

按当前代码行为和目标方案收敛后，下载到处理结束这段链路里，视频文件建议明确分为以下几类：

#### A. 原始分段视频

- 文件形态：`partNNN.mp4`
- 来源：NVR / SDK 下载结果
- 用途：
  - 作为集中处理输入
  - 作为异常复盘依据
  - 作为“重新处理”保留对象

#### B. 分段修复产物

- 文件形态：`partNNN.fixed.mp4`
- 来源：单段分析后触发时间线重建、音视频对齐回退重建、结构修复等产物
- 用途：
  - 替代 raw 分段参与 merge
  - 保留单段修复结果，避免覆盖 raw 分段

#### C. 分段临时处理中间文件

- 文件形态：
  - `partNNN.fixed.timeline.tmp.mp4`
  - `partNNN.fixed.avsync.tmp.mp4`
  - `partNNN.timeline.tmp.mp4`
- 来源：单段时间线重建、音视频无损对齐、原子替换前的临时文件
- 用途：
  - 保证处理中断或文件占用时不破坏原文件
  - 支撑 finalize pending / 原子替换

#### D. 合并产物与合并修复产物

- 文件形态：
  - `*.merged.mp4`
  - `*.repaired.mp4`
- 来源：merge 阶段 concat 产物及少量保守修复产物
- 用途：
  - 作为 merge 阶段输出
  - 在过渡期内承接极少量结构性修复

#### E. 源视频 finalize 阶段派生产物

- 文件形态：
  - `*.timeline.mp4`
  - `*.aligned.mp4`
  - `*.faststart.mp4`
- 来源：source finalize 阶段的时间轴归零、时长裁剪/补齐、faststart 重封装
- 用途：
  - 生成最终 `source_video`
  - 保证最终封装可交付

#### F. 最终源视频

- 文件形态：任务最终 `downloaded` 源视频文件
- 来源：merge 完成并经 source finalize 后提交成功的最终文件
- 用途：
  - 作为 `DOWNLOAD` 步骤权威产物
  - 作为后续 `TRANSCODE` 输入

### 4.2.3 哪些文件保留，哪些文件只应短暂存在

建议规则固定为：

- 长期保留到任务最终成功前：
  - `partNNN.mp4`
  - `.done`
  - `process_state.json`
- 按需保留到本轮处理结束：
  - `partNNN.fixed.mp4`
  - `*.merged.mp4`
  - `*.timeline.mp4`
  - `*.aligned.mp4`
  - `*.faststart.mp4`
- 应尽快清理的临时文件：
  - `*.timeline.tmp.mp4`
  - `*.avsync.tmp.mp4`

核心原则还是：

- **raw 分段是证据，不能轻易删，也不能覆盖**
- 派生产物是处理结果，可以按生命周期清理
- 临时文件只是过程态，完成后应及时清理

## 4.3 Phase B：集中处理所有已下载分段

这一阶段才进入真正的“检测 / 分类 / 修复”。

### 这一阶段要做什么

对每个 raw `partNNN.mp4`：

1. 做结构和时间线检测
2. 做异常分类
3. 选择对应修复路径
4. 如果需要，生成派生 merge 输入文件
5. 更新 `process_state.json`

### 这一阶段**不应该**做什么

- 不应该为了“兜住所有情况”在末尾再盲目多跑一轮大修复
- 不应该把分类不清的问题推迟到 merge 阶段
- 不应该覆盖 raw `partNNN.mp4`

### “单段归一化”在目标方案中的准确定义

这里需要明确回答你问的“单段归一化是什么意思”：

在本方案里，它的准确定义应该是：

- 对单个 raw 分段做检测与必要修复
- 产物是一个**可供 merge 使用的输入文件**
- 它可以是：
  - raw 分段本身
  - 也可以是派生的 `.fixed.mp4` / `.norm.mp4`

因此：

- **不是每个分段都一定要归一化**
- **不是每个分段都一定要重编码**
- 正常分段应直接复用 raw 分段
- 只有异常分段才生成派生产物
- 派生产物必须独立命名，不能回写覆盖 raw 分段

### 4.3.1 新流程对现有异常检测逻辑的原则

这一点必须明确写死：

- **这次优化只是把执行顺序从“边下边修”改成“先下完再集中处理”**
- **不是删减现有异常判断、不是减少修复分支、不是放宽原有检测条件**

因此第一版迁移原则必须是：

- 现有 `_normalize_download_part` 中已经在跑的所有检测逻辑，**全部迁移到集中处理阶段继续执行**
- 现有 merge 后的结构检测、边界解码检测、源视频 finalize 检测，**全部保留**
- 在“模块拆分”完成前，允许实现形态变化，但**判断口径和修复覆盖范围不能缩水**

也就是说，新流程不是：

- 下载完后只做更少的判断
- 或者只保留最常见的几类异常

而是：

- 下载完后，把当前所有已存在判断场景**完整搬过去**
- 先做到“能力等价”
- 再在后续阶段继续优化和收缩末尾兜底

### 4.3.2 单段阶段必须完整承接的现有判断场景

按当前 `camera.py` 的真实逻辑，单段集中处理阶段至少要完整保留这些判断与分流：

#### A. 时间线正常，直接复用 raw 分段

- 对应当前：`CAM-DL-NORM-000`
- 判断基础：
  - `timeline_dirty = false`
  - 音频晚到不超过阈值
  - 音视频起始基本对齐
- 处理结果：
  - 直接使用 raw `partNNN.mp4` 参与后续 merge

#### B. 强异常时间线 / 绝对起点异常 / 明显音频晚到

- 对应当前：`CAM-DL-NORM-010`
- 判断基础：
  - `significant_audio_late`
  - 或 `absolute_start_abnormal`
- 处理结果：
  - 执行完整时间线重建
  - 输出 `partNNN.fixed.mp4`
  - 后续要求 merge 走更保守路径
  - 修复后再做结构有效性复检

#### C. 轻微音频晚到 / 轻微脏时间线但风险可接受

- 对应当前：`CAM-DL-NORM-020`
- 判断基础：
  - `minor_audio_late`
  - 或 `timeline_dirty` 但音视频对齐、且不存在绝对起点强异常
- 处理结果：
  - 不修复
  - 继续使用 raw 分段

#### D. 明显 stream gap / 分段时间线异常

- 对应当前：`CAM-DL-NORM-030`
- 判断基础：
  - `timeline_dirty = true`
  - `stream_gap > TIMELINE_GAP_TOLERANCE_SECONDS`
  - 同时不属于“音频早到需要优先走对齐”的场景
- 处理结果：
  - 完整重建时间线
  - 输出 `partNNN.fixed.mp4`

#### E. 音频早于视频但偏差很小

- 对应当前：`CAM-DL-NORM-040`
- 判断基础：
  - `0 < audio_early_by <= AUDIO_EARLY_ALIGN_THRESHOLD_SECONDS`
- 处理结果：
  - 跳过修复
  - 继续使用 raw 分段

#### F. 音频明显早于视频，需要无损对齐

- 对应当前：`CAM-DL-NORM-050`
- 判断基础：
  - `audio_early_by > AUDIO_EARLY_ALIGN_THRESHOLD_SECONDS`
- 处理结果：
  - 先尝试 `itsoffset` 无损对齐
  - 成功则输出 `partNNN.fixed.mp4`

#### G. 无损对齐失败 / 异常，回退完整重建

- 对应当前：`CAM-DL-NORM-051`、`CAM-DL-NORM-052`
- 判断基础：
  - `itsoffset` 调用失败
  - 或执行过程中出现异常
- 处理结果：
  - 回退到完整时间线重建
  - 输出 `partNNN.fixed.mp4`

### 4.3.3 merge 阶段必须保留的现有判断场景

新的集中处理流程上线后，merge 阶段仍然要保留当前已有的这些检测，但角色从“主修复入口”降为“防御性校验 + 过渡期保守修复”：

#### A. merged 输出结构异常检测

- 当前检测函数：`_probe_structural_media_issue`
- 当前覆盖场景包括：
  - `video_packet_duration_abnormal`
  - `audio_packet_duration_abnormal`
  - `stream_end_gap_abnormal`
  - `duration_mismatch_abnormal`

#### B. merge 边界解码异常检测

- 当前检测函数：`_probe_merge_boundary_decode_issue`
- 当前覆盖场景包括：
  - `boundary_decode_abnormal`
  - `invalid data found when processing input`
  - `error submitting packet to decoder`
  - `missing picture in access unit`
  - `no frame`
  - `decoding error`

#### C. merge 路径分流与兜底链

- 当前代码仍保留多条 merge 路径：
  - canonical merge
  - direct concat copy
  - normalized MP4 concat
  - TS fallback
  - merged output final repair

新流程第一版必须做到：

- 这些检测和路径**先完整保留**
- 只是不再把它们当成主要问题识别入口

### 4.3.4 source finalize 阶段必须保留的现有判断场景

`_normalize_source_video` 当前仍承担以下判断和修复动作，新流程第一版不能丢：

#### A. 源视频结构异常先完整重建

- 对应当前：`CAM-DL-SRC-020`、`CAM-DL-SRC-021`

#### B. 源视频时间轴预校准

- 对应当前：`CAM-DL-SRC-010`、`CAM-DL-SRC-011`、`CAM-DL-SRC-030`

#### C. 源视频按目标时长裁剪 / 补齐

- 对应当前：`CAM-DL-SRC-040`、`CAM-DL-SRC-041`

#### D. faststart 重封装

- 对应当前：`CAM-DL-SRC-050`、`CAM-DL-SRC-051`

因此第一版要求是：

- **Phase B 承接原先单段检测修复**
- **Phase C / D 继续保留当前 merge 与 finalize 检测**
- **任何现有判断场景都不能因为流程调整而被遗漏**

## 4.4 Phase C：merge

merge 阶段只接受 Phase B 产出的最终 merge 输入序列。

### merge 阶段的定位

它的职责应该被收敛为：

- 按既定输入列表执行合并
- 做结构合法性验证
- 在失败时给出明确失败落点

它**不应该**继续承担：

- 前面没识别清楚的异常再分类
- 前面没修好的问题再统一兜底补救
- 通道不一致的首次发现点

### 关于“merge 前检查 channel_used 是否一致”

这块要按你的要求重新定位：

- 正确的主防线在下载阶段
- merge 前检查只保留为**防御性断言**
- 它的意义是：
  - 防止错误内容静默混入
  - 不是把下载约束推迟到 merge 再失败

也就是说：

- **短期**保留它，作为最后一道断言
- **中期**在下载锁定稳定后，把它降成“理论上不该触发的告警 / 断言”

## 4.5 Phase D：source finalize

这一阶段只保留最小必要动作：

- 最终文件提交
- 必要的封装优化
- 必要的结构确认

### 这一阶段不再作为主修复舞台

你指出的“末尾再兜底一轮时长修正”问题，这里要明确落下来：

- 第一版过渡期里，某些现有兜底逻辑可能暂时还需要保留
- 但它们的定位只能是：
  - 过渡期保护
  - 断言前的保守兜底
- **不能继续写成主方案的一部分**

最终方向应是：

- 大部分问题在 Phase B 识别并解决
- Phase D 不再承担“第二战场”的角色

---

## 五、清理规则设计

这部分必须严格对齐你的原始约束。

## 5.1 成功完成时

只有在以下条件全部满足后，才能清理 raw 分段：

- merge 成功
- 最终源视频已生成
- 最终源视频已完成必要提交 / 封装
- `DOWNLOAD` 步骤最终状态成功

### 成功后允许删除的内容

- raw `part*.mp4`
- `.done`
- `.fixed.mp4`
- `.timeline.tmp.mp4`
- `.avsync.tmp.mp4`
- `.merged*`
- `.norm*`
- 其他 processing / merge 中间态

## 5.2 任意异常时

任意异常时：

- **不删除 raw `part*.mp4`**
- 可删除本次未完成的临时 `.tmp` 文件
- 可删除明显无效的半成品派生产物

### 目标

保证异常后仍能：

- 复盘样本
- 重试处理
- 执行“重新处理”
- 不必重新占用 NVR 下载

## 5.3 暂停时

暂停时：

- **不删除 raw `part*.mp4`**
- 已完成处理的 metadata 保留
- 当前未完成 `.tmp` 可按安全规则清理
- 下次 resume 时从子阶段断点继续

## 5.4 终止时

终止时：

- 删除 raw `part*.mp4`
- 删除 `.done`
- 删除所有派生产物
- 删除 merged / final source / transcode 相关中间态

这与现有 `_stop_and_reset_task` 的总体方向一致，但要覆盖新的集中处理产物范围。

## 5.5 当前代码与目标规则的差异

当前代码中，merge 成功后就会清理 `part*.mp4`。

这与目标规则冲突，必须调整为：

- 先完成 `SOURCE_FINALIZE`
- 确认最终源视频成功
- 再统一清理 raw 分段与中间态

---

## 六、暂停 / 继续 / 终止 / 重试 / 重新执行 / 重新处理

## 6.1 暂停与继续

### 推荐方案

集中处理继续放在 `DOWNLOAD` 步骤内部，因此：

- `try_pause_task(step=DOWNLOAD)` 继续沿用
- `try_resume_task(step=DOWNLOAD)` 继续沿用

但要补齐一个能力：

- 当前 `DOWNLOAD` 不只是“正在下载分段”
- 还可能是：
  - 正在集中处理第 N 个分段
  - 正在 merge
  - 正在 source finalize

因此必须把**下载子阶段**和**当前处理索引**持久化。

### 建议持久化的信息

建议保留“下载断点状态”和“集中处理状态”两类文件，避免语义混杂：

- `download_state.json`
  - 负责下载断点、已下载分段集合、下载子阶段基础信息
- `process_state.json`
  - 负责集中处理 / merge / finalize 的阶段状态与进度

其中 `process_state.json` 至少记录：

- `started_at_epoch`
- `subphase`
  - `segment_download`
  - `segment_process`
  - `merge`
  - `source_finalize`
- `current_part_idx`
- `processed_parts`
- `total_parts`
- `mode`
  - `normal`
  - `process_only`
- `merge_part_paths`
- `download_elapsed_seconds`
- `process_elapsed_seconds`

### resume 行为

resume 时按子阶段恢复：

- 如果暂停在 `segment_download`
  - 继续下载剩余 raw 分段
- 如果暂停在 `segment_process`
  - 从当前未完成 / 待重处理分段继续
- 如果暂停在 `merge`
  - 重新进入 merge
- 如果暂停在 `source_finalize`
  - 从 finalize 继续

## 6.2 终止

终止逻辑整体仍可沿用当前 `try_stop_task(step=DOWNLOAD)` → `_stop_and_reset_task` 的路线。

但要补齐：

- 新增集中处理阶段产生的派生产物清理
- 新增 process-only 模式下的状态重置

## 6.3 重试与重新执行

当前已有：

- `retry`
- `rerun`

它们的定位应继续保持：

### `retry`

- 面向失败态步骤
- 当前 `DOWNLOAD retry` 本质是“重置后重下”

### `rerun`

- 面向成功态步骤
- 当前 `DOWNLOAD rerun` 本质是“清空现有产物后重新下载并重跑整个链路”

这两个动作都**不能替代**你要的“重新处理”。

## 6.4 新增动作：`重新处理`

这是本需求新增的核心动作。

### 语义定义

`重新处理` =

- **不重新下载 raw 分段**
- 清除 processing / merge / source finalize 相关中间态
- 只保留 raw `part*.mp4`
- 从 `SEGMENT_PROCESS` 子阶段重新开始

### 推荐新增接口

建议新增独立动作，而不是复用 `retry` / `rerun`：

- `try_reprocess_task(server_id=..., step_code="DOWNLOAD")`

### 为什么不能复用现有 `rerun DOWNLOAD`

因为现有 `rerun DOWNLOAD` 会走 `_stop_and_reset_task`：

- 会删除 `part*.mp4`
- 会把整个下载链路恢复成“从零开始”

这和“只重处理已下载分段”的目标相反。

### `重新处理` 的执行规则

点击【重新处理】时：

1. 校验 raw `part*.mp4` 至少存在一组可用分段
2. 删除以下内容：
   - `.done`
   - `.fixed*`
   - `.timeline.tmp*`
   - `.avsync.tmp*`
   - `.merged*`
   - `.norm*`
   - `.aligned.mp4`
   - `.faststart.mp4`
   - 最终 source mp4
   - `download_state.json`
3. 保留：
   - raw `part*.mp4`
4. 以 `mode=process_only` 重新启动 `DOWNLOAD`
5. 跳过 NVR 下载，直接进入 `SEGMENT_PROCESS`

### 按钮展示时机

第一版建议按钮只在以下条件下展示：

- 任务类型为 `CameraTask`
- raw `part*.mp4` 存在
- 当前不处于 `SEGMENT_DOWNLOAD` 正在下载中

更具体地说，可先保守支持这些状态：

- `DOWNLOAD FAILED`
- `DOWNLOAD PAUSED`
- `DOWNLOAD SUCCESS` 但需要重做处理

至于是否允许在“正在集中处理时”直接点【重新处理】，属于 UX 细节，建议作为待确认项。

---

## 七、进度提示与耗时统计

## 7.1 进度条展示要求

集中处理阶段必须体现：

- 当前处理到第几个分段
- 总共有几个分段
- 当前子阶段名称
- 当前阶段耗时
- 累计总耗时

### 推荐文案示例

#### 分段下载阶段

- `正在下载分段 3/12，已下载 146MB/420MB，本段耗时 18 秒，总耗时 2 分 31 秒`

#### 集中处理阶段

- `正在集中处理第 3/12 段：检测分段结构，本段耗时 4 秒，总耗时 6 分 18 秒`
- `正在集中处理第 3/12 段：检测到音频早于视频 0.8s，正在执行无损对齐，本段耗时 7 秒，总耗时 6 分 21 秒`

#### merge 阶段

- `分段集中处理完成，正在合并视频（12 段），本次耗时 26 秒，总耗时 8 分 14 秒`

#### source finalize 阶段

- `视频合并完成，正在提交最终源视频，本次耗时 9 秒，总耗时 8 分 23 秒`

## 7.2 进度数值的推荐拆分

第一版推荐把 `DOWNLOAD` 步骤的 0~100% 按子阶段拆成：

- `0.00 ~ 0.78`：全部分段下载
- `0.78 ~ 0.94`：集中处理分段
- `0.94 ~ 0.98`：merge
- `0.98 ~ 1.00`：source finalize

这样做的原因：

- 下载仍是用户最直观看到的主过程
- 集中处理有明确进度空间，不会一直卡在 96% 不动
- merge / finalize 有独立的尾部空间

数值本身可以微调，但必须满足：

- 集中处理阶段有清晰可见的进度推进
- 不是“下载完成后一直卡在 97%”

## 7.3 耗时统计

当前 `DOWNLOAD` 已经有统一总耗时起点，可继续复用。

第一版建议：

- 整个 `DOWNLOAD` 步骤共用一个总耗时计时器
- 集中处理每个分段单独统计“本段处理耗时”
- 最终完成文案中，把下载耗时 + 处理耗时 + merge 耗时都并入总耗时

同时建议在状态文件里把两类耗时拆开累计：

- `download_elapsed_seconds`
- `process_elapsed_seconds`

这样 pause / resume / reprocess 后仍能稳定恢复展示口径。

---

## 八、异常识别、修复路径与“不要靠末尾兜底”

这一节是本方案必须和“先下载后集中处理”一起落地的另一半。

## 8.1 下载阶段要先把错误源头收紧

当前通道候选来源包括：

- `persisted`
- `hint`
- `task_locked`
- `scanned`

第一版就必须先把这件事修正：

- `scanned` 不能直接成为任务级锁定
- 低可信候选只能作为当前分段的观察结果
- 任务级锁定只能由高可信来源或通过追加验证后提升的来源产生

否则集中处理只是把错误通道的分段更系统地处理一遍。

### 8.1.1 后续通道锁定修复对本方案的影响

后续已修复的通道锁定逻辑不改变“先集中下载、再集中处理”的主线，反而是 Phase A 可以成立的前置条件。

当前实现已经具备以下基础：

- 下载层已经支持任务级 `locked_channel` / `locked_record_type`
- 下载层已经支持 `excluded_channels`，用于在当前任务内排除已确认失败的历史映射
- `DownloadResult` 已经返回：
  - `channel_used`
  - `persisted_channel`
  - `persisted_failed`
  - `mapping_source`
- `camera.py` 已经维护任务级：
  - `_task_locked_channel`
  - `_task_locked_record_type`
  - `_persisted_fail_count`
  - `_disabled_history_channels`
  - `_part_channel_used`
- `.done` 已经写入 `channel_used` / `record_type_used`
- merge 前已经有 `channel_used` 一致性检查

这些能力应被纳入 Phase A 的正式设计，而不是被视为临时修补。

### 8.1.2 Phase A 必须维护的任务级下载上下文

集中下载不是“每段完全独立下载”，而是一个任务内共享上下文驱动的连续过程。

Phase A 至少需要维护：

- `locked_channel`
  - 首个可信成功分段锁定的 SDK 通道
- `locked_record_type`
  - 与锁定通道配套的录像类型
- `mapping_source`
  - 当前分段通道来源，区分 `persisted` / `hint` / `task_locked` / `scanned`
- `persisted_failed`
  - 历史可信映射在当前任务内是否失败
- `disabled_history_channels`
  - 当前任务内被临时禁用的历史映射通道
- `part_channel_used`
  - 每个 raw 分段实际使用的 SDK 通道
- `shared_download_session`
  - 多分段任务复用的下载 session

这些字段的职责是把“通道一致性”前移到下载阶段，而不是留给 merge 阶段首次发现。

### 8.1.3 `scanned` 命中不能无条件提升为任务锁

这里需要特别明确：

- `persisted` / `hint` / `task_locked` 属于相对高可信来源
- `scanned` 属于低可信探索结果

因此推荐规则是：

- `persisted` / `hint` 成功命中后，可以作为任务级锁定来源
- `task_locked` 只表示已经进入锁定态后的复用
- `scanned` 成功默认只证明“当前分段可下载”
- `scanned` 若要提升为任务级锁定，必须经过额外验证

可选的提升验证包括：

- 下载文件大小满足最小阈值
- 下载时长接近期望时长
- 媒体结构可被 `ffprobe` 正常识别
- 时间线不存在明显强异常
- 必要时用相邻小窗口再次验证同一通道

第一版可以先采用保守策略：

- 允许 `scanned` 结果参与当前分段下载成功判断
- 不允许未经验证的 `scanned` 直接写成长期可信映射
- 不允许未经验证的 `scanned` 静默污染后续任务

## 8.2 分段处理阶段要把“样本 → 分类 → 路径”收精确

集中处理不是把原来的 `_normalize_download_part` 平移一下就结束了。

必须额外梳理清楚：

- 哪些样本属于时间线强异常
- 哪些属于音频早 / 晚
- 哪些属于结构损坏
- 哪些只能 fail fast
- 哪些允许进入无损路径
- 哪些允许进入完整重建路径

### 目标

让这些信息形成稳定闭环：

- `classification`
- `normalize_reason`
- 运行时错误码
- 最终 merge 输入文件类型

### 8.2.1 Phase B 的输出不能只有 `merge_part_path`

集中处理阶段要为 merge 提供完整决策上下文。

每个分段处理完成后，至少应输出：

- `part_idx`
- `raw_part_path`
- `merge_part_path`
- `classification`
- `normalize_reason`
- `force_canonical_merge`
- `canonicalize_merge_part`
- `merge_risk_level`
- `processed_at_epoch`

其中：

- `raw_part_path` 永远指向原始 SDK 导出的 `partNNN.mp4`
- `merge_part_path` 指向实际参与 merge 的输入文件
- 正常分段的 `merge_part_path` 可以等于 `raw_part_path`
- 异常分段的 `merge_part_path` 应指向 `.fixed.mp4` / `.norm.mp4` 等派生产物

这样 Phase C 才能继续复用当前已经存在的 canonical merge 策略，而不是在流程拆分后丢失 merge 决策信息。

### 8.2.2 Phase B 启动前必须做状态 reconcile

Phase B 的输入不一定全部来自本轮刚下载完成的分段，也可能来自：

- 上次暂停后保留下来的 raw 分段
- 上次失败后保留下来的 raw 分段
- 重新处理模式保留下来的 raw 分段
- `.done` 存在但处理产物缺失或失效的分段

因此进入集中处理前必须先 reconcile 文件系统状态：

- raw `partNNN.mp4` 是否存在
- `.done` 是否存在
- `.done` 中的下载态字段是否完整
- 原有 `merge_part_path` 是否仍存在
- 原有派生产物是否结构有效
- 若 raw 存在但处理产物无效，应清理处理产物并加入待重处理队列

这个 reconcile 步骤是 resume / reprocess 能够稳定工作的前提。

## 8.3 merge 不能继续当“大杂烩修复站”

merge 阶段只做：

- 合并
- 结构校验
- 明确失败

不再把这些作为主方案：

- 到 merge 再补一轮大修复
- 到 merge 再第一次发现通道限制没做好
- 到 merge 再统一吞掉前面分类不准的问题

## 8.4 source finalize 不能再依赖“末尾再兜底一次”

你指出的这点，需要在文档里明说：

- 第一版为了兼容现网，某些兜底可短暂保留
- 但从设计上，它们只能是**过渡保护**
- 不是正式方案的一部分

后续目标是：

- 把末尾兜底逐步降成告警 / 断言
- 把问题提前收敛到 Phase B

---

## 九、文件与代码层面的修改点

## 9.1 `edge_service/tasks/camera.py`

这是本次改造的主战场。

### 需要做的事情

#### A. 把当前下载循环拆成“下载”和“集中处理”两段

当前下载循环里，分段下载后立即 `_normalize_download_part`。

要改成：

- 下载循环只负责下载 raw 分段
- 全部下载完成后，再进入集中处理函数

#### B. 新增集中处理入口

建议新增类似内部函数：

- `_process_downloaded_parts(...)`

职责：

- 遍历 raw `part*.mp4`
- 调用分类 / 修复逻辑
- 产出 `merge_part_paths`
- 更新 metadata
- 支持 pause / stop

但注意：第一版不允许在这个新入口里“偷减逻辑”。

要求是：

- 当前 `_normalize_download_part` 内所有判断场景和修复路径，完整迁入 `_process_downloaded_parts(...)`
- 只是把“调用时机”从下载循环内，改到“全部 raw 分段下载完成之后”
- 迁移前后同一批样本应得到等价的分类和修复结果

#### C. 扩展 `.done` / metadata 语义

这里需要按职责重新拆分，而不是继续让 `.done` 混合承担下载态与处理态：

- `.done`
  - 只表示 raw 源分段下载完成
  - 记录最小必要下载元数据
- `process_state.json`
  - 记录集中处理阶段状态
  - 记录 `merge_part_path`
  - 记录 `classification` / `normalize_reason`
  - 记录处理耗时与已完成分段集合

#### D. 明确禁止覆盖 raw 分段

需要明确保证：

- raw `part*.mp4` 永远不做就地替换
- 所有修复结果都输出到独立派生文件
- merge 明确消费“最终使用文件路径”，而不是默认回写原始路径

#### E. 调整清理时机

把当前“merge 后立即删 raw part”的逻辑后移到 `SOURCE_FINALIZE` 完成之后。

#### F. 支持 `process_only` 模式

当进入“重新处理”时：

- 不再跑下载循环
- 直接从 raw `part*.mp4` 进入 `_process_downloaded_parts`

## 9.1.1 推荐的模块拆分方式

你提的这点非常关键：

- 视频场景分析
- 分类决策
- 修复执行

应该拆成独立模块，避免后续每加一种样本都去改主流程。

推荐目标不是继续把所有逻辑都堆在 `run_camera_task` 或 `_normalize_download_part` 里，而是拆成下面几层：

### A. 探测层（只负责取证，不负责决策）

建议职责：

- 读取媒体信息
- 输出结构化探测结果
- 不直接改文件

建议承接内容：

- 时间线探测
- 音视频起始偏差探测
- packet / stream / boundary 异常探测
- duration / structure 探测

### B. 分类层（只负责把样本归类）

建议职责：

- 根据探测结果输出 `classification`
- 给出证据字段
- 明确是否允许修复、应该走哪条策略

例如输出：

- `strong_abnormal_timeline`
- `audio_early`
- `audio_late`
- `timeline_gap`
- `stream_end_gap_abnormal`
- `wrong_channel_suspected`

### C. 策略路由层（只负责决定走哪条修复路径）

建议职责：

- 根据分类决定：
  - 直接复用 raw
  - `itsoffset` 无损对齐
  - 完整时间线重建
  - fail fast
  - merge 强制 canonical

### D. 修复执行层（只负责真正操作文件）

建议职责：

- 执行 ffmpeg / rebuild / remux
- 输出派生文件路径
- 不负责样本分类口径定义

### E. 编排层（`run_camera_task` / `_process_downloaded_parts`）

建议职责：

- 串联下载、集中处理、merge、finalize
- 维护状态文件、进度、暂停/继续/终止
- 不直接承载复杂分类细节

这样做的收益是：

- 后续新增一种异常样本时，只改探测 / 分类 / 修复模块
- 不影响 pause / resume / 清理 / 调度主流程
- 容易做单测和样本回归
- 也更适合逐步收缩末尾兜底

## 9.2 `edge_service/video/hik/download.py`

### 需要做的事情

- 明确 `mapping_source` 的信任等级
- 禁止 `scanned` 直接进入任务级锁定
- 加强低可信候选验证
- 把 `mapping_source` 回传并落盘，便于后续 resume / process 阶段使用

## 9.3 `edge_service/video/nvr.py`

### 当前定位

`edge_service/video/nvr.py` 已经作为统一 NVR provider 边界存在。

集中下载方案后续实现时，`camera.py` 不应直接绑定海康下载细节，而应继续通过 provider 边界调用：

- `open_download_session`
- `download_by_time`
- `download_by_time_with_session`
- `close_download_session`

### 需要做的事情

- 保持 `CameraTask` 编排层只依赖统一 provider 接口
- 保证 `DownloadResult` 在 provider 层保持稳定字段语义
- 后续接入非海康 NVR 时，不改变 Phase A / B / C / D 的主流程
- provider 内部负责不同厂商 SDK 差异，任务编排层只消费统一结果

### 对本方案的影响

NVR provider 抽象不改变“集中下载后集中处理”的流程，只改变下载实现边界：

- Phase A 通过 provider 下载 raw 分段
- Phase B 只处理 raw 分段文件，不关心厂商 SDK
- Phase C 只消费处理后的 merge 输入序列
- Phase D 只处理最终 source 文件

## 9.4 `edge_service/video/hik/sdk.py`

### 当前定位

海康 SDK 登录兼容已经从单一 `NET_DVR_Login_V30` 调整为：

- 优先 `NET_DVR_Login_V40`
- 失败后回退 `NET_DVR_Login_V30`

### 对本方案的影响

这属于 provider 内部兼容能力，不改变集中下载主流程。

它的作用是提高 Phase A 打开下载 session 的成功率，尤其是新型号 / 新固件 / 长账号密码场景。

文档和实现上应保持这个边界：

- Phase A 只关心 session open 成功或失败
- V40 / V30 选择逻辑不泄露到 `camera.py`
- 登录失败仍归类为下载阶段失败
- Phase B / C / D 不需要知道 SDK 登录版本

## 9.5 `edge_service/runner/_runner.py`

### 需要做的事情

- 增加新的动作入口：`reprocess`
- 让 `DOWNLOAD` 的 resume 能恢复到正确子阶段
- 让 `DOWNLOAD` 的进度消息能体现子阶段

## 9.6 `edge_service/runner/_task_state.py`

### 需要做的事情

- 新增“只清 processing 产物、保留 raw 分段”的清理函数
- 扩展 stop / reset 覆盖新的集中处理产物
- 区分：
  - 全量重下清理
  - 只重处理清理

建议进一步拆清为三类：

- 失败 / 暂停后的处理中间产物清理
- 终止 / 重下时的源分段与状态全清理
- 最终成功提交后的善后清理

## 9.7 `ops_routes.py` / `monitor_ui`

### 需要做的事情

- 新增按钮 icon：【重新处理】
- 显示当前 `DOWNLOAD` 子阶段
- 集中处理阶段展示 `第 N / M 段`

如果现有前后端 task action 是通过独立路由和 `node_detail.html` 承载，则这里要同步把 `reprocess` 纳入同一套 action 流转。

## 9.8 调度 / 并发约束

这部分虽然不是第一版必须大改的 DB 结构，但设计上必须先写清：

- 下载资源和后处理资源应分层看待
- 下载并发主要受 NVR / 网络限制
- 集中处理并发主要受本地 CPU / 磁盘限制

因此推荐目标模型是：

- 下载阶段尽快释放 NVR 连接
- 集中处理进入本地后处理队列
- 后处理队列具备独立并发上限

第一版如果暂时仍在单任务内串行执行，也要保留后续接入独立后处理队列的演进空间

---

## 十、分阶段执行计划与实现状态跟踪

这一章用于后续实际执行优化时逐项跟踪。

每完成一个阶段，都必须回到本章更新状态：

- 阶段状态
- 已完成需求项
- 未完成 / 延后项
- 关键验证结果
- 如有代码实现，还要补充对应提交 / 版本 / 验证日志

## 10.1 状态标记规范

后续统一使用以下状态：

- `[未开始]`
  - 尚未进入实现
- `[进行中]`
  - 已开始修改或验证，但未完成验收
- `[部分完成]`
  - 已完成一部分能力，但仍有明确缺口
- `[已完成]`
  - 代码实现、验证、文档标记均完成
- `[延后]`
  - 当前阶段明确不做，移动到后续阶段
- `[取消]`
  - 经确认后不再实施

需求项使用 checkbox 标记：

- `[ ]` 未完成
- `[x]` 已完成
- `[~]` 部分完成或需要补验证

## 10.2 总体执行顺序

推荐按以下顺序实施：

| 阶段 | 名称 | 当前状态 | 核心目标 |
|---|---|---|---|
| 阶段 0 | 前置收敛 | [部分完成] | 先收紧下载通道、provider、SDK 兼容与异常分类基础 |
| 阶段 1 | 下载 / 处理拆分 | [未开始] | 把 `DOWNLOAD` 从“边下边修”改为“先下完，再集中处理” |
| 阶段 2 | 状态持久化与恢复 | [未开始] | 支持 pause / resume / 子阶段进度 / 耗时 |
| 阶段 3 | 重新处理 | [未开始] | 支持保留 raw 分段并只重跑处理、merge、finalize |
| 阶段 4 | 清理与 finalize 边界修正 | [未开始] | raw 清理后移到 source finalize 成功之后 |
| 阶段 5 | 兜底收缩与样本回归 | [未开始] | 把末尾兜底逐步降级为告警 / 断言 |

## 10.3 阶段 0：前置收敛

### 阶段状态

`[部分完成]`

### 目标

先把错误样本源头污染收紧，否则后面的集中处理会把错误通道、错误时间线、错误分类更系统地带入后续流程。

### 本阶段要实现什么

- `[x]` 新增统一 NVR provider 边界
- `[x]` 海康 SDK 登录改为优先 `NET_DVR_Login_V40`，失败后回退 `NET_DVR_Login_V30`
- `[x]` 下载层支持 `locked_channel` / `locked_record_type`
- `[x]` 下载层支持 `excluded_channels`
- `[x]` `DownloadResult` 返回 `channel_used` / `persisted_channel` / `persisted_failed`
- `[x]` `camera.py` 维护任务级 `_task_locked_channel`
- `[x]` `.done` 写入 `channel_used` / `record_type_used`
- `[x]` merge 前保留 `channel_used` 一致性断言
- `[~]` 明确 `mapping_source` 信任等级
- `[~]` 禁止未经验证的 `scanned` 直接提升为任务级锁定
- `[ ]` 把 `mapping_source` 写入下载态 metadata
- `[ ]` 补充低可信 `scanned` 的追加验证逻辑
- `[ ]` 梳理异常样本到 `classification` / `normalize_reason` / 错误码的稳定映射

### 重点文件

- `edge_service/video/nvr.py`
- `edge_service/video/hik/sdk.py`
- `edge_service/video/hik/download.py`
- `edge_service/tasks/camera.py`
- `edge_service/video/ffmpeg.py`

### 验收标准

- `[ ]` `persisted` / `hint` / `task_locked` / `scanned` 的信任等级在代码和 metadata 中可区分
- `[ ]` 未经验证的 `scanned` 不会静默污染后续任务
- `[ ]` 同一 CameraTask 内所有已知分段 `channel_used` 一致
- `[ ]` 通道不一致主要在 Phase A 暴露，而不是到 merge 才首次发现
- `[ ]` 新型号海康设备登录兼容不需要 `camera.py` 感知 V30 / V40 差异

### 完成后必须更新

- 把本阶段状态从 `[部分完成]` 改为 `[已完成]`
- 勾选所有已完成需求项
- 在本节补充验证样本、日志或测试命令

## 10.4 阶段 1：把 `DOWNLOAD` 改成“先下完，再集中处理”

### 阶段状态

`[未开始]`

### 目标

把当前“边下边修”切成“两段式”。

### 本阶段要实现什么

- `[ ]` 下载循环只负责 raw `partNNN.mp4` 下载
- `[ ]` 下载过程中不再调用 `_normalize_download_part`
- `[ ]` 新增 `_process_downloaded_parts(...)`
- `[ ]` `_process_downloaded_parts(...)` 完整承接当前 `_normalize_download_part` 的检测、分类、修复能力
- `[ ]` Phase B 输出完整 `processed_parts`
- `[ ]` Phase B 输出 `merge_part_paths`
- `[ ]` Phase B 输出 `force_canonical_merge` / `canonicalize_merge_part_indexes`
- `[ ]` `.done` 收敛为下载态 metadata
- `[ ]` `process_state.json` 承接处理态 metadata
- `[ ]` merge 只消费 Phase B 的最终输出序列

### 重点文件

- `edge_service/tasks/camera.py`

### 验收标准

- `[ ]` 所有 raw 分段下载完成后，才进入集中处理
- `[ ]` 处理阶段可以逐段输出 `classification` / `normalize_reason`
- `[ ]` 正常分段直接复用 raw
- `[ ]` 异常分段输出独立派生产物，不覆盖 raw
- `[ ]` 同一批样本迁移前后分类和修复结果能力等价
- `[ ]` merge 输入来源清晰可追踪

### 完成后必须更新

- 标记本阶段为 `[已完成]` 或 `[部分完成]`
- 补充实际新增函数名
- 补充迁移后仍保留的旧逻辑清单
- 补充已知未迁移项

## 10.5 阶段 2：状态持久化、暂停继续、进度与耗时

### 阶段状态

`[未开始]`

### 目标

让集中处理真正可用，而不是只有 happy path。

### 本阶段要实现什么

- `[ ]` 明确 `download_state.json` 只负责下载断点
- `[ ]` 新增或扩展 `process_state.json`
- `[ ]` 持久化 `subphase`
- `[ ]` 持久化 `current_part_idx`
- `[ ]` 持久化 `processed_parts`
- `[ ]` 持久化 `merge_part_paths`
- `[ ]` 持久化 `download_elapsed_seconds`
- `[ ]` 持久化 `process_elapsed_seconds`
- `[ ]` resume 时根据 `subphase` 回到正确阶段
- `[ ]` 进度文案展示当前子阶段和 `第 N / M 段`
- `[ ]` merge / finalize 支持 cooperative pause / stop 的安全边界

### 重点文件

- `edge_service/tasks/camera.py`
- `edge_service/runner/_runner.py`
- `edge_service/runner/_task_state.py`

### 验收标准

- `[ ]` 暂停在 `segment_download` 后可继续下载剩余 raw
- `[ ]` 暂停在 `segment_process` 后可继续处理剩余分段
- `[ ]` 暂停在 `merge` 后可重新进入 merge
- `[ ]` 暂停在 `source_finalize` 后可重新进入 finalize
- `[ ]` 页面进度不会长期卡在 97%
- `[ ]` 总耗时和当前阶段耗时口径稳定

### 完成后必须更新

- 记录实际 `process_state.json` 字段
- 记录每个 `subphase` 的恢复策略
- 记录暂停 / 继续测试结果

## 10.6 阶段 3：新增“重新处理”动作

### 阶段状态

`[未开始]`

### 目标

把“修完逻辑后不重复下载”真正落地。

### 本阶段要实现什么

- `[ ]` 新增 `reprocess` 动作入口
- `[ ]` 新增 `process_only` 模式
- `[ ]` 新增“保留 raw、清 processing / merge / finalize 产物”的清理函数
- `[ ]` `process_only` 跳过 Phase A
- `[ ]` `process_only` 直接从 Phase B 开始
- `[ ]` UI 增加【重新处理】按钮
- `[ ]` 按钮展示时校验 raw `part*.mp4` 是否存在
- `[ ]` 重新处理前执行状态 reconcile

### 重点文件

- `edge_service/runner/_runner.py`
- `edge_service/runner/_task_state.py`
- `edge_service/tasks/camera.py`
- `ops_routes.py`
- `monitor_ui`

### 验收标准

- `[ ]` 点击【重新处理】不会重新下载分段
- `[ ]` raw `part*.mp4` 被保留
- `[ ]` `.fixed.mp4` / `.norm.mp4` / `.merged*` / source mp4 被按规则清理
- `[ ]` 重新处理能重新生成 `process_state.json`
- `[ ]` 重新处理后可正常 merge / finalize

### 完成后必须更新

- 记录新增 action 名称和路由
- 记录按钮展示条件
- 记录 process-only 的入口参数

## 10.7 阶段 4：清理规则与 source finalize 边界修正

### 阶段状态

`[未开始]`

### 目标

修正当前“merge 成功后立即清理 raw 分段”的问题，把 raw 清理后移到 source finalize 成功之后。

### 本阶段要实现什么

- `[ ]` 移除或后移 merge 后立即清理 `part*.mp4` 的逻辑
- `[ ]` source finalize 成功后统一清理 raw / `.done` / 派生产物 / merge 中间态
- `[ ]` source finalize 失败时保留 raw 和必要 metadata
- `[ ]` pause / stop / failed 三种状态下执行不同清理策略
- `[ ]` 清理函数覆盖 `.fixed.mp4` / `.timeline.tmp.mp4` / `.avsync.tmp.mp4` / `.norm*` / `.merged*`

### 重点文件

- `edge_service/tasks/camera.py`
- `edge_service/runner/_task_state.py`

### 验收标准

- `[ ]` merge 成功但 finalize 失败时，raw `part*.mp4` 仍存在
- `[ ]` DOWNLOAD 最终成功后，raw 和中间态被清理
- `[ ]` DOWNLOAD 失败后，raw 可用于复盘和重新处理
- `[ ]` stop / rerun 仍能做全量清理

### 完成后必须更新

- 记录清理函数拆分结果
- 记录各任务状态下的保留 / 删除规则

## 10.8 阶段 5：兜底收缩与样本回归

### 阶段状态

`[未开始]`

### 目标

在 Phase B 分类和修复路径稳定后，逐步把 merge / source finalize 阶段的大范围兜底降级为过渡保护、告警或断言。

### 本阶段要实现什么

- `[ ]` 建立异常样本清单
- `[ ]` 建立样本到 `classification` 的映射表
- `[ ]` 建立 `classification` 到修复路径的映射表
- `[ ]` 统计 merge fallback 触发原因
- `[ ]` 统计 source finalize 兜底触发原因
- `[ ]` 将已经能在 Phase B 解决的问题从后置兜底中移除
- `[ ]` 将理论上不该触发的后置检查降级为告警 / 断言

### 重点文件

- `edge_service/tasks/camera.py`
- `edge_service/video/ffmpeg.py`
- `edge_service/docs/camera_scene_codes.md`

### 验收标准

- `[ ]` 常见异常能在 Phase B 得到稳定分类
- `[ ]` merge fallback 触发次数明显下降
- `[ ]` source finalize 不再承担主要修复职责
- `[ ]` 失败点更前移，错误原因更明确
- `[ ]` 每类异常样本都有可复现验证记录

### 完成后必须更新

- 记录样本回归结果
- 记录被降级或删除的兜底逻辑
- 记录仍需保留的过渡保护

---

## 十一、当前仍需继续确认 / 继续分析的点

这部分不是方案缺失，而是当前实现前需要明确的设计选择。

## 11.1 是否要把 `PROCESS` 做成 DB 显式步骤

### 当前建议

第一版**不要**。

### 原因

- 现有步骤模型改动太大
- pause / resume / report / follow-up 影响面广
- 第一版用 `DOWNLOAD` 子阶段即可满足目标

## 11.2 【重新处理】按钮的展示范围

### 当前建议

第一版先支持：

- `DOWNLOAD FAILED`
- `DOWNLOAD PAUSED`
- `DOWNLOAD SUCCESS`

且必须存在 raw `part*.mp4`。

### 待确认点

是否允许在“正在集中处理”时直接点击【重新处理】并打断当前 worker。

## 11.3 merge 前 channel 一致性校验是否第一版就完全去掉

### 当前建议

第一版**不要去掉**，但要明确降级为防御性断言。

原因：

- 第一版下载锁定刚改完，仍需要保底防止脏内容合并
- 但它不再被当成主方案中的主要防线

## 11.4 source finalize 里的旧兜底是否立刻全删

### 当前建议

第一版先不一刀切全删。

建议顺序是：

1. 先把 Phase B 分类与修复路径收精确
2. 再逐步把 Phase D 的兜底降成告警 / 断言

否则太早移除，可能导致现网样本暴露得过于粗糙。

---

## 十二、最终结论与推荐下一步

这次需求的完整主线，应该明确写成：

**把单个 `CameraTask` 的 `DOWNLOAD` 链路，从“边下载边修复”改成“先下载全部分段，再集中处理”，同时把异常识别 / 修复路径做精确，把末尾兜底逐步收缩。**

这不是单纯的“流程重排”，而是三件事一起落地：

1. **执行模型重构**
   - 先下载全部 raw 分段
   - 再集中处理

2. **生命周期能力补齐**
   - 暂停 / 继续
   - 清晰进度
   - 总耗时统计
   - 重新处理

3. **根因修复前置**
   - 通道锁定修正
   - 异常分类精确化
   - 末尾兜底收缩

### 推荐实际落地顺序

按实现风险和收益，建议下一步按这个顺序推进：

#### 第一步

先做**阶段 0：下载通道锁定 + 异常分类前置收敛**

#### 第二步

再做**阶段 1：把 `DOWNLOAD` 改成“先下完，再集中处理”**

#### 第三步

补齐**阶段 2：pause / resume / 进度 / 耗时**

#### 第四步

再做**阶段 3：重新处理按钮与 process-only 模式**

#### 第五步

最后做**阶段 4：收缩末尾兜底**

如果只问“当前最应该先落地哪一项”，答案仍然是：

**先把下载通道锁定和异常分类收精准。**

因为这是后续“集中处理”能够真正稳定可用的前提。
