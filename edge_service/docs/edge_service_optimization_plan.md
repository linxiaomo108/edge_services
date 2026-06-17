# 边缘服务（Edge Service）中期优化实施计划

> 文档版本：v1.1（整合 yh.md 优化建议：start 前 RTSP 可达验证、open→start 竞态保障）  
> 创建日期：2026-04-03  
> 基于文档：`plan-01.md`（二次评审）、`plan.md`（分析报告）、`yh.md`（优化建议）  
> 核心原则：**解决真实问题，禁止过度优化，单机闭环优先**

---

## 一、优化目标

将当前"每个用户独占一路 ffmpeg → pipe → 浏览器"的实时流架构，改为"边缘推流 → 中心分发 HLS"模式，实现：

1. 同一摄像头只推一路流，多人复用
2. 消除公网映射对实时流播放的依赖
3. 降低边缘服务 CPU/带宽压力
4. 保留现有 open/play 能力作为兜底

---

## 二、核心约束（红线）

| 编号 | 约束 | 说明 |
|------|------|------|
| R1 | 不引入 MQ / 分布式锁 | 单机闭环 |
| R2 | 不做复杂状态机 | 最多 4 状态：INIT → PUSHING → STOPPED → FAILED |
| R3 | 不做 viewer_count | 边缘不维护观看人数 |
| R4 | 不新增数据库表 | 推流状态用内存字典跟踪 |
| R5 | 不做 WebSocket 长连接控制 | 仅用 HTTP 接口 |
| R6 | 不做跨节点状态同步 | 单中心设计 |
| R7 | 边缘不做流调度 | 边缘只做"受控 ffmpeg 执行器" |

---

## 三、现有资产盘点（保留，不重写）

| 文件 | 能力 | 状态 |
|------|------|------|
| `stream_proxy.py` | RTSP 探测 + 首包验证 + session 管理 + RTSP 可播缓存 | ✅ 成熟，保留 |
| `routes/stream_routes.py` | `/api/stream/open`、`/api/stream/play`、`/api/stream/close`、`/api/stream/status` | ✅ 成熟，保留 |
| `video/nvr.py` | 统一 NVR provider 入口（build_rtsp_url 等） | ✅ 成熟，保留 |
| `video/hik/sdk.py` | 海康 SDK 登录（V40 优先 + V30 回退） | ✅ 成熟，保留 |
| `video/hik/loader.py` | SDK DLL 加载 | ✅ 已修复路径，保留 |

---

## 四、实施阶段总览

```text
阶段一：open 接口补充 stream_id          → 边缘侧小改动，不影响现有链路
阶段二：边缘新增 start/stop 推流接口      → 核心改动，新增推流能力
阶段三：联调与兜底策略                    → 与中心服务联调，保留 play 兜底
阶段四：历史视频闲时上传（可选，视需求）    → 独立功能，不阻塞主线
```

---

## 五、阶段一：open 接口补充 stream_id

### 5.1 目标

在现有 `/api/stream/open` 返回中新增 `streamId` 字段，为后续推流提供唯一标识。

### 5.2 详细任务清单

| 序号 | 任务 | 涉及文件 | 具体内容 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|------|----------|----------|----------|----------|----------|----------|
| 1.1 | 实现 `generate_stream_id` 函数 | `stream_proxy.py` | 生成规则：`{campus_code}_{nvr_device_id}_{channel_num}_{actual_profile}_{hash(rtsp_url)[:4]}`，使用 `hashlib.md5` | ⬜ 未开始 | | | |
| 1.2 | `StreamSession` 数据类新增 `stream_id` 字段 | `stream_proxy.py` | 在 `StreamSession` dataclass 中新增 `stream_id: str = ""` | ⬜ 未开始 | | | |
| 1.3 | `create_or_get_session` 生成并填充 `stream_id` | `stream_proxy.py` | 在 session 创建时调用 `generate_stream_id`，赋值给 session | ⬜ 未开始 | | | |
| 1.4 | `OpenStreamResponse` 新增 `streamId` 字段 | `routes/stream_routes.py` | 在 Pydantic model 中新增 `streamId: str = Field(default="", ...)` | ⬜ 未开始 | | | |
| 1.5 | `api_stream_open` 返回中包含 `streamId` | `routes/stream_routes.py` | 在 JSONResponse 中添加 `"streamId": session.stream_id` | ⬜ 未开始 | | | |
| 1.6 | 单元验证：调用 open 接口确认返回含 streamId | 手动测试 | 调用 open，检查响应中 streamId 格式正确，非空 | ⬜ 未开始 | | | |
| 1.7 | 回归验证：现有 play 链路不受影响 | 手动测试 | open → play 仍能正常出流 | ⬜ 未开始 | | | |

