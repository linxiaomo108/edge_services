# 共享 RTSP 实时流播放优化方案

## 1. 背景与目标

当前实时流播放流程中，用户通过 `/api/stream/open` 创建播放会话，再使用返回的 `lanPlayUrl` 或 `publicPlayUrl` 播放实时流，最后通过 `/api/stream/close` 关闭播放。

现有实现的问题是：多个用户同时观看同一个设备、同一个通道时，边缘服务可能为每个观看者分别启动一个 ffmpeg 进程去拉同一路 RTSP。这样会带来：

- NVR 同一路通道被重复拉流，设备压力增大。
- 边缘服务侧重复启动 ffmpeg，CPU、网络、进程资源消耗增大。
- 用户短时间内反复打开/关闭播放窗口时，重复探测和拉流，响应不够稳定。
- 日志中不容易直接判断当前同一路通道到底启动了几个 ffmpeg。

本方案目标：
前置条件：访问同一个设备的同一个通道
- 同一个明确可识别的 RTSP 源流，只启动一个共享 ffmpeg 源进程。
- 多个观看者通过不同 `viewerId` 订阅同一个源进程输出。
- 保持现有对外接口尽量不变，减少服务端改动。
- 保留精准关闭能力：关闭某个观看者时，不影响其他观看者。
- 增加可读、可排查的业务日志，方便确认是否复用同一个 ffmpeg。
- 通过配置文件开关控制共享 RTSP，方便验证和回退。

## 2. 核心原则

### 2.1 共享判断看源流，不看播放入口

`lanPlayUrl` 和 `publicPlayUrl` 只是用户访问边缘服务的入口不同：

- `lanPlayUrl`：校区内访问边缘服务。
- `publicPlayUrl`：校区外通过公网映射访问边缘服务。

它们不应该参与共享 RTSP 的判断。

共享判断应该基于边缘服务最终拉取 NVR 的源流信息，例如：

- `provider`
- `nvrDeviceId`
- `nvrChannelNum`
- `nvrChannelId`
- `resolvedChannel`
- `streamProfile`
- `outputProtocol`
- `rtspUrl`

最关键的是最终解析出的 `rtspUrl`。只要边缘服务最终拉的是同一个可信源流，就可以共享。

### 2.2 能明确判断同源才共享

业务上，设备相同、通道相同，通常意味着同一个源流。但工程实现上，必须避免误共享：

- 如果最终 `rtspUrl` 完全一致，可以直接共享。
- 如果最终 `rtspUrl` 不一致，但配置中声明了可信 RTSP 地址映射，可以归一化后共享。
- 如果无法明确证明两个地址对应同一个源流，不共享。

这是为了避免把不同 NVR、不同端口映射、不同通道的内容误认为同一路视频。

### 2.3 `sessionId` 表示播放会话，`viewerId` 表示观看者

共享 RTSP 后：

- `sessionId`：表示某一路实时流会话。
- `viewerId`：表示某一个观看者。
- `ffmpegPid`：表示实际拉取 RTSP 的共享源进程。

多个 `viewerId` 可以共享同一个 `sessionId` 下的同一个 `ffmpegPid`。

`viewerId` 不能用于判断源流是否相同，因为每个观看者的 `viewerId` 都不同。

## 3. 对外接口设计

### 3.1 `/api/stream/open`

接口传参保持现有结构，不改变原有必填字段。

继续支持：

- `campusCode`
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
- `reuseIfExists`
- `operatorUserId`
- `operatorUserName`
- `operatorAccount`

接口返回保持现有结构：

- `sessionId`
- `viewerId`
- `lanPlayUrl`
- `publicPlayUrl`
- `outputProtocol`
- `resolvedChannel`
- `sourceType`
- `reused`

变化点：

- `lanPlayUrl` 和 `publicPlayUrl` 继续携带当前观看者的 `viewerId`。
- `operatorUserId/operatorUserName/operatorAccount` 在 `open` 阶段保存到 `sessionId + viewerId` 对应的观看者元数据中，后续播放和关闭日志使用这份元数据。

### 3.2 `/api/stream/play/session/{sessionId}`

接口路径保持：

```text
GET /api/stream/play/session/{sessionId}?viewerId={viewerId}
```

变化点：

- 旧逻辑：每次播放请求启动一个 ffmpeg。
- 新逻辑：每次播放请求订阅共享源流。

如果当前 `sessionId` 对应源流已经有共享 ffmpeg：

- 当前 `viewerId` 加入观看者列表。
- 复用已有 ffmpeg。

如果当前 `sessionId` 对应源流还没有共享 ffmpeg：

- 启动一个新的 ffmpeg。
- 当前 `viewerId` 作为第一个观看者。

