# 中期优化方案分析（基于 2024-04-03 更新版）

## 1. 文档目标

本文档是对 `0403Optimization_plan.md` 更新版的**分析报告**，重点回答：

- 哪些是好的优化
- 哪些可能是过度设计或不对的
- 基于当前代码应该如何落地

核心原则：**解决真实问题，禁止过度优化**。

---

## 2. 新方案核心约束（必须遵守）

更新后的方案明确了**不做**的边界，这是最重要的变化：

### 2.1 明确不做

- ❌ 不做复杂分布式调度
- ❌ 不引入 MQ / 分布式锁
- ❌ 不做复杂多状态机系统
- ❌ 不做缓存 / CDN / 预热策略
- ❌ 不做跨节点一致性状态同步

### 2.2 边缘职责收敛

- ❌ 边缘不维护 viewer_count
- ❌ 边缘不做流调度
- ❌ 边缘不做复杂状态机

### 2.3 中心职责收敛

- ❌ 中心不扩展为分布式调度系统
- ❌ 中心不扩展为复杂任务系统

### 2.4 状态机收敛

新方案要求最小状态设计：

```text
INIT → PUSHING → STOPPED → FAILED
```

只有 4 个状态，不允许扩展。

---

## 3. 哪些是好的优化（✔ 应该做）

### 3.1 ✔ 保留现有 open 能力

新方案明确保留：

- RTSP 探测
- 端口探测（554 / 18080）
- 主/子码流回退
- 首包验证
- session 创建与复用

**分析：完全正确**。

当前 `edge_service/stream_proxy.py` 已经实现了完整的 RTSP 可播解析能力，包括：

- `_probe_rtsp_url`
- `_probe_mpegts_first_chunk`
- `_probe_rtsp_url_with_fallback`
- `_resolve_playable_rtsp_url`
- RTSP 可播缓存（今天刚加入）

这些都是成熟资产，中期方案应当完全复用，不应重写。

### 3.2 ✔ 播放链路重构为推流模式

新方案核心变化：

```text
原：play → ffmpeg → pipe → 用户
新：start_stream → 边缘推流 → 中心服务 → HLS → 用户
```

**分析：方向正确**。

当前每个用户都对应一路 ffmpeg，边缘资源消耗与用户数强耦合。改成推流模式后：

- 同一 stream 只推一路
- 中心负责 HLS 分发
- 多人复用同一流

这是解决"资源爆炸"问题的正确路径。

### 3.3 ✔ 统一播放入口

所有客户端仅访问中心服务（m3u8）。

**分析：方向正确**。

当前系统依赖公网映射或 VPN 访问边缘资源，不稳定。改成中心统一入口后：

- 消除公网映射依赖
- 统一访问路径
- 便于监控和排障

### 3.4 ✔ 历史视频策略明确

新方案新增了历史视频策略：

- 已完成视频：闲时上传到中心（主路径）
- 未完成视频：按需推流（补充路径）

**分析：方向正确，补充了之前的盲区**。

当前历史视频和实时流混在一起考虑，容易边界不清。新方案把两者拆开：

- 历史视频走"中心存储"
- 实时流走"边缘推流"

这样边缘职责更聚焦。

### 3.5 ✔ 边缘推流用 copy 模式

```bash
ffmpeg -rtsp_transport tcp \
-i {rtsp_url} \
-c copy \
-f flv rtmp://center/live/{stream_id}
```

**分析：完全正确**。

`-c copy` 不转码，直接封装推流，边缘 CPU 压力最小。

### 3.6 ✔ 简单自动重试

```text
ffmpeg退出 → 简单自动重试
RTSP失败 → 重新open
推流失败 → 返回错误 + 可重试
```

**分析：方向正确**。

不需要复杂的重试策略，简单重试即可。失败后返回错误，让上层决定是否重试。

### 3.7 ✔ 单中心设计

新方案明确：中期只做单中心，不做分布式。

**分析：完全正确**。

单机闭环没跑通之前，上分布式只会增加复杂度，没有收益。

---

## 4. 之前方案中哪些是过度设计（❌ 需要修正）

### 4.1 ❌ 状态机过度设计

之前方案（旧 plan.md）设计了 7 个状态：

```text
INIT, PREPARING, PUSHING, RETRYING, FAILED, STOPPING, STOPPED
```

新方案要求只保留 4 个状态：

```text
INIT → PUSHING → STOPPED → FAILED
```

**结论：之前过度设计，应该简化**。

实际上：

- `PREPARING` 可以合并到 `INIT`（准备中 = 尚未推流）
- `RETRYING` 可以合并到 `PUSHING`（重试中 = 仍在尝试推流）
- `STOPPING` 可以省略（停止是瞬时动作，不需要中间态）

### 4.2 ❌ session 生命周期过度设计

