from __future__ import annotations

import ipaddress
import logging
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import urlsplit

from .db import bjt_now_iso
from .utils import get_local_ip as _get_local_ip
from .video.ffmpeg import ffmpeg_bin as _ffmpeg_bin, ffmpeg_exists
from .video.nvr import build_rtsp_url, normalize_provider

_log = logging.getLogger("edge.stream_proxy")


def _probe_rtsp_url(rtsp_url: str) -> None:
    cmd = [
        "ffprobe",
        "-rtsp_transport",
        "tcp",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(rtsp_url),
    ]
    try:
        probe = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=12,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"实时流探测超时: {rtsp_url}") from exc
    except Exception as exc:
        raise RuntimeError(f"实时流探测失败: {exc}") from exc
    stdout = str(probe.stdout or "").strip()
    stderr = str(probe.stderr or "").strip()
    if probe.returncode == 0 and stdout:
        return
    detail = stderr or stdout or f"exit={probe.returncode}"
    raise RuntimeError(f"实时流不可播放: {detail}")


def _probe_mpegts_first_chunk(rtsp_url: str, *, timeout_sec: float = 12.0) -> None:
    cmd = [
        _ffmpeg_bin(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-rtsp_flags",
        "prefer_tcp",
        "-timeout",
        str(int(max(1.0, float(timeout_sec)) * 1000000)),
        "-analyzeduration",
        "20000000",
        "-probesize",
        "20000000",
        "-use_wallclock_as_timestamps",
        "1",
        "-fflags",
        "+discardcorrupt",
        "-i",
        str(rtsp_url),
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
        "-frames:v",
        "1",
        "-f",
        "mpegts",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=max(3.0, float(timeout_sec) + 3.0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"实时流首包探测超时: {rtsp_url}") from exc
    except Exception as exc:
        raise RuntimeError(f"实时流首包探测失败: {exc}") from exc
    stdout = bytes(proc.stdout or b"")
    stderr = bytes(proc.stderr or b"").decode("utf-8", errors="ignore").strip()
    if proc.returncode == 0 and stdout:
        return
    detail = stderr or f"exit={proc.returncode}, bytes={len(stdout)}"
    raise RuntimeError(f"实时流无法输出首包: {detail}")


def _rtsp_probe_port_candidates() -> list[int]:
    return [554, 18080]


def _probe_rtsp_url_with_fallback(
    username: str,
    password: str,
    ip: str,
    channel: int,
    *,
    main_stream: bool,
    provider: str,
) -> tuple[str, int]:
    last_error: Exception | None = None
    attempt_seq = 0
    for rtsp_port in _rtsp_probe_port_candidates():
        for attempt in range(1, 4):
            attempt_seq += 1
            rtsp_url = build_rtsp_url(
                username,
                password,
                ip,
                rtsp_port,
                channel,
                main_stream=main_stream,
                provider=provider,
            )
            _log.info("stream rtsp probe try: port=%s attempt=%s/3 total_attempt=%s url=%s", rtsp_port, attempt, attempt_seq, rtsp_url)
            try:
                _probe_rtsp_url(rtsp_url)
                return rtsp_url, rtsp_port
            except Exception as exc:
                last_error = exc
    if last_error is not None:
        raise RuntimeError(str(last_error))
    raise RuntimeError("实时流探测失败")


def _resolve_playable_rtsp_url(
    username: str,
    password: str,
    ip: str,
    channel: int,
    *,
    provider: str,
    stream_profile: str,
) -> tuple[str, int, str]:
    profile_order = [str(stream_profile or "main").strip().lower() or "main"]
    if profile_order[0] == "main":
        profile_order.append("sub")
    last_error: Exception | None = None
    for profile in profile_order:
        try:
            rtsp_url, resolved_rtsp_port = _probe_rtsp_url_with_fallback(
                username,
                password,
                ip,
                channel,
                main_stream=(profile != "sub"),
                provider=provider,
            )
            _probe_mpegts_first_chunk(rtsp_url)
            if profile != str(stream_profile or "main").strip().lower():
                _log.warning("stream profile fallback: requested=%s actual=%s channel=%s rtsp_url=%s", stream_profile, profile, channel, rtsp_url)
            return rtsp_url, resolved_rtsp_port, profile
        except Exception as exc:
            last_error = exc
            _log.warning("stream playable probe failed: profile=%s channel=%s error=%s", profile, channel, exc)
    if last_error is not None:
        raise RuntimeError(str(last_error))
    raise RuntimeError("实时流探测失败")


def _verify_cached_playable_rtsp_url(
    username: str,
    password: str,
    ip: str,
    channel: int,
    *,
    provider: str,
    rtsp_port: int,
    actual_stream_profile: str,
) -> str:
    rtsp_url = build_rtsp_url(
        username,
        password,
        ip,
        int(rtsp_port),
        channel,
        main_stream=(str(actual_stream_profile or "main").strip().lower() != "sub"),
        provider=provider,
    )
    _log.info("stream rtsp cache verify: provider=%s channel=%s rtsp_port=%s profile=%s url=%s", provider, channel, rtsp_port, actual_stream_profile, rtsp_url)
    _probe_mpegts_first_chunk(rtsp_url, timeout_sec=8.0)
    return rtsp_url


@dataclass
class StreamSession:
    session_id: str
    session_key: str
    campus_code: str
    nvr_device_id: int
    nvr_channel_num: int
    nvr_channel_id: str
    resolved_channel: int
    stream_profile: str
    output_protocol: str
    source_type: str
    rtsp_url: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StreamSessionManager:
    def __init__(self, *, db, load_monitor_cfg, session_ttl_sec: int = 900) -> None:
        self._db = db
        self._load_monitor_cfg = load_monitor_cfg
        self._session_ttl_sec = max(60, int(session_ttl_sec or 900))
        self._lock = threading.Lock()
        self._sessions: dict[str, StreamSession] = {}
        self._session_keys: dict[str, str] = {}
        self._device_channel_index: dict[tuple[int, int], str] = {}

    def _cleanup_expired_locked(self, now_ts: float | None = None) -> None:
        now_value = float(now_ts or time.time())
        expired_ids = [
            sid
            for sid, sess in self._sessions.items()
            if now_value - float(sess.updated_at or 0.0) > self._session_ttl_sec
        ]
        for sid in expired_ids:
            sess = self._sessions.pop(sid, None)
            if sess is None:
                continue
            if self._session_keys.get(sess.session_key) == sid:
                self._session_keys.pop(sess.session_key, None)
            dc_key = (sess.nvr_device_id, sess.nvr_channel_num)
            if self._device_channel_index.get(dc_key) == sid:
                self._device_channel_index.pop(dc_key, None)

    def _normalize_output_protocol(self, value: str) -> str:
        text = str(value or "mpegts").strip().lower()
        if text in {"", "mpegts", "ts", "mpeg-ts"}:
            return "mpegts"
        raise ValueError("当前仅支持 mpegts 输出协议")

    def _normalize_stream_profile(self, value: str) -> str:
        text = str(value or "main").strip().lower()
        if text in {"", "main", "sub"}:
            return text or "main"
        raise ValueError("streamProfile 仅支持 main 或 sub")

    def _to_positive_int(self, value: Any, default: int = 0) -> int:
        try:
            result = int(str(value or "").strip())
        except Exception:
            return default
        return result if result > 0 else default

    def _load_rtsp_probe_cache(
        self,
        *,
        nvr_device_id: int,
        channel_num: int,
        provider: str,
        ip_address: str,
        account: str,
        requested_profile: str,
    ) -> dict[str, Any] | None:
        if self._db is None or nvr_device_id <= 0 or channel_num <= 0:
            return None
        try:
            row = self._db.fetch_one(
                "SELECT * FROM edge_nvr_rtsp_probe_cache WHERE nvr_device_id=? AND channel_num=? AND provider=? AND ip_address=? AND account=? AND requested_profile=? LIMIT 1",
                (
                    int(nvr_device_id),
                    int(channel_num),
                    str(provider or "").strip(),
                    str(ip_address or "").strip(),
                    str(account or "").strip(),
                    str(requested_profile or "main").strip().lower() or "main",
                ),
            )
            return dict(row) if row else None
        except Exception as e:
            _log.warning("stream rtsp cache load failed: nvr_device_id=%s channel_num=%s error=%s", nvr_device_id, channel_num, e)
            return None

    def _save_rtsp_probe_cache(
        self,
        *,
        nvr_device_id: int,
        channel_num: int,
        provider: str,
        ip_address: str,
        account: str,
        requested_profile: str,
        actual_profile: str,
        rtsp_port: int,
        rtsp_url: str,
    ) -> None:
        if self._db is None or nvr_device_id <= 0 or channel_num <= 0 or rtsp_port <= 0 or not str(rtsp_url or "").strip():
            return
        now = bjt_now_iso()
        try:
            with self._db.connect() as conn:
                conn.execute(
                    """
INSERT INTO edge_nvr_rtsp_probe_cache(
  nvr_device_id, channel_num, provider, ip_address, account,
  requested_profile, actual_profile, rtsp_port, rtsp_url,
  verified_at, success_count, created_time, updated_time
) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)
ON CONFLICT(nvr_device_id, channel_num, provider, ip_address, account, requested_profile) DO UPDATE SET
  actual_profile=excluded.actual_profile,
  rtsp_port=excluded.rtsp_port,
  rtsp_url=excluded.rtsp_url,
  verified_at=excluded.verified_at,
  success_count=edge_nvr_rtsp_probe_cache.success_count + 1,
  updated_time=excluded.updated_time
                    """.strip(),
                    (
                        int(nvr_device_id),
                        int(channel_num),
                        str(provider or "").strip(),
                        str(ip_address or "").strip(),
                        str(account or "").strip(),
                        str(requested_profile or "main").strip().lower() or "main",
                        str(actual_profile or "main").strip().lower() or "main",
                        int(rtsp_port),
                        str(rtsp_url or "").strip(),
                        now,
                        now,
                        now,
                    ),
                )
                conn.commit()
            _log.info("stream rtsp cache saved: nvr_device_id=%s channel_num=%s provider=%s requested_profile=%s actual_profile=%s rtsp_port=%s", nvr_device_id, channel_num, provider, requested_profile, actual_profile, rtsp_port)
        except Exception as e:
            _log.warning("stream rtsp cache save failed: nvr_device_id=%s channel_num=%s error=%s", nvr_device_id, channel_num, e)

    def _extract_candidate_channel(self, candidate_channels: list[Any] | None) -> int:
        for item in candidate_channels or []:
            if isinstance(item, dict):
                for key in ("sdkChannel", "sdk_channel", "channel", "channelNum", "channelId", "id"):
                    value = self._to_positive_int(item.get(key), 0)
                    if value > 0:
                        return value
                continue
            value = self._to_positive_int(item, 0)
            if value > 0:
                return value
        return 0

    def resolve_channel(
        self,
        *,
        nvr_device_id: int,
        nvr_channel_num: int,
        nvr_channel_id: str,
        candidate_channels: list[Any] | None,
    ) -> int:
        candidate = self._extract_candidate_channel(candidate_channels)
        if candidate > 0:
            _log.info("stream channel resolve by candidate: nvr_device_id=%s web_channel_num=%s sdk_channel=%s", nvr_device_id, nvr_channel_num, candidate)
            return candidate
        if nvr_channel_num > 0:
            _log.info("stream channel resolve by default web_channel_num: nvr_device_id=%s web_channel_num=%s", nvr_device_id, nvr_channel_num)
            return nvr_channel_num
        channel_from_id = self._to_positive_int(nvr_channel_id, 0)
        if channel_from_id > 0:
            _log.info("stream channel resolve by channel_id: nvr_device_id=%s nvr_channel_id=%s sdk_channel=%s", nvr_device_id, nvr_channel_id, channel_from_id)
            return channel_from_id
        raise ValueError("无法解析 NVR 通道，请检查 nvrChannelNum / nvrChannelId / candidateChannels")

    def create_or_get_session(
        self,
        *,
        campus_code: str,
        nvr_device_id: int,
        nvr_channel_num: int,
        nvr_channel_id: str,
        ip_address: str,
        port: int,
        account: str,
        password: str,
        provider: str | None,
        candidate_channels: list[Any] | None,
        stream_profile: str,
        output_protocol: str,
        reuse_if_exists: bool,
    ) -> tuple[StreamSession, bool]:
        if not ffmpeg_exists():
            raise RuntimeError("未检测到 ffmpeg，无法启用实时流代理")
        if not str(ip_address or "").strip():
            raise ValueError("ipAddress 不能为空")
        normalized_protocol = self._normalize_output_protocol(output_protocol)
        normalized_profile = self._normalize_stream_profile(stream_profile)
        normalized_provider = normalize_provider(provider)
        normalized_account = str(account or "").strip()
        normalized_password = str(password or "").strip()
        normalized_ip_address = str(ip_address or "").strip()
        normalized_ip_lower = normalized_ip_address.lower()
        if normalized_ip_lower in {"string", "null", "none", "undefined"}:
            raise ValueError("ipAddress 不能使用占位值，请填写真实设备 IP 或可解析主机名")
        try:
            ipaddress.ip_address(normalized_ip_address)
        except Exception:
            if not normalized_ip_address or any(ch.isspace() for ch in normalized_ip_address):
                raise ValueError("ipAddress 格式不合法，请填写真实设备 IP 或可解析主机名")
        resolved_channel = self.resolve_channel(
            nvr_device_id=nvr_device_id,
            nvr_channel_num=nvr_channel_num,
            nvr_channel_id=nvr_channel_id,
            candidate_channels=candidate_channels,
        )
        # RTSP 实时流使用业务通道号，而非 SDK 缓存通道号
        # SDK 通道号（resolved_channel）仅用于 SDK 下载，RTSP URL 规则不同
        rtsp_channel = int(nvr_channel_num) if int(nvr_channel_num) > 0 else int(resolved_channel)
        cached_probe = self._load_rtsp_probe_cache(
            nvr_device_id=nvr_device_id,
            channel_num=nvr_channel_num,
            provider=normalized_provider,
            ip_address=normalized_ip_address,
            account=normalized_account,
            requested_profile=normalized_profile,
        )
        rtsp_url: str
        resolved_rtsp_port: int
        actual_stream_profile: str
        if cached_probe is not None:
            try:
                resolved_rtsp_port = self._to_positive_int(cached_probe.get("rtsp_port"), 0)
                actual_stream_profile = self._normalize_stream_profile(cached_probe.get("actual_profile"))
                if resolved_rtsp_port <= 0:
                    raise ValueError("invalid cached rtsp_port")
                rtsp_url = _verify_cached_playable_rtsp_url(
                    normalized_account,
                    normalized_password,
                    normalized_ip_address,
                    rtsp_channel,
                    provider=normalized_provider,
                    rtsp_port=resolved_rtsp_port,
                    actual_stream_profile=actual_stream_profile,
                )
                _log.info("stream rtsp cache hit: nvr_device_id=%s nvr_channel_num=%s resolved_channel=%s rtsp_channel=%s rtsp_port=%s requested_profile=%s actual_profile=%s", nvr_device_id, nvr_channel_num, resolved_channel, rtsp_channel, resolved_rtsp_port, normalized_profile, actual_stream_profile)
            except Exception as exc:
                _log.warning("stream rtsp cache verify failed: nvr_device_id=%s nvr_channel_num=%s resolved_channel=%s rtsp_channel=%s requested_profile=%s error=%s", nvr_device_id, nvr_channel_num, resolved_channel, rtsp_channel, normalized_profile, exc)
                rtsp_url, resolved_rtsp_port, actual_stream_profile = _resolve_playable_rtsp_url(
                    normalized_account,
                    normalized_password,
                    normalized_ip_address,
                    rtsp_channel,
                    provider=normalized_provider,
                    stream_profile=normalized_profile,
                )
        else:
            rtsp_url, resolved_rtsp_port, actual_stream_profile = _resolve_playable_rtsp_url(
                normalized_account,
                normalized_password,
                normalized_ip_address,
                rtsp_channel,
                provider=normalized_provider,
                stream_profile=normalized_profile,
            )
        self._save_rtsp_probe_cache(
            nvr_device_id=nvr_device_id,
            channel_num=nvr_channel_num,
            provider=normalized_provider,
            ip_address=normalized_ip_address,
            account=normalized_account,
            requested_profile=normalized_profile,
            actual_profile=actual_stream_profile,
            rtsp_port=resolved_rtsp_port,
            rtsp_url=rtsp_url,
        )
        _log.info("stream rtsp url built: provider=%s nvr_channel_num=%s resolved_channel=%s rtsp_channel=%s rtsp_port=%s profile=%s url=%s", normalized_provider, nvr_channel_num, resolved_channel, rtsp_channel, resolved_rtsp_port, actual_stream_profile, rtsp_url)
        session_key = "|".join(
            [
                str(campus_code or "").strip(),
                str(nvr_device_id),
                str(nvr_channel_num),
                str(nvr_channel_id or "").strip(),
                normalized_ip_address,
                str(resolved_rtsp_port),
                normalized_provider,
                str(resolved_channel),
                actual_stream_profile,
                normalized_protocol,
            ]
        )
        now_ts = time.time()
        with self._lock:
            self._cleanup_expired_locked(now_ts)
            if reuse_if_exists:
                existing_id = self._session_keys.get(session_key)
                if existing_id:
                    existing = self._sessions.get(existing_id)
                    if existing is not None:
                        existing.updated_at = now_ts
                        _log.info("stream session reused: session_id=%s nvr_device_id=%s nvr_channel_num=%s resolved_channel=%s profile=%s protocol=%s", existing.session_id, existing.nvr_device_id, existing.nvr_channel_num, existing.resolved_channel, existing.stream_profile, existing.output_protocol)
                        return existing, True
        _log.info("stream rtsp probe ok: nvr_device_id=%s nvr_channel_num=%s rtsp_port=%s rtsp_url=%s", nvr_device_id, nvr_channel_num, resolved_rtsp_port, rtsp_url)
        with self._lock:
            self._cleanup_expired_locked(now_ts)
            if reuse_if_exists:
                existing_id = self._session_keys.get(session_key)
                if existing_id:
                    existing = self._sessions.get(existing_id)
                    if existing is not None:
                        existing.updated_at = time.time()
                        _log.info("stream session reused after probe: session_id=%s nvr_device_id=%s nvr_channel_num=%s resolved_channel=%s profile=%s protocol=%s", existing.session_id, existing.nvr_device_id, existing.nvr_channel_num, existing.resolved_channel, existing.stream_profile, existing.output_protocol)
                        return existing, True
            session_id = uuid.uuid4().hex
            session = StreamSession(
                session_id=session_id,
                session_key=session_key,
                campus_code=str(campus_code or "").strip(),
                nvr_device_id=int(nvr_device_id),
                nvr_channel_num=int(nvr_channel_num),
                nvr_channel_id=str(nvr_channel_id or "").strip(),
                resolved_channel=int(resolved_channel),
                stream_profile=actual_stream_profile,
                output_protocol=normalized_protocol,
                source_type=f"{normalized_provider}_rtsp",
                rtsp_url=rtsp_url,
                created_at=now_ts,
                updated_at=now_ts,
            )
            self._sessions[session_id] = session
            self._session_keys[session_key] = session_id
            self._device_channel_index[(int(nvr_device_id), int(nvr_channel_num))] = session_id
            _log.info("stream session created: session_id=%s nvr_device_id=%s nvr_channel_num=%s resolved_channel=%s profile=%s protocol=%s", session.session_id, session.nvr_device_id, session.nvr_channel_num, session.resolved_channel, session.stream_profile, session.output_protocol)
            return session, False

    def get_session(self, session_id: str) -> StreamSession | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        with self._lock:
            self._cleanup_expired_locked()
            session = self._sessions.get(sid)
            if session is None:
                return None
            session.updated_at = time.time()
            return session

    def get_session_by_key(self, nvr_device_id: int, channel_num: int) -> StreamSession | None:
        """根据 nvrDeviceId 和 channelNum 查找会话"""
        with self._lock:
            self._cleanup_expired_locked()
            sid = self._device_channel_index.get((int(nvr_device_id), int(channel_num)))
            if not sid:
                return None
            sess = self._sessions.get(sid)
            if sess is None:
                self._device_channel_index.pop((int(nvr_device_id), int(channel_num)), None)
                return None
            sess.updated_at = time.time()
            return sess

    def close_session(self, session_id: str) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return False
        with self._lock:
            session = self._sessions.pop(sid, None)
            if session is None:
                return False
            if self._session_keys.get(session.session_key) == sid:
                self._session_keys.pop(session.session_key, None)
            dc_key = (session.nvr_device_id, session.nvr_channel_num)
            if self._device_channel_index.get(dc_key) == sid:
                self._device_channel_index.pop(dc_key, None)
            _log.info("stream session closed: session_id=%s nvr_device_id=%s nvr_channel_num=%s resolved_channel=%s", session.session_id, session.nvr_device_id, session.nvr_channel_num, session.resolved_channel)
            return True

    def build_public_access_base_url(self, request_base_url: str) -> str:
        cfg = self._load_monitor_cfg() or {}
        public_base_url = str(cfg.get("publicBaseUrl") or "").strip().rstrip("/")
        public_host = str(cfg.get("publicHost") or "").strip()
        public_port = str(cfg.get("publicPort") or "").strip()
        public_scheme = str(cfg.get("publicScheme") or "").strip().lower()
        if public_base_url:
            _log.debug("stream access base url use publicBaseUrl: %s", public_base_url)
            return public_base_url
        if public_host:
            scheme = public_scheme or (urlsplit(str(request_base_url or "")).scheme or "http")
            host = public_host
            if public_port:
                _log.debug("stream access base url use public host/port: %s://%s:%s", scheme, host, public_port)
                return f"{scheme}://{host}:{public_port}".rstrip("/")
            _log.debug("stream access base url use public host: %s://%s", scheme, host)
            return f"{scheme}://{host}".rstrip("/")
        return self.build_lan_access_base_url(request_base_url)

    def build_lan_access_base_url(self, request_base_url: str) -> str:
        local_ip = _get_local_ip()
        parsed = urlsplit(str(request_base_url or ""))
        host = parsed.hostname or "127.0.0.1"
        if host in ("127.0.0.1", "localhost", "::1"):
            host = local_ip
        scheme = parsed.scheme or "http"
        port = parsed.port
        if port:
            result = f"{scheme}://{host}:{port}"
        else:
            result = f"{scheme}://{host}"
        _log.debug("stream access base url fallback local: %s (original: %s)", result, str(request_base_url or "").strip().rstrip("/"))
        return result

    def save_nvr_device(
        self,
        *,
        nvr_device_id: int,
        campus_code: str | None = None,
        ip_address: str | None = None,
        port: int | None = None,
        account: str | None = None,
        device_model: str | None = None,
        serial_number: str | None = None,
        sdk_start_channel: int | None = None,
        sdk_start_dchan: int | None = None,
        ip_chan_num: int | None = None,
        chan_num: int | None = None,
        online_status: int | None = None,
    ) -> None:
        if self._db is None or int(nvr_device_id or 0) <= 0:
            return
        try:
            now = bjt_now_iso()
            with self._db.connect() as conn:
                conn.execute(
                    """
INSERT INTO edge_nvr_device(
  nvr_device_id, campus_code, ip_address, port, account, device_model, serial_number,
  sdk_start_channel, sdk_start_dchan, ip_chan_num, chan_num, online_status,
  last_online_time, created_time, updated_time
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(nvr_device_id) DO UPDATE SET
  campus_code=COALESCE(excluded.campus_code, edge_nvr_device.campus_code),
  ip_address=COALESCE(excluded.ip_address, edge_nvr_device.ip_address),
  port=COALESCE(excluded.port, edge_nvr_device.port),
  account=COALESCE(excluded.account, edge_nvr_device.account),
  device_model=COALESCE(excluded.device_model, edge_nvr_device.device_model),
  serial_number=COALESCE(excluded.serial_number, edge_nvr_device.serial_number),
  sdk_start_channel=COALESCE(excluded.sdk_start_channel, edge_nvr_device.sdk_start_channel),
  sdk_start_dchan=COALESCE(excluded.sdk_start_dchan, edge_nvr_device.sdk_start_dchan),
  ip_chan_num=COALESCE(excluded.ip_chan_num, edge_nvr_device.ip_chan_num),
  chan_num=COALESCE(excluded.chan_num, edge_nvr_device.chan_num),
  online_status=COALESCE(excluded.online_status, edge_nvr_device.online_status),
  last_online_time=CASE WHEN excluded.online_status=1 THEN excluded.last_online_time ELSE edge_nvr_device.last_online_time END,
  updated_time=excluded.updated_time
                    """.strip(),
                    (
                        int(nvr_device_id),
                        str(campus_code or "").strip() or None,
                        str(ip_address or "").strip() or None,
                        int(port) if port else None,
                        str(account or "").strip() or None,
                        str(device_model or "").strip() or None,
                        str(serial_number or "").strip() or None,
                        int(sdk_start_channel) if sdk_start_channel else None,
                        int(sdk_start_dchan) if sdk_start_dchan else None,
                        int(ip_chan_num) if ip_chan_num else None,
                        int(chan_num) if chan_num else None,
                        int(online_status) if online_status is not None else None,
                        now if online_status == 1 else None,
                        now,
                        now,
                    ),
                )
                conn.commit()
            _log.debug("nvr device saved: nvr_device_id=%s ip=%s model=%s", nvr_device_id, ip_address, device_model)
        except Exception as e:
            _log.warning("nvr device save failed: nvr_device_id=%s error=%s", nvr_device_id, e)

    def save_nvr_channel(
        self,
        *,
        nvr_device_id: int,
        channel_num: int,
        channel_id: str | None = None,
        channel_name: str | None = None,
        sdk_channel: int | None = None,
        rtsp_channel: int | None = None,
        stream_status: int | None = None,
    ) -> None:
        if self._db is None or int(nvr_device_id or 0) <= 0 or int(channel_num or 0) <= 0:
            return
        try:
            now = bjt_now_iso()
            with self._db.connect() as conn:
                conn.execute(
                    """
INSERT INTO edge_nvr_channel(
  nvr_device_id, channel_num, channel_id, channel_name, sdk_channel, rtsp_channel, stream_status, created_time, updated_time
) VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(nvr_device_id, channel_num) DO UPDATE SET
  channel_id=COALESCE(excluded.channel_id, edge_nvr_channel.channel_id),
  channel_name=COALESCE(excluded.channel_name, edge_nvr_channel.channel_name),
  sdk_channel=COALESCE(excluded.sdk_channel, edge_nvr_channel.sdk_channel),
  rtsp_channel=COALESCE(excluded.rtsp_channel, edge_nvr_channel.rtsp_channel),
  stream_status=COALESCE(excluded.stream_status, edge_nvr_channel.stream_status),
  updated_time=excluded.updated_time
                    """.strip(),
                    (
                        int(nvr_device_id),
                        int(channel_num),
                        str(channel_id or "").strip() or None,
                        str(channel_name or "").strip() or None,
                        int(sdk_channel) if sdk_channel else None,
                        int(rtsp_channel) if rtsp_channel else None,
                        int(stream_status) if stream_status is not None else None,
                        now,
                        now,
                    ),
                )
                conn.commit()
            _log.debug("nvr channel saved: nvr_device_id=%s channel_num=%s sdk_channel=%s", nvr_device_id, channel_num, sdk_channel)
        except Exception as e:
            _log.warning("nvr channel save failed: nvr_device_id=%s channel_num=%s error=%s", nvr_device_id, channel_num, e)

    def get_nvr_device(self, nvr_device_id: int) -> dict[str, Any] | None:
        if self._db is None or int(nvr_device_id or 0) <= 0:
            return None
        try:
            row = self._db.fetch_one(
                "SELECT * FROM edge_nvr_device WHERE nvr_device_id=? LIMIT 1",
                (int(nvr_device_id),),
            )
            return dict(row) if row else None
        except Exception:
            return None

    def get_nvr_channel(self, nvr_device_id: int, channel_num: int) -> dict[str, Any] | None:
        if self._db is None or int(nvr_device_id or 0) <= 0 or int(channel_num or 0) <= 0:
            return None
        try:
            row = self._db.fetch_one(
                "SELECT * FROM edge_nvr_channel WHERE nvr_device_id=? AND channel_num=? LIMIT 1",
                (int(nvr_device_id), int(channel_num)),
            )
            return dict(row) if row else None
        except Exception:
            return None

    def activate_channel(self, nvr_device_id: int, channel_num: int) -> bool:
        """激活通道，更新 activated_at 时间戳"""
        if self._db is None or int(nvr_device_id or 0) <= 0 or int(channel_num or 0) <= 0:
            return False
        try:
            now_ts = time.time()
            with self._db.connect() as conn:
                cursor = conn.execute(
                    "UPDATE edge_nvr_channel SET activated_at=? WHERE nvr_device_id=? AND channel_num=?",
                    (now_ts, int(nvr_device_id), int(channel_num)),
                )
                conn.commit()
                if cursor.rowcount > 0:
                    _log.debug("channel activated: nvr_device_id=%s channel_num=%s activated_at=%s", nvr_device_id, channel_num, now_ts)
                    return True
            return False
        except Exception as e:
            _log.warning("channel activate failed: nvr_device_id=%s channel_num=%s error=%s", nvr_device_id, channel_num, e)
            return False

    def is_channel_active(self, nvr_device_id: int, channel_num: int, ttl_sec: int = 300) -> bool:
        """检查通道是否在激活有效期内"""
        if self._db is None or int(nvr_device_id or 0) <= 0 or int(channel_num or 0) <= 0:
            return False
        try:
            row = self._db.fetch_one(
                "SELECT activated_at FROM edge_nvr_channel WHERE nvr_device_id=? AND channel_num=? LIMIT 1",
                (int(nvr_device_id), int(channel_num)),
            )
            if not row:
                return False
            activated_at = row.get("activated_at") if isinstance(row, dict) else row[0]
            if activated_at is None:
                return False
            now_ts = time.time()
            return (now_ts - float(activated_at)) < ttl_sec
        except Exception:
            return False

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            self._cleanup_expired_locked()
            now_ts = time.time()
            active_sessions = []
            for sess in self._sessions.values():
                age_sec = now_ts - float(sess.created_at or 0.0)
                idle_sec = now_ts - float(sess.updated_at or 0.0)
                active_sessions.append({
                    "sessionId": sess.session_id,
                    "campusCode": sess.campus_code,
                    "nvrDeviceId": sess.nvr_device_id,
                    "nvrChannelNum": sess.nvr_channel_num,
                    "resolvedChannel": sess.resolved_channel,
                    "streamProfile": sess.stream_profile,
                    "outputProtocol": sess.output_protocol,
                    "sourceType": sess.source_type,
                    "ageSeconds": round(age_sec, 1),
                    "idleSeconds": round(idle_sec, 1),
                })
            cfg = self._load_monitor_cfg() or {}
            enabled = bool(cfg.get("enableStreamProxy"))
            return {
                "enabled": enabled,
                "sessionTtlSeconds": self._session_ttl_sec,
                "activeSessionCount": len(active_sessions),
                "activeSessions": active_sessions,
                "publicBaseUrl": str(cfg.get("publicBaseUrl") or "").strip(),
                "publicHost": str(cfg.get("publicHost") or "").strip(),
                "publicPort": str(cfg.get("publicPort") or "").strip(),
                "publicScheme": str(cfg.get("publicScheme") or "").strip(),
            }