### 3.3 `/api/stream/close`

接口保持：

```json
{
  "sessionId": "string",
  "viewerId": "string"
}
```

关闭规则：

- 传入 `sessionId + viewerId`：只关闭当前观看者。
- 当前源流还有其他观看者：ffmpeg 继续运行。
- 当前源流没有观看者：进入无人观看保活倒计时。
- 保活时间内有人重新观看：取消关闭倒计时，继续复用 ffmpeg。
- 保活时间到期仍无人观看：关闭 ffmpeg。

### 3.4 `/api/stream/status`

该接口不是播放业务必需接口，主要用于排查和运维。

建议增加共享 RTSP 状态，例如：

```json
{
  "sharedRtspEnabled": true,
  "activeSourceCount": 1,
  "sources": [
    {
      "sessionId": "xxx",
      "nvrDeviceId": 61,
      "nvrChannelNum": 1,
      "streamProfile": "main",
      "outputProtocol": "mpegts",
      "ffmpegPid": 12345,
      "viewerCount": 2,
      "idleClosing": false,
      "idleCloseAt": null
    }
  ]
}
```

使用场景：

- 验证同一路播放是否只启动了一个 ffmpeg。
- 排查用户反馈卡顿时，当前有多少观看者。
- 排查无人观看后 ffmpeg 是否仍未退出。
- 运维页面展示实时流运行状态。

## 4. 配置设计

共享 RTSP 能力必须放到配置文件中，便于验证和回退，不依赖重新打包。

建议在 `config.json` 中增加：

