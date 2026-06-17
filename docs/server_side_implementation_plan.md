# 服务端（SmartCampus 后端）实时流中期优化实施计划

> 文档版本：v1.1（整合 yh.md 优化建议：节点心跳失效机制、streamId 缓存、播放流程优化）  
> 创建日期：2026-04-03  
> 基于文档：`plan-01.md`（二次评审）、`plan.md`（分析报告）、`edge_service_optimization_plan.md`、`center_service_implementation_plan.md`、`yh.md`（优化建议）  
> 核心原则：**解决真实问题，禁止过度优化，业务层轻量编排**

---

## 一、服务端在三层架构中的定位

```text
┌──────────┐       ┌───────────────────┐       ┌──────────────┐       ┌──────────────┐
│ 前端/App  │──────→│ 服务端(SmartCampus) │──────→│  中心服务     │──────→│   边缘服务    │
│          │← UI ──│  (业务编排层)       │← 状态 │ (流媒体管理)  │← RTMP │ (ffmpeg推流)  │
└──────────┘       └───────────────────┘       └──────────────┘       └──────────────┘
```

| 层级 | 角色 | 核心职责 |
|------|------|----------|
| **前端** | 展示层 | 发起播放请求、展示 HLS 播放器、观看心跳 |
| **服务端（本计划）** | 业务编排层 | 权限校验、设备信息查询、调用中心/边缘、返回播放地址、业务日志 |
| **中心服务** | 流媒体管理层 | ZLMediaKit、推流生命周期、超时回收、HLS 分发 |
| **边缘服务** | 推流执行层 | RTSP 探测、ffmpeg 推流、start/stop 执行 |

### 服务端 vs 中心服务的边界

| 职责 | 服务端（SmartCampus） | 中心服务 |
|------|----------------------|----------|
| 用户鉴权 / 权限校验 | ✅ | ❌ |
| 设备信息查询（NVR IP、账号等） | ✅ | ❌ |
| 校区 → 边缘节点路由 | ✅ | ❌ |
| 前端接口（播放请求、历史记录等） | ✅ | ❌ |
| 触发边缘 open/start/stop | ❌（委托中心或直接调用） | ✅ |
| ZLMediaKit 管理 | ❌ | ✅ |
| HLS 分片 / 流状态监控 | ❌ | ✅ |
| 推流超时回收 / 失败反控 | ❌ | ✅ |

> **关键决策**：服务端是"业务入口 + 权限守门人 + 设备信息提供者"，不直接管理流媒体。

---

## 二、核心约束（红线）

| 编号 | 约束 | 说明 |
|------|------|------|
| R1 | 服务端不直接管理 ffmpeg / RTMP / HLS | 流媒体管理交给中心服务 |
| R2 | 服务端不维护推流进程状态 | 推流状态查询透传到中心 |
| R3 | 服务端不引入新的中间件 | 不加 MQ / Redis（除非已有） |
| R4 | 服务端不做流调度 | 调度由中心负责 |
| R5 | 最小改动原则 | 在现有 SmartCampus 后端基础上新增接口，不重构 |
| R6 | 与现有技术栈一致 | 使用现有框架（Java Spring / 其他），不引入新语言 |

---

## 三、实施阶段总览

```text
阶段一：数据准备 — 设备信息、校区-边缘映射接口          → 为中心/边缘提供数据支撑
阶段二：实时播放业务接口                               → 前端请求播放的业务入口
阶段三：播放状态与历史记录                             → 查询、审计、运维支撑
阶段四：前端对接与联调                                 → 端到端打通
阶段五：历史视频集中播放（可选）                        → 配合边缘上传 + 中心存储
```

---

## 四、阶段一：数据准备 — 设备信息与边缘节点映射

### 4.1 目标

为中心服务调用边缘节点提供必要的设备信息和路由信息。中心或服务端触发边缘 open 时，需要知道 NVR 的 IP、端口、账号密码等，这些数据由服务端数据库持有。

### 4.2 详细任务清单

