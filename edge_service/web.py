from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Path as ApiPath, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from .config import EdgeConfig, load_config
from .credential_store import CredentialStore
from .runner import EdgeRunner
from .state import EdgeState
from .base_url_store import BaseUrlStore
from .token_store import TokenStore
from .system_metrics import SystemMetrics
from .db import Db, DbConfig, bjt_now_iso
from .routes.ops_routes import create_ops_router
from .routes.mobile_record_routes import create_mobile_record_router
from .routes.stream_routes import create_stream_router
from .routes.task_routes import create_task_router
from .routes.update_routes import create_update_routes
from .recording import MobileRecordService
from .stream_proxy import StreamSessionManager
from .utils import get_local_ip as _get_local_ip
from .utils import safe_name as _safe_name_util, get_lesson_dir as _get_lesson_dir_util, resolve_lesson_date as _resolve_lesson_date_util, task_type_prefix as _task_type_prefix_util
from .monitor_config import load_monitor_cfg as _load_monitor_cfg_module, save_monitor_cfg as _save_monitor_cfg_module


class ConnectRequest(BaseModel):
    connectionName: str
    serverAddress: str
    campusCode: str = ""
    serverId: str = ""
    startDate: str = ""


class TestRequest(BaseModel):
    serverAddress: str
    serverId: str = ""
    campusCode: str = ""


class ConcurrencyConfig(BaseModel):
    download: int = 2
    transcode: int = 2
    asr: int = 2
    subtitle: int = 2
    analysis: int = 2
    bindPort: int = 18080
    downloadPath: str = r"D:\Videos"
    taskControl: dict[str, bool] = {
        "download": True,
        "transcode": True,
        "speech": True,
        "subtitle": True,
        "analysis": True,
    }
    speechModel: str = "medium"
    speechWordTimestamps: bool = True
    speechPromptMode: str = "off"
    speechVadMode: str = "builtin"
    speechPromptText: str = ""
    speechHallucinationFilter: bool = True
    speechTemperature: float = 0.0
    speechRetryTemperature: float = 0.4
    executionMode: str = "manual"


class OpenFolderRequest(BaseModel):
    path: str


class SelectFolderRequest(BaseModel):
    startPath: str = r"D:\Videos"


class StartTaskRequest(BaseModel):
    id: str


class OpenHistoryVideoRequest(BaseModel):
    lessonId: str
    taskType: str | int
    sourceType: str = "hls"
    reuseIfExists: bool = True


class OpenHistoryVideoResponse(BaseModel):
    success: bool
    message: str
    lessonId: str = ""
    taskType: int = 0
    taskTypeName: str = ""
    sourceType: str = ""
    serverTaskId: int = 0
    sessionId: str = ""
    publicPlayUrl: str = ""
    lanPlayUrl: str = ""
    subtitlePublicUrl: str = ""
    subtitleLanUrl: str = ""
    reused: bool = False


class CloseHistoryVideoRequest(BaseModel):
    sessionId: str


def _is_ignorable_asyncio_connection_reset(context: dict) -> bool:
    try:
        exc = context.get("exception")
    except Exception:
        exc = None
    if not isinstance(exc, ConnectionResetError):
        return False
    try:
        winerror = int(getattr(exc, "winerror", 0) or 0)
    except Exception:
        winerror = 0
    if winerror != 10054 and "[WinError 10054]" not in str(exc):
        return False
    message = str(context.get("message") or "")
    handle = context.get("handle")
    handle_text = repr(handle) if handle is not None else ""
    combined = f"{message} {handle_text}"
    return "_ProactorBasePipeTransport._call_connection_lost" in combined or "_call_connection_lost" in combined


def _install_asyncio_exception_handler() -> None:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    state = {"last_log_at": 0.0}

    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        if _is_ignorable_asyncio_connection_reset(context):
            now = time.monotonic()
            if now - float(state.get("last_log_at") or 0.0) >= 300.0:
                state["last_log_at"] = now
                logging.getLogger("edge.asyncio").warning("忽略 Windows asyncio transport reset 噪声: %s", str(context.get("exception") or "ConnectionResetError[10054]"))
            return
        if previous_handler is not None:
            previous_handler(loop, context)
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def _build_browser_playable_source(source_path: Path) -> Path | None:
    path = Path(source_path)
    if path.exists():
        return path
    faststart_path = path.with_name(path.stem + ".faststart.mp4")
    if faststart_path.exists():
        return faststart_path
    return None