之前方案设计了 5 个 session 状态：

```text
CREATED, ACTIVE, IDLE, EXPIRED, CLEANED
```

新方案明确：不做复杂多状态机系统。

**结论：之前过度设计**。

session 的生命周期应该沿用当前代码中已有的 TTL 机制：

- 创建时记录时间
- 超时后自动清理
- 不需要显式状态机

当前 `StreamSessionManager` 已经有 `_cleanup_expired_locked` 方法，足够使用。

### 4.3 ❌ 边缘模块拆分过度

之前方案建议拆成 6 个模块：

```text
manager.py, state_machine.py, process_manager.py, models.py, repository.py, scheduler.py, protocol.py
```

新方案要求：边缘只负责推流执行，不做复杂状态机。

**结论：之前过度设计**。

边缘侧推流逻辑应该尽量简单：

- 一个函数启动 ffmpeg 推流
- 一个函数停止推流
- 一个字典跟踪当前推流进程
- 简单重试

不需要独立的 state_machine、repository、scheduler、protocol 模块。

### 4.4 ❌ 控制协议过度设计

之前方案定义了大量事件：

- `start_stream_request`
- `stop_stream_request`
- `query_stream_status`
- `start_stream_ack`
- `stream_preparing`
- `stream_pushing`
- `stream_retrying`
- `stream_failed`
- `stream_stopping`
- `stream_stopped`
- `edge_heartbeat`

新方案只需要两个接口：

```text
POST /stream/start
POST /stream/stop
```

**结论：之前过度设计**。

边缘只需要暴露简单的 HTTP 接口，不需要 WebSocket 长连接和复杂事件协议。

### 4.5 ❌ viewer_count 设计过度

之前方案设计了活跃模型：

- `watch_start`
- `watch_keepalive`
- `watch_stop`

新方案明确：边缘不维护 viewer_count，是否推流只在中心判断。

**结论：之前过度设计**。

viewer_count 应该完全由中心负责，边缘只管推流执行。

### 4.6 ❌ 数据模型过度设计

之前方案设计了：

- `stream_task`（18 个字段）
- `stream_session`（12 个字段）
- `edge_node`

新方案要求最小状态设计。

**结论：之前过度设计**。

边缘侧不需要新建这些表。当前已有的表结构足够：

- `edge_nvr_device`
- `edge_nvr_channel`
- `edge_nvr_rtsp_probe_cache`

推流状态可以用内存字典跟踪，不需要持久化。

---

## 5. 基于当前代码的落地建议

### 5.1 保留的现有能力

当前代码中应该完全保留：

| 文件 | 能力 | 保留原因 |
|------|------|----------|
| `stream_proxy.py` | RTSP 探测 + 首包验证 | 成熟资产 |
| `stream_proxy.py` | session 创建与复用 | 成熟资产 |
| `stream_proxy.py` | RTSP 可播缓存 | 今天刚加入，直接复用 |
| `routes/stream_routes.py` | open 接口 | 保留，补充 stream_id |
| `routes/stream_routes.py` | play 接口 | 保留作为兜底 |
| `video/nvr.py` | 统一 provider 入口 | 成熟资产 |

### 5.2 需要新增的能力

边缘侧只需要新增：

| 新增内容 | 实现方式 |
|----------|----------|
| `POST /stream/start` | 新增路由，启动 ffmpeg 推流 |
| `POST /stream/stop` | 新增路由，停止 ffmpeg 推流 |
| 推流进程管理 | 内存字典跟踪，不需要持久化 |
| stream_id 生成 | 在 open 返回中补充 |

### 5.3 不需要新增的能力

根据新方案约束，以下能力**不应该在边缘实现**：

- ❌ 复杂状态机
- ❌ viewer_count 维护
- ❌ 流调度逻辑
- ❌ 控制协议 / WebSocket
- ❌ scheduler / repository 模块
- ❌ 分布式状态同步

---

## 6. stream_id 生成规则

新方案要求：同一 stream_id 只能推一次（由中心保证）。

建议 stream_id 生成规则：

```python
import hashlib

def generate_stream_id(campus_code: str, nvr_device_id: int, channel_num: int, actual_profile: str, rtsp_url: str) -> str:
    url_hash = hashlib.md5(rtsp_url.encode()).hexdigest()[:4]
    return f"{campus_code}_{nvr_device_id}_{channel_num}_{actual_profile}_{url_hash}"
```

例如：

```text
campus001_7_3_sub_ab12
```

**为什么加 hash？** 防止配置漂移：
- 设备替换但 device_id 不变
- RTSP URL 路径变化
- 厂商改路径格式

加入 `hash(rtsp_url)` 可自动区分这些情况，避免流复用错误。

---

## 7. 边缘侧推流实现建议

### 7.1 最小实现方案（支持覆盖式启动）