### 5.3 stream_id 生成规则

```python
import hashlib

def generate_stream_id(campus_code: str, nvr_device_id: int, channel_num: int, actual_profile: str, rtsp_url: str) -> str:
    url_hash = hashlib.md5(rtsp_url.encode()).hexdigest()[:4]
    return f"{campus_code}_{nvr_device_id}_{channel_num}_{actual_profile}_{url_hash}"
```

**示例输出**：`1729444713394536449_7_2_sub_ab12`

**为什么加 hash？**
- 设备替换但 device_id 不变 → hash 变化 → 自动区分
- RTSP URL 路径变化 → hash 变化 → 避免流复用错误
- 无需额外数据库支持

---

## 六、阶段二：边缘新增 start/stop 推流接口

### 6.1 目标

新增 `POST /api/stream/start` 和 `POST /api/stream/stop` 接口，支持边缘向中心推流（RTMP）。

### 6.2 详细任务清单

| 序号 | 任务 | 涉及文件 | 具体内容 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|------|----------|----------|----------|----------|----------|----------|
| 2.1 | 新增推流进程管理模块 | `stream_push.py`（新建） | 内存字典 `_push_processes: dict[str, PushContext]` 跟踪推流进程；`PushContext` 包含 `proc`, `stream_id`, `rtsp_url`, `push_url`, `state`, `retry_count`, `cooldown_until` | ⬜ 未开始 | | | |
| 2.2 | 实现 `start_push` 函数 | `stream_push.py` | 启动 `ffmpeg -rtsp_transport tcp -i {rtsp_url} -c copy -f flv {push_url}`；支持 `force=True` 覆盖式启动；状态转为 PUSHING；**启动前先做轻量 RTSP 可达验证**（见 2.16） | ⬜ 未开始 | | | |
| 2.3 | 实现 `stop_push` 函数 | `stream_push.py` | terminate 进程，从字典移除，状态转为 STOPPED | ⬜ 未开始 | | | |
| 2.4 | 实现分阶段重试逻辑 | `stream_push.py` | 短期重试 3 次（间隔 3 秒）→ 失败后冷却 30 秒 → 冷却后允许再次 start；冷却期内 start 返回 FAILED + cooldown 剩余时间 | ⬜ 未开始 | | | |
| 2.5 | 实现 `query_push_status` 函数 | `stream_push.py` | 返回指定 stream_id 的推流状态（INIT/PUSHING/STOPPED/FAILED）、进程是否存活、重试次数 | ⬜ 未开始 | | | |
| 2.6 | 新增 `POST /api/stream/start` 路由 | `routes/stream_routes.py` | 请求参数：`streamId`, `rtspUrl`, `pushUrl`, `force`；调用 `start_push`；返回状态 | ⬜ 未开始 | | | |
| 2.7 | 新增 `POST /api/stream/stop` 路由 | `routes/stream_routes.py` | 请求参数：`streamId`；调用 `stop_push`；返回结果 | ⬜ 未开始 | | | |
| 2.8 | 新增 `GET /api/stream/push-status` 路由 | `routes/stream_routes.py` | 返回所有推流状态或指定 stream_id 状态 | ⬜ 未开始 | | | |
| 2.9 | ffmpeg 推流命令参数调优 | `stream_push.py` | 添加 `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5` 等 RTSP 重连参数 | ⬜ 未开始 | | | |
| 2.10 | 推流进程异常退出自动感知 | `stream_push.py` | 后台线程/协程定期 poll 检查进程状态，异常退出时触发短期重试 | ⬜ 未开始 | | | |
| 2.11 | 单元验证：start → 确认 ffmpeg 进程启动 | 手动测试 | 调用 start，查看进程列表中有对应 ffmpeg | ⬜ 未开始 | | | |
| 2.12 | 单元验证：stop → 确认 ffmpeg 进程终止 | 手动测试 | 调用 stop，查看进程列表中 ffmpeg 消失 | ⬜ 未开始 | | | |
| 2.13 | 单元验证：force=True 覆盖式启动 | 手动测试 | start → start(force=True) → 旧进程终止，新进程启动 | ⬜ 未开始 | | | |
| 2.14 | 单元验证：重试 3 次后进入冷却 | 手动测试 | 模拟 RTSP 不可用，验证 3 次重试后状态变为 FAILED + cooldown | ⬜ 未开始 | | | |
| 2.15 | 回归验证：现有 open/play/close 不受影响 | 手动测试 | 现有链路正常工作 | ⬜ 未开始 | | | |
| 2.16 | start 前轻量 RTSP 可达验证 | `stream_push.py` | start 启动 ffmpeg 前，先对 session 缓存的 `rtspUrl` 做一次轻量探测（TCP 连接测试或 ffprobe 快速超时 3s），失败则直接返回错误，不启动推流。避免 open 与 start 之间 RTSP 失效导致的竞态问题 | ⬜ 未开始 | | | |
| 2.17 | 单元验证：RTSP 不可达时 start 返回错误 | 手动测试 | 模拟 RTSP 不可用，调用 start，确认返回失败而不是启动 ffmpeg | ⬜ 未开始 | | | |

