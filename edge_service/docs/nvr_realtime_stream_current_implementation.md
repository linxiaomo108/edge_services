# 实时视频流当前代码实现流程说明

## 1. 文档目的

本文档基于当前代码实现，总结边缘服务中“实时视频流”从接入请求、连接 NVR、构建可播放 RTSP、创建会话、输出 MPEG-TS 流，到停止播放和关闭会话的完整业务流程。

本文档描述的是**当前已落地实现**，不是需求设计稿。

---

## 2. 先看主线：实时视频流是如何跑起来的

当前代码中的实时视频流主链路可以概括为：

1. 服务端调用 `POST /api/stream/open`
2. 边缘服务校验 `enableStreamProxy` 和 `campusCode`
3. 边缘服务根据请求里携带的 NVR 信息与通道信息，解析本次要访问的 RTSP 目标
4. 边缘服务探测 RTSP 地址是否可达，并进一步验证它是否真的能输出 MPEG-TS 首包
5. 探测成功后，边缘服务创建或复用内存中的 `StreamSession`
6. 边缘服务把本次成功结果写回本地设备表、通道表和通道映射缓存，并激活通道
7. 边缘服务把 `sessionId`、`publicPlayUrl`、`lanPlayUrl` 返回给服务端
8. 服务端或播放器访问 `GET /api/stream/play/{nvrDeviceId}/{channelNum}`
9. 边缘服务启动 `ffmpeg` 从 RTSP 拉流，并把输出持续转成 HTTP `video/mp2t`
10. 播放过程中持续保活；如果断流则按既定策略重试恢复
11. 播放停止后回收当前 `ffmpeg` 进程；调用方可再通过 `POST /api/stream/close` 主动释放 session

这条链路里最核心的几个点是：

- **设备定位依赖请求参数，不是先从本地设备表反查凭据再连接 NVR**
- **通道解析有缓存，但实时流链路并不会像下载链路那样做大范围 SDK 通道枚举探索**
- **`open` 成功不代表“地址看起来像对的”，而是代表边缘服务已经验证过“这个 RTSP 实际可播”**
- **`play` 阶段不是返回静态地址，而是现场启动 `ffmpeg` 做 RTSP -> MPEG-TS 中转**
- **稳定性依赖两层机制：播放阶段重试恢复 + 会话/通道保活**

---

## 3. 边缘服务如何定位设备、通道与可播流

## 3.1 边缘服务是通过哪些信息连接 NVR 的

当前实时流链路里，边缘服务连接 NVR 所依赖的关键信息，直接来自 `open` 请求体：

- `nvrDeviceId`
- `nvrChannelNum`
- `nvrChannelId`
- `provider`
- `ipAddress`
- `port`
- `account`
- `password`
- `streamProfile`
- `outputProtocol`

其中：

- `nvrDeviceId`
  - 是业务侧的设备标识
  - 用于本地缓存、会话索引、通道表落库
- `nvrChannelNum`
  - 是业务通道号
  - 当前实时流 RTSP 拼接优先就是用它
- `nvrChannelId`
  - 是业务通道 ID 标识
  - 在 `nvrChannelNum` 不可用时才作为回退解析来源
- `ipAddress`、`port`、`account`、`password`
  - 才是真正用于连接 NVR 的访问参数

这里有一个很重要的实现特征：

- **当前 `open` 阶段不会先根据 `nvrDeviceId` 去本地设备表查询 NVR 凭据，再反向组装连接参数**
- **它是直接使用请求里携带的 IP、端口、账号、密码去探测 RTSP**
- 只有在探测成功并建立 session 后，才会把设备信息和通道信息写回本地库

所以从业务实现上说：

- 本地库在实时流链路中当前更像是**成功结果缓存与状态记录**
- 而不是实时播放前的**唯一数据来源**

## 3.2 实时流的通道是如何解析出来的

`open` 调用内部会进入 `StreamSessionManager.resolve_channel(...)`，解析顺序是：

1. **优先查本地通道映射缓存**
   - 表：`edge_nvr_channel_map`
   - 查询条件：`nvr_device_id + web_channel_num`
   - 命中后直接得到 `sdk_channel`

