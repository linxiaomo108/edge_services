# 移动录制接口对接说明

本文档描述边缘服务第一阶段移动录制接口。预览仍使用现有实时流接口，录制使用独立移动录制接口。

默认访问地址示例：

```text
http://{edgeHost}:18080
```

Swagger 文档：

```text
http://{edgeHost}:18080/docs
```

## 业务约束

- 同一教室同一时间只允许一个进行中的录制任务。
- 同一录制人同一时间只允许一个进行中的录制任务。
- 不同教室、不同录制人允许同时录制。
- 取消录制会停止 ffmpeg 并删除本地录制目录。
- 录制 HLS 分片目标时长为 10 秒。
- 录制使用主码流，优先 `-c copy`，不二次压缩。
- 预览继续使用现有实时流接口：`POST /api/stream/open`。

## 字段总说明

下面这些字段会在多个接口中出现，先统一解释一次。

- `taskId`：移动录制任务ID。由服务端生成，整个录制生命周期都用它作为唯一标识。
- `campusCode`：校区编码。边缘服务会先校验它是否与本机所属校区一致，避免串校区。
- `classroomId`：教室ID。用于判断同一教室是否已经有人在录制。
- `classroomName`：教室名称。主要用于页面展示和回调通知，可为空。
- `nvrDeviceId`：NVR设备ID。边缘服务用它记录和追踪这台录像机。
- `nvrChannelNum`：教室后置摄像头在 NVR 上对应的通道号。移动录制主要录这个通道。
- `nvrChannelId`：通道ID字符串。一般用于业务侧对账或兼容已有通道标识，可为空。
- `provider`：NVR厂商标识，第一阶段默认 `hikvision`。
- `ipAddress`：NVR设备地址，通常是内网 IP，也可以是能解析的主机名。
- `port`：NVR 的 RTSP 端口。默认 `554`。
- `account` / `password`：NVR 登录账号和密码。边缘服务会用它们去解析和拉流。
- `recordUserId`：录制人ID。用于限制“同一个人同一时刻只能录一个任务”。
- `recordUserName`：录制人姓名。用于列表和教室占用提示。
- `estimatedDurationSeconds`：预计录制时长。到点后边缘服务会自动停止。
- `extendSeconds`：每次延长的秒数。
- `callbackUrl`：已废弃。当前边缘服务忽略该字段，统一使用本机配置里的 `serverAddress` 自动拼接默认回调地址：`{serverAddress}/api/v1/record-task/callback`。

回调结果中还会出现这些字段：

- `status`：任务最终结果状态，不是录制中状态。
- `stopReason`：为什么停止录制。常见值是 `manual`、`auto_timeout`、`cancel`。
- `playUrl`：录制完成后 H5 直接访问的播放地址，指向边缘服务的 `index.m3u8`。
- `outputDir`：本地录制文件所在目录，供排障或后续归档使用。
- `m3u8Path`：本地 `index.m3u8` 的完整路径。
- `segmentCount`：本次录制实际生成了多少个 `.ts` 分片。
- `fileSize`：本次录制所有 HLS 文件的总大小，单位字节。
- `duration`：录制成片时长，单位秒。
- `codec`：录制视频编码格式，例如 `hevc` 或 `h264`。
- `format`：输出格式，第一阶段固定为 `hls`。
- `startTime` / `finishTime`：录制开始和录制结束的时间。
- `errorMessage`：失败原因描述。只有失败或异常时才有值。

## 统一返回格式

```json
{
  "ok": true,
  "code": "OK",
  "message": "ok",
  "data": {}
}
```

失败时：

```json
{
  "ok": false,
  "code": "CLASSROOM_RECORDING_CONFLICT",
  "message": "该教室正在录制中",
  "data": {
    "current": {}
  }
}
```

## 1. 查询教室录制占用状态

```http
GET /api/mobile-record/classrooms/status?classroomIds=room_301,room_302
```

用途：

- 选择教室页面展示教室是否可选。
- 展示当前教室由谁正在录制。
- 前端主动刷新教室状态。

### 请求参数说明

- `classroomIds`：逗号分隔的教室ID列表。建议前端把当前页可见的教室一次性传过来，便于批量刷新。

### 返回字段说明

