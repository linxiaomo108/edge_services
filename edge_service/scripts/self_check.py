from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from edge_service.video.hik.loader import resolve_sdk_dir
from edge_service.video.hik.systrans import resolve_systrans_sdk_dir


def _ok(label: str) -> None:
    print(f"[OK] {label}")


def _warn(label: str) -> None:
    print(f"[WARN] {label}")


def _fail(label: str) -> None:
    print(f"[FAIL] {label}")


def main() -> int:
    sdk_dir = resolve_sdk_dir()
    systrans_sdk_dir = resolve_systrans_sdk_dir()
    ffmpeg_bin = os.getenv("EDGE_FFMPEG_BIN") or "ffmpeg"

    print(f"python={sys.version.split()[0]}")
    print(f"sdk_dir={sdk_dir}")
    print(f"systrans_sdk_dir={systrans_sdk_dir}")
    print(f"ffmpeg_bin={ffmpeg_bin}")

    if Path(sdk_dir).exists():
        must = ["HCNetSDK.dll", "HCCore.dll", "PlayCtrl.dll"]
        missing = [n for n in must if not (Path(sdk_dir) / n).exists()]
        if missing:
            _warn(f"Hik SDK 缺少文件: {', '.join(missing)}")
        else:
            _ok("Hik SDK 文件存在")
    else:
        _fail("Hik SDK 目录不存在")
        return 2

    if Path(systrans_sdk_dir).exists():
        must = ["SystemTransform.dll", "StreamTransClient.dll"]
        missing = [n for n in must if not (Path(systrans_sdk_dir) / n).exists()]
        if missing:
            _warn(f"Hik SystemTransform SDK 缺少文件: {', '.join(missing)}")
        else:
            _ok("Hik SystemTransform SDK 文件存在")
    else:
        _fail("Hik SystemTransform SDK 目录不存在")
        return 2

    if shutil.which(ffmpeg_bin) or Path(ffmpeg_bin).exists():
        _ok("FFmpeg 可用")
    else:
        _warn("FFmpeg 未找到（转码/字幕挂载需要）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