| 序号 | 任务 | 具体内容 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|------|----------|----------|----------|----------|----------|
| 1.1 | 梳理现有 NVR 设备表结构 | 确认 NVR 设备表中已有字段：设备ID、IP、端口、账号、密码、校区编码、在线状态 | ⬜ 未开始 | | | |
| 1.2 | 梳理现有通道表结构 | 确认通道表中已有字段：通道号、通道ID、所属设备ID | ⬜ 未开始 | | | |
| 1.3 | 新增/确认「边缘节点」表或配置 | 确保有 `edge_node` 表或配置：`node_id`, `campus_code`, `base_url`（边缘可达地址）, `status`, `last_seen_at`（最后心跳时间） | ⬜ 未开始 | | | |
| 1.4 | 新增接口：查询设备连接信息 | `GET /api/server/nvr/connection-info?nvrDeviceId=xxx`<br>返回：ipAddress, port, account, password, campusCode, provider<br>**仅限内部调用（中心服务）** | ⬜ 未开始 | | | |
| 1.5 | 新增接口：查询校区对应边缘节点 | `GET /api/server/edge-node?campusCode=xxx`<br>返回：nodeId, baseUrl, status<br>**仅限内部调用** | ⬜ 未开始 | | | |
| 1.6 | 边缘节点注册更新 + 心跳失效机制 | 边缘服务通过已有 `registerInfo` 每 30 秒心跳更新 `edge_node` 的 `base_url`、`status`、`last_seen_at`；服务端定时检查 `now - last_seen_at > 90s` → 标记节点不可用；节点恢复心跳后自动标记可用。避免"幽灵节点"问题 | ⬜ 未开始 | | | |
| 1.7 | 接口鉴权：内部调用认证 | 1.4 和 1.5 仅允许中心服务调用，增加内部 token / IP 白名单校验 | ⬜ 未开始 | | | |
| 1.8 | 单元验证 | 调用 1.4、1.5 接口，确认数据正确返回 | ⬜ 未开始 | | | |

### 4.3 数据流向

```text
前端请求播放(campusCode, nvrDeviceId, channelNum)
  │
  ├─ 服务端查 edge_node 表 → 获取边缘 base_url
  ├─ 服务端查 NVR 设备表   → 获取 ip, port, account, password, provider
  │
  └─ 将上述信息打包 → 传递给中心服务 → 中心调用边缘 open/start
```

---

## 五、阶段二：实时播放业务接口

### 5.1 目标

提供前端"请求播放实时流"的业务入口。前端无需感知边缘/中心的存在，只与服务端交互。

### 5.2 详细任务清单

| 序号 | 任务 | 具体内容 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|------|----------|----------|----------|----------|----------|
| 2.1 | 新增接口：请求实时播放 | `POST /api/server/stream/play`<br>参数：`campusCode`, `nvrDeviceId`, `channelNum`, `streamProfile`（可选，默认 sub）<br>返回：`hlsUrl`, `streamId`, `state` | ⬜ 未开始 | | | |
| 2.2 | play 接口：权限校验 | 校验当前登录用户是否有权限查看该校区/设备的视频流 | ⬜ 未开始 | | | |
| 2.3 | play 接口：查询设备信息 | 从数据库获取 NVR 连接信息（IP、账号密码等） | ⬜ 未开始 | | | |
| 2.4 | play 接口：查询边缘节点 | 根据 campusCode 查找对应的边缘节点 base_url | ⬜ 未开始 | | | |
| 2.5 | play 接口：调用中心服务 | 将设备信息 + 边缘节点地址 → 调用中心 `POST /api/center/stream/play-request`<br>中心负责触发边缘 open → start → 返回 hlsUrl | ⬜ 未开始 | | | |
| 2.6 | play 接口：返回播放地址 | 将中心返回的 `hlsUrl` + `streamId` 包装后返回前端 | ⬜ 未开始 | | | |
| 2.7 | play 接口：异常处理 | 边缘不可达 → 返回错误码；中心超时 → 返回错误码；设备不存在 → 返回 404 | ⬜ 未开始 | | | |
| 2.8 | 新增接口：停止播放 | `POST /api/server/stream/stop`<br>参数：`streamId`<br>转发到中心 `POST /api/center/stream/stop` | ⬜ 未开始 | | | |
| 2.9 | 新增接口：观看心跳 | `POST /api/server/stream/heartbeat`<br>参数：`streamId`<br>转发到中心 `POST /api/center/stream/heartbeat`<br>（如果采用前端心跳方案） | ⬜ 未开始 | | | |
| 2.10 | 接口文档更新 | 在 API 文档（Swagger / 接口文档）中新增上述接口说明 | ⬜ 未开始 | | | |
| 2.11 | 单元验证 | 模拟前端调用 play → 服务端正确调用中心 → 返回 hlsUrl | ⬜ 未开始 | | | |
| 2.12 | streamId 缓存机制 | 服务端内存缓存 `(campusCode + nvrDeviceId + channelNum) → streamId`，TTL 30~60 秒。有缓存时跳过 open 直接调用中心 start，无缓存时先 open 再 start。大幅缩短二次播放延迟 | ⬜ 未开始 | | | |
| 2.13 | streamId 缓存失效处理 | 缓存的 streamId 对应的 start 失败时，清除缓存并回退到完整 open → start 流程 | ⬜ 未开始 | | | |
| 2.14 | 单元验证：缓存命中时跳过 open | 手动测试 | 首次 play → open + start；立即再次 play → 跳过 open，直接 start | ⬜ 未开始 | | | |