- `exists`：当前教室在边缘服务里是否查到了状态记录。
- `taskId`：当前占用这个教室的录制任务ID。
- `classroomId`：教室ID。
- `classroomName`：教室名称。
- `recordUserId`：当前正在录制的老师ID。
- `recordUserName`：当前正在录制的老师姓名。
- `status`：当前教室对应任务的状态，通常是 `starting`、`recording`、`stopping` 之一。
- `isRecording`：当前是否处于录制占用中。
- `selectable`：前端是否允许再次选择这个教室。
- `startTime`：该任务实际开始录制的时间。
- `maxEndTime`：自动停止时间。
- `elapsedSeconds`：已经录了多少秒。
- `remainingSeconds`：距离自动停止还有多少秒。

### 返回示例

```json
{
  "ok": true,
  "code": "OK",
  "message": "ok",
  "data": {
    "items": [
      {
        "exists": true,
        "taskId": "record_20260605_001",
        "classroomId": "room_301",
        "classroomName": "301教室",
        "recordUserId": "u_001",
        "recordUserName": "张老师",
        "status": "recording",
        "isRecording": true,
        "selectable": false,
        "startTime": "2026-06-05T10:00:00+08:00",
        "maxEndTime": "2026-06-05T11:00:00+08:00",
        "elapsedSeconds": 600,
        "remainingSeconds": 3000
      }
    ]
  }
}
```

## 2. 开始移动录制

```http
POST /api/mobile-record/start
Content-Type: application/json
```

### 请求体示例

```json
{
  "taskId": "record_20260605_001",
  "campusCode": "NJ",
  "classroomId": "room_301",
  "classroomName": "301教室",
  "nvrDeviceId": 10001,
  "nvrChannelNum": 4,
  "nvrChannelId": "cam_back_301",
  "provider": "hikvision",
  "ipAddress": "192.168.9.83",
  "port": 554,
  "account": "admin",
  "password": "******",
  "recordUserId": "u_001",
  "recordUserName": "张老师",
  "estimatedDurationSeconds": 3600,
  "callbackUrl": ""
}
```

### 字段说明

- `taskId`：服务端生成的录制任务ID，边缘服务内唯一。
- `campusCode`：校区编码。边缘服务会先校验它是否和本机所属校区一致。
- `classroomId`：教室ID，用于教室互斥。同一教室同一时间只能有一个进行中的任务。
- `classroomName`：教室名称，用于前端和回调展示。
- `nvrDeviceId`：NVR设备ID。边缘服务用它记录这次录制来自哪台录像机。
- `nvrChannelNum`：服务端提供的教室后置摄像头固定通道号。录制主码流时会直接用这个通道。
- `nvrChannelId`：通道ID字符串。可选，不影响第一阶段录制主流程。
- `provider`：NVR厂商标识。第一阶段默认 `hikvision`。
- `ipAddress`：NVR的 IP 或主机名，边缘服务会用它去拼 RTSP。
- `port`：NVR RTSP 端口。默认 `554`。
- `account` / `password`：连接 NVR 的账号密码。
- `recordUserId`：录制人ID。用于“同一录制人同一时间只能有一个任务”的互斥控制。
- `recordUserName`：录制人姓名，用于展示和回调。
- `estimatedDurationSeconds`：预计录制时长，单位秒。到时后边缘服务自动停止。
- `callbackUrl`：已废弃，保留仅为兼容旧版本；当前边缘服务不会使用它计算回调地址。

### 成功返回

```json
{
  "ok": true,
  "code": "OK",
  "message": "ok",
  "data": {
    "taskId": "record_20260605_001",
    "status": "recording",
    "playUrl": "http://edge.example.com/api/mobile-record/play/record_20260605_001/index.m3u8",
    "startTime": "2026-06-05T10:00:00+08:00",
    "maxEndTime": "2026-06-05T11:00:00+08:00",
    "elapsedSeconds": 0,
    "remainingSeconds": 3600
  }
}
```

### 可能失败码

- `TASK_EXISTS`：`taskId` 已存在。
- `CLASSROOM_RECORDING_CONFLICT`：教室已被占用。
- `CHANNEL_RECORDING_CONFLICT`：同一个 NVR 设备和通道已有进行中的录制任务。
- `USER_RECORDING_CONFLICT`：录制人已有进行中的录制。
- `START_FAILED`：录像设备取流失败、目录创建失败或 ffmpeg 启动失败。