### 6.3 推流状态定义（4 状态）

```text
INIT     → 已创建，尚未推流
PUSHING  → ffmpeg 进程运行中，正在推流（含重试中）
STOPPED  → 已停止（正常停止或中心调用 stop）
FAILED   → 重试耗尽，进入冷却期
```

### 6.4 start_push 核心逻辑（伪代码）

```python
def start_push(stream_id: str, rtsp_url: str, push_url: str, force: bool = False) -> PushResult:
    ctx = _push_processes.get(stream_id)
    if ctx is not None:
        if ctx.state == "FAILED" and ctx.cooldown_until and time.time() < ctx.cooldown_until:
            return PushResult(success=False, state="FAILED", message="冷却中", cooldown_remaining=...)
        if ctx.proc and ctx.proc.poll() is None:
            if not force:
                return PushResult(success=True, state="PUSHING", message="已在推流")
            ctx.proc.terminate()
            ctx.proc.wait(timeout=5)
    cmd = ["ffmpeg", "-rtsp_transport", "tcp", "-i", rtsp_url, "-c", "copy", "-f", "flv", push_url]
    proc = subprocess.Popen(cmd, ...)
    _push_processes[stream_id] = PushContext(proc=proc, state="PUSHING", ...)
    return PushResult(success=True, state="PUSHING", message="推流已启动")
```

### 6.5 分阶段重试流程

```text
ffmpeg 退出（非正常）
  ├─ retry_count < 3 → 等待 3 秒 → 重新启动 ffmpeg → retry_count++
  └─ retry_count >= 3 → state = FAILED, cooldown_until = now + 30s
                         └─ 30 秒后允许再次 start（重置 retry_count）
```

---

## 七、阶段三：联调与兜底策略

### 7.1 目标

与中心服务（ZLMediaKit）联调，确保推流链路通畅；保留现有 play 接口作为兜底。

### 7.2 详细任务清单

| 序号 | 任务 | 涉及文件 | 具体内容 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|------|----------|----------|----------|----------|----------|----------|
| 3.1 | 与中心 ZLMediaKit 联调推流 | 联调 | 边缘 start → RTMP 推流到中心 → 中心生成 HLS → 浏览器播放 m3u8 | ⬜ 未开始 | | | |
| 3.2 | 验证中心调用 stop 可正确终止推流 | 联调 | 中心发 POST /api/stream/stop → 边缘终止 ffmpeg | ⬜ 未开始 | | | |
| 3.3 | 验证 force=True 覆盖式启动 | 联调 | 中心检测流断 → 调用 start(force=True) → 边缘重启推流 | ⬜ 未开始 | | | |
| 3.4 | 保留 play 接口兜底 | `routes/stream_routes.py` | 不删除现有 play 接口，作为中心不可用时的直连兜底方案 | ⬜ 未开始 | | | |
| 3.5 | 添加推流监控日志 | `stream_push.py` | 推流启动/停止/重试/失败均有结构化日志 | ⬜ 未开始 | | | |
| 3.6 | 端到端验证：完整播放链路 | 联调 | 服务端调用 open → 获取 streamId → 调用 start → 中心输出 HLS → 前端播放 | ⬜ 未开始 | | | |

---

## 八、阶段四：历史视频闲时上传（可选）

### 8.1 目标

边缘在空闲时段将已完成的历史视频上传到中心服务器，由中心统一存储和播放。

### 8.2 详细任务清单