### 5.3 请求播放核心流程

```text
前端 POST /api/server/stream/play
  │
  ├─ 1. 鉴权：校验用户权限
  │
  ├─ 2. 查边缘节点：campusCode → edge_node.base_url + 检查节点可用性（last_seen_at < 90s）
  │   └─ 节点不可用 → 返回错误
  │
  ├─ 3. 查 streamId 缓存：(campusCode + nvrDeviceId + channelNum)
  │   ├─ 命中 → 跳过步骤 4，直接到步骤 5
  │   └─ 未命中 → 继续
  │
  ├─ 4. 查设备 + open：NVR 连接信息 → 调用边缘 open → 获取 streamId → 写入缓存
  │
  ├─ 5. 调中心：POST /api/center/stream/start-by-id
  │      Body: { streamId, edgeBaseUrl }
  │   ├─ 失败且缓存命中 → 清除缓存 → 回退到步骤 4
  │   └─ 成功 → 继续
  │
  ├─ 6. 返回：{ streamId, state: "PREPARING", hlsUrl: null }
  │      前端开始轮询 status
  │
  └─ 7. 前端轮询 GET /api/server/stream/status?streamId=xxx
       ├─ PREPARING → 继续等待
       ├─ PUSHING   → 获取 hlsUrl → 播放
       └─ FAILED    → 显示错误
```

### 5.4 调用链路设计（两种模式）

#### 模式 A：服务端 → 中心 → 边缘（推荐）

```text
前端 → 服务端 → 中心服务 → 边缘服务
                   ↑
          服务端提供设备信息 + 边缘地址
          中心负责调用边缘 open/start
```

**优点**：服务端只与中心交互，链路清晰；中心统一管理推流生命周期。

#### 模式 B：服务端 → 边缘 + 中心（备选）

```text
前端 → 服务端 → 边缘（open，获取 streamId）
              → 中心（start，传入 streamId + pushUrl）
```

**优点**：服务端直接控制流程；**缺点**：服务端需要了解更多流媒体细节。

**建议采用模式 A**：服务端只做业务编排，将设备信息和边缘地址一次性传给中心，由中心负责后续所有流媒体操作。

---

## 六、阶段三：播放状态与历史记录

### 6.1 目标

提供流状态查询和播放历史记录，支持运维排障和审计。

### 6.2 详细任务清单