def build_app(cfg: EdgeConfig) -> FastAPI:
    state = EdgeState(edge_id=cfg.edge_id)
    service_version_info = dict(getattr(cfg, "version_info", {}) or {})
    if getattr(sys, 'frozen', False):
        base_dir = Path(sys.executable).resolve().parent.parent
    else:
        base_dir = Path(__file__).resolve().parent
    output_dir = base_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics = SystemMetrics(disk_path=str(base_dir))
    db_path = str(output_dir / "edge.db")
    db = Db(DbConfig(path=db_path))
    db.init_schema()

    def _load_monitor_cfg() -> dict:
        return _load_monitor_cfg_module()

    def _save_monitor_cfg(data: dict) -> None:
        _save_monitor_cfg_module(data)

    def _ensure_service_id() -> str:
        data = _load_monitor_cfg()
        sid = str(data.get("serviceId") or "").strip()
        if sid:
            return sid
        sid = str(uuid.uuid4())
        data["serviceId"] = sid
        _save_monitor_cfg(data)
        return sid

    service_id = _ensure_service_id()
    init_cfg = _load_monitor_cfg()
    token_store = TokenStore(token=str(init_cfg.get("authToken") or init_cfg.get("accessKey") or ""))
    credential_store = CredentialStore(
        access_key=str(init_cfg.get("accessKey") or "").strip(),
        access_secret=str(init_cfg.get("accessSecret") or "").strip(),
    )
    base_url_store = BaseUrlStore(base_url=str(init_cfg.get("serverAddress") or ""))
    runner = EdgeRunner(cfg=cfg, state=state, db_path=db_path, token_store=token_store, base_url_store=base_url_store, credential_store=credential_store)
    stream_session_manager = StreamSessionManager(db=db, load_monitor_cfg=_load_monitor_cfg)
    mobile_record_service = MobileRecordService(db=db, load_monitor_cfg=_load_monitor_cfg, session_manager=stream_session_manager)
    tasks_lock = asyncio.Lock()
    heartbeat_state: dict[str, str] = {
        "status": "idle",
        "lastSuccessAt": "",
        "lastFailureAt": "",
        "lastAttemptAt": "",
        "lastError": "",
        "message": "未开始心跳",
    }

    def _normalize_server_address(addr: str) -> str:
        value = str(addr or "").strip()
        if not value:
            return ""
        if value.startswith("http://") or value.startswith("https://"):
            return value.rstrip("/")
        return ("http://" + value).rstrip("/")

    def _unwrap_remote_payload(payload: object) -> dict:
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload.get("data") or {}
        if isinstance(payload, dict):
            return payload
        return {}

    def _remote_call_ok(payload: object) -> bool:
        if not isinstance(payload, dict):
            return True
        if "success" in payload:
            return bool(payload.get("success"))
        code = str(payload.get("code") or "").strip()
        if code:
            return code in {"0", "100000", "200"}
        return True

    def _runtime_auth_token(data: dict) -> str:
        return str(data.get("authToken") or data.get("token") or "").strip()

    async def _register_remote(server_address: str, server_id: str, campus_code: str) -> tuple[bool, dict, str]:
        import logging
        log = logging.getLogger("edge.web")
        
        addr = _normalize_server_address(server_address)
        log.info("注册远程服务器: 原始地址=%s, 规范化地址=%s", server_address, addr)
        if not addr:
            return False, {}, "服务器地址不能为空"
        if addr.lower() == "mock" or addr.lower().startswith("mock://"):
            log.info("使用Mock模式")
            return True, {"accessKey": "mock-access-key", "accessSecret": "mock-access-secret"}, ""
        device_id_str = str(server_id or "").strip()
        if not device_id_str:
            return False, {}, "服务器ID不能为空"
        # 尝试转换为整数，如果失败则使用字符串
        try:
            device_id: int | str = int(device_id_str)
        except ValueError:
            device_id = device_id_str
        payload = {
            "deviceId": device_id,
            "schoolAreaCode": str(campus_code or "").strip(),
        }
        register_url = addr.rstrip("/") + "/api/v1/device/edge/register"
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "EdgeServiceClient/1.0",
        }
        log.info("注册请求: url=%s", register_url)
        log.info("注册请求: payload=%s", payload)
        log.info("注册请求: headers=%s", request_headers)
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=False,
                trust_env=False,
                http2=False,
            ) as client:
                res = await client.post(
                    register_url,
                    headers=request_headers,
                    json=payload,
                )
            log.info("注册响应: status=%s", res.status_code)
            if res.history:
                log.info("注册响应: history=%s", [str(item.status_code) + ":" + str(item.headers.get("location") or "") for item in res.history])
            log.info("注册响应: headers=%s", dict(res.headers))
            log.info("注册响应: body=%s", res.text[:500] if res.text else "(empty)")
            if res.status_code >= 400:
                return False, {}, f"注册失败({res.status_code}): {res.text[:200] if res.text else ''}"
            body = res.json()
            if not _remote_call_ok(body):
                return False, {}, str(body.get("message") or body.get("msg") or "注册失败")
            log.info("注册成功: %s", body)
            return True, _unwrap_remote_payload(body), ""
        except Exception as exc:
            log.exception("注册异常: %s", exc)
            return False, {}, str(exc) or "注册失败"

    async def _save_registration(connection_name: str, server_address: str, campus_code: str, server_id: str, start_date: str, register_data: dict) -> dict:
        data = _load_monitor_cfg()
        data["serviceId"] = service_id
        data["connectionName"] = str(connection_name or data.get("connectionName") or "").strip()
        data["serverAddress"] = _normalize_server_address(server_address)
        data["campusCode"] = str(campus_code or "").strip()
        data["schoolAreaName"] = str(register_data.get("schoolAreaName") or data.get("schoolAreaName") or "").strip()
        data["serverId"] = str(server_id or "").strip()
        data["startDate"] = str(start_date or "").strip()
        data["registerInfo"] = register_data or {}
        data["accessKey"] = str(register_data.get("accessKey") or data.get("accessKey") or "").strip()
        data["accessSecret"] = str(register_data.get("accessSecret") or data.get("accessSecret") or "").strip()
        auth_token = str(register_data.get("authToken") or register_data.get("token") or data.get("authToken") or "").strip()
        if auth_token:
            data["authToken"] = auth_token
        _save_monitor_cfg(data)
        await token_store.set(_runtime_auth_token(data))
        await credential_store.set(str(data.get("accessKey") or ""), str(data.get("accessSecret") or ""))
        await base_url_store.set(str(data.get("serverAddress") or ""))
        return data

    async def _send_heartbeat_once() -> None:
        data = _load_monitor_cfg()
        addr = _normalize_server_address(str(data.get("serverAddress") or ""))
        access_key = str(data.get("accessKey") or "").strip()
        access_secret = str(data.get("accessSecret") or "").strip()
        if not addr or not access_key or not access_secret:
            heartbeat_state["status"] = "idle"
            heartbeat_state["message"] = "未配置心跳凭据"
            return
        heartbeat_state["lastAttemptAt"] = bjt_now_iso()
        snap = await metrics.snapshot()
        body = {
            "accessKey": access_key,
            "accessSecret": access_secret,
            "softVersion": str(cfg.version or "1.0.0"),
            "cpu": f"{int(round(float(snap.cpu_percent or 0)))}%",
            "gpu": f"{int(round(float(snap.gpu_percent or 0)))}%",
            "ram": f"{int(round(float(snap.ram_percent or 0)))}%",
            "disk": f"{int(round(float(snap.disk_percent or 0)))}%",
        }
        heartbeat_url = addr.rstrip("/") + "/api/v1/device/edge/heartbeat"
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False, http2=False) as client:
            try:
                res = await client.post(
                    heartbeat_url,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "EdgeServiceClient/1.0",
                    },
                    json=body,
                )
            except Exception as exc:
                detail = str(exc).strip() or repr(exc)
                raise RuntimeError(
                    f"heartbeat_request_failed url={heartbeat_url} type={exc.__class__.__name__} detail={detail[:200]}"
                ) from exc
        if res.status_code >= 400:
            response_text = (res.text or "").strip()
            response_summary = response_text[:200] if response_text else "(empty)"
            raise RuntimeError(
                f"heartbeat_failed status={res.status_code} url={heartbeat_url} body={response_summary}"
            )
        heartbeat_state["status"] = "success"
        heartbeat_state["lastSuccessAt"] = bjt_now_iso()
        heartbeat_state["lastError"] = ""
        heartbeat_state["message"] = "心跳正常"

    async def _heartbeat_loop() -> None:
        while True:
            try:
                await _send_heartbeat_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err_text = str(exc).strip() or repr(exc)
                heartbeat_state["status"] = "failed"
                heartbeat_state["lastFailureAt"] = bjt_now_iso()
                heartbeat_state["lastError"] = err_text[:300]
                heartbeat_state["message"] = "心跳失败"
                logging.getLogger("edge.heartbeat").warning("heartbeat failed: %s", err_text)
            await asyncio.sleep(30)

    async def _cleanup_task_logs_loop() -> None:
        log = logging.getLogger("edge.db.maintenance")
        retention_days = max(1, int(cfg.db_log_retention_days))
        while True:
            try:
                deleted = await asyncio.to_thread(db.cleanup_task_logs, retention_days=retention_days, batch_size=5000)
                if deleted > 0:
                    log.info("cleanup edge_task_log retention_days=%s deleted=%s", retention_days, deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cleanup edge_task_log failed")
            await asyncio.sleep(24 * 60 * 60)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.getLogger("edge.web").info(
            "service startup version=%s build_time=%s git_commit=%s git_branch=%s dirty=%s package_mode=%s package_name=%s version_file=%s",
            str(service_version_info.get("version") or cfg.version or ""),
            str(service_version_info.get("buildTime") or ""),
            str(service_version_info.get("gitCommit") or ""),
            str(service_version_info.get("gitBranch") or ""),
            bool(service_version_info.get("dirty", False)),
            str(service_version_info.get("packageMode") or ""),
            str(service_version_info.get("packageName") or ""),
            str(service_version_info.get("versionFile") or ""),
        )
        _install_asyncio_exception_handler()
        task = asyncio.create_task(runner.run_forever())
        await mobile_record_service.start_background()
        heartbeat_task = asyncio.create_task(_heartbeat_loop())
        cleanup_task = asyncio.create_task(_cleanup_task_logs_loop())
        async def _open_browser():
            try:
                import webbrowser
                await asyncio.sleep(0.6)
                access_host = _get_local_ip()
                webbrowser.open(f"http://{access_host}:{cfg.bind_port}/", new=1, autoraise=True)
            except Exception:
                pass
        open_task = asyncio.create_task(_open_browser())
        try:
            yield
        finally:
            open_task.cancel()
            heartbeat_task.cancel()
            cleanup_task.cancel()
            await runner.stop()
            task.cancel()
            with contextlib.suppress(Exception):
                await open_task
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cleanup_task
            await mobile_record_service.aclose()
            await runner.aclose()

    app = FastAPI(lifespan=lifespan)

    def _custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title="边缘服务业务接口文档",
            version=str(cfg.version),
            description=(
                "保留监控页面与本地运维会直接使用的接口。"
                "文档中的 `server_task_id` 均表示服务端下发的任务 ID，"
                "不是 lesson_id，也不是本地数据库自增 id。"
                "其中查询参数 `id` 的格式固定为 `{server_task_id}-{STEP}`，"
                "例如 `4-DOWNLOAD`、`4-TRANSCODE`。"
            ),
            routes=app.routes,
        )
        allow_paths = {
            "/api/task/video",
            "/api/task/subtitle-vtt",
            "/api/history/video/open",
            "/api/history/video/play/{session_id}",
            "/api/history/video/close",
            "/api/stream/open",
            "/api/stream/play/{nvr_device_id}/{channel_num}",
            "/api/stream/close",
            "/api/stream/status",
            "/api/mobile-record/classrooms/status",
            "/api/mobile-record/start",
            "/api/mobile-record/stop",
            "/api/mobile-record/extend",
            "/api/mobile-record/status",
            "/api/mobile-record/list",
            "/api/mobile-record/play/{task_id}",
            "/api/mobile-record/play/close",
            "/api/mobile-record/delete",
            "/api/speech-report/{server_task_id}",
            "/api/analysis-report/{server_task_id}",
            "/api/server-artifacts/{server_task_id}",
        }
        schema["paths"] = {k: v for k, v in schema.get("paths", {}).items() if k in allow_paths}
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = _custom_openapi

    @app.get("/", response_class=HTMLResponse)
    async def index():
        page = base_dir / "monitor_ui" / "node_detail.html"
        html = page.read_text(encoding="utf-8", errors="ignore")
        return HTMLResponse(html)

    @app.get("/api/meta")
    async def api_meta():
        data = _load_monitor_cfg()
        last_heartbeat_at = str(heartbeat_state.get("lastSuccessAt") or heartbeat_state.get("lastAttemptAt") or "")
        return JSONResponse(
            {
                "serviceId": service_id,
                "version": cfg.version,
                "connectionName": str(data.get("connectionName") or ""),
                "serverAddress": str(data.get("serverAddress") or ""),
                "campusCode": str(data.get("campusCode") or ""),
                "startDate": str(data.get("startDate") or ""),
                "schoolAreaName": str(data.get("schoolAreaName") or ""),
                "serverId": str(data.get("serverId") or ""),
                "enableStreamProxy": bool(data.get("enableStreamProxy")) if "enableStreamProxy" in data else False,
                "publicBaseUrl": str(data.get("publicBaseUrl") or ""),
                "publicHost": str(data.get("publicHost") or ""),
                "publicPort": str(data.get("publicPort") or ""),
                "publicScheme": str(data.get("publicScheme") or ""),
                "heartbeatStatus": str(heartbeat_state.get("status") or "idle"),
                "lastHeartbeatAt": last_heartbeat_at,
            }
        )

    @app.get("/api/heartbeat-status")
    async def api_heartbeat_status():
        return JSONResponse(dict(heartbeat_state))

    @app.post("/api/connect")
    async def api_connect(req: ConnectRequest):
        current = _load_monitor_cfg()
        previous_start_date = str(current.get("startDate") or "").strip()
        next_start_date = str(req.startDate or "").strip()
        ok, register_data, message = await _register_remote(req.serverAddress, req.serverId, req.campusCode)
        if not ok:
            return JSONResponse({"ok": False, "message": message or "连接失败"}, status_code=400)
        await _save_registration(req.connectionName, req.serverAddress, req.campusCode, req.serverId, next_start_date, register_data)
        refresh_result = None
        if previous_start_date != next_start_date:
            refresh_result = await runner.repoll_pending_tasks_only()
        return JSONResponse({"ok": True, "data": register_data, "refreshedPending": refresh_result is not None, "refreshResult": refresh_result})

    @app.post("/api/test-connection")
    async def api_test_connection(req: TestRequest):
        ok, register_data, message = await _register_remote(req.serverAddress, req.serverId, req.campusCode)
        if not ok:
            return JSONResponse({"ok": False, "message": message or "连接失败"}, status_code=400)
        current = _load_monitor_cfg()
        await _save_registration(str(current.get("connectionName") or ""), req.serverAddress, req.campusCode, req.serverId, str(current.get("startDate") or ""), register_data)
        return JSONResponse({"ok": True, "data": register_data})

    app.include_router(
        create_task_router(
            db=db,
            runner=runner,
            tasks_lock=tasks_lock,
            get_lesson_dir=_get_lesson_dir_util,
            start_task_model=StartTaskRequest,
        )
    )
    app.include_router(create_mobile_record_router(service=mobile_record_service))

    history_session_lock = threading.Lock()
    history_session_ttl_sec = 1800
    history_sessions: dict[str, dict[str, object]] = {}
    history_session_keys: dict[str, str] = {}

    def _normalize_history_task_type(value: object) -> tuple[int, str]:
        text = str(value or "").strip().lower()
        if text in {"teacher", "1"}:
            return 1, "teacher"
        if text in {"student", "2"}:
            return 2, "student"
        if text in {"ppt", "3"}:
            return 3, "ppt"
        if text in {"view", "0"}:
            return 0, "view"
        raise ValueError("taskType 仅支持 teacher/student/ppt/view 或 1/2/3/0")

    def _normalize_history_source_type(value: object) -> str:
        text = str(value or "hls").strip().lower()
        if text in {"hls", "m3u8"}:
            return "hls"
        if text in {"mp4", "source"}:
            return "mp4"
        raise ValueError("sourceType 仅支持 mp4 或 hls")

    def _cleanup_history_sessions_locked(now_ts: float | None = None) -> None:
        now_value = float(now_ts or time.time())
        expired_ids = [
            sid
            for sid, sess in history_sessions.items()
            if now_value - float(sess.get("updated_at") or 0.0) > float(history_session_ttl_sec)
        ]
        for sid in expired_ids:
            sess = history_sessions.pop(sid, None)
            if sess is None:
                continue
            session_key = str(sess.get("session_key") or "")
            if session_key and history_session_keys.get(session_key) == sid:
                history_session_keys.pop(session_key, None)

    def _touch_history_session(session_id: str) -> dict[str, object] | None:
        with history_session_lock:
            _cleanup_history_sessions_locked()
            sess = history_sessions.get(str(session_id or "").strip())
            if sess is None:
                return None
            sess["updated_at"] = time.time()
            return dict(sess)

    def _close_history_session(session_id: str) -> bool:
        sid = str(session_id or "").strip()
        with history_session_lock:
            _cleanup_history_sessions_locked()
            sess = history_sessions.pop(sid, None)
            if sess is None:
                return False
            session_key = str(sess.get("session_key") or "")
            if session_key and history_session_keys.get(session_key) == sid:
                history_session_keys.pop(session_key, None)
            return True

    def _resolve_video_paths(server_task_id: int) -> dict[str, Path | None]:
        row = db.fetch_one(
            """
SELECT
  t.id,
  t.lesson_id,
  t.lesson_date,
  t.download_start,
  t.task_type,
  t.server_task_id,
  (
    SELECT s.output_file_path
    FROM edge_stream_task_step s
    WHERE s.task_id=t.id AND s.step_code='DOWNLOAD'
    LIMIT 1
  ) AS download_output_file_path,
  (
    SELECT s.output_file_path
    FROM edge_stream_task_step s
    WHERE s.task_id=t.id AND s.step_code='TRANSCODE'
    LIMIT 1
  ) AS transcode_output_file_path
FROM edge_stream_task t
WHERE t.server_task_id=? AND t.task_kind='CameraTask'
ORDER BY t.id DESC
LIMIT 1
            """.strip(),
            (int(server_task_id),),
        )
        if row is None:
            return {"source": None, "hls_dir": None}
        lesson_id = str(row["lesson_id"] or "")
        lesson_date = _resolve_lesson_date_util(row["lesson_date"], row["download_start"])
        task_type = int(row["task_type"] or 0)
        stid = str(row["server_task_id"] or "")
        prefix = _task_type_prefix_util(task_type)
        out_dir = _get_lesson_dir_util(lesson_date, lesson_id)
        video_stem = f"{prefix}_{stid}"
        source_path = out_dir / f"{video_stem}.mp4"
        hls_dir = out_dir / f"{video_stem}_1080P"
        download_output_file_path = str(row["download_output_file_path"] or "").strip()
        transcode_output_file_path = str(row["transcode_output_file_path"] or "").strip()
        if download_output_file_path:
            source_path = Path(download_output_file_path)
        else:
            source_artifact = db.fetch_one(
                "SELECT file_path FROM edge_lesson_output WHERE lesson_id=? AND server_task_id=? AND file_type='source_video' ORDER BY id DESC LIMIT 1",
                (int(row["lesson_id"] or 0), int(server_task_id)),
            )
            source_artifact_path = str(source_artifact["file_path"] or "").strip() if source_artifact else ""
            if source_artifact_path:
                source_path = Path(source_artifact_path)
        if transcode_output_file_path:
            transcode_path = Path(transcode_output_file_path)
            hls_dir = transcode_path.parent if transcode_path.suffix.lower() == ".m3u8" else transcode_path
        else:
            transcode_artifact = db.fetch_one(
                "SELECT file_path FROM edge_lesson_output WHERE lesson_id=? AND server_task_id=? AND file_type='transcoded_video' ORDER BY id DESC LIMIT 1",
                (int(row["lesson_id"] or 0), int(server_task_id)),
            )
            transcode_artifact_path = str(transcode_artifact["file_path"] or "").strip() if transcode_artifact else ""
            if transcode_artifact_path:
                transcode_path = Path(transcode_artifact_path)
                hls_dir = transcode_path.parent if transcode_path.suffix.lower() == ".m3u8" else transcode_path
        return {"source": source_path, "hls_dir": hls_dir}

    def _resolve_history_video_target(lesson_id: str, task_type: int) -> dict[str, object] | None:
        row = db.fetch_one(
            "SELECT id, lesson_id, lesson_date, download_start, task_type, server_task_id FROM edge_stream_task WHERE lesson_id=? AND task_type=? ORDER BY id DESC LIMIT 1",
            (str(lesson_id or "").strip(), int(task_type)),
        )
        if row is None:
            return None
        lesson_date = _resolve_lesson_date_util(row["lesson_date"], row["download_start"])
        server_task_id = int(row["server_task_id"] or 0)
        prefix = _task_type_prefix_util(task_type)
        out_dir = _get_lesson_dir_util(lesson_date, str(row["lesson_id"] or ""))
        video_stem = f"{prefix}_{server_task_id}"
        return {
            "lesson_id": str(row["lesson_id"] or "").strip(),
            "task_type": int(task_type),
            "task_type_name": prefix,
            "server_task_id": server_task_id,
            "source_path": out_dir / f"{video_stem}.mp4",
            "hls_dir": out_dir / f"{video_stem}_1080P",
        }

    def _create_or_get_history_session(*, lesson_id: str, task_type: int, task_type_name: str, source_type: str, reuse_if_exists: bool) -> tuple[dict[str, object], bool]:
        target = _resolve_history_video_target(lesson_id, task_type)
        if target is None:
            raise FileNotFoundError("未找到对应课次视频任务")
        source_path = Path(str(target.get("source_path") or ""))
        hls_dir = Path(str(target.get("hls_dir") or ""))
        if source_type == "mp4":
            playable = _build_browser_playable_source(source_path)
            if not playable or not playable.exists():
                raise FileNotFoundError("MP4 文件不存在")
        else:
            if not hls_dir.exists() or not (hls_dir / "index.m3u8").exists():
                raise FileNotFoundError("HLS 文件不存在")
        session_key = f"{str(lesson_id).strip()}::{int(task_type)}::{source_type}"
        now_ts = time.time()
        with history_session_lock:
            _cleanup_history_sessions_locked(now_ts)
            existing_id = history_session_keys.get(session_key)
            if reuse_if_exists and existing_id:
                existing = history_sessions.get(existing_id)
                if existing is not None:
                    existing["updated_at"] = now_ts
                    return dict(existing), True
            session_id = str(uuid.uuid4())
            sess = {
                "session_id": session_id,
                "session_key": session_key,
                "lesson_id": str(target.get("lesson_id") or "").strip(),
                "task_type": int(task_type),
                "task_type_name": str(task_type_name or "").strip(),
                "source_type": str(source_type or "").strip().lower(),
                "server_task_id": int(target.get("server_task_id") or 0),
                "source_path": str(source_path),
                "hls_dir": str(hls_dir),
                "created_at": now_ts,
                "updated_at": now_ts,
            }
            history_sessions[session_id] = sess
            history_session_keys[session_key] = session_id
            return dict(sess), False

    def _build_history_session_response(session: dict[str, object], request_base_url: str, reused: bool) -> OpenHistoryVideoResponse:
        session_id = str(session.get("session_id") or "").strip()
        source_type = str(session.get("source_type") or "").strip().lower()
        public_base = stream_session_manager.build_public_access_base_url(request_base_url)
        lan_base = stream_session_manager.build_lan_access_base_url(request_base_url)
        subtitle_public = ""
        subtitle_lan = ""
        if source_type == "hls":
            hls_dir = Path(str(session.get("hls_dir") or ""))
            if (hls_dir / "subtitles.zh.vtt").exists():
                subtitle_public = f"{public_base}/api/history/video/subtitle/{session_id}"
                subtitle_lan = f"{lan_base}/api/history/video/subtitle/{session_id}"
        else:
            source_path = Path(str(session.get("source_path") or ""))
            if source_path.exists() and (source_path.parent / f"{source_path.stem}.zh.vtt").exists():
                subtitle_public = f"{public_base}/api/history/video/subtitle/{session_id}"
                subtitle_lan = f"{lan_base}/api/history/video/subtitle/{session_id}"
        return OpenHistoryVideoResponse(
            success=True,
            message="ok",
            lessonId=str(session.get("lesson_id") or "").strip(),
            taskType=int(session.get("task_type") or 0),
            taskTypeName=str(session.get("task_type_name") or "").strip(),
            sourceType=source_type,
            serverTaskId=int(session.get("server_task_id") or 0),
            sessionId=session_id,
            publicPlayUrl=f"{public_base}/api/history/video/play/{session_id}",
            lanPlayUrl=f"{lan_base}/api/history/video/play/{session_id}",
            subtitlePublicUrl=subtitle_public,
            subtitleLanUrl=subtitle_lan,
            reused=bool(reused),
        )

    def _build_history_hls_master_playlist(session_id: str, hls_dir: Path) -> str | None:
        if not hls_dir.exists() or not (hls_dir / "index.m3u8").exists():
            return None
        lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        vtt_file = hls_dir / "subtitles.zh.vtt"
        if vtt_file.exists():
            sub_url = f"/api/history/video/subtitle-m3u8/{session_id}"
            lines.append('#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,LANGUAGE="zh",URI="' + sub_url + '"')
            lines.append('#EXT-X-STREAM-INF:BANDWIDTH=4000000,AVERAGE-BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",SUBTITLES="subs"')
        else:
            lines.append('#EXT-X-STREAM-INF:BANDWIDTH=4000000,AVERAGE-BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"')
        lines.append(f"/api/history/video/hls-media/{session_id}")
        return "\n".join(lines) + "\n"

    def _rewrite_history_hls_media_playlist(session_id: str, hls_dir: Path) -> str | None:
        m3u8 = hls_dir / "index.m3u8"
        if not m3u8.exists():
            return None
        raw = m3u8.read_text(encoding="utf-8", errors="ignore")
        def _rewrite_seg(line: str) -> str:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return f"/api/history/video/hls-seg/{session_id}?file={stripped}"
            return line
        return "\n".join(_rewrite_seg(line) for line in raw.splitlines())

    def _rewrite_hls_media_playlist(task_ref: str, hls_dir: Path) -> str | None:
        m3u8 = hls_dir / "index.m3u8"
        if not m3u8.exists():
            return None
        raw = m3u8.read_text(encoding="utf-8", errors="ignore")
        def _rewrite_seg(line: str) -> str:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return f"/api/task/hls-seg?id={task_ref}&file={stripped}"
            return line
        return "\n".join(_rewrite_seg(line) for line in raw.splitlines())

    def _build_task_hls_master_playlist(task_ref: str, hls_dir: Path) -> str | None:
        if not hls_dir.exists() or not (hls_dir / "index.m3u8").exists():
            return None
        lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        vtt_file = hls_dir / "subtitles.zh.vtt"
        if vtt_file.exists():
            sub_url = f"/api/task/subtitle-m3u8?id={task_ref}"
            lines.append('#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,LANGUAGE="zh",URI="' + sub_url + '"')
            lines.append('#EXT-X-STREAM-INF:BANDWIDTH=4000000,AVERAGE-BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",SUBTITLES="subs"')
        else:
            lines.append('#EXT-X-STREAM-INF:BANDWIDTH=4000000,AVERAGE-BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"')
        lines.append(f"/api/task/hls-media?id={task_ref}")
        return "\n".join(lines) + "\n"

    @app.post(
        "/api/history/video/open",
        tags=["业务接口"],
        summary="打开历史视频播放地址",
        description="按 lessonId、taskType、sourceType 创建或复用历史视频播放会话，返回公网与局域网播放地址。sourceType=mp4 时返回稳定 MP4 播放会话；sourceType=hls 时返回稳定 HLS 会话地址。",
        response_model=OpenHistoryVideoResponse,
        responses={200: {"description": "返回历史视频可播放地址"}, 404: {"description": "未找到对应历史视频"}},
    )
    async def api_history_video_open(req: OpenHistoryVideoRequest, request: Request):
        lesson_id = str(req.lessonId or "").strip()
        if not lesson_id:
            return JSONResponse({"success": False, "message": "lessonId 不能为空"}, status_code=400)
        try:
            task_type, task_type_name = _normalize_history_task_type(req.taskType)
            source_type = _normalize_history_source_type(req.sourceType)
            session, reused = await asyncio.to_thread(
                _create_or_get_history_session,
                lesson_id=lesson_id,
                task_type=task_type,
                task_type_name=task_type_name,
                source_type=source_type,
                reuse_if_exists=bool(req.reuseIfExists),
            )
        except ValueError as exc:
            return JSONResponse({"success": False, "message": str(exc)}, status_code=400)
        except FileNotFoundError as exc:
            return JSONResponse({"success": False, "message": str(exc), "lessonId": lesson_id}, status_code=404)
        result = await asyncio.to_thread(_build_history_session_response, session, str(request.base_url).rstrip("/"), reused)
        return result

    @app.get("/api/history/video/play/{session_id}", summary="播放历史视频", description="根据历史视频播放会话获取播放内容。mp4 返回 video/mp4 文件；hls 返回会话级 master m3u8。")
    async def api_history_video_play(session_id: str):
        session = await asyncio.to_thread(_touch_history_session, session_id)
        if session is None:
            return JSONResponse({"success": False, "message": "历史视频会话不存在，请先调用 open 接口"}, status_code=404)
        source_type = str(session.get("source_type") or "").strip().lower()
        if source_type == "hls":
            hls_dir = Path(str(session.get("hls_dir") or ""))
            playlist = await asyncio.to_thread(_build_history_hls_master_playlist, str(session.get("session_id") or ""), hls_dir)
            if not playlist:
                return JSONResponse({"success": False, "message": "HLS 文件不存在"}, status_code=404)
            return Response(content=playlist, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        source_path = Path(str(session.get("source_path") or ""))
        playable = await asyncio.to_thread(_build_browser_playable_source, source_path)
        if playable and playable.exists():
            return FileResponse(str(playable), media_type="video/mp4", headers={"Cache-Control": "no-store", "Pragma": "no-cache", "Content-Disposition": "inline"})
        return JSONResponse({"success": False, "message": "MP4 文件不存在"}, status_code=404)

    @app.get("/api/history/video/hls-media/{session_id}", include_in_schema=False)
    async def api_history_video_hls_media(session_id: str):
        session = await asyncio.to_thread(_touch_history_session, session_id)
        if session is None:
            return JSONResponse({"success": False, "message": "历史视频会话不存在，请先调用 open 接口"}, status_code=404)
        if str(session.get("source_type") or "").strip().lower() != "hls":
            return JSONResponse({"success": False, "message": "当前会话不是 HLS"}, status_code=400)
        hls_dir = Path(str(session.get("hls_dir") or ""))
        playlist = await asyncio.to_thread(_rewrite_history_hls_media_playlist, str(session.get("session_id") or ""), hls_dir)
        if not playlist:
            return JSONResponse({"success": False, "message": "HLS 文件不存在"}, status_code=404)
        return Response(content=playlist, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.get("/api/history/video/hls-seg/{session_id}", include_in_schema=False)
    async def api_history_video_hls_seg(session_id: str, file: str = ""):
        session = await asyncio.to_thread(_touch_history_session, session_id)
        if session is None:
            return JSONResponse({"success": False, "message": "历史视频会话不存在，请先调用 open 接口"}, status_code=404)
        if str(session.get("source_type") or "").strip().lower() != "hls":
            return JSONResponse({"success": False, "message": "当前会话不是 HLS"}, status_code=400)
        seg_name = str(file or "").strip()
        if not seg_name:
            return JSONResponse({"success": False, "message": "file 不能为空"}, status_code=400)
        import re as _re
        safe_seg = _re.sub(r'[/\\]', '', seg_name)
        hls_dir = Path(str(session.get("hls_dir") or ""))
        seg_path = hls_dir / safe_seg
        if not seg_path.exists():
            return JSONResponse({"success": False, "message": "分片不存在"}, status_code=404)
        mt = "video/mp2t" if safe_seg.endswith(".ts") else "application/vnd.apple.mpegurl"
        return FileResponse(str(seg_path), media_type=mt, headers={"Cache-Control": "no-store", "Pragma": "no-cache", "Content-Disposition": "inline"})

    @app.get("/api/history/video/subtitle/{session_id}", include_in_schema=False)
    async def api_history_video_subtitle(session_id: str):
        session = await asyncio.to_thread(_touch_history_session, session_id)
        if session is None:
            return JSONResponse({"success": False, "message": "历史视频会话不存在，请先调用 open 接口"}, status_code=404)
        source_type = str(session.get("source_type") or "").strip().lower()
        if source_type == "hls":
            hls_dir = Path(str(session.get("hls_dir") or ""))
            vtt = hls_dir / "subtitles.zh.vtt"
            if vtt.exists():
                return FileResponse(str(vtt), media_type="text/vtt", filename="subtitles.zh.vtt")
            return JSONResponse({"success": False, "message": "字幕文件不存在"}, status_code=404)
        source_path = Path(str(session.get("source_path") or ""))
        if source_path.exists():
            vtt = source_path.parent / f"{source_path.stem}.zh.vtt"
            if vtt.exists():
                return FileResponse(str(vtt), media_type="text/vtt", filename=vtt.name)
        return JSONResponse({"success": False, "message": "字幕文件不存在"}, status_code=404)

    @app.get("/api/history/video/subtitle-m3u8/{session_id}", include_in_schema=False)
    async def api_history_video_subtitle_m3u8(session_id: str):
        session = await asyncio.to_thread(_touch_history_session, session_id)
        if session is None:
            return JSONResponse({"success": False, "message": "历史视频会话不存在，请先调用 open 接口"}, status_code=404)
        if str(session.get("source_type") or "").strip().lower() != "hls":
            return JSONResponse({"success": False, "message": "当前会话不是 HLS"}, status_code=400)
        hls_dir = Path(str(session.get("hls_dir") or ""))
        vtt = hls_dir / "subtitles.zh.vtt"
        if not vtt.exists():
            return JSONResponse({"success": False, "message": "字幕文件不存在"}, status_code=404)
        duration = 0.0
        m3u8_file = hls_dir / "index.m3u8"
        if m3u8_file.exists():
            for line in m3u8_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("#EXTINF:"):
                    try:
                        duration += float(line.split(":")[1].rstrip(","))
                    except Exception:
                        pass
        if duration <= 0:
            duration = 7200.0
        vtt_url = f"/api/history/video/subtitle/{session_id}"
        playlist = "#EXTM3U\n" f"#EXT-X-TARGETDURATION:{int(duration) + 1}\n" "#EXT-X-VERSION:3\n" "#EXT-X-MEDIA-SEQUENCE:0\n" "#EXT-X-PLAYLIST-TYPE:VOD\n" f"#EXTINF:{duration:.3f},\n" f"{vtt_url}\n" "#EXT-X-ENDLIST\n"
        return Response(content=playlist, media_type="application/vnd.apple.mpegurl")

    @app.post("/api/history/video/close", summary="关闭历史视频播放会话", description="关闭指定历史视频播放会话，释放会话状态。")
    async def api_history_video_close(req: CloseHistoryVideoRequest):
        session_id = str(req.sessionId or "").strip()
        if not session_id:
            return JSONResponse({"success": False, "message": "sessionId 不能为空"}, status_code=400)
        closed = await asyncio.to_thread(_close_history_session, session_id)
        if not closed:
            return JSONResponse({"success": False, "message": "历史视频会话不存在或已关闭"}, status_code=404)
        return JSONResponse({"success": True, "message": "ok"})

    async def _stream_mp4_file(path: Path, request: Request) -> Response:
        file_size = int(path.stat().st_size)
        range_header = str(request.headers.get("range") or "").strip().lower()
        start = 0
        end = file_size - 1
        status_code = 200
        if range_header.startswith("bytes="):
            spec = range_header.split("=", 1)[1].split(",", 1)[0].strip()
            left, _, right = spec.partition("-")
            try:
                if left:
                    start = max(0, int(left))
                if right:
                    end = min(file_size - 1, int(right))
                if start > end or start >= file_size:
                    return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
                status_code = 206
            except Exception:
                start = 0
                end = file_size - 1
                status_code = 200
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Disposition": "inline",
            "Content-Length": str(max(0, end - start + 1)),
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        if request.method.upper() == "HEAD":
            return Response(status_code=status_code, media_type="video/mp4", headers=headers)

        async def _iter_file():
            with path.open("rb") as fh:
                fh.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    if await request.is_disconnected():
                        break
                    chunk = await asyncio.to_thread(fh.read, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(_iter_file(), status_code=status_code, media_type="video/mp4", headers=headers)

    @app.api_route("/api/task/video", methods=["GET", "HEAD"], include_in_schema=False)
    async def api_task_video(request: Request, id: str = "", source: str = "source"):
        tid = str(id or "").strip()
        source_type = str(source or "source").strip().lower()
        if not tid or "-" not in tid:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id = tid.split("-", 1)[0].strip()
        try:
            sid = int(server_id)
        except Exception:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        paths = await asyncio.to_thread(_resolve_video_paths, sid)
        if source_type == "hls":
            hls_dir = paths.get("hls_dir")
            if not hls_dir or not hls_dir.exists():
                return JSONResponse({"ok": False, "message": "HLS文件不存在"}, status_code=404)
            playlist = await asyncio.to_thread(_build_task_hls_master_playlist, tid, hls_dir)
            if not playlist:
                return JSONResponse({"ok": False, "message": "HLS文件不存在"}, status_code=404)
            return Response(content=playlist, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        source_path = paths.get("source")
        playable = await asyncio.to_thread(_build_browser_playable_source, source_path) if source_path else None
        if playable and playable.exists():
            return await _stream_mp4_file(playable, request)
        return JSONResponse({"ok": False, "message": "MP4 文件不存在"}, status_code=404)

    @app.post("/api/task/video/close", include_in_schema=False)
    async def api_task_video_close():
        return JSONResponse({"ok": True})

    @app.get("/api/task/hls-media", include_in_schema=False)
    async def api_task_hls_media(id: str = ""):
        tid = str(id or "").strip()
        if not tid or "-" not in tid:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id = tid.split("-", 1)[0].strip()
        try:
            sid = int(server_id)
        except Exception:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        paths = await asyncio.to_thread(_resolve_video_paths, sid)
        hls_dir = paths.get("hls_dir")
        if not hls_dir or not hls_dir.exists():
            return JSONResponse({"ok": False, "message": "HLS文件不存在"}, status_code=404)
        playlist = await asyncio.to_thread(_rewrite_hls_media_playlist, tid, hls_dir)
        if not playlist:
            return JSONResponse({"ok": False, "message": "HLS文件不存在"}, status_code=404)
        return Response(content=playlist, media_type="application/vnd.apple.mpegurl")

    @app.get("/api/task/hls-seg", include_in_schema=False)
    async def api_task_hls_seg(id: str = "", file: str = ""):
        tid = str(id or "").strip()
        seg_name = str(file or "").strip()
        if not tid or "-" not in tid or not seg_name:
            return JSONResponse({"ok": False, "message": "bad params"}, status_code=400)
        server_id = tid.split("-", 1)[0].strip()
        try:
            sid = int(server_id)
        except Exception:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        paths = await asyncio.to_thread(_resolve_video_paths, sid)
        hls_dir = paths.get("hls_dir")
        if not hls_dir or not hls_dir.exists():
            return JSONResponse({"ok": False, "message": "HLS目录不存在"}, status_code=404)
        import re as _re
        safe_seg = _re.sub(r'[/\\]', '', seg_name)
        seg_path = hls_dir / safe_seg
        if not seg_path.exists():
            return JSONResponse({"ok": False, "message": "分片不存在"}, status_code=404)
        mt = "video/mp2t" if safe_seg.endswith(".ts") else "application/vnd.apple.mpegurl"
        return FileResponse(str(seg_path), media_type=mt, headers={"Content-Disposition": "inline"})

    @app.get("/api/task/subtitle-m3u8", include_in_schema=False)
    async def api_task_subtitle_m3u8(id: str = ""):
        tid = str(id or "").strip()
        if not tid or "-" not in tid:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id = tid.split("-", 1)[0].strip()
        try:
            sid = int(server_id)
        except Exception:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        paths = await asyncio.to_thread(_resolve_video_paths, sid)
        hls_dir = paths.get("hls_dir")
        if not hls_dir or not hls_dir.exists():
            return JSONResponse({"ok": False, "message": "字幕文件不存在"}, status_code=404)
        vtt = hls_dir / "subtitles.zh.vtt"
        if not vtt.exists():
            return JSONResponse({"ok": False, "message": "字幕文件不存在"}, status_code=404)
        duration = 0.0
        m3u8_file = hls_dir / "index.m3u8"
        if m3u8_file.exists():
            for line in m3u8_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("#EXTINF:"):
                    try:
                        duration += float(line.split(":")[1].rstrip(","))
                    except Exception:
                        pass
        if duration <= 0:
            duration = 7200.0
        vtt_url = f"/api/task/subtitle-vtt?id={tid}&source=hls"
        playlist = "#EXTM3U\n" f"#EXT-X-TARGETDURATION:{int(duration) + 1}\n" "#EXT-X-VERSION:3\n" "#EXT-X-MEDIA-SEQUENCE:0\n" "#EXT-X-PLAYLIST-TYPE:VOD\n" f"#EXTINF:{duration:.3f},\n" f"{vtt_url}\n" "#EXT-X-ENDLIST\n"
        return Response(content=playlist, media_type="application/vnd.apple.mpegurl")

    @app.get(
        "/api/task/subtitle-vtt",
        tags=["业务接口"],
        summary="获取字幕文件（VTT）",
        description=("按业务类型获取字幕 VTT 文件。" "当 `source=source` 时返回源视频对应字幕；" "当 `source=hls` 时返回 HLS 目录中的字幕。"),
        responses={200: {"description": "返回 VTT 字幕文件"}, 400: {"description": "参数格式错误"}, 404: {"description": "字幕文件不存在"}},
    )
    async def api_task_subtitle_vtt(
        id: str = Query(default="", description=("服务端任务标识，不是 lesson_id。格式固定为 `{server_task_id}-{STEP}`。" "示例：`4-DOWNLOAD` 表示任务 4 的源视频字幕；`4-TRANSCODE` 表示任务 4 的 HLS 字幕。"), examples={"source_subtitle": {"summary": "源视频字幕", "value": "4-DOWNLOAD"}, "hls_subtitle": {"summary": "HLS 字幕", "value": "4-TRANSCODE"}}),
        source: str = Query(default="hls", description="字幕来源类型。`source` 表示源视频字幕，`hls` 表示 HLS 字幕。", examples={"source": {"summary": "源视频字幕", "value": "source"}, "hls": {"summary": "HLS 字幕", "value": "hls"}}),
    ):
        tid = str(id or "").strip()
        if not tid or "-" not in tid:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        server_id = tid.split("-", 1)[0].strip()
        try:
            sid = int(server_id)
        except Exception:
            return JSONResponse({"ok": False, "message": "bad id"}, status_code=400)
        paths = await asyncio.to_thread(_resolve_video_paths, sid)
        if source == "hls":
            hls_dir = paths.get("hls_dir")
            if hls_dir and hls_dir.exists():
                vtt = hls_dir / "subtitles.zh.vtt"
                if vtt.exists():
                    return FileResponse(str(vtt), media_type="text/vtt", filename="subtitles.zh.vtt")
        else:
            src = paths.get("source")
            if src:
                vtt = src.parent / f"{src.stem}.zh.vtt"
                if vtt.exists():
                    return FileResponse(str(vtt), media_type="text/vtt", filename=vtt.name)
                for teacher_vtt in sorted(src.parent.glob("teacher_*.zh.vtt")):
                    if teacher_vtt.exists():
                        return FileResponse(str(teacher_vtt), media_type="text/vtt", filename=teacher_vtt.name)
        return JSONResponse({"ok": False, "message": "字幕文件不存在"}, status_code=404)

    @app.get("/api/files", include_in_schema=False)
    async def api_files(path: str = ""):
        fp = str(path or "").strip()
        if not fp:
            return JSONResponse({"ok": False, "message": "path required"}, status_code=400)
        fp = fp.replace("/", "\\") if "\\" in fp or ":" in fp else fp
        p = Path(fp)
        if not p.exists():
            return JSONResponse({"ok": False, "message": "file not found"}, status_code=404)
        if p.is_dir():
            files = []
            for f in sorted(p.iterdir()):
                files.append({"name": f.name, "isDir": f.is_dir(), "size": f.stat().st_size if f.is_file() else 0})
            return JSONResponse({"ok": True, "files": files})
        ext = p.suffix.lower()
        mt_map = {".mp4": "video/mp4", ".ts": "video/mp2t", ".m3u8": "application/vnd.apple.mpegurl", ".srt": "text/plain", ".vtt": "text/vtt", ".json": "application/json", ".wav": "audio/wav", ".html": "text/html", ".txt": "text/plain"}
        return FileResponse(str(p), media_type=mt_map.get(ext, "application/octet-stream"), filename=p.name)

    @app.get("/api/server-artifacts/{server_task_id}",
             tags=["业务接口"],
             summary="获取任务产物汇总信息",
             description=("返回某个服务端任务对应的业务产物访问地址汇总，"
                          "包括源视频、HLS 视频、源字幕、HLS 字幕、语音转写报告、视频分析报告等。"),
             responses={200: {"description": "返回产物地址汇总 JSON"}, 404: {"description": "未找到对应任务"}},
             )
    async def api_server_artifacts(server_task_id: int = ApiPath(..., description="服务端任务 ID，不是 lesson_id，也不是本地 edge_stream_task 表的自增主键。示例：`4`。", examples={"task_id": {"summary": "任务 4", "value": 4}})):
        row = await asyncio.to_thread(lambda: db.fetch_one("SELECT id FROM edge_stream_task WHERE server_task_id=? ORDER BY id DESC LIMIT 1", (int(server_task_id),)))
        if row is None:
            return JSONResponse({"ok": False, "message": "未找到任务"}, status_code=404)
        data = await asyncio.to_thread(_build_server_artifacts, int(server_task_id))
        return JSONResponse({"ok": True, "data": data})

    @app.get(
        "/api/analysis-report/{server_task_id}",
        response_class=HTMLResponse,
        tags=["业务接口"],
        summary="获取视频分析报告",
        description="返回指定服务端任务对应课次的视频分析 HTML 报告。报告按课次共享，不是按单个本地 task_id 存储。",
        responses={200: {"description": "返回 HTML 分析报告"}, 404: {"description": "未找到任务或分析报告尚未生成"}},
    )
    async def api_analysis_report(server_task_id: int = ApiPath(..., description="服务端任务 ID，不是 lesson_id。示例：`4`。", examples={"task_id": {"summary": "任务 4", "value": 4}})):
        task_row = await asyncio.to_thread(lambda: db.fetch_one("SELECT lesson_id, lesson_date, download_start FROM edge_stream_task WHERE server_task_id=? ORDER BY id DESC LIMIT 1", (int(server_task_id),)))
        if task_row is None:
            return HTMLResponse("<h2>未找到任务</h2>", status_code=404)
        lesson_id = str(task_row["lesson_id"] or "").strip()
        lesson_date = _resolve_lesson_date_util(task_row["lesson_date"], task_row["download_start"])
        report_html = _get_lesson_dir_util(lesson_date, lesson_id) / "report" / "report_web.html"
        if not report_html.exists():
            return HTMLResponse("<h2>分析报告尚未生成</h2>", status_code=404)
        return HTMLResponse(report_html.read_text(encoding="utf-8-sig", errors="ignore"))

    @app.get(
        "/api/speech-report/{server_task_id}",
        response_class=HTMLResponse,
        tags=["业务接口"],
        summary="获取语音转写报告",
        description="返回指定服务端任务的语音转写 HTML 报告，页面中会展示转写内容，并提供字幕文件访问入口。",
        responses={200: {"description": "返回 HTML 转写报告"}, 404: {"description": "未找到任务或转写报告尚未生成"}},
    )
    async def api_speech_report(server_task_id: int = ApiPath(..., description="服务端任务 ID，不是 lesson_id。示例：`4`。", examples={"task_id": {"summary": "任务 4", "value": 4}})):
        task_row = await asyncio.to_thread(lambda: db.fetch_one("SELECT t.id, t.lesson_id, t.task_type, t.relate_class, t.relate_lesson, s.output_file_path, s.start_time, s.end_time FROM edge_stream_task t JOIN edge_stream_task_step s ON s.task_id=t.id AND s.step_code='SPEECH' WHERE t.server_task_id=? ORDER BY t.id DESC LIMIT 1", (int(server_task_id),)))
        if task_row is None:
            return HTMLResponse("<h2>未找到转写任务</h2>", status_code=404)
        output_path = str(task_row["output_file_path"] or "").strip()
        relate_class = str(task_row["relate_class"] or "-")
        relate_lesson = str(task_row["relate_lesson"] or "-")
        start_time = str(task_row["start_time"] or "-")
        end_time = str(task_row["end_time"] or "-")
        lesson_id = int(task_row["lesson_id"] or 0)
        srt_content = ""
        vtt_url = ""
        srt_url = ""
        if output_path:
            p = Path(output_path)
            parent = p.parent if p.is_file() else p
            for f in sorted(parent.glob("*.zh.srt")):
                srt_url = f"/api/files?path={str(f).replace(chr(92), '/')}"
                try:
                    srt_content = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    srt_content = "(无法读取字幕文件)"
                break
            for f in sorted(parent.glob("*.zh.vtt")):
                vtt_url = f"/api/files?path={str(f).replace(chr(92), '/')}"
                break
        else:
            outputs = await asyncio.to_thread(lambda: db.fetch_all("SELECT file_type, file_path FROM edge_lesson_output WHERE lesson_id=? AND file_type IN ('subtitle_srt','subtitle_vtt')", (lesson_id,)))
            for o in (outputs or []):
                fp = str(o["file_path"] or "")
                ft = str(o["file_type"] or "")
                if ft == "subtitle_srt" and not srt_url:
                    srt_url = f"/api/files?path={fp.replace(chr(92), '/')}"
                    try:
                        srt_content = Path(fp).read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        srt_content = "(无法读取字幕文件)"
                if ft == "subtitle_vtt" and not vtt_url:
                    vtt_url = f"/api/files?path={fp.replace(chr(92), '/')}"
        import html as _html
        srt_escaped = _html.escape(srt_content or "(无转写内容)")
        report_html = f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\"/>
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>
<title>语音转写报告 - 任务 {server_task_id}</title>
<script src=\"https://cdn.tailwindcss.com\"></script>
</head>
<body class=\"bg-slate-50 min-h-screen\">
<div class=\"max-w-4xl mx-auto py-8 px-6\">
  <div class=\"flex items-center justify-between mb-6\">
    <h1 class=\"text-2xl font-bold text-slate-800\">语音转写报告</h1>
    <div class=\"flex items-center gap-3\">
      {f'<a href="{vtt_url}" target="_blank" class="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors">查看字幕文件 (VTT)</a>' if vtt_url else ''}
      {f'<a href="{srt_url}" target="_blank" class="px-4 py-2 bg-violet-600 text-white rounded-lg text-sm font-medium hover:bg-violet-700 transition-colors">查看字幕文件 (SRT)</a>' if srt_url else ''}
    </div>
  </div>
  <div class=\"bg-white rounded-xl border border-slate-200 p-6 mb-6\">
    <div class=\"grid grid-cols-2 gap-4 text-sm\">
      <div><span class=\"text-slate-500\">任务 ID：</span><span class=\"font-mono text-slate-800\">{server_task_id}</span></div>
      <div><span class=\"text-slate-500\">关联班级：</span><span class=\"text-slate-800\">{_html.escape(relate_class)}</span></div>
      <div><span class=\"text-slate-500\">关联课次：</span><span class=\"text-slate-800\">{_html.escape(relate_lesson)}</span></div>
      <div><span class=\"text-slate-500\">开始时间：</span><span class=\"font-mono text-slate-800\">{_html.escape(start_time)}</span></div>
      <div><span class=\"text-slate-500\">完成时间：</span><span class=\"font-mono text-slate-800\">{_html.escape(end_time)}</span></div>
    </div>
  </div>
  <div class=\"bg-white rounded-xl border border-slate-200 p-6\">
    <h2 class=\"text-lg font-semibold text-slate-800 mb-4\">转写内容</h2>
    <pre class=\"whitespace-pre-wrap text-sm text-slate-700 leading-relaxed font-mono bg-slate-50 rounded-lg p-4 max-h-[600px] overflow-auto border border-slate-100\">{srt_escaped}</pre>
  </div>
</div>
</body>
</html>"""
        return HTMLResponse(report_html)

    app.include_router(
        create_ops_router(
            db=db,
            runner=runner,
            state=state,
            metrics=metrics,
            load_monitor_cfg=_load_monitor_cfg,
            save_monitor_cfg=_save_monitor_cfg,
            service_version_info=service_version_info,
            concurrency_model=ConcurrencyConfig,
            open_folder_model=OpenFolderRequest,
            select_folder_model=SelectFolderRequest,
        )
    )

    app.include_router(
        create_stream_router(
            load_monitor_cfg=_load_monitor_cfg,
            session_manager=stream_session_manager,
        )
    )

    app.include_router(create_update_routes())

    return app


def create_app() -> FastAPI:
    cfg = load_config()
    return build_app(cfg)
