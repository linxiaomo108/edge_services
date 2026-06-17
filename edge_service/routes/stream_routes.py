from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..video.ffmpeg import ffmpeg_bin as _ffmpeg_bin

_log = logging.getLogger("edge.stream_routes")


class OpenStreamRequest(BaseModel):
    """创建实时流播放会话请求"""
    campusCode: str = Field(default="", description="校区编码，用于访问控制校验")
    nvrDeviceId: int = Field(..., description="NVR设备ID")
    nvrChannelNum: int = Field(..., description="业务通道号（前端展示用）")
    nvrChannelId: str = Field(default="", description="通道ID标识")
    provider: str = Field(default="", description="NVR厂商标识，默认hikvision")
    ipAddress: str = Field(..., description="NVR设备IP地址")
    port: int = Field(default=554, description="NVR设备端口，默认554")
    account: str = Field(default="", description="NVR登录账号")
    password: str = Field(default="", description="NVR登录密码")
    streamProfile: str = Field(default="main", description="码流类型：main=主码流，sub=子码流")
    outputProtocol: str = Field(default="mpegts", description="输出协议，当前仅支持mpegts")
    reuseIfExists: bool = Field(default=True, description="是否复用已存在的会话")


class CloseStreamRequest(BaseModel):
    """关闭实时流播放会话请求"""
    sessionId: str = Field(default="", description="会话ID")