| 序号 | 任务 | 具体内容 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|------|----------|----------|----------|----------|----------|
| 3.1 | 新增接口：查询流状态 | `GET /api/server/stream/status?streamId=xxx`<br>透传中心 `GET /api/center/stream/status` 的结果<br>返回：state, hlsUrl, pushDuration, viewerCount | ⬜ 未开始 | | | |
| 3.2 | 新增接口：查询当前活跃流列表 | `GET /api/server/stream/active-list?campusCode=xxx`<br>查询指定校区当前正在推流的通道列表 | ⬜ 未开始 | | | |
| 3.3 | 新增表：播放记录表 | `stream_play_log`：`id`, `user_id`, `campus_code`, `nvr_device_id`, `channel_num`, `stream_id`, `hls_url`, `play_start_time`, `play_end_time`, `duration_sec`, `result`（success/fail/timeout） | ⬜ 未开始 | | | |
| 3.4 | play 接口写入播放记录 | 每次 play 请求成功后写入 `stream_play_log` | ⬜ 未开始 | | | |
| 3.5 | stop / 超时回调更新记录 | 流停止时更新 `play_end_time` 和 `duration_sec`<br>可通过中心 webhook 回调或服务端定时同步 | ⬜ 未开始 | | | |
| 3.6 | 新增接口：查询播放历史 | `GET /api/server/stream/play-log?campusCode=xxx&startDate=xxx&endDate=xxx`<br>分页查询播放记录 | ⬜ 未开始 | | | |
| 3.7 | 单元验证 | 播放 → 查状态 → 停止 → 查记录，全链路数据正确 | ⬜ 未开始 | | | |

### 6.3 stream_play_log 数据模型

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigint | 主键 |
| user_id | bigint | 发起播放的用户 ID |
| user_name | varchar(64) | 用户名（冗余，便于查询） |
| campus_code | varchar(64) | 校区编码 |
| nvr_device_id | int | NVR 设备 ID |
| channel_num | int | 通道号 |
| stream_id | varchar(128) | 流唯一标识 |
| hls_url | varchar(256) | HLS 播放地址 |
| play_start_time | datetime | 播放开始时间 |
| play_end_time | datetime | 播放结束时间（可为空） |
| duration_sec | int | 播放时长（秒） |
| result | varchar(16) | success / fail / timeout |
| fail_reason | varchar(256) | 失败原因（可为空） |
| created_at | datetime | 记录创建时间 |

---

## 七、阶段四：前端对接与联调

### 7.1 目标

前端通过服务端提供的接口完成实时流播放全流程。

### 7.2 详细任务清单

| 序号 | 任务 | 具体内容 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|------|----------|----------|----------|----------|----------|
| 4.1 | 前端播放页面适配 | 点击"实时播放"按钮 → 调用 `POST /api/server/stream/play` → 获取 hlsUrl → hls.js 播放 | ⬜ 未开始 | | | |
| 4.2 | 前端心跳集成（如果采用） | 播放中每 30 秒调用 `POST /api/server/stream/heartbeat` | ⬜ 未开始 | | | |
| 4.3 | 前端停止/关闭处理 | 页面关闭 / 切换 → 调用 `POST /api/server/stream/stop` 或依赖超时自动回收 | ⬜ 未开始 | | | |
| 4.4 | 前端错误处理 | 播放失败提示、重试按钮、加载 loading 状态 | ⬜ 未开始 | | | |
| 4.5 | 前端播放状态展示 | 显示当前流状态（加载中 / 播放中 / 已断开） | ⬜ 未开始 | | | |
| 4.6 | 端到端联调 | 前端 → 服务端 → 中心 → 边缘，全链路打通 | ⬜ 未开始 | | | |
| 4.7 | 多人同时播放验证 | 多个前端同时请求同一路流 → 只触发一次推流 → 多人播放同一 HLS | ⬜ 未开始 | | | |
| 4.8 | 不同校区切换验证 | 前端切换校区后请求播放 → 服务端正确路由到对应边缘节点 | ⬜ 未开始 | | | |
| 4.9 | 权限拦截验证 | 无权限用户请求播放 → 服务端返回 403 | ⬜ 未开始 | | | |

### 7.3 前端播放时序

```text
用户点击"实时播放"
  │
  ├─ 前端显示 Loading
  │
  ├─ POST /api/server/stream/play → 等待响应
  │   ├─ 成功 → 获取 hlsUrl → hls.js 加载 m3u8 → 播放
  │   └─ 失败 → 显示错误提示 + 重试按钮
  │
  ├─ 播放中：每 30 秒 POST /api/server/stream/heartbeat
  │
  └─ 页面关闭 / 用户点停止：POST /api/server/stream/stop
```

---

## 八、阶段五：历史视频集中播放（可选）

### 8.1 目标

