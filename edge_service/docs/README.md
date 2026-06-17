# 边缘服务（edge_services）

## 目标

本项目是部署在各校区数据服务器上的独立服务，用于从服务端拉取任务并在本地执行，同时提供本地监控页面查看执行情况。

当前阶段不实现心跳连接，仅实现：

- 拉取待执行任务列表（串行按 taskId 执行）
- 执行过程中每 10 秒上报一次任务状态与进度
- 任务完成的最后一次上报增加阶段产物信息（文件地址、大小等）
- 本地监控页面（固定 IP 访问）

## 目录结构

- sdk/：后续从 NVR 下载视频使用的 SDK（不要改动）
- edge_service/：Python 边缘服务源码
- assets/：报告模板等静态素材
- config/：环境变量示例
- docs/：文档与旧项目实现要点摘录
- scripts/：本机环境自检脚本

## 环境准备

- Python 3.13+

在 `edge_services` 目录下创建虚拟环境并安装依赖：

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

视频相关依赖（可选）：

```bash
pip install -r requirements-video.txt
```

AI 相关依赖（可选，可能要求 Python 版本与运行环境满足对应包的安装条件）：

```bash
pip install -r requirements-ai.txt
```

## 启动

默认启动本地监控服务并在后台启动任务执行器：

```bash
python -m edge_service
```

浏览器访问：

- http://<本机IP>:18080/

## 配置（环境变量）

- EDGE_SERVER_BASE_URL：服务端地址（默认 http://localhost:8080）
- EDGE_TASK_LIST_PATH：任务列表路径（默认 /api/edge/tasks）
- EDGE_TASK_REPORT_PATH：任务状态上报路径（默认 /api/edge/tasks/report）
- EDGE_ID：边缘服务实例标识（默认 local-dev）
- EDGE_POLL_INTERVAL_SEC：拉取任务间隔秒（默认 5）
- EDGE_REPORT_INTERVAL_SEC：上报间隔秒（默认 10）
- EDGE_BIND_HOST：监控服务绑定地址（默认 0.0.0.0）
- EDGE_BIND_PORT：监控服务端口（默认 18080）
- EDGE_SIMULATE：是否模拟执行（默认 1，后续接入真实下载/转码/语音等实现后可关闭）
- EDGE_HIK_SDK_DIR：海康 SDK 目录（默认 edge_services/sdk）
- EDGE_FFMPEG_BIN：ffmpeg 可执行文件（默认 ffmpeg）

## 自检

```bash
python scripts/self_check.py
```

## 任务与排障文档

- `camera_scene_codes.md`：`camera.py` 下载/合并链路 scene code 清单、典型场景说明与后续补充规范。