class OpenStreamResponse(BaseModel):
    """创建实时流播放会话响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="结果消息")
    campusCode: str = Field(default="", description="校区编码")
    nvrDeviceId: int = Field(default=0, description="NVR设备ID")
    nvrChannelNum: int = Field(default=0, description="业务通道号")
    nvrChannelId: str = Field(default="", description="通道ID标识")
    sessionId: str = Field(default="", description="会话ID")
    publicPlayUrl: str = Field(default="", description="默认播放地址。若配置了公网映射则返回公网地址，否则回退为边缘服务本机地址")
    lanPlayUrl: str = Field(default="", description="边缘服务本机局域网播放地址，供同校区或已确认可直连边缘服务内网的场景使用")
    outputProtocol: str = Field(default="", description="输出协议")
    resolvedChannel: int = Field(default=0, description="解析后的SDK通道号")
    sourceType: str = Field(default="", description="流来源类型")
    reused: bool = Field(default=False, description="是否复用已有会话")


class StreamStatusResponse(BaseModel):
    """实时流代理状态响应"""
    enabled: bool = Field(..., description="是否启用")
    sessionTtlSeconds: int = Field(..., description="会话超时时间（秒）")
    activeSessionCount: int = Field(..., description="活跃会话数量")
    activeSessions: list = Field(default=[], description="活跃会话列表")
    publicBaseUrl: str = Field(default="", description="公网基础URL")
    publicHost: str = Field(default="", description="公网主机")
    publicPort: str = Field(default="", description="公网端口")
    publicScheme: str = Field(default="", description="公网协议")


_STREAM_STDERR_NOISE_TOKENS = (
    "non-monotonic dts",
    "this may result in incorrect timestamps in the output file",
    "bad cseq",
    "cseq ",
    "packet with pts",
    "duration 0",
    "may not be precise",
)


def _is_noisy_stream_stderr(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return any(token in lowered for token in _STREAM_STDERR_NOISE_TOKENS)


def _is_important_stream_stderr(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    important_tokens = (
        "invalid data found",
        "connection refused",
        "connection timed out",
        "timed out",
        "timeout",
        "unable to",
        "error",
        "failed",
        "401",
        "403",
        "404",
        "500",
        "unauthorized",
        "forbidden",
        "network is unreachable",
        "no route to host",
    )
    return any(token in lowered for token in important_tokens)


def create_stream_router(*, load_monitor_cfg, session_manager) -> APIRouter:
    router = APIRouter()

    def _as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return default

    def _env_int(name: str, default: int) -> int:
        raw = str(os.getenv(name) or "").strip()
        if not raw:
            return int(default)
        try:
            value = int(raw)
        except Exception:
            return int(default)
        return value if value > 0 else int(default)

    def _cfg_value(key: str) -> str:
        data = load_monitor_cfg() or {}
        return str(data.get(key) or "").strip()

    def _stream_enabled() -> bool:
        data = load_monitor_cfg() or {}
        return _as_bool(data.get("enableStreamProxy") if "enableStreamProxy" in data else False, False)

    def _validate_campus(campus_code: str) -> tuple[bool, str]:
        local_code = _cfg_value("campusCode")
        request_code = str(campus_code or "").strip()
        if not request_code:
            return False, "campusCode 不能为空"
        if local_code and request_code != local_code:
            return False, "campusCode 校验失败"
        return True, ""

    def _build_public_play_url(request: Request, nvr_device_id: int, channel_num: int) -> str:
        base_url = session_manager.build_public_access_base_url(str(request.base_url).rstrip("/"))
        return f"{base_url}/api/stream/play/{nvr_device_id}/{channel_num}"

    def _build_lan_play_url(request: Request, nvr_device_id: int, channel_num: int) -> str:
        base_url = session_manager.build_lan_access_base_url(str(request.base_url).rstrip("/"))
        return f"{base_url}/api/stream/play/{nvr_device_id}/{channel_num}"

    def _build_stream_attempts(rtsp_url: str) -> list[tuple[str, list[str]]]:
        ffmpeg_bin = _ffmpeg_bin()
        base_output = [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-fflags",
            "+genpts",
            "-muxdelay",
            "0.1",
            "-muxpreload",
            "0.1",
            "-f",
            "mpegts",
            "pipe:1",
        ]
        common_prefix = [
            ffmpeg_bin,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
        ]
        def _input_args(*, transport: str, timeout_us: int, prefer_tcp: bool) -> list[str]:
            args = [
                "-rtsp_transport",
                transport,
            ]
            if prefer_tcp:
                args.extend([
                    "-rtsp_flags",
                    "prefer_tcp",
                ])
            args.extend([
                "-timeout",
                str(timeout_us),
                "-analyzeduration",
                "20000000",
                "-probesize",
                "20000000",
                "-use_wallclock_as_timestamps",
                "1",
                "-fflags",
                "+discardcorrupt",
                "-i",
                rtsp_url,
            ])
            return args
        return [
            (
                "tcp_prefer",
                [
                    *common_prefix,
                    *_input_args(transport="tcp", timeout_us=15 * 1000000, prefer_tcp=True),
                    *base_output,
                ],
            ),
            (
                "tcp_retry",
                [
                    *common_prefix,
                    *_input_args(transport="tcp", timeout_us=20 * 1000000, prefer_tcp=False),
                    *base_output,
                ],
            ),
            (
                "udp_fallback",
                [
                    *common_prefix,
                    *_input_args(transport="udp", timeout_us=12 * 1000000, prefer_tcp=False),
                    *base_output,
                ],
            ),
        ]

    def _get_client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
        if request.client:
            return request.client.host
        return "unknown"

    def _touch_stream_activity(nvr_device_id: int, channel_num: int, reason: str) -> None:
        session_manager.activate_channel(nvr_device_id, channel_num)
        session_manager.get_session_by_key(nvr_device_id, channel_num)
        _log.debug("PLAY touch reason=%s nvrDeviceId=%s nvrChannelNum=%s", reason, nvr_device_id, channel_num)

    @router.post("/api/stream/open", summary="创建实时流播放会话", description="创建或复用实时流播放会话，返回 publicPlayUrl 与 lanPlayUrl。接口不再基于请求来源 IP 自动切换内外网地址，调用方应按自身场景选择可访问地址。需要先启用实时流代理功能。该接口仅用于实时流播放，不用于历史视频回放。历史视频请改用 `POST /api/history/video/open` 创建会话：入参为 `lessonId + taskType + sourceType(mp4/hls)`，返回会话级公网/局域网播放地址；随后通过 `GET /api/history/video/play/{session_id}` 播放，并在结束后调用 `POST /api/history/video/close` 主动关闭会话。", response_model=OpenStreamResponse)
    async def api_stream_open(req: OpenStreamRequest, request: Request) -> OpenStreamResponse:
        client_ip = _get_client_ip(request)
        request_time = time.strftime("%Y-%m-%d %H:%M:%S")
        _log.info("OPEN client_ip=%s requestTime=%s nvrDeviceId=%s nvrChannelNum=%s", client_ip, request_time, req.nvrDeviceId, req.nvrChannelNum)
        if not _stream_enabled():
            _log.warning("OPEN rejected=proxy_disabled client_ip=%s nvrDeviceId=%s nvrChannelNum=%s", client_ip, req.nvrDeviceId, req.nvrChannelNum)
            return JSONResponse({"success": False, "message": "实时流代理未启用"}, status_code=400)
        ok, message = _validate_campus(req.campusCode)
        if not ok:
            _log.warning("OPEN rejected=campus_invalid client_ip=%s campusCode=%s nvrDeviceId=%s nvrChannelNum=%s", client_ip, str(req.campusCode or "").strip(), req.nvrDeviceId, req.nvrChannelNum)
            return JSONResponse({"success": False, "message": message}, status_code=403)
        _log.info("OPEN processing client_ip=%s campusCode=%s provider=%s nvrDeviceId=%s nvrChannelNum=%s nvrChannelId=%s nvrIp=%s nvrPort=%s profile=%s protocol=%s reuse=%s", client_ip, str(req.campusCode or "").strip(), str(req.provider or "").strip(), req.nvrDeviceId, req.nvrChannelNum, str(req.nvrChannelId or "").strip(), str(req.ipAddress or "").strip(), int(req.port or 554), str(req.streamProfile or "main").strip(), str(req.outputProtocol or "mpegts").strip(), bool(req.reuseIfExists))
        try:
            session, reused = session_manager.create_or_get_session(
                campus_code=req.campusCode,
                nvr_device_id=int(req.nvrDeviceId),
                nvr_channel_num=int(req.nvrChannelNum),
                nvr_channel_id=str(req.nvrChannelId or "").strip(),
                ip_address=str(req.ipAddress or "").strip(),
                port=int(req.port or 554),
                account=str(req.account or "").strip(),
                password=str(req.password or "").strip(),
                provider=str(req.provider or "").strip() or None,
                candidate_channels=None,
                stream_profile=req.streamProfile,
                output_protocol=req.outputProtocol,
                reuse_if_exists=bool(req.reuseIfExists),
            )
        except ValueError as exc:
            return JSONResponse({"success": False, "message": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return JSONResponse({"success": False, "message": str(exc)}, status_code=500)
        if not reused:
            session_manager.save_nvr_device(
                nvr_device_id=int(req.nvrDeviceId),
                campus_code=str(req.campusCode or "").strip(),
                ip_address=str(req.ipAddress or "").strip(),
                port=int(req.port or 554),
                account=str(req.account or "").strip(),
                online_status=1,
            )
            session_manager.save_nvr_channel(
                nvr_device_id=int(req.nvrDeviceId),
                channel_num=int(req.nvrChannelNum),
                channel_id=str(req.nvrChannelId or "").strip(),
                sdk_channel=session.resolved_channel,
                rtsp_channel=int(req.nvrChannelNum),
                stream_status=1,
            )
        session_manager.activate_channel(int(req.nvrDeviceId), int(req.nvrChannelNum))
        public_play_url = _build_public_play_url(request, int(req.nvrDeviceId), int(req.nvrChannelNum))
        lan_play_url = _build_lan_play_url(request, int(req.nvrDeviceId), int(req.nvrChannelNum))
        return JSONResponse(
            {
                "success": True,
                "message": "ok",
                "campusCode": session.campus_code,
                "nvrDeviceId": session.nvr_device_id,
                "nvrChannelNum": session.nvr_channel_num,
                "nvrChannelId": session.nvr_channel_id,
                "sessionId": session.session_id,
                "publicPlayUrl": public_play_url,
                "lanPlayUrl": lan_play_url,
                "outputProtocol": session.output_protocol,
                "resolvedChannel": session.resolved_channel,
                "sourceType": session.source_type,
                "reused": reused,
            }
        )

    @router.get("/api/stream/play/{nvr_device_id}/{channel_num}", summary="播放实时流", description="根据NVR设备ID和通道号获取MPEG-TS实时流数据。需要先调用open接口激活通道。返回Content-Type为video/mp2t的流式响应。")
    async def api_stream_play(nvr_device_id: int, channel_num: int, request: Request):
        client_ip = _get_client_ip(request)
        request_time = time.strftime("%Y-%m-%d %H:%M:%S")
        _log.info("PLAY client_ip=%s requestTime=%s nvrDeviceId=%s nvrChannelNum=%s", client_ip, request_time, nvr_device_id, channel_num)
        session = session_manager.get_session_by_key(nvr_device_id, channel_num)
        if session is None:
            _log.warning("PLAY rejected=session_not_found client_ip=%s nvrDeviceId=%s nvrChannelNum=%s", client_ip, nvr_device_id, channel_num)
            return JSONResponse({"success": False, "message": "流会话不存在，请先调用open接口"}, status_code=404)
        if not session_manager.is_channel_active(nvr_device_id, channel_num, ttl_sec=300):
            if not session_manager.activate_channel(nvr_device_id, channel_num):
                _log.warning("PLAY rejected=channel_reactivate_failed client_ip=%s nvrDeviceId=%s nvrChannelNum=%s sessionId=%s", client_ip, nvr_device_id, channel_num, session.session_id)
                return JSONResponse({"success": False, "message": "通道未激活或已过期，请先调用open接口"}, status_code=403)
            _log.info("PLAY reactivated client_ip=%s nvrDeviceId=%s nvrChannelNum=%s sessionId=%s", client_ip, nvr_device_id, channel_num, session.session_id)
        if session.output_protocol != "mpegts":
            _log.warning("stream play unsupported protocol: nvr_device_id=%s channel_num=%s protocol=%s", nvr_device_id, channel_num, session.output_protocol)
            return JSONResponse({"success": False, "message": "当前仅支持 mpegts 输出协议"}, status_code=400)
        attempt_plans = _build_stream_attempts(session.rtsp_url)
        first_chunk_timeout_sec = _env_int("EDGE_STREAM_FIRST_CHUNK_TIMEOUT_SEC", 8)
        retry_delay_ms = _env_int("EDGE_STREAM_RETRY_DELAY_MS", 800)
        recovery_retry_delay_ms = _env_int("EDGE_STREAM_RECOVERY_DELAY_MS", 500)
        max_recovery_attempts = _env_int("EDGE_STREAM_MAX_RECOVERY_ATTEMPTS", 3)
        activity_touch_interval_sec = _env_int("EDGE_STREAM_ACTIVITY_TOUCH_INTERVAL_SEC", 30)
        _touch_stream_activity(nvr_device_id, channel_num, "play_start")
        _log.info("PLAY ffmpegBin=%s attempts=%s", _ffmpeg_bin(), len(attempt_plans))

        async def _iter_stream():
            recovery_attempt = 0
            while True:
                stream_started = False
                for attempt_index, (attempt_name, cmd) in enumerate(attempt_plans, start=1):
                    stderr_lines: list[str] = []
                    noisy_stderr_count = 0
                    first_chunk_at: float | None = None
                    attempt_started_at = time.time()
                    last_touch_at = attempt_started_at
                    _log.info("PLAY starting client_ip=%s nvrDeviceId=%s nvrChannelNum=%s sessionId=%s recoveryAttempt=%s/%s attempt=%s mode=%s rtspUrl=%s", client_ip, nvr_device_id, channel_num, session.session_id, recovery_attempt, max_recovery_attempts, attempt_index, attempt_name, session.rtsp_url)
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )

                    async def _read_stderr() -> None:
                        if proc.stderr is None:
                            return
                        try:
                            while True:
                                line = await proc.stderr.readline()
                                if not line:
                                    break
                                text = line.decode("utf-8", errors="ignore").strip()
                                if not text:
                                    continue
                                is_noisy = _is_noisy_stream_stderr(text)
                                if is_noisy:
                                    noisy_stderr_count += 1
                                if len(stderr_lines) < 20 and (not is_noisy or _is_important_stream_stderr(text)):
                                    stderr_lines.append(text)
                                if _is_important_stream_stderr(text):
                                    _log.warning("stream ffmpeg stderr: sessionId=%s recoveryAttempt=%s attempt=%s mode=%s %s", session.session_id, recovery_attempt, attempt_index, attempt_name, text)
                        except Exception:
                            _log.exception("stream stderr reader failed: session_id=%s recovery_attempt=%s attempt=%s mode=%s", session.session_id, recovery_attempt, attempt_index, attempt_name)

                    stderr_task = asyncio.create_task(_read_stderr())
                    try:
                        if proc.stdout is None:
                            continue
                        try:
                            first_chunk = await asyncio.wait_for(proc.stdout.read(64 * 1024), timeout=float(first_chunk_timeout_sec))
                        except asyncio.TimeoutError:
                            _log.warning("stream first chunk timeout: session_id=%s recovery_attempt=%s attempt=%s mode=%s timeout_sec=%s", session.session_id, recovery_attempt, attempt_index, attempt_name, first_chunk_timeout_sec)
                            first_chunk = b""
                        if not first_chunk:
                            _log.warning("stream empty first chunk: session_id=%s recovery_attempt=%s attempt=%s mode=%s returncode=%s", session.session_id, recovery_attempt, attempt_index, attempt_name, proc.returncode)
                            continue
                        stream_started = True
                        first_chunk_at = time.time()
                        last_touch_at = first_chunk_at
                        _touch_stream_activity(nvr_device_id, channel_num, "first_chunk")
                        _log.info("stream first chunk: session_id=%s recovery_attempt=%s attempt=%s mode=%s bytes=%s latency_sec=%.3f", session.session_id, recovery_attempt, attempt_index, attempt_name, len(first_chunk), first_chunk_at - attempt_started_at)
                        yield first_chunk
                        while True:
                            chunk = await proc.stdout.read(64 * 1024)
                            if not chunk:
                                break
                            now_ts = time.time()
                            if now_ts - last_touch_at >= float(activity_touch_interval_sec):
                                _touch_stream_activity(nvr_device_id, channel_num, "streaming")
                                last_touch_at = now_ts
                            yield chunk
                    finally:
                        if proc.returncode is None:
                            proc.terminate()
                            try:
                                await asyncio.wait_for(proc.wait(), timeout=3.0)
                            except Exception:
                                proc.kill()
                                with contextlib.suppress(Exception):
                                    await proc.wait()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(stderr_task, timeout=1.0)
                        _log.info("stream play exit: session_id=%s recovery_attempt=%s attempt=%s mode=%s returncode=%s", session.session_id, recovery_attempt, attempt_index, attempt_name, proc.returncode)
                        if stderr_lines:
                            _log.info("stream ffmpeg stderr summary: session_id=%s recovery_attempt=%s attempt=%s mode=%s %s", session.session_id, recovery_attempt, attempt_index, attempt_name, "\n".join(stderr_lines))
                        elif noisy_stderr_count > 0:
                            _log.debug("stream ffmpeg noisy stderr suppressed: sessionId=%s recoveryAttempt=%s attempt=%s mode=%s count=%s", session.session_id, recovery_attempt, attempt_index, attempt_name, noisy_stderr_count)
                    if stream_started:
                        break
                    if attempt_index < len(attempt_plans):
                        await asyncio.sleep(max(0.0, float(retry_delay_ms) / 1000.0))
                if not stream_started:
                    _log.error("stream play failed after retries: session_id=%s nvr_device_id=%s channel_num=%s attempts=%s recovery_attempt=%s", session.session_id, nvr_device_id, channel_num, len(attempt_plans), recovery_attempt)
                    return
                if recovery_attempt >= max_recovery_attempts:
                    _log.error("stream recovery exhausted: session_id=%s nvr_device_id=%s channel_num=%s recovery_attempt=%s", session.session_id, nvr_device_id, channel_num, recovery_attempt)
                    return
                recovery_attempt += 1
                _touch_stream_activity(nvr_device_id, channel_num, "recovery")
                _log.warning("stream interrupted, restarting: session_id=%s nvr_device_id=%s channel_num=%s recovery_attempt=%s/%s delay_ms=%s", session.session_id, nvr_device_id, channel_num, recovery_attempt, max_recovery_attempts, recovery_retry_delay_ms)
                await asyncio.sleep(max(0.0, float(recovery_retry_delay_ms) / 1000.0))

        return StreamingResponse(
            _iter_stream(),
            media_type="video/mp2t",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @router.post("/api/stream/close", summary="关闭实时流会话", description="关闭指定的实时流播放会话，释放相关资源。")
    async def api_stream_close(req: CloseStreamRequest):
        session_id = str(req.sessionId or "").strip()
        if not session_id:
            return JSONResponse({"success": False, "message": "sessionId 不能为空"}, status_code=400)
        closed = session_manager.close_session(session_id)
        if not closed:
            return JSONResponse({"success": False, "message": "流会话不存在或已关闭"}, status_code=404)
        return JSONResponse({"success": True, "message": "ok"})

    @router.get("/api/stream/status", summary="查看实时流代理状态", description="获取实时流代理的运行状态，包括启用状态、会话TTL、活跃会话列表和公网配置。", response_model=StreamStatusResponse)
    async def api_stream_status() -> StreamStatusResponse:
        status = session_manager.get_status()
        return JSONResponse({"success": True, **status})

    return router