已完成的历史视频上传到中心后，前端通过服务端统一获取播放地址。

### 8.2 详细任务清单

| 序号 | 任务 | 具体内容 | 完成状态 | 完成时间 | 自测结果 | 自测时间 |
|------|------|----------|----------|----------|----------|----------|
| 5.1 | 新增接口：查询历史视频列表 | `GET /api/server/video/list?campusCode=xxx&date=xxx`<br>返回已上传到中心的历史视频列表 | ⬜ 未开始 | | | |
| 5.2 | 新增接口：获取历史视频播放地址 | `GET /api/server/video/play-url?videoId=xxx`<br>返回中心侧的点播地址（HLS 或直接文件 URL） | ⬜ 未开始 | | | |
| 5.3 | 视频上传状态同步 | 边缘上传完成后通知服务端（或中心通知服务端）更新视频状态 | ⬜ 未开始 | | | |
| 5.4 | 前端历史视频页面 | 历史视频列表 + 点播播放器 | ⬜ 未开始 | | | |
| 5.5 | 联调验证 | 边缘上传 → 中心存储 → 服务端查询 → 前端播放 | ⬜ 未开始 | | | |

> **注意**：阶段五为可选阶段，依赖边缘阶段四和中心阶段五同时就绪。

---

## 九、不合理之处与解决方案

### 9.1 ⚠️ 服务端与中心服务的职责可能重叠

**问题**：中心服务计划中已定义了 `POST /api/center/stream/play-request` 等接口，如果服务端只是简单透传，是否有必要单独建一层？

**解决方案**：

服务端层**不是简单透传**，它提供以下中心服务不具备的能力：

| 能力 | 服务端 | 中心服务 |
|------|--------|----------|
| 用户鉴权 / 权限校验 | ✅ | ❌ |
| NVR 设备连接信息（IP/账号/密码） | ✅ 持有 | ❌ 需要服务端提供 |
| 校区 → 边缘节点路由 | ✅ 持有 | ❌ 需要服务端提供 |
| 播放历史记录 / 审计 | ✅ | ❌ |
| 前端统一网关 | ✅ | ❌ |

**结论**：服务端层必要，但应保持轻量——只做鉴权、数据查询、转发，不做流媒体管理。

### 9.2 ⚠️ 设备敏感信息传递的安全性

**问题**：服务端需要将 NVR 的 IP、账号、密码传递给中心服务，中心再传给边缘。如果中心和服务端不在同一台机器，存在敏感信息在网络中传输的风险。

**解决方案**：

- **方案 A（推荐）**：设备信息不经过中心。服务端直接告诉中心"用哪个边缘节点的哪个 streamId"，而非传递 NVR 账号密码。设备信息由边缘自行缓存（边缘 open 时已经缓存了）。
- **方案 B**：中心与服务端之间走内网通信 + HTTPS + 内部 token 认证。

**建议采用方案 A**：

```text
服务端 → 边缘: POST /api/stream/open（含设备信息）→ 返回 streamId
服务端 → 中心: POST /api/center/stream/start-by-id（只传 streamId + edgeBaseUrl）
中心   → 边缘: POST /api/stream/start（只传 streamId + pushUrl）
```

这样设备敏感信息只在"服务端 → 边缘"之间传递（VPN 内网），不经过中心。

### 9.3 ⚠️ 调用链路过长导致延迟

**问题**：前端 → 服务端 → 中心 → 边缘，链路 3 跳。如果每跳 1~2 秒，用户等待 3~6 秒才能开始播放。

**解决方案**：

- 边缘 open 最耗时（RTSP 探测），需 2~5 秒，其余调用 < 500ms
- 服务端可以**异步返回**：先返回"正在准备"，前端轮询状态，就绪后自动播放
- 或者**流水线化**：服务端直接调用边缘 open，不等中心，拿到 streamId 后同时通知中心 start

**建议流水线优化 + streamId 缓存**：