```json
{
  "stream": {
    "sharedRtspEnabled": false,
    "sharedRtspIdleSeconds": 30,
    "sharedRtspViewerQueueSize": 32,
    "sharedRtspSlowViewerMaxDrops": 3,
    "trustedRtspAliases": [
      {
        "internal": "192.168.9.177:554",
        "external": "60.xxx.xxx.xxx:8554"
      }
    ]
  }
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `sharedRtspEnabled` | 是否启用共享 RTSP。初期建议默认 `false`，验证通过后再改为 `true`。 |
| `sharedRtspIdleSeconds` | 最后一个观看者离开后，源流保活多少秒。建议默认 30 秒。 |
| `sharedRtspViewerQueueSize` | 每个观看者的播放数据队列大小，防止慢客户端无限堆积内存。 |
| `sharedRtspSlowViewerMaxDrops` | 慢客户端队列连续满多少次后主动断开该观看者。 |
| `trustedRtspAliases` | 可信 RTSP 地址映射。只有明确知道内网地址和公网映射地址对应同一个 NVR 源流时才配置。 |

## 5. 共享源流判断规则

### 5.1 默认规则

默认只对最终 `rtspUrl` 完全一致的源流共享。

例如：

```text
rtsp://admin:***@192.168.9.177:554/Streaming/Channels/101
rtsp://admin:***@192.168.9.177:554/Streaming/Channels/101
```

可以共享。

### 5.2 可信映射规则

如果配置了：

```json
{
  "internal": "192.168.9.177:554",
  "external": "60.xxx.xxx.xxx:8554"
}
```

则这两个地址可以归一化为同一个源流：

```text
rtsp://admin:***@192.168.9.177:554/Streaming/Channels/101
rtsp://admin:***@60.xxx.xxx.xxx:8554/Streaming/Channels/101
```

归一化后认为可以共享。

### 5.3 不共享情况

以下情况不共享：

- `streamProfile` 不同，例如 `main` 和 `sub`。
- `outputProtocol` 不同。
- 最终 `resolvedChannel` 不同。
- 最终 `rtspUrl` 不同，且没有配置可信映射。
- 设备或通道信息不完整，无法确认同源。

## 6. 内部实现方案

### 6.1 新增共享源流管理模块

建议新增：

```text
edge_service/video/stream_relay.py
```

核心对象：

```text
SharedStreamRelay
StreamSource
StreamSubscriber
```

职责：

- 管理共享 ffmpeg 源进程。
- 管理观看者订阅队列。
- 管理无人观看保活倒计时。
- 管理源流异常恢复。
- 管理慢客户端断开。
- 提供状态查询。

### 6.2 `StreamSource` 状态

每个共享源流维护：

```text
sessionId
nvrDeviceId
nvrChannelNum
streamProfile
outputProtocol
rtspUrl
ffmpegPid
process
subscribers
viewerCount
startedAt
lastChunkAt
idleSince
idleCloseAt
idleCloseTask
recoveryCount
```

### 6.3 数据分发逻辑

ffmpeg 只启动一次：

```text
NVR RTSP -> ffmpeg -> stdout chunk -> 分发给所有 viewer queue
```

每个观看者一个独立队列：

```text
viewer A queue
viewer B queue
viewer C queue
```

注意：

- 不能让慢客户端阻塞其他观看者。
- 队列必须有上限。
- 如果某个观看者长时间消费不过来，只断开该观看者，不影响共享源流和其他观看者。

## 7. 日志设计

日志要方便业务排查，避免打印大量无意义 ffmpeg 细节。

常规业务日志中不打印 `sourceKey`，只打印业务可理解字段。

### 7.1 创建播放会话

```text
[STREAM] 创建播放会话 sessionId=xxx viewerId=aaa 设备=61 通道=1 码流=main 用户=张三 账号=zhangsan
```

### 7.2 启动源流

```text
[STREAM] 启动源流 sessionId=xxx 设备=61 通道=1 码流=main ffmpegPid=12345 当前观看人数=1
```

### 7.3 复用源流

```text
[STREAM] 加入观看 sessionId=xxx viewerId=bbb 用户=李四 复用ffmpegPid=12345 当前观看人数=2
```

### 7.4 观看者离开

```text
[STREAM] 离开观看 sessionId=xxx viewerId=aaa 用户=张三 当前观看人数=1 ffmpegPid=12345 继续运行
```

### 7.5 最后观看者离开

```text
[STREAM] 最后观看者离开 sessionId=xxx viewerId=bbb 用户=李四 当前观看人数=0 ffmpegPid=12345 将在30秒后关闭 计划关闭时间=2026-07-01 10:20:30
```

### 7.6 关闭倒计时

倒计时不需要每秒打印，建议每 10 秒打印一次：

```text
[STREAM] 源流等待关闭 sessionId=xxx ffmpegPid=12345 剩余20秒
[STREAM] 源流等待关闭 sessionId=xxx ffmpegPid=12345 剩余10秒
```

### 7.7 倒计时期间重新加入

```text
[STREAM] 新观看者加入，取消关闭倒计时 sessionId=xxx viewerId=ccc 用户=王五 ffmpegPid=12345 当前观看人数=1
```

### 7.8 无人观看超时关闭

```text
[STREAM] 无人观看超时，关闭源流 sessionId=xxx ffmpegPid=12345 无人观看开始时间=2026-07-01 10:20:00 实际关闭时间=2026-07-01 10:20:30
```

### 7.9 异常日志

只有对排查有意义的异常才打印，例如：

- RTSP 取流失败。
- ffmpeg 异常退出。
- 源流恢复失败。
- 慢客户端被断开。
- 共享源流无人观看后未能关闭。

ffmpeg 高频 stderr 不直接刷屏，必要时做节流或归类。

## 8. 需要改动的文件

### 8.1 `edge_service/routes/stream_routes.py`

主要改动：

- `/api/stream/play/session/{sessionId}` 从“每个 viewer 启动 ffmpeg”改为“订阅共享源流”。
- `/api/stream/close` 从“直接关闭 viewer 进程”改为“关闭 viewer 订阅”。
- `viewer_meta` 继续保存用户信息，供播放和关闭日志使用。
- 日志改为业务可读格式。

### 8.2 新增 `edge_service/video/stream_relay.py`

主要职责：

- 创建共享源流。
- 分发 ffmpeg stdout 数据。
- 管理 subscriber 队列。
- 处理 viewer 关闭。
- 处理无人观看保活。
- 提供运行状态。

### 8.3 配置读取逻辑

需要确认当前配置读取入口，增加：

- `stream.sharedRtspEnabled`
- `stream.sharedRtspIdleSeconds`
- `stream.sharedRtspViewerQueueSize`
- `stream.sharedRtspSlowViewerMaxDrops`
- `stream.trustedRtspAliases`

### 8.4 接口文档

需要更新：

- `/api/stream/open`：说明返回的 `viewerId` 与精准关闭、共享源流的关系。
- `/api/stream/play/session/{sessionId}`：说明 `viewerId` 是观看者标识。
- `/api/stream/close`：说明传 `sessionId + viewerId` 只关闭当前观看者。
- `/api/stream/status`：说明该接口用于排查和运维，不是业务播放必需接口。

## 9. 验证计划

### 9.1 单人播放

步骤：

1. 调用 `/api/stream/open`。
2. 使用 `lanPlayUrl` 播放。
3. 调用 `/api/stream/close`。

预期：

- 播放正常。
- 日志出现一次启动源流。
- close 后进入 30 秒保活。
- 30 秒后关闭 ffmpeg。

### 9.2 两人同通道播放

步骤：

1. 用户 A 打开同一设备同一通道。
2. 用户 B 打开同一设备同一通道。

预期：

- 只出现一次“启动源流”日志。
- 第二个用户出现“加入观看，复用 ffmpeg”日志。
- 两个用户看到的 `ffmpegPid` 相同。
- `/api/stream/status` 中同一路 `viewerCount=2`。

### 9.3 关闭其中一个观看者

步骤：

1. A、B 同时观看。
2. A 调用 close。

预期：

- A 被关闭。
- B 继续播放。
- ffmpeg 不退出。
- viewerCount 从 2 变成 1。

### 9.4 最后一个观看者关闭

步骤：

1. B 调用 close。

预期：

- viewerCount 变成 0。
- 进入 30 秒保活倒计时。
- 每 10 秒打印一次倒计时日志。
- 30 秒到期关闭 ffmpeg。

### 9.5 保活期间重新加入

步骤：

1. 最后一个观看者关闭。
2. 30 秒内新用户重新打开同一路。

预期：

- 取消关闭倒计时。
- 复用原 ffmpeg。
- 不重新探测、不重新启动源流。

### 9.6 不同码流不共享

步骤：

1. 用户 A 打开主码流 `main`。
2. 用户 B 打开子码流 `sub`。

预期：

- 启动两个不同源流。
- 两个 `ffmpegPid` 不同。

### 9.7 可信 RTSP 映射验证

步骤：

1. 配置 `trustedRtspAliases`。
2. 分别通过内网 RTSP 和公网映射 RTSP 创建同一源流。

预期：

- 归一化后共享同一个 ffmpeg。
- 如果不配置可信映射，则不共享。

## 10. 风险与影响

### 10.1 正向影响

- 同一路多人观看时，NVR 压力明显降低。
- 边缘服务 ffmpeg 进程数量减少。
- 用户短时间重复打开关闭时响应更快。
- 实时流运行状态更容易排查。

### 10.2 风险

- 一个共享源流异常，会影响同一路下所有观看者。
- 慢客户端如果不处理好，可能拖慢分发或占用内存。
- 异步队列和进程清理逻辑更复杂，需防止残留 ffmpeg。
- 如果误判同源，可能导致用户看到错误通道内容。

### 10.3 风险控制

- 初期通过 `sharedRtspEnabled=false` 默认关闭。
- 验证阶段手动打开配置。
- 第一版只对明确同源的 RTSP 共享。
- 可信映射必须显式配置，不自动猜测。
- 每个 viewer 队列设置上限。
- 慢客户端只断开自身，不影响其他 viewer。
- 状态接口和日志必须能看出当前 ffmpeg 数量和 viewer 数量。

## 11. 开发步骤建议

### 阶段一：基础共享能力

- 新增配置项。
- 新增共享源流管理模块。
- 修改播放接口接入共享源流。
- 修改关闭接口支持关闭单个 viewer。
- 增加核心日志。

### 阶段二：保活与状态查询

- 增加最后观看者离开后的 30 秒保活。
- 增加 10 秒一次的倒计时日志。
- 增强 `/api/stream/status` 返回共享源状态。

### 阶段三：慢客户端保护

- 为每个 viewer 增加有界队列。
- 增加慢客户端检测和断开。
- 增加慢客户端相关日志。

### 阶段四：可信 RTSP 地址归一化

- 增加 `trustedRtspAliases`。
- 实现 RTSP 地址归一化。
- 验证内网 RTSP 与公网映射 RTSP 的共享。

### 阶段五：灰度启用

- 默认关闭配置。
- 本地测试打开。
- 小范围服务器打开。
- 观察 NVR 压力、播放稳定性、ffmpeg 数量。
- 验证通过后再默认开启。

## 12. 本次讨论确认的注意事项

- `lanPlayUrl/publicPlayUrl` 不参与共享判断。
- 同一个设备、同一个通道，业务上应视为同一路，但工程上必须能明确证明同源后才共享。
- 内网 RTSP 和公网 RTSP 如果明确是同一个映射，可以通过可信映射配置归一化后共享。
- 如果不能明确证明同源，不共享，避免错误通道内容。
- 日志不要打印大量无业务意义内容。
- 常规日志中不要突出 `sourceKey`，它只是内部实现细节。
- 日志应重点展示 `sessionId`、`viewerId`、用户信息、设备通道、`ffmpegPid`、当前观看人数。
- 最后一个 viewer 离开后，不立即关闭 ffmpeg，进入 30 秒保活。
- 保活倒计时每 10 秒打印一次即可，不要每秒刷屏。
- 30 秒内有人重新观看，应取消关闭倒计时并继续复用。
- `/api/stream/status` 是排查和运维接口，不是业务播放必需接口。