2. **其次尝试请求显式给出的候选通道**
   - 兼容字段：`sdkChannel`、`sdk_channel`、`channel`、`channelNum`、`channelId`、`id`
   - 命中后会把结果写回 `edge_nvr_channel_map`

3. **再回退为业务通道号 `nvrChannelNum` 本身**
   - 这是当前实时流链路里最常见的路径
   - 也会写回 `edge_nvr_channel_map`

4. **最后再尝试把 `nvrChannelId` 转成正整数通道**
   - 仅在前面都不可用时兜底

解析结果会得到一个 `resolvedChannel`，它的含义更接近：

- “当前业务通道在本地缓存体系中的解析结果”

而不是：

- “本次 RTSP 一定会按这个 SDK 通道去访问 NVR”

## 3.3 当前实时流链路中的“通道探索”到底是什么

这一点要和下载链路区分开。

在下载链路里，通道探索更偏向：

- 枚举候选 SDK 通道
- 尝试不同用户 ID、录像类型、偏移量
- 找到真正有录像的下载入口

但在**当前实时流链路**里，并没有做这种大范围的 SDK 通道扫描。它的“探索”更准确地说是：

- **先解析一个业务侧认为应该访问的通道**
- **再围绕这个通道去探索哪一个 RTSP 地址真正可播**

当前实时流链路的探索重点不在“枚举大量通道”，而在：

- 主码流还是子码流可用
- RTSP 端口 554 还是 18080 可用
- RTSP 虽然能连，但是否真的能产出 MPEG-TS 首包

另外，`open` 路由当前传给 `create_or_get_session(...)` 的 `candidate_channels` 实际是 `None`，因此在当前实现下，实时流的常态路径基本是：

- 查 `edge_nvr_channel_map`
- 没命中就直接回退到 `nvrChannelNum`
- 然后用这个业务通道号去构建 RTSP 路径

所以当前实时流“通道探索”的业务实质是：

- **通道解析相对轻量**
- **可播探测相对严格**

## 3.4 为什么 `resolvedChannel` 和真正 RTSP 使用的通道可能不是一个概念

在 `create_or_get_session(...)` 中，代码明确做了这件事：

- `resolvedChannel` 继续保留解析结果
- 但 `rtsp_channel` 优先使用 `nvrChannelNum`

设计原因是：

- 下载链路里的 SDK 通道缓存，未必适用于 RTSP URL 规则
- RTSP 地址通常更应该按业务通道号来拼接
- 这样可以避免把下载探索结果误用到实时流 RTSP 路径上

也就是说，当前实时流链路里：

- `resolvedChannel` 更偏向**缓存与记录语义**
- `rtsp_channel` 更偏向**实际访问 NVR 的 RTSP 路径语义**

## 3.5 `open` 成功后，本地会记录哪些结果

在 session 创建或复用成功后，边缘服务会补写本地状态：

- `save_nvr_device(...)`
  - 保存设备基本信息
  - 包括 `nvr_device_id`、`campus_code`、`ip_address`、`port`、`account`、在线状态等
- `save_nvr_channel(...)`
  - 保存通道记录
  - 包括 `channel_num`、`channel_id`、`sdk_channel`、`rtsp_channel`、`stream_status`
- `activate_channel(...)`
  - 更新 `edge_nvr_channel.activated_at`

这里也体现出当前链路的顺序关系：

- **先探测可播并建立 session**
- **再把成功结果写入本地表**

---

## 4. 完整业务流程：从连接设备到播放成功

## 4.1 第一步：服务端请求 `open`

服务端先调用：

```text
POST /api/stream/open
```

边缘服务进入 `api_stream_open()` 后，依次执行以下逻辑。

### 4.1.1 检查实时流代理是否启用

通过 `_stream_enabled()` 读取监控配置中的 `enableStreamProxy`。

如果未启用：

- 直接返回 `400`
- 不进入后续任何 RTSP / ffmpeg / session 流程

### 4.1.2 校验校区

通过 `_validate_campus(req.campusCode)`：

- 读取本地配置的 `campusCode`
- 与请求值比较

如果不一致：

- 返回 `403`
- 不访问 NVR
- 不创建 session

### 4.1.3 调用 `session_manager.create_or_get_session(...)`

这是 `open` 的核心逻辑入口。

---

## 4.2 第二步：创建或复用 session

