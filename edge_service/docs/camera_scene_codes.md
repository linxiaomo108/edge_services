# camera.py 下载/合并 Scene Code 说明

## 目标

本文档整理 `edge_service/tasks/camera.py` 中与视频下载、分段归一化、分段合并、源视频后处理相关的 scene code，便于根据日志中的 code 快速定位具体分支与故障场景。

当前约定：

- 日志中的 scene code 统一通过 `[%s]` 形式输出，例如 `[CAM-DL-NORM-010]`
- 本文档只覆盖 `camera.py` 中下载与合并链路的场景码
- 代码中的常量定义是最终事实来源；本文档用于排障与维护时快速查阅

## 命名规范

### 1. 下载分段归一化

前缀：`CAM-DL-NORM-xxx`

用于单个下载分段在进入最终合并前的时间线、音视频起始偏差、`itsoffset` 对齐、完整重建等分支。

### 2. 下载后源视频后处理

前缀：`CAM-DL-SRC-xxx`

用于多分段合并完成后的源视频后处理，例如：时间轴归零、结构异常重建、时长裁剪/补齐、`faststart` 重封装。

### 3. 合并阶段

前缀：`CAM-MRG-xxx`

用于多分段最终合并流程，包括：

- canonical merge
- direct concat
- normalized concat
- TS bridge fallback
- merged output final repair

## Scene Code 清单

## `CAM-DL-NORM-*`

- `CAM-DL-NORM-000`
  - 场景：分段时间线正常，跳过下载阶段音视频修复
  - 典型日志：分段时间线正常，跳过下载修复

- `CAM-DL-NORM-010`
  - 场景：分段命中强异常时间线，执行完整重建，并要求后续走 canonical merge
  - 典型日志：命中强异常时间线修复；分段时间线重建完成；分段时间轴归零失败
  - 典型原因：`significant_audio_late`、绝对时间轴起点异常、重建后仍需强制 canonical merge

- `CAM-DL-NORM-020`
  - 场景：分段仅有轻微音频晚到或轻微脏时间线，风险可接受，跳过修复
  - 典型日志：音视频起始偏差可忽略，跳过修复

- `CAM-DL-NORM-030`
  - 场景：分段存在明显 `stream gap`，执行完整重建以恢复时间线
  - 典型日志：命中时间线异常修复；分段时间线重建完成；分段时间轴归零失败

- `CAM-DL-NORM-040`
  - 场景：分段音频略早于视频但在阈值内，跳过对齐
  - 典型日志：音频早于视频但偏差可忽略，跳过无损对齐

- `CAM-DL-NORM-050`
  - 场景：分段音频早于视频且超过阈值，执行 `itsoffset` 无损对齐
  - 典型日志：执行 `itsoffset` 无损对齐；音视频已无损对齐

- `CAM-DL-NORM-051`
  - 场景：`itsoffset` 对齐失败，回退到完整重建
  - 典型日志：音视频无损对齐失败，回退完整重建；回退后的时间线重建完成/失败

- `CAM-DL-NORM-052`
  - 场景：`itsoffset` 对齐异常，回退到完整重建
  - 典型日志：音视频无损对齐异常，回退完整重建；回退后的时间线重建完成/失败

## `CAM-DL-SRC-*`

- `CAM-DL-SRC-010`
  - 场景：源视频时间线异常或被强制校准，执行源视频时间线重建
  - 典型日志：源视频时间轴已归零

- `CAM-DL-SRC-011`
  - 场景：源视频时间线重建失败
  - 典型日志：源视频时间轴校准失败

- `CAM-DL-SRC-020`
  - 场景：合并后的源视频结构异常，先执行完整重建
  - 典型日志：源视频结构异常，执行完整重建

- `CAM-DL-SRC-021`
  - 场景：源视频结构异常重建失败，但继续后续流程
  - 典型日志：源视频结构异常重建失败，继续后续流程

- `CAM-DL-SRC-030`
  - 场景：源视频预校准时间轴失败，但继续后续流程
  - 典型日志：源视频预校准时间轴失败，继续后续流程

- `CAM-DL-SRC-040`
  - 场景：源视频裁剪到课次基准时长
  - 典型日志：源视频已按课次基准时长对齐