### 业务说明

开始录制时，边缘服务会做两次校验：

- 第一次是页面选择教室时的“占用展示”，方便用户提前看到这个教室是否可选。
- 第二次是点击【开始录制】那一刻的最终强校验，防止中途被别人抢先录走。

## 3. 结束或取消移动录制

```http
POST /api/mobile-record/stop
Content-Type: application/json
```

### 结束并保留

```json
{
  "taskId": "record_20260605_001",
  "action": "finish",
  "operatorUserId": "u_001",
  "stopReason": "manual"
}
```

### 取消并删除

```json
{
  "taskId": "record_20260605_001",
  "action": "cancel",
  "operatorUserId": "u_001",
  "stopReason": "cancel"
}
```

### 字段说明

- `taskId`：要停止的任务ID。必须是已经开始过录制的任务。
- `action`：停止方式。
  - `finish` 表示正常结束并保留文件。
  - `cancel` 表示取消录制并删除本地文件。
- `operatorUserId`：执行停止操作的人ID，通常是发起这次录制的老师，也可以是服务端代操作人员。
- `stopReason`：停止原因。`manual` 表示手动停止，`cancel` 表示取消，`auto_timeout` 表示自动到时停止。

### 业务说明

- `action=finish`：正常结束录制。边缘服务会停止 ffmpeg、校验 HLS、保留文件，并生成 `playUrl`。
- `action=cancel`：取消录制。边缘服务会停止 ffmpeg，并删除本地录制目录，状态变成 `cancelled`。
- 接口幂等，已结束任务重复调用会返回当前最终状态。

## 4. 延长移动录制时长

```http
POST /api/mobile-record/extend
Content-Type: application/json
```

### 请求体

```json
{
  "taskId": "record_20260605_001",
  "extendSeconds": 1800,
  "operatorUserId": "u_001"
}
```

### 字段说明

- `taskId`：要延长的录制任务ID。
- `extendSeconds`：这一次额外延长多少秒。
- `operatorUserId`：执行延长操作的人ID。

### 业务说明

- 只有 `recording` 状态允许延长。
- 延长后自动停止时间 `maxEndTime` 会更新。
- 前端展示的剩余时长也会跟着刷新。

## 5. 查询移动录制状态

按任务查询：

```http
GET /api/mobile-record/status?taskId=record_20260605_001
```

按教室查询当前进行中录制：

```http
GET /api/mobile-record/status?classroomId=room_301
```

### 用途

- 录制中页面展示已录时长、剩余时长、状态。
- 停止后获取 `playUrl`。
- 也可用于教室页主动刷新单个教室当前状态。

### 返回字段说明

- `taskId`：当前任务ID。
- `classroomId`：教室ID。
- `classroomName`：教室名称。
- `recordUserId`：录制人ID。
- `recordUserName`：录制人姓名。
- `status`：任务状态。常见值：`starting`、`recording`、`stopping`、`finished`、`cancelled`、`failed`、`interrupted`。
- `isRecording`：当前是否还在录制中。
- `selectable`：如果是教室状态查询，这个字段表示是否可再次选择。
- `startTime`：实际开始录制时间。
- `maxEndTime`：自动停止时间。
- `finishTime`：录制结束时间。
- `elapsedSeconds`：已录制时长。
- `remainingSeconds`：剩余可录制时长。
- `estimatedDurationSeconds`：初始预计录制时长。
- `extendDurationSeconds`：累计延长的总秒数。
- `playUrl`：录制完成后可直接播放的 HLS 地址。
- `outputDir`：本地输出目录。
- `segmentCount`：已生成分片数。
- `fileSize`：录制文件总大小。
- `durationSeconds`：录制时长。
- `codec`：视频编码。
- `errorMessage`：失败或中断原因。

## 6. 查询移动录制任务列表

```http
GET /api/mobile-record/list?classroomId=room_301&recordUserId=u_001&status=finished&date=2026-06-05&limit=100
```

### 用途

- 服务端补偿查询。
- 后续管理页面或排障。

### 查询参数说明