`StreamSessionManager.create_or_get_session(...)` 中，主要流程如下。

### 4.2.1 检查 ffmpeg 是否存在

如果当前机器没有 `ffmpeg`：

- 直接抛出异常
- `open` 返回 `500`

因为当前实时流实现依赖 `ffmpeg` 输出 MPEG-TS。

### 4.2.2 规范化参数

包括：

- `outputProtocol`
  - 当前只支持 `mpegts`
- `streamProfile`
  - 当前只支持 `main` 或 `sub`
- `provider`
  - 通过 `edge_service/video/nvr.py` 的 `normalize_provider()` 归一化
  - 当前默认归一到 `hikvision`

### 4.2.3 解析业务通道

调用 `resolve_channel(...)`，解析逻辑为：

- **优先查本地缓存**
  - `edge_nvr_channel_map`
- **其次看候选通道**
  - 当前 `open` 路由里传入的是 `candidate_channels=None`
  - 因此这一支当前不会生效
- **最后回退为业务通道号本身**
  - 即 `nvrChannelNum`

解析完成后，会得到：

- `resolved_channel`

注意：

- 当前实时流代码中，`resolved_channel` 主要用于记录和缓存
- **真正构建 RTSP URL 时，优先使用的是业务通道号 `nvrChannelNum`**
- 这意味着当前实时流链路的“通道探索”并不是去大范围枚举 RTSP 通道
- 它更像是：
  - 先找一个最可信的业务通道入口
  - 再围绕这个入口验证哪种 RTSP 访问方式真正可播
- 代码里明确注释：
  - RTSP 实时流使用业务通道号
  - SDK 通道号主要用于下载链路，不直接决定 RTSP 路径

### 4.2.4 计算 RTSP 目标通道

当前实现：

- 如果 `nvrChannelNum > 0`
  - 则 `rtsp_channel = nvrChannelNum`
- 否则才回退为 `resolved_channel`

这一步的设计目的是避免把下载链路里的 SDK 通道缓存误用于 RTSP 地址拼接。

因此从业务上看，边缘服务在实时流阶段连接 NVR 时，真正拿去构建 RTSP 地址的核心要素是：

- `provider`
- `ipAddress`
- `port`（更准确地说，后续还会再探测 RTSP 端口）
- `account`
- `password`
- `rtsp_channel`
- `streamProfile`

---

## 4.3 第三步：构建并验证 RTSP 地址

### 4.3.1 provider 统一入口

RTSP URL 构建通过：

- `edge_service/video/nvr.py -> build_rtsp_url(...)`

当前支持：

- `hik`
- `hikvision`
- `haikang`
- `海康`

最终都归一为：

- `hikvision`

### 4.3.2 海康 RTSP URL 规则

海康 RTSP URL 最终由：

- `edge_service/video/hik/download.py::_build_rtsp_url(...)`

负责拼接。

主码流 / 子码流的区别体现在：

- 主码流 `stream_id=1`
- 子码流 `stream_id=2`

### 4.3.3 RTSP 端口探测

`_resolve_playable_rtsp_url(...)` 内部会调用：

- `_probe_rtsp_url_with_fallback(...)`

当前 RTSP 端口候选为：

- `554`
- `18080`

每个端口最多尝试 3 次。

这一阶段的探测并不是直接拉整路流，而是先通过 `_probe_rtsp_url(...)` 调用 `ffprobe` 去确认：

- 这个 RTSP URL 能否连通
- 返回的数据里是否至少能识别出媒体流类型

只有这一步成功，才会进入下一步更严格的“首包可播验证”。

### 4.3.4 主码流失败时自动回退子码流

当前逻辑：

- 如果请求 `streamProfile=main`
- 则先探测主码流
- 如果主码流探测失败，再自动尝试子码流

也就是说：

- `open` 成功返回时，代表边缘服务已经找到一个**实际可播**的 RTSP 源
- 返回结果中的 `streamProfile` 可能已经从请求值 `main` 回退成 `sub`

### 4.3.5 可播性校验不是只看 RTSP 连通

当前实现不是“RTSP URL 能连就算成功”。

在 RTSP 地址探测成功后，还会执行：

- `_probe_mpegts_first_chunk(rtsp_url)`

