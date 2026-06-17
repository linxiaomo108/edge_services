# 旧项目实现要点摘录（E:\Project\video_service_code\api）

本文用于记录旧项目中与“下载/转码/语音转写/字幕挂载/课堂分析”相关的关键实现点，便于在 `edge_services` 中按同样方式重写实现。

## 1. 海康 NVR 按时间下载（SDK）

核心文件：

- `api/backend/app/hikvision_sdk.py`
- `api/backend/app/hik_download.py`
- `api/backend/app/sdk_loader.py`

关键流程（SDK 模式）：

- `NET_DVR_Init` 初始化，设置 `NET_DVR_SetConnectTime`、`NET_DVR_SetReconnect`
- `NET_DVR_Login_V30(ip, port, username, password, DeviceInfo*)` 登录获取 userId
- 构造 `PlayCond`：
  - `dwChannel` 通道号
  - `struStartTime/struStopTime` 开始结束时间
  - `byRecordFileType` 录像类型（多轮尝试：`0xFF`、`0x00` 等）
- `NET_DVR_GetFileByTime_V40(userId, savePath, PlayCond*)` 启动下载，返回 handle
- `NET_DVR_PlayBackControl_V40(handle, NET_DVR_PLAYSTART, ...)` 开始
- 轮询 `NET_DVR_GetDownloadPos(handle)` 获取进度（0-100，或 -1 表示结束/异常）
- 通过文件大小增长检测“卡住”场景（stall timeout）并中止：`NET_DVR_StopGetFile(handle)`
- 下载完成后释放：`NET_DVR_StopGetFile(handle)` + `NET_DVR_Logout_V30(userId)`

通道映射与重试策略：

- 针对 DS-7808 系列，存在 Web 通道与 SDK 通道不一致，采用候选列表遍历尝试
- 下载不仅尝试通道，还会尝试多组时间窗口（严格窗口 / 扩展 ±5m 等）

## 2. 转码分片压缩（FFmpeg）

核心文件：

- `api/backend/app/ffmpeg_utils.py`
- `api/backend/app/task_manager.py`（`create_transcode_task` / `_run_transcode`）

关键流程：

- MP4 快启：`remux_copy`（视频 copy，音频按需转 AAC，`-movflags +faststart`）
- HLS 打包：`generate_hls_variants`（按变体参数生成 `index.m3u8` + 分片）
- 进度计算：
  - 从 FFmpeg 输出解析 `time=HH:MM:SS.xx`
  - 结合总时长估算百分比

## 3. 语音转写 & 字幕生成

核心文件：

- `api/backend/app/task_manager.py`（`_asr_transcribe` / `_run_subtitle` / `_run_transcribe`）
- `api/backend/app/report_generator.py`（`_extract_wav`）

关键流程（字幕任务）：

- 从视频提取音频 wav（16k）
- ASR 识别：
  - 优先 `faster_whisper`（模型缓存，GPU 优先，失败回退 CPU）
  - 其次 FunASR，再次 PaddleSpeech（兜底）
- 生成字幕文件：SRT + VTT
- 字幕挂载到 MP4：FFmpeg `-c:s mov_text`，并将 VTT 复制到 HLS 目录供前端播放

拆分建议（边缘服务）：

- 将“语音转写（产出 SRT/VTT/报告）”与“字幕挂载（对多视角 MP4/HLS 生效）”拆为两个阶段执行

## 4. 课堂分析报告

核心文件：

- `api/backend/app/report_generator.py`
- `api/backend/app/templates/report_template.html`

关键要点：

- 使用 FFmpeg/ffprobe 获取视频元数据（时长、码率、分辨率、帧率）
- 生成缩略图
- 结合音频/画面特征（依赖 cv2/numpy/webrtcvad 等）生成分析指标
- 通过 HTML 模板注入数据并产出报告 HTML