- `classroomId`：按教室过滤，只查这个教室的录制记录。
- `recordUserId`：按录制人过滤，只查这个人录过的任务。
- `status`：按状态过滤，例如 `recording`、`finished`、`failed`。
- `date`：按开始日期过滤，格式 `YYYY-MM-DD`。
- `limit`：最多返回多少条记录，默认 100。

### 返回说明

列表返回的每一项和 `status` 接口返回字段一致，主要用于页面列表展示和问题排查。

## 7. 播放录制文件

播放地址由 `start/status/stop` 返回：

```text
http://{edgeHost}:18080/api/mobile-record/play/{taskId}/index.m3u8
```

### 说明

- H5 直接访问边缘服务公网映射地址。
- 只有 `finished` 状态任务允许读取 HLS 文件。
- `taskId` 是录制任务ID。
- `path` 是录制目录下的相对路径，通常就是 `index.m3u8`，也可以是某个 `.ts` 分片路径。

## 服务端需要提供的 callback 接口

边缘服务会向默认回调地址发起 `POST` 请求：

```text
{serverAddress}/api/v1/record-task/callback
```

其中 `serverAddress` 取边缘服务当前配置中的服务端基础地址。

### callback 字段说明

- `taskId`：录制任务ID。
- `classroomId`：本次录制对应的教室ID。
- `cameraId`：摄像头标识。第一阶段通常直接回传 NVR 通道ID或通道号，方便服务端对账。
- `status`：录制结果状态。
  - `finished` 表示正常完成。
  - `cancelled` 表示取消录制。
  - `failed` 表示录制失败。
- `stopReason`：停止原因。
  - `manual`：人工手动结束。
  - `auto_timeout`：超过预计时长自动结束。
  - `cancel`：取消录制。
- `playUrl`：H5 直接访问的播放地址。只有录制成功后有值。
- `outputDir`：本地录制目录，便于服务端排障或后续归档。
- `m3u8Path`：本地 `index.m3u8` 完整路径。
- `segmentCount`：生成的 ts 分片数量。
- `fileSize`：录制输出的总大小，单位字节。
- `duration`：录制时长，单位秒。
- `codec`：实际写出的主视频编码格式。
- `format`：固定为 `hls`。
- `startTime`：录制实际开始时间。
- `finishTime`：录制实际结束时间。
- `errorMessage`：失败原因。如果成功则为 `null`。

### 成功回调请求体示例

```json
{
  "taskId": "record_20260605_001",
  "classroomId": "room_301",
  "cameraId": "cam_back_301",
  "status": "finished",
  "stopReason": "manual",
  "playUrl": "http://edge.example.com/api/mobile-record/play/record_20260605_001/index.m3u8",
  "outputDir": "D:\\Videos\\Record\\2026-06-05\\record_20260605_001",
  "m3u8Path": "D:\\Videos\\Record\\2026-06-05\\record_20260605_001\\index.m3u8",
  "segmentCount": 360,
  "fileSize": 1850000000,
  "duration": 3600.0,
  "codec": "hevc",
  "format": "hls",
  "startTime": "2026-06-05T10:00:00+08:00",
  "finishTime": "2026-06-05T11:00:00+08:00",
  "errorMessage": null
}
```

### 失败回调请求体示例

```json
{
  "taskId": "record_20260605_001",
  "classroomId": "room_301",
  "cameraId": "cam_back_301",
  "status": "failed",
  "stopReason": null,
  "playUrl": null,
  "outputDir": "D:\\Videos\\Record\\2026-06-05\\record_20260605_001",
  "m3u8Path": "D:\\Videos\\Record\\2026-06-05\\record_20260605_001\\index.m3u8",
  "segmentCount": 0,
  "fileSize": 0,
  "duration": 0,
  "codec": "",
  "format": "hls",
  "startTime": "2026-06-05T10:00:00+08:00",
  "finishTime": "2026-06-05T10:01:00+08:00",
  "errorMessage": "录制文件无效：未生成有效 HLS 分片"
}
```

### 服务端返回要求

- HTTP 状态码 `2xx` 视为成功。
- 非 `2xx` 或请求失败时，边缘服务会重试。
- 当前重试间隔：`0s、10s、30s、60s、300s`。

### 服务端 callback 建议返回示例

成功时建议返回：

```json
{
  "ok": true,
  "message": "received"
}
```

失败时建议返回：

```json
{
  "ok": false,
  "message": "invalid payload"
}
```