这一阶段会直接调用 `ffmpeg`：

- 从 RTSP 读取
- 尝试输出一个 MPEG-TS 首包

只有当：

- RTSP 可访问
- 并且 `ffmpeg` 真的能产出 MPEG-TS 首包

才认为这个 RTSP 地址**真正可播放**。

这是当前实现的重要特性，它避免了“RTSP 可连但播放器实际上拉不出数据”的假成功。

所以 `open` 成功的业务含义其实是：

- 边缘服务已经完成了“地址拼接”
- 已经完成了“端口可达性验证”
- 已经完成了“主/子码流可用性选择”
- 已经完成了“至少能吐出一个 MPEG-TS 首包”的可播验证

这比单纯返回一个 RTSP 地址要严格得多。

---

## 4.4 第四步：创建或复用 StreamSession

RTSP 可播验证通过后，会生成一个 `session_key`，其组成包含：

- `campusCode`
- `nvrDeviceId`
- `nvrChannelNum`
- `nvrChannelId`
- `ipAddress`
- RTSP 端口
- provider
- `resolvedChannel`
- 实际使用的 `streamProfile`
- `outputProtocol`

### 4.4.1 复用逻辑

如果请求中：

- `reuseIfExists=True`

则会尝试按 `session_key` 复用已有 session。

命中后：

- 不重新创建 session
- 只更新 `updated_at`
- 返回同一个 `sessionId`

### 4.4.2 创建逻辑

如果没有可复用 session，则创建新的 `StreamSession`，保存到：

- `_sessions`
- `_session_keys`

session 中主要保存：

- `session_id`
- `nvr_device_id`
- `nvr_channel_num`
- `resolved_channel`
- `stream_profile`
- `output_protocol`
- `source_type`
- `rtsp_url`
- `created_at`
- `updated_at`

---

## 4.5 第五步：`open` 返回播放地址

`api_stream_open()` 在 session 创建/复用成功后，会继续执行：

- `save_nvr_device(...)`
- `save_nvr_channel(...)`
- `activate_channel(...)`

### 4.5.1 本地落库

仅在 `reused=False` 时，写入：

- `edge_nvr_device`
- `edge_nvr_channel`

其中会记录：

- NVR 基本信息
- 通道信息
- 解析后的 `sdk_channel`
- `rtsp_channel`
- 通道状态

### 4.5.2 激活通道

调用：

- `activate_channel(nvr_device_id, channel_num)`

本质是更新 `edge_nvr_channel.activated_at`。

### 4.5.3 生成播放 URL

返回两种地址：

- `publicPlayUrl`
- `lanPlayUrl`

生成规则：

- `publicPlayUrl`
  - 优先使用配置中的 `publicBaseUrl`
  - 否则使用 `publicHost/publicPort/publicScheme`
  - 再不行就回退为本机局域网地址
- `lanPlayUrl`
  - 直接使用边缘服务本机可访问 LAN 地址

最终返回路径格式为：

```text
/api/stream/play/{nvrDeviceId}/{nvrChannelNum}
```

注意：

- 播放地址中**不包含 `sessionId`**
- `sessionId` 会单独放在 `open` 的响应体中返回

---

## 5. 完整业务流程：从拿到播放地址到真正播放成功

## 5.1 服务端/播放器访问 `play`

调用：

```text
GET /api/stream/play/{nvr_device_id}/{channel_num}
```

进入 `api_stream_play()` 后，边缘服务依次执行以下逻辑。

### 5.1.1 检查通道是否仍在激活期

通过：

- `is_channel_active(nvr_device_id, channel_num, ttl_sec=300)`

判断该通道是否在最近 300 秒内被激活。

如果激活态已过期：

- 返回 `403`
- 提示先重新调用 `open`

这说明：

- **播放 URL 本身不是签名临时 URL**
- 但它依赖服务器端的“通道激活状态”
- 激活超时后，必须重新 `open`

### 5.1.2 根据 `nvrDeviceId + channelNum` 取回 session

调用：

- `get_session_by_key(nvr_device_id, channel_num)`

如果找不到 session：

- 返回 `404`
- 提示先调用 `open`

### 5.1.3 校验输出协议

当前只支持：

- `mpegts`

否则返回 `400`。

---