| 序号 | 任务 | 涉及文件 | 具体内容 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|------|----------|----------|----------|----------|----------|----------|
| 4.1 | 设计上传策略 | 设计文档 | 闲时判断逻辑（无下载/转码任务时）、上传队列、断点续传 | ⬜ 未开始 | | | |
| 4.2 | 实现上传接口调用 | `tasks/upload.py`（新建） | 调用中心上传 API，分片上传，支持断点续传 | ⬜ 未开始 | | | |
| 4.3 | 闲时调度逻辑 | `runner/` | 在任务调度中增加闲时上传触发 | ⬜ 未开始 | | | |
| 4.4 | 上传进度上报 | `runner/` | 上传进度通过心跳或独立接口上报中心 | ⬜ 未开始 | | | |
| 4.5 | 联调验证 | 联调 | 边缘上传 → 中心接收 → 中心可播放 | ⬜ 未开始 | | | |

> **注意**：阶段四为可选阶段，不阻塞阶段一~三的主线开发。视业务需求决定是否实施。

---

## 九、不合理之处与解决方案

### 9.1 ⚠️ open 接口目前不返回 RTSP URL 给中心

**问题**：当前 open 接口返回 `publicPlayUrl` 和 `lanPlayUrl`（都是 play 接口地址），但中心调用 start 推流时需要知道 `rtspUrl` 和 `streamId`。如果 start 接口要求调用方传入 `rtspUrl`，意味着中心需要自己拼 RTSP URL，这与"边缘负责 RTSP 解析"的职责划分矛盾。

**解决方案**：

- **方案 A（推荐）**：`open` 接口额外返回 `rtspUrl`（已解析的可播 RTSP 地址）和 `streamId`，中心拿到后直接传给 `start`
- **方案 B**：`start` 接口不要求传 `rtspUrl`，而是边缘根据 `streamId` 自行查找已缓存的 session 中的 `rtspUrl`

**建议采用方案 B**：这样中心不需要感知 RTSP URL，只需管理 `streamId`。start 接口只需 `streamId` + `pushUrl`。

| 序号 | 补充任务 | 涉及文件 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|----------|----------|----------|----------|----------|----------|
| fix-1.1 | open 返回新增 `streamId` | `routes/stream_routes.py` | ⬜ 未开始 | | | |
| fix-1.2 | start 接口通过 `streamId` 查找 session 获取 `rtspUrl` | `stream_push.py` + `stream_proxy.py` | ⬜ 未开始 | | | |
| fix-1.3 | start 接口参数简化为：`streamId` + `pushUrl` + `force` | `routes/stream_routes.py` | ⬜ 未开始 | | | |

### 9.2 ⚠️ 推流目标地址（pushUrl）的来源未明确

**问题**：`start` 接口需要 `pushUrl`（RTMP 推流地址），这个地址应由中心生成并下发，而非边缘自行拼接。

**解决方案**：中心调用 `start` 时传入 `pushUrl`，格式为：`rtmp://{center_host}/live/{stream_id}`。边缘不感知中心地址，只负责执行推流。

### 9.3 ⚠️ 冷却期 30 秒可能不够灵活

**问题**：固定 30 秒冷却期在某些场景（NVR 重启、网络恢复）下可能过长或过短。

**解决方案**：冷却时间通过环境变量 `EDGE_PUSH_COOLDOWN_SEC` 配置，默认 30 秒。同时 `start(force=True)` 可以**无视冷却期**强制启动，供中心在确认问题已解决后使用。

### 9.4 ⚠️ 现有 play 接口与新推流模式的关系需明确

**问题**：新增 start/stop 推流后，现有 play 接口（边缘直出 MPEG-TS）是保留还是废弃？

**解决方案**：**保留 play 接口作为兜底**。当中心不可用或 HLS 链路异常时，可以临时回退到边缘直出模式。未来根据实际情况决定是否废弃。

### 9.5 ⚠️ open 与 start 之间的 RTSP 有效性竞态

**问题**：open 返回 streamId 后，到中心调用 start 之间可能存在时间差。在此期间 RTSP 源可能变得不可用（偶发网络抨动、NVR 重启等），导致 ffmpeg 启动失败但中心认为"应该有流"。

**解决方案**：
- start 启动 ffmpeg 前，边缘先做一次轻量 RTSP 可达验证（TCP 连接测试或 ffprobe 快速超时 3s）
- 验证失败则 start 直接返回错误（不启动 ffmpeg），中心可决定是否重新 open
- 不采用中心校验 `validatedAt` 的方式，因为这违反"中心不感知 RTSP"的约束 R6

