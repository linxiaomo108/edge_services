"""
设备检测模块
自动检测最佳计算设备（GPU/CPU）
"""

import os
import logging
from typing import Tuple, Optional

log = logging.getLogger("edge.device")

# 缓存检测结果
_cached_device: Optional[Tuple[str, str]] = None


def detect_compute_device() -> Tuple[str, str]:
    """
    自动检测最佳计算设备
    
    Returns:
        Tuple[str, str]: (device_type, device_info)
        - device_type: "cuda" 或 "cpu"
        - device_info: 设备详细信息
    """
    global _cached_device
    if _cached_device is not None:
        return _cached_device
    
    # 检查环境变量强制指定
    force_device = os.getenv("EDGE_COMPUTE_DEVICE", "").strip().lower()
    if force_device == "cpu":
        _cached_device = ("cpu", "强制使用CPU模式")
        log.info("计算设备: %s (%s)", *_cached_device)
        return _cached_device
    
    # 尝试检测CUDA
    try:
        import torch
        if torch.cuda.is_available():
            gpu_count = torch.cuda.device_count()
            gpu_name = torch.cuda.get_device_name(0)
            gpu_props = torch.cuda.get_device_properties(0)
            gpu_memory_gb = gpu_props.total_memory / (1024 ** 3)
            
            # 检查显存是否足够（large-v3需要约6GB）
            min_memory_gb = float(os.getenv("EDGE_MIN_GPU_MEMORY_GB", "5.5"))
            if gpu_memory_gb >= min_memory_gb:
                _cached_device = ("cuda", f"{gpu_name} ({gpu_memory_gb:.1f}GB)")
                log.info("计算设备: CUDA - %s", _cached_device[1])
                return _cached_device
            else:
                _cached_device = ("cpu", f"GPU显存不足: {gpu_memory_gb:.1f}GB < {min_memory_gb}GB")
                log.warning("GPU显存不足，使用CPU模式: %s", _cached_device[1])
                return _cached_device
        else:
            _cached_device = ("cpu", "CUDA不可用")
            log.info("计算设备: CPU (CUDA不可用)")
            return _cached_device
    except ImportError:
        _cached_device = ("cpu", "未安装PyTorch")
        log.info("计算设备: CPU (未安装PyTorch)")
        return _cached_device
    except Exception as e:
        _cached_device = ("cpu", f"检测失败: {e}")
        log.warning("设备检测失败，使用CPU模式: %s", e)
        return _cached_device


def get_whisper_device() -> str:
    """
    获取Whisper使用的设备
    
    Returns:
        str: "cuda" 或 "cpu"
    """
    device, _ = detect_compute_device()
    return device


def get_device_info() -> dict:
    """
    获取设备详细信息
    
    Returns:
        dict: 设备信息字典
    """
    device, info = detect_compute_device()
    result = {
        "device": device,
        "info": info,
        "cuda_available": False,
        "gpu_name": None,
        "gpu_memory_gb": None,
        "gpu_count": 0,
    }
    
    try:
        import torch
        result["cuda_available"] = torch.cuda.is_available()
        if result["cuda_available"]:
            result["gpu_count"] = torch.cuda.device_count()
            result["gpu_name"] = torch.cuda.get_device_name(0)
            result["gpu_memory_gb"] = round(
                torch.cuda.get_device_properties(0).total_memory / (1024 ** 3), 2
            )
    except Exception:
        pass
    
    return result


def print_device_info():
    """打印设备信息到控制台"""
    info = get_device_info()
    print("=" * 50)
    print("计算设备检测结果")
    print("=" * 50)
    print(f"使用设备: {info['device'].upper()}")
    print(f"设备信息: {info['info']}")
    if info['cuda_available']:
        print(f"GPU数量: {info['gpu_count']}")
        print(f"GPU名称: {info['gpu_name']}")
        print(f"GPU显存: {info['gpu_memory_gb']} GB")
    print("=" * 50)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print_device_info()