## 5.2 第六步：边缘服务启动 ffmpeg 拉流并输出 MPEG-TS

在 `api_stream_play()` 中，会先根据 session 中保存的 `rtsp_url` 生成播放尝试方案：

- `tcp_prefer`
- `tcp_retry`
- `udp_fallback`

每套方案都由 `_build_stream_attempts(session.rtsp_url)` 生成。

### 5.2.1 ffmpeg 输入特征

当前播放阶段 ffmpeg 具备这些特点：

- 支持 TCP 优先、UDP 回退
- 设置 RTSP 超时
- 设置 `analyzeduration`
- 设置 `probesize`
- 使用 `copy` 直接转封装
- 输出为 `mpegts`
- 输出目标是 `pipe:1`

也就是说：

- 边缘服务并不做转码
- 主要做的是 RTSP -> MPEG-TS 的中转输出

### 5.2.2 首包判定

`play` 阶段会等待 `ffmpeg` 输出首个 chunk：

- 默认首包超时时间由环境变量控制
- 若首包超时或首包为空，则切换到下一种 attempt

只有当读取到非空首包后，才认为此次播放真正开始成功。

### 5.2.3 活跃度续期

在以下时机会调用 `_touch_stream_activity(...)`：

- `play_start`
- `first_chunk`
- 播放过程中定时 `streaming`
- 恢复重试前的 `recovery`

该操作会：

- 刷新通道激活时间
- 更新 session 最近活动时间

这保证了：

- 正在播放的流不会因为 300 秒激活窗口自然失效
- 正在活跃的 session 不会被 900 秒 session TTL 提前回收

从代码实现上看，`_touch_stream_activity(...)` 实际做了两件事：

- 调用 `activate_channel(...)` 更新 `edge_nvr_channel.activated_at`
- 调用 `get_session_by_key(...)` 刷新对应 session 的 `updated_at`

所以播放保活既影响数据库里的“通道激活态”，也影响内存里的“session 存活时间”。

### 5.2.4 返回给调用方的响应

`play` 返回的是：

- `StreamingResponse`
- `Content-Type: video/mp2t`

播放器或服务端读取的是一个持续输出的 MPEG-TS 字节流。

---

## 6. 播放中断后的恢复逻辑

当前 `play` 流程内置断流恢复机制。

### 6.1 单次播放尝试内部的回退

在一次恢复轮次中，会依次尝试：

- TCP 优先
- TCP 重试
- UDP 回退

只要有一种方式能产出首包，就继续播放。

### 6.2 播放中途断流后的自动恢复

如果某次播放已经开始，但后续流中断：

- 代码会进入恢复流程
- 等待 `EDGE_STREAM_RECOVERY_DELAY_MS`
- 最多重试 `EDGE_STREAM_MAX_RECOVERY_ATTEMPTS`

同时，在单次 attempt 内部还有首包级别的重试：

- 首包超时，则认为该 attempt 失败
- 首包为空，则认为该 attempt 失败
- 然后切换到下一种传输方案继续尝试

如果恢复次数耗尽：

- 本次流式输出结束
- 调用方会感知到播放终止

---

## 7. 停止播放与关闭会话的完整流程

## 7.1 用户/服务端停止读取播放流

当播放器关闭或调用方断开 HTTP 连接时：

- `StreamingResponse` 的异步生成器退出
- 进入 `finally` 块

### 7.1.1 ffmpeg 进程回收

在 `finally` 中会执行：

- `proc.terminate()`
- 必要时 `proc.kill()`
- 等待 stderr reader 结束

因此：

- **当前正在播放的 ffmpeg 子进程会被回收**
- 这一步不依赖 `close` 接口

## 7.2 调用 `POST /api/stream/close`

如调用方在停止播放后继续调用：

```text
POST /api/stream/close
```

并传入：

- `sessionId`

则边缘服务执行：

- `session_manager.close_session(session_id)`

该操作会：

- 从 `_sessions` 删除 session
- 从 `_session_keys` 删除索引

### 7.2.1 `close` 的真实语义

当前 `close` 的作用是：

- **主动释放会话**
- **阻止后续继续复用该 session**

但它**不是**“强制掐断当前正在输出的流”的控制指令。

也就是说：