| 序号 | 补充任务 | 涉及文件 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|----------|----------|----------|----------|----------|----------|
| fix-5.1 | start 前轻量 RTSP 可达验证 | `stream_push.py` | ⬜ 未开始 | | | |
| fix-5.2 | 验证失败时返回明确错误代码 | `stream_push.py` + `routes/stream_routes.py` | ⬜ 未开始 | | | |

### 9.6 ⚠️ stream_id 与现有 session_id 的关系需梳理

**问题**：当前 open 接口已返回 `sessionId`，新增 `streamId` 后两者含义不同但容易混淆。

**解决方案**：
- `sessionId`：**会话标识**，每次 open 可能不同（不复用时），用于边缘内部管理
- `streamId`：**流标识**，同一摄像头同一码流始终相同，用于推流和中心管理
- 在接口文档和代码注释中明确区分

### 9.7 ⚠️ 相同型号 NVR 的快速探索逻辑无法在客户端之间共享

**问题**：当前通道快速探索主要依赖设备级缓存（`nvr_device_id + web_channel_num`）和本机规则推断。对于同型号的新 NVR，第一次接入时仍需要在每台客户端本地重新探索，无法直接复用其他客户端已经验证过的“高概率探索顺序”。

**目标边界**：
- 不共享某台具体设备的成功结果，不做“同型号固定通道映射”
- 仅共享**型号级探索策略**，例如候选优先级、扩展扫描范围、是否启用数字通道优先
- 运行时仍必须结合设备返回的 `startChan`、`startDChan`、`ipChanNum` 动态生成候选列表

**解决方案（纳入中期优化，不立即实施）**：
- 边缘新增“型号级探索策略”拉取与本地缓存能力，支持从中心获取按型号下发的探索模板
- 运行时优先级调整为：**设备级缓存 > 同 NVR 偏移量 > 中心下发的型号级策略 > 代码内置默认规则 > 兜底扫描**
- 边缘仅缓存策略版本和策略内容，不把其他客户端的具体设备命中结果直接落到本机设备缓存
- 策略更新采用版本感知机制，避免每次都重新发版客户端

| 序号 | 补充任务 | 涉及文件 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|----------|----------|----------|----------|----------|----------|
| fix-7.1 | 设计型号级探索策略本地数据结构 | `video/hik/download.py` + `db.py` | ⬜ 未开始 | | | |
| fix-7.2 | 边缘新增策略拉取与版本缓存能力 | `web.py` + `api_client` 相关模块 | ⬜ 未开始 | | | |
| fix-7.3 | 通道候选生成逻辑支持“远端策略覆盖内置规则” | `video/hik/download.py` | ⬜ 未开始 | | | |
| fix-7.4 | 运行时保持设备级缓存优先，不允许型号策略覆盖已验证设备映射 | `video/hik/download.py` | ⬜ 未开始 | | | |
| fix-7.5 | 新增本地缓存验证：客户端重启后仍可使用上次同步的型号策略 | 手动测试 | ⬜ 未开始 | | | |
| fix-7.6 | 联调验证：中心调整同型号策略后，无需发版，边缘客户端自动生效 | 联调 | ⬜ 未开始 | | | |

---

## 十、依赖与风险

| 风险项 | 影响 | 缓解措施 |
|--------|------|----------|
| 中心 ZLMediaKit 部署延迟 | 阶段三无法联调 | 边缘侧阶段一、二可独立开发和测试 |
| NVR 不稳定导致推流频繁重试 | 边缘资源浪费 | 分阶段重试 + 冷却机制 + force 覆盖 |
| VPN 网络带宽不足 | 推流卡顿 | 优先使用子码流（sub），`-c copy` 不转码 |
| ffmpeg 进程"假死" | 边缘认为在推流但实际无数据 | 中心检测流断 → 调用 start(force=True) |

---

## 十一、完成标准

| 阶段 | 完成标准 |
|------|----------|
| 阶段一 | open 接口返回含 `streamId`，格式正确，现有 play 链路不受影响 |
| 阶段二 | start/stop 接口可用，ffmpeg 推流进程可管理，重试/冷却机制生效 |
| 阶段三 | 端到端链路通畅：open → start → RTMP推流 → HLS播放，中心可 stop |
| 阶段四 | 历史视频可闲时上传到中心（可选） |

---

## 十二、状态标注说明

| 标记 | 含义 |
|------|------|
| ⬜ 未开始 | 尚未开始实施 |
| 🔄 进行中 | 正在开发/测试 |
| ✅ 已完成 | 开发完成且自测通过 |
| ❌ 已废弃 | 需求变更，不再实施 |
| ⚠️ 阻塞中 | 遇到问题，需外部协助 |