```text
首次播放（无缓存）：
  1. 服务端 → 边缘 open（2~5 秒，最耗时）→ 获取 streamId
  2. 服务端缓存 (campusCode+nvrDeviceId+channelNum) → streamId（TTL 30~60s）
  3. 服务端 → 中心 start（传入 streamId + edgeBaseUrl）→ < 500ms
  4. 中心 → 边缘 start → 边缘开始推流
  5. 等待 HLS 首片生成（2~3 秒，可能更长）
  6. 返回 hlsUrl

二次播放（缓存命中）：
  1. 服务端查缓存 → 命中 streamId → 跳过 open
  2. 服务端 → 中心 start（streamId + edgeBaseUrl）
  3. 如果 start 失败（streamId 失效）→ 清除缓存 → 回退到完整 open → start 流程
```

总延迟：首次 ≈ open 耗时 + HLS 首片延迟 ≈ 4~8 秒；二次 ≈ start + HLS 首片 ≈ 2~5 秒。可通过"先返回 loading → 就绪后播放"改善用户体验。

### 9.4 ⚠️ 如果边缘已有 open 缓存，链路可大幅缩短

**问题**：边缘 RTSP 探测结果有缓存机制（`edge_nvr_rtsp_probe_cache`），如果缓存命中，open 可在 < 1 秒完成。

**解决方案**：首次播放较慢，后续复用缓存后延迟大幅降低。服务端无需额外处理，边缘缓存机制自动生效。

### 9.5 ⚠️ 服务端需要感知"流已就绪"的时机

**问题**：服务端调用中心 start 后，HLS 不是立即可用的，需要等 ZLMediaKit 收到 RTMP 推流并生成首个 m3u8。如果服务端立即返回 hlsUrl，前端可能访问 404。

**解决方案（三选一）**：

| 方案 | 实现 | 优缺点 |
|------|------|--------|
| A：服务端同步等待 | 服务端调用 start 后轮询中心流状态，确认 PUSHING 后再返回 | 简单，但阻塞服务端线程 |
| B：前端轮询 | 服务端立即返回 streamId + state=PREPARING，前端轮询直到 state=PUSHING | 服务端无阻塞，前端稍复杂 |
| C：WebSocket 推送 | 流就绪后服务端通过 WS 通知前端 | 最佳体验，但实现复杂 |

**建议采用方案 B**：前端轮询是最平衡的方案，实现简单，体验可接受。

```text
前端调用 play → 服务端返回 { streamId, state: "PREPARING", hlsUrl: null }
前端每 1 秒轮询 GET /api/server/stream/status?streamId=xxx
  ├─ state=PREPARING → 继续等待（显示 Loading）
  ├─ state=PUSHING   → 获取 hlsUrl → 开始播放
  └─ state=FAILED    → 显示错误
```

### 9.6 ⚠️ 服务端必须缓存 streamId（关键体验优化）

**问题**：每次播放都走完整 open → start 流程，open 需要 2~5 秒（RTSP 探测），用户体验不稳定。

**解决方案**：

服务端内存缓存：

| 项目 | 说明 |
|------|------|
| 缓存 Key | `campusCode + nvrDeviceId + channelNum` |
| 缓存 Value | `streamId` |
| TTL | 30~60 秒（可配置） |
| 命中逻辑 | 有缓存 → 跳过 open，直接 start |
| 未命中逻辑 | 无缓存 → open → 写入缓存 → start |
| 失效处理 | start 失败 → 清除缓存 → 回退完整 open → start |

**效果**：二次播放延迟从 4~8 秒缩短到 2~5 秒。

---

## 十、完整接口清单

### 10.1 前端 → 服务端

| 接口 | 方法 | 说明 | 鉴权 |
|------|------|------|------|
| `/api/server/stream/play` | POST | 请求实时播放 | ✅ 用户登录 + 设备权限 |
| `/api/server/stream/stop` | POST | 停止播放 | ✅ 用户登录 |
| `/api/server/stream/status` | GET | 查询流状态 | ✅ 用户登录 |
| `/api/server/stream/heartbeat` | POST | 观看心跳（可选） | ✅ 用户登录 |
| `/api/server/stream/active-list` | GET | 当前活跃流列表 | ✅ 管理员权限 |
| `/api/server/stream/play-log` | GET | 播放历史记录 | ✅ 管理员权限 |

### 10.2 服务端 → 边缘（直接调用）

