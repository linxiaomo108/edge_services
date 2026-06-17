from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .base_url_store import BaseUrlStore
from .config import EdgeConfig
from .credential_store import CredentialStore
from .token_store import TokenStore

log = logging.getLogger("edge.api_client")

_POLL_SUCCESS_LOG_INTERVAL_SEC = 300.0
_last_poll_success_log_at = 0.0


@dataclass(frozen=True)
class EdgeTask:
    task_id: str
    task_kind: str
    raw: dict[str, Any]


def _unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("code"), int) and "data" in payload:
        return payload.get("data")
    return payload


class EdgeApiClient:
    def __init__(self, cfg: EdgeConfig, token_store: TokenStore | None = None, base_url_store: BaseUrlStore | None = None, credential_store: CredentialStore | None = None) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=False,
            trust_env=False,
            http2=False,
        )
        self._token_store = token_store
        self._base_url_store = base_url_store
        self._credential_store = credential_store

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _base_url(self) -> str:
        if self._base_url_store is None:
            return self._cfg.server_base_url
        v = (await self._base_url_store.get()).strip()
        if not v:
            return self._cfg.server_base_url
        lv = v.lower()
        if lv == "mock" or lv.startswith("mock://"):
            return self._cfg.server_base_url
        if v.startswith("http://") or v.startswith("https://"):
            return v.rstrip("/")
        return ("http://" + v).rstrip("/")

    async def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base = await self._base_url()
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"

    async def _auth_headers(self, *, include_token: bool = True, include_credentials: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {}
        if include_token and self._token_store is not None:
            token = (await self._token_store.get()).strip()
            if token:
                headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        if include_credentials and self._credential_store is not None:
            access_key, access_secret = await self._credential_store.get()
            access_key = str(access_key or "").strip()
            access_secret = str(access_secret or "").strip()
            if access_key:
                headers["accessKey"] = access_key
            if access_secret:
                headers["accessSecret"] = access_secret
        return headers

    async def _external_poll_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "EdgeServiceClient/1.0",
        }
        if self._credential_store is not None:
            access_key, access_secret = await self._credential_store.get()
            access_key = str(access_key or "").strip()
            access_secret = str(access_secret or "").strip()
            if access_key:
                headers["access_key"] = access_key
            if access_secret:
                headers["access_secret"] = access_secret
        return headers

    async def fetch_pending_tasks(self) -> list[EdgeTask]:
        url = await self._url(self._cfg.task_list_path)
        res = await self._client.get(url, params={"edgeId": self._cfg.edge_id}, headers=await self._auth_headers())
        if res.status_code == 404:
            return []
        res.raise_for_status()
        payload = _unwrap_data(res.json())
        tasks: list[dict[str, Any]] = []
        if isinstance(payload, list):
            tasks = payload
        elif isinstance(payload, dict):
            items = payload.get("items") or payload.get("tasks") or payload.get("list") or []
            if isinstance(items, list):
                tasks = items
        out: list[EdgeTask] = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("taskId") or t.get("id") or "")
            kind = str(t.get("taskKind") or "")
            if not tid or not kind:
                continue
            out.append(EdgeTask(task_id=tid, task_kind=kind, raw=t))
        return out

    async def fetch_external_poll_tasks(self, *, campus_code: str, start_date: str, limit: int = 100, min_task_id: int = 0) -> list[EdgeTask]:
        base = await self._base_url()
        url = f"{base}/api/v1/stream-task/external/poll"
        payload = {
            "minTaskId": int(min_task_id),
            "limit": int(limit),
            "lessonSchoolAreaCode": str(campus_code or "").strip() or "101",
            "startDate": str(start_date or "").strip(),
        }
        headers = await self._external_poll_headers()
        log.debug("poll请求: url=%s, payload=%s, headers=%s", url, payload, {k: v[:20] + "..." if len(v) > 20 else v for k, v in headers.items()})
        try:
            res = await self._client.post(url, json=payload, headers=headers)
            log.debug("poll响应: status=%s, headers=%s", res.status_code, dict(res.headers))
            if res.status_code == 404:
                log.debug("poll返回404，无任务")
                return []
            if res.status_code == 405:
                log.warning("poll返回405 Method Not Allowed，请检查API路径是否正确: %s", url)
                log.warning("  响应内容: %s", res.text[:500] if res.text else "(empty)")
                return []
            res.raise_for_status()
            j = res.json()
            log.debug("poll响应JSON: %s", str(j)[:500])
            data = j.get("data") if isinstance(j, dict) else None
            items = data if isinstance(data, list) else []
            out: list[EdgeTask] = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                tid = str(it.get("taskId") or it.get("id") or "")
                kind = str(it.get("taskKind") or "")
                if not tid or not kind:
                    continue
                out.append(EdgeTask(task_id=tid, task_kind=kind, raw=it))
            global _last_poll_success_log_at
            now = time.monotonic()
            if now - _last_poll_success_log_at >= _POLL_SUCCESS_LOG_INTERVAL_SEC:
                log.info("poll成功，获取到 %d 个任务", len(out))
                _last_poll_success_log_at = now
            return out
        except httpx.HTTPStatusError as e:
            log.error("poll HTTP错误: %s, 响应: %s", e, e.response.text[:500] if e.response.text else "(empty)")
            raise
        except Exception as e:
            log.error("poll异常: %s", e)
            raise

    async def report_step_update(
        self,
        *,
        task_id: int,
        step_code: str,
        step_status: int,
        step_process: int,
        start_time: str | None = None,
        end_time: str | None = None,
        video_size: int | None = None,
        video_format: str | None = None,
        video_shard_num: int | None = None,
        output_file_url: str | None = None,
    ) -> bool:
        import logging
        import asyncio
        
        base = await self._base_url()
        url = f"{base}/api/v1/stream-task/external/step/update"
        
        # 使用服务端期望的格式：整数类型 + 无时区时间
        body: dict[str, Any] = {
            "taskId": int(task_id),
            "stepCode": step_code,
            "stepStatus": int(step_status),
            "stepProcess": int(step_process),
        }
        normalized_start_time: str | None = None
        if start_time is not None:
            # 移除时区偏移，只保留 yyyy-MM-ddTHH:mm:ss 格式
            normalized_start_time = str(start_time).replace('+08:00', '') if '+08:00' in str(start_time) else start_time
        normalized_end_time: str | None = None
        if end_time is not None:
            normalized_end_time = str(end_time).replace('+08:00', '') if '+08:00' in str(end_time) else end_time
        body["startTime"] = normalized_start_time
        body["endTime"] = normalized_end_time
        body["videoSize"] = int(video_size) if video_size is not None else None
        body["videoFormat"] = str(video_format) if video_format is not None else None
        body["videoShardNum"] = int(video_shard_num) if video_shard_num is not None else None
        body["outputFileUrl"] = str(output_file_url) if output_file_url is not None else None
        
        # 重试机制：最多重试3次，每次间隔递增（1s, 2s, 4s）
        max_retries = 3
        
        # 记录请求体用于诊断（仅在调试时启用）
        # log = logging.getLogger("edge.api_client")
        # log.info("report_step_update request body: %s", json.dumps(body, ensure_ascii=False))
        
        for attempt in range(max_retries + 1):
            try:
                res = await self._client.post(url, json=body, headers=await self._auth_headers(include_token=False, include_credentials=True))
                if res.status_code < 400:
                    # 上报成功
                    if attempt > 0:
                        logging.getLogger("edge.api_client").info(
                            "report_step_update succeeded after %s retries: task=%s step=%s", 
                            attempt, task_id, step_code
                        )
                    return True
                else:
                    # 服务器返回错误
                    if attempt < max_retries:
                        wait_sec = 2 ** attempt  # 1, 2, 4 秒
                        logging.getLogger("edge.api_client").warning(
                            "report_step_update failed (attempt %s/%s): status=%s, retrying in %ss...", 
                            attempt + 1, max_retries + 1, res.status_code, wait_sec
                        )
                        await asyncio.sleep(wait_sec)
                    else:
                        logging.getLogger("edge.api_client").warning(
                            "report_step_update failed after %s attempts: status=%s body=%s", 
                            max_retries + 1, res.status_code, res.text[:200]
                        )
            except Exception as e:
                if attempt < max_retries:
                    wait_sec = 2 ** attempt
                    logging.getLogger("edge.api_client").warning(
                        "report_step_update request error (attempt %s/%s): %s, retrying in %ss...", 
                        attempt + 1, max_retries + 1, str(e), wait_sec
                    )
                    await asyncio.sleep(wait_sec)
                else:
                    logging.getLogger("edge.api_client").warning(
                        "report_step_update request error after %s attempts", max_retries + 1, exc_info=True
                    )
        return False

    async def report_task(self, *, task_id: str, task_kind: str, status: str, stage: str, progress: float, message: str, artifacts: list[dict[str, Any]] | None) -> None:
        url = await self._url(self._cfg.task_report_path)
        body: dict[str, Any] = {
            "edgeId": self._cfg.edge_id,
            "taskId": task_id,
            "taskKind": task_kind,
            "status": status,
            "stage": stage,
            "progress": progress,
            "message": message,
        }
        if artifacts is not None:
            body["artifacts"] = artifacts
        res = await self._client.post(url, json=body, headers=await self._auth_headers())
        if res.status_code == 404:
            return
        if res.status_code >= 400:
            res.raise_for_status()

    async def get_client_version(self) -> dict[str, Any] | None:
        """获取服务端最新客户端版本信息"""
        try:
            url = await self._url("/api/client/version")
            res = await self._client.get(url, headers=await self._auth_headers())
            if res.status_code == 404:
                return None
            if res.status_code >= 400:
                return None
            return _unwrap_data(res.json())
        except Exception:
            return None

    async def get_client_updates(self, from_version: str, to_version: str) -> dict[str, Any] | None:
        """获取客户端更新清单"""
        try:
            url = await self._url(f"/api/client/updates?from={from_version}&to={to_version}")
            res = await self._client.get(url, headers=await self._auth_headers())
            if res.status_code == 404:
                return None
            if res.status_code >= 400:
                return None
            return _unwrap_data(res.json())
        except Exception:
            return None
