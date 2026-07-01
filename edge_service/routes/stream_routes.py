from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..video.ffmpeg import ffmpeg_bin as _ffmpeg_bin

_log = logging.getLogger("edge.stream_routes")


class OpenStreamRequest(BaseModel):
    """创建实时流播放会话请求"""
    campusCode: str = Field(default="", description="格式：string。校区编码，用于校验请求是否发到了正确的边缘服务节点；必须与边缘服务本地配置一致。")
    nvrDeviceId: int = Field(..., description="格式：int。NVR 设备在业务系统中的设备ID，用于标识哪一台录像设备。")
    nvrChannelNum: int = Field(..., description="格式：int。业务通道号，也就是要实时预览的摄像头通道号；海康 RTSP 地址会按该通道号生成。")
    nvrChannelId: str = Field(default="", description="格式：string。业务侧通道ID，可为空；用于日志和通道记录，实时取流主要使用 nvrChannelNum。")
    provider: str = Field(default="", description="格式：string。NVR 厂商标识。当前建议传 `hikvision`；为空时按默认厂商规则处理。")
    ipAddress: str = Field(..., description="格式：string。NVR 的 RTSP 可访问 IP 或域名，不能传 string/null/none/undefined 等占位值。")
    port: int = Field(default=554, description="格式：int。NVR 的 RTSP 端口，默认 554；如果使用端口映射，传映射后的 RTSP 端口。")
    account: str = Field(default="", description="格式：string。NVR 登录账号，用于生成 RTSP 地址。")
    password: str = Field(default="", description="格式：string。NVR 登录密码，用于生成 RTSP 地址。")
    streamProfile: str = Field(default="main", description="格式：string。码流类型，固定值：`main`=优先主码流，`sub`=子码流。当前传 main 时主码流不可播放会自动回退 sub；传 sub 时只使用子码流。")
    outputProtocol: str = Field(default="mpegts", description="格式：string。输出协议，当前固定使用 `mpegts`；兼容传入 `ts`、`mpeg-ts`，内部统一为 mpegts。")
    reuseIfExists: bool = Field(default=True, description="格式：boolean。是否复用已有实时流会话。建议传 true；同设备同通道重复 open 时会复用 sessionId 并跳过重复探测，但每次 open 仍会生成新的 viewerId。")
    operatorUserId: str = Field(default="", description="格式：string。实时预览发起人的用户ID。建议服务端必传；边缘服务会将它与 viewerId 绑定，后续播放/关闭日志即使没有再次传参，也能自动补齐观看人。")
    operatorUserName: str = Field(default="", description="格式：string。实时预览发起人的姓名。建议服务端必传；仅用于日志追踪和排查，不参与 NVR 鉴权。")
    operatorAccount: str = Field(default="", description="格式：string。实时预览发起人的登录账号。建议服务端必传；仅用于日志追踪和排查，不参与 NVR 鉴权。")


class CloseStreamRequest(BaseModel):
    """关闭实时流播放会话请求"""
    sessionId: str = Field(default="", description="实时流播放会话ID，来自 `/api/stream/open` 返回的 `sessionId`。")
    viewerId: str = Field(default="", description="观看连接ID，来自 `/api/stream/open` 返回的 `viewerId`。传入时只关闭该用户/页面的播放连接；为空时关闭整个 sessionId 对应的实时流会话。操作者信息不需要在关闭接口再次传入，边缘服务会使用 open 阶段与 viewerId 绑定的信息记录日志。")