- `CAM-DL-SRC-041`
  - 场景：源视频补齐到课次基准时长
  - 典型日志：源视频已补齐到课次基准时长

- `CAM-DL-SRC-050`
  - 场景：源视频执行 `faststart` 重封装
  - 典型日志：源视频已重封装为 `faststart`

- `CAM-DL-SRC-051`
  - 场景：源视频 `faststart` 重封装失败
  - 典型日志：源视频 `faststart` 重封装失败

## `CAM-MRG-*`

- `CAM-MRG-010`
  - 场景：强异常任务对单段做 canonical A/V 重编码，产出 merge-ready MP4
  - 典型日志：`download merge forcing canonical path`；`merge canonical path forced`；`canonicalize part start`；`canonicalize part accepted`；`canonicalize part failed`

- `CAM-MRG-011`
  - 场景：强异常任务使用 canonical MP4 列表执行最终 `concat copy`
  - 典型日志：`canonical concat completed`；`canonical concat ffmpeg command failed`

- `CAM-MRG-020`
  - 场景：普通任务优先尝试 direct concat copy
  - 典型日志：`trying direct concat`；`direct concat accepted`

- `CAM-MRG-030`
  - 场景：普通任务先对单段做 normalized remux，再参与 concat
  - 典型日志：`normalize part start`；`normalize part failed`

- `CAM-MRG-031`
  - 场景：normalized MP4 列表执行最终 `concat copy`
  - 典型日志：`normalized concat accepted`；`merge normalized concat failed, trying TS-bridge fallback`

- `CAM-MRG-040`
  - 场景：TS fallback 使用 copy video + audio profile 生成单段 TS
  - 典型日志：`ts fallback copy path start`

- `CAM-MRG-041`
  - 场景：TS fallback 单段 copy 失败后，执行视频重编码兜底
  - 典型日志：`ts fallback escalating to video reencode`；`ts fallback reencode start`；`ts fallback part reencode path failed`

- `CAM-MRG-050`
  - 场景：TS fallback 最终合并因音频包异常改为 audio reencode
  - 典型日志：`final ts merge switches to audio reencode`

- `CAM-MRG-051`
  - 场景：TS fallback 最终合并直接 copy A/V
  - 典型日志：`final ts merge uses copy path`

- `CAM-MRG-060`
  - 场景：merged 输出因音频包异常执行最终修复
  - 典型日志：`attempting final merged output repair`；`final merged output repair succeeded`；`final merged output repair failed`；`final merged output repair still abnormal`；`final ts merge rejected after structural probe`

## 排障建议

拿到日志中的 scene code 后，建议按以下顺序排查：

- 先确认 code 前缀
  - `CAM-DL-NORM`：看单分段下载归一化
  - `CAM-DL-SRC`：看多分段合并后的源视频后处理
  - `CAM-MRG`：看最终合并路径

- 再确认是哪个阶段的日志语义
  - 命中分支
  - 分支执行成功
  - 分支执行失败
  - 回退到下一层 fallback

- 最后结合同一任务的相邻日志
  - 是否出现 `force_canonical_merge`
  - 是否出现 `merge_output_structural_invalid`
  - 是否进入 `TS-bridge fallback`
  - 是否出现最终 repair

## 后续新增场景时的补充规范

新增 scene code 时，必须同时满足以下要求：

- 在 `camera.py` 常量区新增 code 常量
- 紧邻常量写清楚中文注释，描述触发条件与处理动作
- 在该分支的关键日志中输出相同 code
  - 首次命中日志
  - 分支成功日志
  - 分支失败日志
  - 回退日志（如果存在）
- 如该分支会抛出 `RuntimeError`，优先把 code 带入异常串，便于只看异常文本也能定位

建议继续按以下区间管理编号：

- `000`：正常/跳过
- `010-019`：主路径 A
- `020-029`：主路径 B
- `030-039`：异常修复/回退
- `040-049`：变体路径
- `050-059`：失败后 fallback
- `060-069`：最终修复/输出兜底

## 维护说明

如果后续 `camera.py` 的下载/合并流程出现新分支，修改代码时应同步更新本文档；如果代码与本文档不一致，以 `camera.py` 中的常量定义和实际日志输出为准。