- 如果播放连接已经断开，再调 `close`，语义是完整的
- 如果流还在播放，仅调用 `close`，并不保证马上中断当前那条已经运行起来的 ffmpeg 流

### 7.2.2 为什么调用方仍应保留 `sessionId`

因为：

- `play` URL 中不带 `sessionId`
- `close` 接口只接受 `sessionId`

所以服务端如果想主动关闭 session，必须自行保存 `open` 返回的 `sessionId`。

---

## 8. TTL 与自动回收机制

## 8.1 通道激活 TTL

`play` 前置检查中，通道激活窗口是：

- `300 秒`

含义：

- 调用 `open` 后，如果长时间不播放
- 或停止播放很久后不重新 `open`
- 再直接访问旧的 `play` 地址会失败

## 8.2 session TTL

`StreamSessionManager` 默认 session TTL 是：

- `900 秒`

回收规则：

- 如果一个 session 长时间没有更新 `updated_at`
- `_cleanup_expired_locked()` 会把它从内存中清掉

## 8.3 两个 TTL 的区别

- **300 秒激活 TTL**
  - 控制通道是否允许进入 `play`
- **900 秒 session TTL**
  - 控制内存中 session 是否保留、是否还能复用

因此，可能出现这种情况：

- session 还没被清理
- 但通道激活已经过期
- 此时仍然需要重新调用 `open`

---

## 9. 当前实现中的关键约束与注意事项

### 9.1 当前播放地址不是一次性签名 URL

- URL 路径本身相对稳定
- 真正会失效的是服务端状态：
  - 通道激活状态
  - session 是否还存在

### 9.2 当前推荐调用顺序

推荐调用顺序为：

1. `POST /api/stream/open`
2. 保存 `sessionId`
3. 使用 `publicPlayUrl` 或 `lanPlayUrl` 播放
4. 停止播放
5. `POST /api/stream/close` 主动释放 session
6. 若后续再次播放，重新调用 `open`

### 9.3 当前 `play` 不使用 `sessionId`

当前实现中：

- `play` 依赖的是：
  - `nvrDeviceId`
  - `channelNum`
- `close` 依赖的是：
  - `sessionId`

因此调用方必须同时管理：

- 播放地址
- `sessionId`

### 9.4 当前只支持 `mpegts`

虽然接口里保留了 `outputProtocol`，但当前实现仅支持：

- `mpegts`

### 9.5 当前 provider 已做统一边界

实时流链路中，provider 已通过 `edge_service/video/nvr.py` 做统一入口归一化。

目前实际落地的 RTSP 构建厂商是：

- `hikvision`

---

## 10. 端到端时序总结

### 10.1 正常播放成功路径

1. 服务端调用 `POST /api/stream/open`
2. 边缘服务检查代理开关与 `campusCode`
3. 边缘服务解析业务通道
4. 边缘服务构建 RTSP URL
5. 边缘服务探测 RTSP 连通性
6. 边缘服务用 `ffmpeg` 验证 MPEG-TS 首包可输出
7. 边缘服务创建或复用 `StreamSession`
8. 边缘服务激活通道并返回播放地址与 `sessionId`
9. 服务端/播放器访问 `GET /api/stream/play/{nvrDeviceId}/{channelNum}`
10. 边缘服务校验激活态和 session
11. 边缘服务启动 `ffmpeg` 拉 RTSP 并输出 MPEG-TS
12. 播放器读到首包并开始播放成功

### 10.2 停止播放路径

1. 播放器停止读取流或断开连接
2. 边缘服务回收当前 `ffmpeg` 进程
3. 调用方可选地调用 `POST /api/stream/close`
4. 边缘服务删除内存 session
5. 若未手动 `close`，则后续依赖 TTL 自动回收

---

## 11. 当前实现一句话总结

当前实时流实现是一个**基于 RTSP 探测 + ffmpeg 转发 + 内存 session 管理 + 激活态控制**的边缘流代理方案：

- `open` 负责准备可播会话并返回播放地址
- `play` 负责真正输出 MPEG-TS 字节流
- `close` 负责主动释放会话
- 会话与激活态分别受 `900s` 和 `300s` 两层 TTL 控制
- 主码流不可播时会自动回退到子码流
- 播放停止后 ffmpeg 会被自动回收
