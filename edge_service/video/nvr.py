from __future__ import annotations

from typing import Any

from .hik.download import (
    _build_rtsp_url as _hik_build_rtsp_url,
    close_download_session as _hik_close_download_session,
    download_by_time as _hik_download_by_time,
    download_by_time_with_session as _hik_download_by_time_with_session,
    open_download_session as _hik_open_download_session,
)

_DEFAULT_PROVIDER = "hikvision"
_PROVIDER_ALIASES = {
    "": _DEFAULT_PROVIDER,
    "hik": _DEFAULT_PROVIDER,
    "hikvision": _DEFAULT_PROVIDER,
    "haikang": _DEFAULT_PROVIDER,
    "海康": _DEFAULT_PROVIDER,
}


def normalize_provider(provider: str | None = None) -> str:
    text = str(provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(text, _DEFAULT_PROVIDER)


def build_rtsp_url(
    username: str,
    password: str,
    ip: str,
    port: int,
    channel: int,
    *,
    main_stream: bool = True,
    provider: str | None = None,
) -> str:
    resolved = normalize_provider(provider)
    if resolved == "hikvision":
        return _hik_build_rtsp_url(username, password, ip, port, channel, main_stream=main_stream)
    raise ValueError(f"unsupported nvr provider: {resolved}")


def open_download_session(*, provider: str | None = None, **kwargs: Any):
    resolved = normalize_provider(provider)
    if resolved == "hikvision":
        return _hik_open_download_session(**kwargs)
    raise ValueError(f"unsupported nvr provider: {resolved}")


def close_download_session(session: Any, *, provider: str | None = None) -> None:
    resolved = normalize_provider(provider)
    if resolved == "hikvision":
        _hik_close_download_session(session)
        return
    raise ValueError(f"unsupported nvr provider: {resolved}")


def download_by_time(*, provider: str | None = None, **kwargs: Any):
    resolved = normalize_provider(provider)
    if resolved == "hikvision":
        return _hik_download_by_time(**kwargs)
    raise ValueError(f"unsupported nvr provider: {resolved}")


def download_by_time_with_session(session: Any, *, provider: str | None = None, **kwargs: Any):
    resolved = normalize_provider(provider)
    if resolved == "hikvision":
        return _hik_download_by_time_with_session(session, **kwargs)
    raise ValueError(f"unsupported nvr provider: {resolved}")