```python
# 内存字典跟踪推流进程
_push_processes: dict[str, subprocess.Popen] = {}

def start_push(stream_id: str, rtsp_url: str, push_url: str, force: bool = False) -> bool:
    """启动推流，支持强制重启"""
    if stream_id in _push_processes:
        proc = _push_processes[stream_id]
        if proc.poll() is None:
            if not force:
                return True  # 已在推流，直接返回
            # force=True: 先停掉旧进程
            proc.terminate()
            proc.wait(timeout=5)
    cmd = [
        "ffmpeg", "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-c", "copy",
        "-f", "flv", push_url
    ]
    proc = subprocess.Popen(cmd, ...)
    _push_processes[stream_id] = proc
    return True

def stop_push(stream_id: str) -> bool:
    proc = _push_processes.pop(stream_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
    return True
```

**关键点：`force=True` 支持覆盖式启动**

为什么需要？因为 `poll() is None` 无法检测 ffmpeg "假死"（进程存在但无数据输出）。中心检测到流断了可以调用 `start(force=True)` 强制重启。

### 7.2 重试策略（分阶段）

```text
短期重试：3 次（间隔 3 秒）
失败后：进入冷却期（30 秒）
冷却后：允许再次 start
```

**为什么分阶段？**
- RTSP 场景常见偶发断流、网络抖动、NVR 限流
- 3 次失败 ≠ 永久失败
- 但也不能边缘无限重试（浪费资源）

正确做法：短期快速重试 → 冷却 → 允许再次触发。

---

## 8. 修正后的实施阶段

### 8.1 阶段一：open 补充 stream_id

- 在现有 `open` 返回中补充 `streamId`
- 生成规则：`{campus_code}_{nvr_device_id}_{channel_num}_{actual_profile}`
- 不改变现有 play 链路

### 8.2 阶段二：边缘新增 start/stop 接口

- 新增 `POST /stream/start`（支持 `force` 参数）
- 新增 `POST /stream/stop`
- 内存字典跟踪推流进程
- 分阶段重试

**stop 触发机制（必须明确）：**
- ✅ 由中心触发（基于超时）
- 逻辑：如果 60 秒没有人播放 HLS → 中心调用 `stop_stream`
- ❌ 边缘不自己判断 viewer
- ❌ 不用长连接控制

### 8.3 阶段三：中心接入

- 中心实现 ZLMediaKit
- 接收边缘 RTMP 推流
- 输出 HLS
- 控制推流触发时机

**中心必须具备反控能力：**
- 推流失败 / HLS 未生成 → 调用 `/stream/stop`
- 避免边缘疯狂推流浪费资源
- 本质：中心要有"反向刹车能力"

### 8.4 阶段四：历史视频上传

- 边缘闲时上传已完成视频到中心
- 中心负责存储和播放

---

## 9. 架构本质：控制面 vs 数据面分离

当前架构已经隐式形成了控制面与数据面的分离：

| 层面 | 组件 |
|------|------|
| **控制面（Control Plane）** | center、start/stop 接口、stream_id |
| **数据面（Data Plane）** | ffmpeg、RTSP、RTMP、HLS |

**重要原则：后续所有优化都应遵循**

> ✅ 控制简单 + 数据链路稳定

而不是继续"设计系统"。

---

## 10. 结论

### 10.1 新方案的核心价值

- **解决真实问题**：消除公网映射依赖、支持多人复用、降低边缘压力
- **禁止过度优化**：不做分布式、不做复杂状态机、不引入新组件

### 10.2 之前方案需要修正的部分

| 之前设计 | 问题 | 修正 |
|----------|------|------|
| 7 状态推流状态机 | 过度设计 | 简化为 4 状态 |
| 5 状态 session 生命周期 | 过度设计 | 沿用现有 TTL 机制 |
| 6 个边缘模块 | 过度设计 | 不拆分，在现有代码中新增 |
| 复杂控制协议 | 过度设计 | 简单 HTTP 接口 |
| viewer_count 活跃模型 | 边缘不应负责 | 移到中心 |
| 新增 3 张表 | 过度设计 | 不新增，用内存跟踪 |

### 10.3 必须补充的 5 个关键点

基于二次评审采纳：

1. **start 支持覆盖式启动**：`force=True` 参数，解决 ffmpeg 假死问题
2. **stop 由中心触发**：超时 60 秒无人观看 → 调用 stop
3. **stream_id 加 hash**：防止设备替换/URL变化导致流复用错误
4. **重试分阶段**：短期快速重试 → 冷却 → 允许再次触发
5. **中心反控能力**：推流失败/HLS未生成 → 调用 stop

### 10.4 一句话总结

```text
用"中心转发 + 边缘推流 + 历史视频中心存储"替代"直接访问边缘"，
用最小改动解决最大问题，禁止过度设计。
```