class OpenStreamResponse(BaseModel):
    """创建实时流播放会话响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="结果消息")
    campusCode: str = Field(default="", description="校区编码")
    nvrDeviceId: int = Field(default=0, description="NVR设备ID")
    nvrChannelNum: int = Field(default=0, description="业务通道号")
    nvrChannelId: str = Field(default="", description="通道ID标识")
    sessionId: str = Field(default="", description="实时流播放会话ID。后续播放和关闭都使用这个会话ID定位本次实时流会话。")
    viewerId: str = Field(default="", description="观看连接ID。本次 open 调用生成一个 viewerId，并拼到播放地址 query 中；关闭单个用户播放时需要把 sessionId 和 viewerId 一起传给 `/api/stream/close`。")
    publicPlayUrl: str = Field(default="", description="公网播放地址。格式为 `/api/stream/play/session/{sessionId}?viewerId={viewerId}`。服务端/前端应直接使用该字段，不要自行拼接设备和通道播放地址；观看人信息由 open 阶段传入并在边缘服务内部与 viewerId 绑定。")
    lanPlayUrl: str = Field(default="", description="局域网播放地址。格式为 `/api/stream/play/session/{sessionId}?viewerId={viewerId}`。用于同校区或可直连边缘服务内网的场景；观看人信息同样由 open 阶段绑定，不会重复拼到播放地址中。")
    outputProtocol: str = Field(default="", description="输出协议。当前固定为 `mpegts`，播放接口返回 `video/mp2t` 流式数据。")
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
    viewer_controls: dict[tuple[str, str], dict[str, Any]] = {}
    viewer_meta: dict[tuple[str, str], dict[str, str]] = {}
    viewer_controls_lock = asyncio.Lock()

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

    def _play_query(*, viewer_id: str) -> str:
        return urlencode({"viewerId": str(viewer_id or "").strip()})

    def _build_public_play_url(
        request: Request,
        session_id: str,
        viewer_id: str,
    ) -> str:
        base_url = session_manager.build_public_access_base_url(str(request.base_url).rstrip("/"))
        query = _play_query(viewer_id=viewer_id)
        return f"{base_url}/api/stream/play/session/{session_id}?{query}"

    def _build_lan_play_url(
        request: Request,
        session_id: str,
        viewer_id: str,
    ) -> str:
        base_url = session_manager.build_lan_access_base_url(str(request.base_url).rstrip("/"))
        query = _play_query(viewer_id=viewer_id)
        return f"{base_url}/api/stream/play/session/{session_id}?{query}"

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

    def _trace_text(value: Any) -> str:
        text = str(value or "").strip()
        if text.lower() in {"null", "none", "undefined"}:
            return ""
        return text

    def _find_body_text(data: Any, keys: tuple[str, ...]) -> str:
        if not isinstance(data, dict):
            return ""
        lowered = {str(k).lower(): v for k, v in data.items()}
        for key in keys:
            value = lowered.get(str(key).lower())
            text = _trace_text(value)
            if text:
                return text
        for nested_key in ("command", "data", "params", "requestParams"):
            nested = lowered.get(nested_key.lower())
            text = _find_body_text(nested, keys)
            if text:
                return text
        return ""

    def _meta_value(meta: dict[str, str] | None, key: str) -> str:
        if not meta:
            return ""
        return _trace_text(meta.get(key))

    def _touch_stream_activity(nvr_device_id: int, channel_num: int, reason: str) -> None:
        session_manager.activate_channel(nvr_device_id, channel_num)
        session_manager.get_session_by_key(nvr_device_id, channel_num)
        _log.debug("PLAY touch reason=%s nvrDeviceId=%s nvrChannelNum=%s", reason, nvr_device_id, channel_num)

    @router.post("/api/stream/open", summary="创建实时流播放会话", description="创建或复用实时流播放会话，返回 sessionId、viewerId、publicPlayUrl 与 lanPlayUrl。publicPlayUrl/lanPlayUrl 均为观看连接级播放地址，格式为 `/api/stream/play/session/{sessionId}?viewerId={viewerId}`；调用方应直接使用返回的播放地址，不要自行拼接设备和通道。sessionId 表示同一设备同一通道的实时流会话，viewerId 表示本次用户/页面观看连接。关闭单个用户播放时，向 `/api/stream/close` 同时传 sessionId 和 viewerId。接口不再基于请求来源 IP 自动切换内外网地址，调用方应按自身场景选择可访问地址。需要先启用实时流代理功能。该接口仅用于实时流播放，不用于历史视频回放。", response_model=OpenStreamResponse)
    async def api_stream_open(req: OpenStreamRequest, request: Request) -> OpenStreamResponse:
        client_ip = _get_client_ip(request)
        request_time = time.strftime("%Y-%m-%d %H:%M:%S")
        operator_user_id = _trace_text(req.operatorUserId)
        operator_user_name = _trace_text(req.operatorUserName)
        operator_account = _trace_text(req.operatorAccount)
        if not (operator_user_id and operator_user_name and operator_account):
            try:
                raw_body = await request.json()
            except Exception:
                raw_body = {}
            operator_user_id = operator_user_id or _find_body_text(raw_body, ("operatorUserId", "operator_user_id", "operatorUserID", "userId", "user_id"))
            operator_user_name = operator_user_name or _find_body_text(raw_body, ("operatorUserName", "operator_user_name", "operatorName", "userName", "user_name", "name"))
            operator_account = operator_account or _find_body_text(raw_body, ("operatorAccount", "operator_account", "account", "operatorLoginAccount", "loginAccount", "username"))
        _log.info("OPEN request clientIp=%s requestTime=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s nvrDeviceId=%s nvrChannelNum=%s", client_ip, request_time, operator_user_id, operator_user_name, operator_account, req.nvrDeviceId, req.nvrChannelNum)
        if not _stream_enabled():
            _log.warning("OPEN rejected=proxy_disabled client_ip=%s nvrDeviceId=%s nvrChannelNum=%s", client_ip, req.nvrDeviceId, req.nvrChannelNum)
            return JSONResponse({"success": False, "message": "实时流代理未启用"}, status_code=400)
        ok, message = _validate_campus(req.campusCode)
        if not ok:
            _log.warning("OPEN rejected=campus_invalid client_ip=%s campusCode=%s nvrDeviceId=%s nvrChannelNum=%s", client_ip, str(req.campusCode or "").strip(), req.nvrDeviceId, req.nvrChannelNum)
            return JSONResponse({"success": False, "message": message}, status_code=403)
        _log.debug("OPEN processing clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s campusCode=%s provider=%s nvrDeviceId=%s nvrChannelNum=%s nvrChannelId=%s nvrIp=%s nvrPort=%s profile=%s protocol=%s reuse=%s", client_ip, operator_user_id, operator_user_name, operator_account, str(req.campusCode or "").strip(), str(req.provider or "").strip(), req.nvrDeviceId, req.nvrChannelNum, str(req.nvrChannelId or "").strip(), str(req.ipAddress or "").strip(), int(req.port or 554), str(req.streamProfile or "main").strip(), str(req.outputProtocol or "mpegts").strip(), bool(req.reuseIfExists))
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
        viewer_id = uuid.uuid4().hex
        viewer_key = (session.session_id, viewer_id)
        async with viewer_controls_lock:
            viewer_meta[viewer_key] = {
                "operatorUserId": operator_user_id,
                "operatorUserName": operator_user_name,
                "operatorAccount": operator_account,
                "openClientIp": client_ip,
                "createdAt": str(time.time()),
            }
        public_play_url = _build_public_play_url(request, session.session_id, viewer_id)
        lan_play_url = _build_lan_play_url(request, session.session_id, viewer_id)
        _log.info("OPEN success clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s sessionId=%s viewerId=%s nvrDeviceId=%s nvrChannelNum=%s requestedProfile=%s actualProfile=%s reused=%s", client_ip, operator_user_id, operator_user_name, operator_account, session.session_id, viewer_id, session.nvr_device_id, session.nvr_channel_num, str(req.streamProfile or "main").strip(), session.stream_profile, reused)
        return JSONResponse(
            {
                "success": True,
                "message": "ok",
                "campusCode": session.campus_code,
                "nvrDeviceId": session.nvr_device_id,
                "nvrChannelNum": session.nvr_channel_num,
                "nvrChannelId": session.nvr_channel_id,
                "sessionId": session.session_id,
                "viewerId": viewer_id,
                "publicPlayUrl": public_play_url,
                "lanPlayUrl": lan_play_url,
                "outputProtocol": session.output_protocol,
                "resolvedChannel": session.resolved_channel,
                "sourceType": session.source_type,
                "reused": reused,
            }
        )

    @router.get("/api/stream/play/session/{session_id}", summary="播放实时流", description="根据 open 接口返回的播放地址播放实时流。路径参数 `session_id` 是 open 返回的 sessionId；query 参数 `viewerId` 是 open 返回的 viewerId，用于标识本次用户/页面观看连接。调用方应直接使用 open 返回的 lanPlayUrl 或 publicPlayUrl，不要自行拼接设备和通道播放地址。返回 Content-Type 为 `video/mp2t` 的 MPEG-TS 流式响应；这是持续流接口，在 Swagger/浏览器 Network 中长时间 loading 属于正常现象。")
    async def api_stream_play(
        session_id: str,
        request: Request,
        viewerId: str | None = Query(
            default=None,
            description="观看连接ID。必须使用 `/api/stream/open` 返回的 `viewerId`，或直接访问 open 返回的 lanPlayUrl/publicPlayUrl。用于后续 `/api/stream/close` 精准关闭当前用户/页面的播放连接。",
        ),
    ):
        client_ip = _get_client_ip(request)
        viewer_id = str(viewerId or request.query_params.get("viewer_id") or "").strip()
        viewer_id_source = "query"
        if not viewer_id:
            viewer_id = uuid.uuid4().hex
            viewer_id_source = "generated_missing_query"
        viewer_key_for_meta = (str(session_id or "").strip(), viewer_id)
        async with viewer_controls_lock:
            meta = dict(viewer_meta.get(viewer_key_for_meta) or {})
        operator_user_id = _meta_value(meta, "operatorUserId")
        operator_user_name = _meta_value(meta, "operatorUserName")
        operator_account = _meta_value(meta, "operatorAccount")
        session = session_manager.get_session(session_id)
        if session is None:
            _log.warning("PLAY rejected=session_not_found clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s sessionId=%s viewerId=%s viewerIdSource=%s", client_ip, operator_user_id, operator_user_name, operator_account, str(session_id or "").strip(), viewer_id, viewer_id_source)
            return JSONResponse({"success": False, "message": "流会话不存在，请先调用open接口"}, status_code=404)
        nvr_device_id = int(session.nvr_device_id)
        channel_num = int(session.nvr_channel_num)
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
        _log.debug("PLAY ffmpegBin=%s attempts=%s", _ffmpeg_bin(), len(attempt_plans))
        viewer_key = (session.session_id, viewer_id)
        async with viewer_controls_lock:
            old_control = viewer_controls.get(viewer_key)
            if old_control is not None:
                old_control["stop"].set()
                old_proc = old_control.get("proc")
                if old_proc is not None and getattr(old_proc, "returncode", None) is None:
                    with contextlib.suppress(Exception):
                        old_proc.terminate()
            stop_event = asyncio.Event()
            viewer_controls[viewer_key] = {
                "stop": stop_event,
                "proc": None,
                "client_ip": client_ip,
                "operatorUserId": operator_user_id,
                "operatorUserName": operator_user_name,
                "operatorAccount": operator_account,
                "created_at": time.time(),
            }
        _log.info("PLAY request clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s sessionId=%s viewerId=%s viewerIdSource=%s nvrDeviceId=%s nvrChannelNum=%s", client_ip, operator_user_id, operator_user_name, operator_account, session.session_id, viewer_id, viewer_id_source, nvr_device_id, channel_num)

        async def _iter_stream():
            recovery_attempt = 0
            try:
                while not stop_event.is_set():
                    stream_started = False
                    for attempt_index, (attempt_name, cmd) in enumerate(attempt_plans, start=1):
                        if stop_event.is_set():
                            return
                        stderr_lines: list[str] = []
                        noisy_stderr_count = 0
                        first_chunk_at: float | None = None
                        attempt_started_at = time.time()
                        last_touch_at = attempt_started_at
                        _log.debug("PLAY ffmpeg_start clientIp=%s sessionId=%s viewerId=%s recoveryAttempt=%s/%s attempt=%s mode=%s", client_ip, session.session_id, viewer_id, recovery_attempt, max_recovery_attempts, attempt_index, attempt_name)
                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        async with viewer_controls_lock:
                            control = viewer_controls.get(viewer_key)
                            if control is not None:
                                control["proc"] = proc

                        async def _read_stderr() -> None:
                            nonlocal noisy_stderr_count
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
                                        _log.warning("stream ffmpeg stderr: sessionId=%s viewerId=%s recoveryAttempt=%s attempt=%s mode=%s %s", session.session_id, viewer_id, recovery_attempt, attempt_index, attempt_name, text)
                            except Exception:
                                _log.exception("stream stderr reader failed: session_id=%s viewer_id=%s recovery_attempt=%s attempt=%s mode=%s", session.session_id, viewer_id, recovery_attempt, attempt_index, attempt_name)

                        stderr_task = asyncio.create_task(_read_stderr())
                        try:
                            if proc.stdout is None:
                                continue
                            try:
                                first_chunk = await asyncio.wait_for(proc.stdout.read(64 * 1024), timeout=float(first_chunk_timeout_sec))
                            except asyncio.TimeoutError:
                                _log.warning("stream first chunk timeout: session_id=%s viewer_id=%s recovery_attempt=%s attempt=%s mode=%s timeout_sec=%s", session.session_id, viewer_id, recovery_attempt, attempt_index, attempt_name, first_chunk_timeout_sec)
                                first_chunk = b""
                            if stop_event.is_set():
                                return
                            if not first_chunk:
                                _log.warning("stream empty first chunk: session_id=%s viewer_id=%s recovery_attempt=%s attempt=%s mode=%s returncode=%s", session.session_id, viewer_id, recovery_attempt, attempt_index, attempt_name, proc.returncode)
                                continue
                            stream_started = True
                            first_chunk_at = time.time()
                            last_touch_at = first_chunk_at
                            _touch_stream_activity(nvr_device_id, channel_num, "first_chunk")
                            _log.info("PLAY started clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s sessionId=%s viewerId=%s nvrDeviceId=%s nvrChannelNum=%s latencySec=%.3f", client_ip, operator_user_id, operator_user_name, operator_account, session.session_id, viewer_id, nvr_device_id, channel_num, first_chunk_at - attempt_started_at)
                            yield first_chunk
                            while not stop_event.is_set():
                                try:
                                    chunk = await asyncio.wait_for(proc.stdout.read(64 * 1024), timeout=1.0)
                                except asyncio.TimeoutError:
                                    continue
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
                            async with viewer_controls_lock:
                                control = viewer_controls.get(viewer_key)
                                if control is not None and control.get("proc") is proc:
                                    control["proc"] = None
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(stderr_task, timeout=1.0)
                            _log.debug("stream play exit: session_id=%s viewer_id=%s recovery_attempt=%s attempt=%s mode=%s returncode=%s stopped=%s", session.session_id, viewer_id, recovery_attempt, attempt_index, attempt_name, proc.returncode, stop_event.is_set())
                            if stderr_lines:
                                _log.debug("stream ffmpeg stderr summary: session_id=%s viewer_id=%s recovery_attempt=%s attempt=%s mode=%s %s", session.session_id, viewer_id, recovery_attempt, attempt_index, attempt_name, "\n".join(stderr_lines))
                            elif noisy_stderr_count > 0:
                                _log.debug("stream ffmpeg noisy stderr suppressed: sessionId=%s viewerId=%s recoveryAttempt=%s attempt=%s mode=%s count=%s", session.session_id, viewer_id, recovery_attempt, attempt_index, attempt_name, noisy_stderr_count)
                        if stream_started:
                            break
                        if attempt_index < len(attempt_plans):
                            await asyncio.sleep(max(0.0, float(retry_delay_ms) / 1000.0))
                    if stop_event.is_set():
                        return
                    if not stream_started:
                        _log.error("stream play failed after retries: session_id=%s viewer_id=%s nvr_device_id=%s channel_num=%s attempts=%s recovery_attempt=%s", session.session_id, viewer_id, nvr_device_id, channel_num, len(attempt_plans), recovery_attempt)
                        return
                    if recovery_attempt >= max_recovery_attempts:
                        _log.error("stream recovery exhausted: session_id=%s viewer_id=%s nvr_device_id=%s channel_num=%s recovery_attempt=%s", session.session_id, viewer_id, nvr_device_id, channel_num, recovery_attempt)
                        return
                    recovery_attempt += 1
                    _touch_stream_activity(nvr_device_id, channel_num, "recovery")
                    _log.warning("stream interrupted, restarting: session_id=%s viewer_id=%s nvr_device_id=%s channel_num=%s recovery_attempt=%s/%s delay_ms=%s", session.session_id, viewer_id, nvr_device_id, channel_num, recovery_attempt, max_recovery_attempts, recovery_retry_delay_ms)
                    await asyncio.sleep(max(0.0, float(recovery_retry_delay_ms) / 1000.0))
            finally:
                async with viewer_controls_lock:
                    control = viewer_controls.get(viewer_key)
                    if control is not None and control.get("stop") is stop_event:
                        viewer_controls.pop(viewer_key, None)
                _log.info("PLAY ended clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s sessionId=%s viewerId=%s nvrDeviceId=%s nvrChannelNum=%s", client_ip, operator_user_id, operator_user_name, operator_account, session.session_id, viewer_id, nvr_device_id, channel_num)

        return StreamingResponse(
            _iter_stream(),
            media_type="video/mp2t",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @router.post("/api/stream/close", summary="关闭实时流播放", description="关闭实时流播放连接。推荐传 `sessionId + viewerId`，只关闭本次用户/页面的观看连接，不影响同一 sessionId 下其他用户观看；关闭操作是幂等的，如果该 viewerId 已经因为浏览器断开、弹窗关闭或播放流结束而不活跃，接口仍返回成功并标记 alreadyEnded=true；如果只传 `sessionId` 且 `viewerId` 为空，则关闭整个实时流会话，并停止该 sessionId 下当前已登记的所有观看连接。")
    async def api_stream_close(req: CloseStreamRequest, request: Request):
        client_ip = _get_client_ip(request)
        session_id = str(req.sessionId or "").strip()
        viewer_id = str(req.viewerId or "").strip()
        meta_key = (session_id, viewer_id) if session_id and viewer_id else ("", "")
        async with viewer_controls_lock:
            meta = dict(viewer_meta.get(meta_key) or {})
        operator_user_id = _meta_value(meta, "operatorUserId")
        operator_user_name = _meta_value(meta, "operatorUserName")
        operator_account = _meta_value(meta, "operatorAccount")
        if not session_id:
            return JSONResponse({"success": False, "message": "sessionId 不能为空"}, status_code=400)
        _log.info("CLOSE request clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s sessionId=%s viewerId=%s", client_ip, operator_user_id, operator_user_name, operator_account, session_id, viewer_id)
        if viewer_id:
            viewer_key = (session_id, viewer_id)
            async with viewer_controls_lock:
                control = viewer_controls.get(viewer_key)
                if control is None:
                    _log.info("CLOSE viewer_already_ended clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s sessionId=%s viewerId=%s", client_ip, operator_user_id, operator_user_name, operator_account, session_id, viewer_id)
                    viewer_meta.pop(viewer_key, None)
                    return JSONResponse({"success": True, "message": "播放连接已结束", "closedScope": "viewer", "sessionId": session_id, "viewerId": viewer_id, "alreadyEnded": True})
                control["stop"].set()
                proc = control.get("proc")
                if proc is not None and getattr(proc, "returncode", None) is None:
                    with contextlib.suppress(Exception):
                        proc.terminate()
                viewer_meta.pop(viewer_key, None)
            _log.info("CLOSE viewer_success clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s sessionId=%s viewerId=%s", client_ip, operator_user_id, operator_user_name, operator_account, session_id, viewer_id)
            return JSONResponse({"success": True, "message": "ok", "closedScope": "viewer", "sessionId": session_id, "viewerId": viewer_id, "alreadyEnded": False})
        async with viewer_controls_lock:
            for (sid, _viewer_id), control in list(viewer_controls.items()):
                if sid != session_id:
                    continue
                control["stop"].set()
                proc = control.get("proc")
                if proc is not None and getattr(proc, "returncode", None) is None:
                    with contextlib.suppress(Exception):
                        proc.terminate()
            for key in list(viewer_meta.keys()):
                if key[0] == session_id:
                    viewer_meta.pop(key, None)
        closed = session_manager.close_session(session_id)
        if not closed:
            _log.warning("CLOSE session_not_found clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s sessionId=%s", client_ip, operator_user_id, operator_user_name, operator_account, session_id)
            return JSONResponse({"success": False, "message": "流会话不存在或已关闭"}, status_code=404)
        _log.info("CLOSE session_success clientIp=%s operatorUserId=%s operatorUserName=%s operatorAccount=%s sessionId=%s", client_ip, operator_user_id, operator_user_name, operator_account, session_id)
        return JSONResponse({"success": True, "message": "ok", "closedScope": "session", "sessionId": session_id, "viewerId": ""})

    @router.get("/api/stream/status", summary="查看实时流代理状态", description="获取实时流代理的运行状态，包括启用状态、会话TTL、活跃会话列表和公网配置。", response_model=StreamStatusResponse)
    async def api_stream_status() -> StreamStatusResponse:
        status = session_manager.get_status()
        return JSONResponse({"success": True, **status})

    return router
