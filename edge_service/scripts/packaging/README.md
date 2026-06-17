# EdgeService 客户端打包指南

## 概述

本目录包含将边缘服务打包为独立客户端的所有脚本和配置。

## 文件说明

| 文件 | 说明 |
|------|------|
| `build.py` | 主打包脚本 |
| `edge_service.spec` | PyInstaller配置文件 |

## 打包步骤

### 1. 安装依赖

```bash
pip install pyinstaller
pip install -r requirements.txt
pip install -r requirements-ai.txt
pip install -r requirements-video.txt
```

### 2. 运行打包脚本

```bash
cd scripts/packaging
python build.py
```

### 3. 打包选项

```bash
# 完整打包（包含模型和FFmpeg）
python build.py

# 跳过模型下载（手动放置模型）
python build.py --skip-model

# 跳过FFmpeg下载（手动放置FFmpeg）
python build.py --skip-ffmpeg

# 跳过PyInstaller打包（仅复制资源）
python build.py --skip-pyinstaller
```

## 输出结构

打包完成后，输出目录 `dist/EdgeServiceClient/` 结构如下：

```
EdgeServiceClient/
├── start.bat                # 启动脚本
├── config.example.json      # 配置模板
├── version.json             # 版本信息
├── README.txt               # 使用说明
│
├── core/                    # 核心程序
│   ├── EdgeServiceCore.exe
│   └── _internal/
│
├── sdk/                     # 海康SDK
│   └── *.dll
│
├── ffmpeg/                  # FFmpeg
│   ├── ffmpeg.exe
│   └── ffprobe.exe
│
├── models/                  # Whisper模型
│   └── faster-whisper-large-v3/
│
├── docs/                    # 配置文件
│   ├── correction_dict.json
│   └── hallucination_blacklist.json
│
├── monitor_ui/              # 前端页面
│   └── node_detail.html
│
└── output/                  # 运行时数据
```

## 手动准备资源

如果自动下载失败，可以手动准备以下资源：

### FFmpeg

1. 下载: https://github.com/BtbN/FFmpeg-Builds/releases
2. 选择: `ffmpeg-master-latest-win64-gpl.zip`
3. 解压 `ffmpeg.exe` 和 `ffprobe.exe` 到 `ffmpeg/` 目录

### Whisper模型

1. 下载: https://huggingface.co/Systran/faster-whisper-large-v3
2. 下载以下文件:
   - config.json
   - model.bin
   - tokenizer.json
   - vocabulary.json
3. 放入 `models/faster-whisper-large-v3/` 目录

## 用户配置

用户拷贝客户端后，需要编辑 `config.json`:

```json
{
  "server": {
    "address": "http://your-server:8080",
    "campusCode": "your-campus-code",
    "serverId": "edge-001"
  },
  "local": {
    "bindPort": 18080,
    "downloadPath": "D:\\Videos"
  }
}
```

## 更新机制

### 版本检查

客户端启动时会检查服务端是否有新版本：

```
GET /api/client/version
```

### 获取更新清单

```
GET /api/client/updates?from=1.0.0&to=1.1.0
```

### 下载更新包

```
GET /api/client/download/{filename}
```

### 服务端配置

在服务端 `updates/` 目录下放置：

1. `version.json` - 版本信息
2. `manifest.json` - 更新清单
3. `*.zip` - 更新包文件

## 预估体积

| 组件 | 大小 |
|------|------|
| Python + 依赖 | ~500MB |
| faster-whisper-large-v3 | ~3GB |
| 海康SDK | ~50MB |
| FFmpeg | ~100MB |
| **总计** | **~4GB** |

## 常见问题

### Q: GPU不可用怎么办？
A: 程序会自动检测并回退到CPU模式，无需手动配置。

### Q: 模型加载很慢？
A: 首次加载需要较长时间，后续会缓存在内存中。

### Q: 如何更新客户端？
A: 启动时会自动检查更新，也可以手动下载新版本覆盖。