| 接口 | 方法 | 说明 | 备注 |
|------|------|------|------|
| `/api/stream/open` | POST | 创建流会话，获取 streamId | 传入设备连接信息 |

### 10.3 服务端 → 中心

| 接口 | 方法 | 说明 | 备注 |
|------|------|------|------|
| `/api/center/stream/start-by-id` | POST | 触发推流（只传 streamId + edgeBaseUrl） | 不传设备敏感信息 |
| `/api/center/stream/stop` | POST | 停止推流 | 转发 |
| `/api/center/stream/status` | GET | 查询流状态 | 透传 |
| `/api/center/stream/heartbeat` | POST | 观看心跳 | 透传 |

### 10.4 中心 → 服务端（回调，可选）

| 接口 | 方法 | 说明 | 备注 |
|------|------|------|------|
| `/api/server/stream/callback/stopped` | POST | 流已停止回调 | 用于更新播放记录 |
| `/api/server/stream/callback/failed` | POST | 流失败回调 | 用于更新播放记录 |

---

## 十一、与边缘/中心计划的对应关系

| 服务端阶段 | 依赖的边缘阶段 | 依赖的中心阶段 | 说明 |
|-----------|--------------|--------------|------|
| 阶段一：数据准备 | 无 | 无 | 服务端独立完成 |
| 阶段二：播放接口 | 阶段一（open 补充 streamId） | 阶段二（流管理模块） | 需要 streamId 和中心 start 接口 |
| 阶段三：状态与记录 | — | 阶段二（流状态查询） | 需要中心状态接口 |
| 阶段四：前端联调 | 阶段三（联调） | 阶段三（前端对接） | 全部就绪后端到端联调 |
| 阶段五：历史视频 | 阶段四（上传） | 阶段五（接收） | 可选，全部就绪后联调 |

---

## 十二、推荐实施顺序（时间线）

```text
Week 1-2:  服务端阶段一（数据准备） ← 与边缘阶段一、中心阶段一并行
           ↓
Week 3-4:  服务端阶段二（播放接口） ← 与边缘阶段二、中心阶段二并行
           ↓
Week 5:    服务端阶段三（状态与记录）
           ↓
Week 5-6:  服务端阶段四（前端联调） ← 三方联调
           ↓
Week 7+:   可选：服务端阶段五（历史视频播放）
```

---

## 十三、依赖与风险

| 风险项 | 影响 | 缓解措施 |
|--------|------|----------|
| 中心服务接口未就绪 | 服务端阶段二无法联调 | 服务端先 mock 中心接口，开发自测 |
| 边缘 open 接口未补充 streamId | 服务端无法获取 streamId | 服务端可临时用 sessionId 替代 |
| 前端 hls.js 兼容性 | 部分浏览器不支持 | 使用 hls.js polyfill，覆盖主流浏览器 |
| 调用链路延迟 | 用户等待过长 | streamId 缓存 + 异步返回 + 前端轮询状态 |
| NVR 账号密码传输安全 | 信息泄露 | 敏感信息只在服务端→边缘间传递（VPN内网） |
| 边缘节点"幽灵节点" | 服务端用失效地址调用边缘 | edge_node 心跳失效机制（last_seen_at > 90s → 不可用） |
| streamId 缓存过期/失效 | 用 stale streamId 调 start 失败 | 缓存失效自动回退完整 open → start 流程 |

---

## 十四、完成标准

| 阶段 | 完成标准 |
|------|----------|
| 阶段一 | 设备信息接口、边缘节点查询接口可用，数据正确 |
| 阶段二 | play 接口可调通，权限校验生效，能返回 hlsUrl |
| 阶段三 | 流状态可查询，播放记录可写入和查询 |
| 阶段四 | 前端完整播放链路打通，多人复用、权限拦截验证通过 |
| 阶段五 | 历史视频列表可查、点播可播放（可选） |

---

## 十五、状态标注说明

| 标记 | 含义 |
|------|------|
| ⬜ 未开始 | 尚未开始实施 |
| 🔄 进行中 | 正在开发/测试 |
| ✅ 已完成 | 开发完成且自测通过 |
| ❌ 已废弃 | 需求变更，不再实施 |
| ⚠️ 阻塞中 | 遇到问题，需外部协助 |
