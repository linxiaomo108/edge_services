from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from ..utils import load_package_capabilities, load_package_mode


class _OpenFolderRequest(BaseModel):
    path: str


class _SelectFolderRequest(BaseModel):
    startPath: str = r"D:\Videos"


class _ForceBackfillTranscodeRequest(BaseModel):
    targets: list[dict[str, Any]]


class _BackfillOldUrlHostsRequest(BaseModel):
    limit: int = 200


def create_ops_router(
    *,
    db,
    runner,
    state,
    metrics,
    load_monitor_cfg,
    save_monitor_cfg,
    service_version_info,
    concurrency_model: type[Any],
    open_folder_model: type[Any],
    select_folder_model: type[Any],
) -> APIRouter:
    router = APIRouter()
    ConcurrencyConfig = concurrency_model

    def _as_bool(value: Any, default: bool) -> bool:
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

    @router.post("/api/poll-sync")
    async def api_poll_sync():
        try:
            await runner.poll_now()
            snap = await state.snapshot()
            if snap.last_error:
                return JSONResponse({"ok": False, "message": str(snap.last_error)})
            return JSONResponse({"ok": True, "message": "同步完成"})
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)})

    @router.post("/api/report/backfill-completed")
    async def api_backfill_completed_reports(limit: int = 200):
        try:
            safe_limit = max(1, min(int(limit or 200), 1000))
            result = await runner.backfill_completed_step_reports(safe_limit)
            return JSONResponse({"ok": True, **result})
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)})

    @router.post("/api/report/backfill-transcode")
    async def api_backfill_transcode_reports(req: _ForceBackfillTranscodeRequest):
        try:
            targets = req.targets if isinstance(req.targets, list) else []
            result = await runner.force_backfill_transcode_reports(targets)
            return JSONResponse({"ok": True, **result})
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)})

    @router.get("/api/report/backfill-old-url-hosts")
    async def api_backfill_old_url_hosts_page():
        html = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>历史上报 URL 修复</title>
  <style>
    body{font-family:Arial,"Microsoft YaHei",sans-serif;margin:24px;background:#f7f8fa;color:#1f2937}
    h1{font-size:22px;margin:0 0 12px}
    .card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
    button{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:9px 14px;cursor:pointer;margin-right:8px}
    button.secondary{background:#4b5563} button.danger{background:#dc2626} button:disabled{background:#9ca3af;cursor:not-allowed}
    input{border:1px solid #d1d5db;border-radius:8px;padding:8px;width:80px}
    table{width:100%;border-collapse:collapse;background:#fff;font-size:13px}
    th,td{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;vertical-align:top}
    th{background:#f3f4f6;position:sticky;top:0}
    code{word-break:break-all;white-space:pre-wrap}
    .ok{color:#059669;font-weight:bold}.fail{color:#dc2626;font-weight:bold}.pending{color:#d97706;font-weight:bold}
    .muted{color:#6b7280}.rules{white-space:pre-wrap;background:#111827;color:#d1d5db;padding:10px;border-radius:8px;overflow:auto}
  </style>
</head>
<body>
  <h1>历史上报 URL 修复</h1>
  <div class="card">
    <div>默认访问：<code>http://127.0.0.1:18080/api/report/backfill-old-url-hosts</code>，也可将 <code>127.0.0.1</code> 换成边缘服务自己的 IP。</div>
    <div class="muted">页面打开后自动从本地库查询命中记录；点击确认更新后，先上报中心成功，再更新本地记录。</div>
  </div>
  <div class="card">
    <label>每次最多处理 <input id="limit" type="number" min="1" max="1000" value="200" /> 条</label>
    <button onclick="preview()">刷新列表</button>
    <button class="danger" id="executeBtn" onclick="executeUpdate()" disabled>确认更新</button>
    <span id="summary" class="muted"></span>
  </div>
  <div class="card">
    <strong>当前生效规则</strong>
    <pre id="rules" class="rules">加载中...</pre>
  </div>
  <div class="card">
    <table>
      <thead><tr><th>状态</th><th>serverTaskId</th><th>步骤</th><th>原 URL</th><th>新 URL</th><th>消息</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
<script>
async function preview(){
  const limit = document.getElementById('limit').value || 200;
  document.getElementById('summary').textContent = '查询中...';
  const res = await fetch(`/api/report/backfill-old-url-hosts/preview?limit=${encodeURIComponent(limit)}`);
  const data = await res.json();
  render(data);
}
function render(data){
  document.getElementById('rules').textContent = JSON.stringify(data.configuredRules || [], null, 2);
  const items = data.items || [];
  document.getElementById('summary').textContent = `开关=${data.enabled ? '开启' : '关闭或无有效规则'}，命中 ${items.length} 条`;
  document.getElementById('executeBtn').disabled = !data.enabled || items.length === 0;
  const tbody = document.getElementById('rows');
  tbody.innerHTML = '';
  for(const item of items){
    const cls = item.status === 'updated' ? 'ok' : (item.status === 'failed' ? 'fail' : 'pending');
    const tr = document.createElement('tr');
    tr.innerHTML = `<td class="${cls}">${escapeHtml(item.status || '')}</td>
      <td>${escapeHtml(String(item.serverTaskId || ''))}</td>
      <td>${escapeHtml(item.stepCode || '')}</td>
      <td><code>${escapeHtml(item.oldOutputFileUrl || '')}</code></td>
      <td><code>${escapeHtml(item.newOutputFileUrl || '')}</code></td>
      <td>${escapeHtml(item.message || '')}</td>`;
    tbody.appendChild(tr);
  }
}
async function executeUpdate(){
  if(!confirm('确认将命中的历史 URL 上报到中心，并在中心成功后更新本地记录？')) return;
  const btn = document.getElementById('executeBtn');
  btn.disabled = true;
  document.getElementById('summary').textContent = '更新中...';
  const limit = Number(document.getElementById('limit').value || 200);
  const res = await fetch('/api/report/backfill-old-url-hosts/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({limit})});
  const data = await res.json();
  render(data);
}
function escapeHtml(s){return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
preview();
</script>
</body>
</html>
        """.strip()
        return HTMLResponse(html)

    @router.get("/api/report/backfill-old-url-hosts/preview")
    async def api_preview_backfill_old_url_hosts(limit: int = 200):
        try:
            safe_limit = max(1, min(int(limit or 200), 1000))
            result = await runner.preview_report_url_rewrite_backfill(safe_limit)
            return JSONResponse({"ok": True, **result})
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)})

    @router.post("/api/report/backfill-old-url-hosts/execute")
    async def api_execute_backfill_old_url_hosts(req: _BackfillOldUrlHostsRequest):
        try:
            safe_limit = max(1, min(int(req.limit or 200), 1000))
            result = await runner.execute_report_url_rewrite_backfill(safe_limit)
            return JSONResponse({"ok": True, **result})
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)})

    @router.post("/api/report/backfill-old-url-hosts")
    async def api_backfill_old_url_hosts_execute_compat(req: _BackfillOldUrlHostsRequest):
        try:
            safe_limit = max(1, min(int(req.limit or 200), 1000))
            result = await runner.execute_report_url_rewrite_backfill(safe_limit)
            return JSONResponse({"ok": True, **result})
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)})

    @router.post("/api/reset-db")
    async def api_reset_db():
        try:
            def _do_clear():
                with db.connect() as conn:
                    table_rows = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                    table_names = [str(row["name"] or "").strip() for row in table_rows if str(row["name"] or "").strip()]
                    for table_name in table_names:
                        conn.execute(f'DELETE FROM "{table_name}"')
                    if table_names:
                        placeholders = ",".join("?" for _ in table_names)
                        conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", table_names)
                    conn.commit()
            await state.set_error(None)
            await runner.reset_local_data_and_repoll(_do_clear)
            snap = await state.snapshot()
            if snap.last_error:
                return JSONResponse({
                    "ok": True,
                    "repolled": False,
                    "message": f"数据库已清空；立即重拉失败，系统将后台自动重试: {snap.last_error}",
                })
            return JSONResponse({"ok": True, "repolled": True, "message": "数据库已清空并重新拉取"})
        except Exception as exc:
            return JSONResponse({"ok": False, "message": str(exc)})

    @router.get("/api/concurrency")
    async def api_get_concurrency():
        cfg_data = load_monitor_cfg()
        raw = cfg_data.get("concurrency") if isinstance(cfg_data, dict) else None
        base = {"download": 2, "transcode": 2, "asr": 2, "subtitle": 2, "analysis": 2}
        if isinstance(raw, dict):
            for key in list(base.keys()):
                try:
                    value = int(raw.get(key))
                    if value >= 0:
                        base[key] = value
                except Exception:
                    continue
        package_mode = load_package_mode()
        package_caps = load_package_capabilities()
        if not package_caps.get("speech", True):
            base["asr"] = 0
        if not package_caps.get("subtitle", True):
            base["subtitle"] = 0
        task_control_raw = cfg_data.get("taskControl") if isinstance(cfg_data.get("taskControl"), dict) else {}
        task_control = {
            "download": bool(_as_bool(task_control_raw.get("download", True), True) and package_caps.get("download", True)),
            "transcode": bool(_as_bool(task_control_raw.get("transcode", True), True) and package_caps.get("transcode", True)),
            "speech": bool(_as_bool(task_control_raw.get("speech", True), True) and package_caps.get("speech", True)),
            "subtitle": bool(_as_bool(task_control_raw.get("subtitle", True), True) and package_caps.get("subtitle", True)),
            "analysis": bool(_as_bool(task_control_raw.get("analysis", True), True) and package_caps.get("analysis", True)),
        }
        download_path = str(cfg_data.get("downloadPath") or r"D:\Videos")
        try:
            bind_port = int(cfg_data.get("bindPort") or 18080)
        except Exception:
            bind_port = 18080
        speech_model = str(cfg_data.get("speechModel") or "medium").strip().lower()
        if speech_model not in {"small", "medium", "large", "large-v3"}:
            speech_model = "medium"
        speech_prompt_mode = str(cfg_data.get("speechPromptMode") or "off").strip().lower()
        if speech_prompt_mode not in {"off", "auto", "custom"}:
            speech_prompt_mode = "off"
        speech_vad_mode = str(cfg_data.get("speechVadMode") or "builtin").strip().lower()
        if speech_vad_mode not in {"builtin", "silero", "off"}:
            speech_vad_mode = "builtin"
        speech_prompt_text = str(cfg_data.get("speechPromptText") or "")
        speech_word_timestamps = _as_bool(cfg_data.get("speechWordTimestamps") if "speechWordTimestamps" in cfg_data else True, True)
        speech_hallucination_filter = _as_bool(cfg_data.get("speechHallucinationFilter") if "speechHallucinationFilter" in cfg_data else True, True)
        try:
            speech_temperature = float(cfg_data.get("speechTemperature", 0.0) or 0.0)
        except Exception:
            speech_temperature = 0.0
        try:
            speech_retry_temperature = float(cfg_data.get("speechRetryTemperature", 0.4) or 0.4)
        except Exception:
            speech_retry_temperature = 0.4
        execution_mode = str(cfg_data.get("executionMode") or "manual").strip().lower()
        if execution_mode not in {"manual", "auto"}:
            execution_mode = "manual"
        enable_stream_proxy = _as_bool(cfg_data.get("enableStreamProxy") if "enableStreamProxy" in cfg_data else False, False)
        public_base_url = str(cfg_data.get("publicBaseUrl") or "").strip()
        public_host = str(cfg_data.get("publicHost") or "").strip()
        public_port = str(cfg_data.get("publicPort") or "").strip()
        public_scheme = str(cfg_data.get("publicScheme") or "").strip().lower()
        return JSONResponse({
            "ok": True,
            "concurrency": base,
            "downloadPath": download_path,
            "bindPort": max(1, min(65535, bind_port)),
            "taskControl": task_control,
            "speechModel": speech_model,
            "speechWordTimestamps": speech_word_timestamps,
            "speechPromptMode": speech_prompt_mode,
            "speechVadMode": speech_vad_mode,
            "speechPromptText": speech_prompt_text,
            "speechHallucinationFilter": speech_hallucination_filter,
            "speechTemperature": max(0.0, min(1.0, speech_temperature)),
            "speechRetryTemperature": max(0.0, min(1.0, speech_retry_temperature)),
            "executionMode": execution_mode,
            "enableStreamProxy": enable_stream_proxy,
            "publicBaseUrl": public_base_url,
            "publicHost": public_host,
            "publicPort": public_port,
            "publicScheme": public_scheme,
            "packageMode": package_mode,
            "packageCapabilities": package_caps,
        })

    @router.post("/api/concurrency")
    async def api_set_concurrency(req: dict[str, Any] = Body(...)):
        cfg_data = load_monitor_cfg()
        req = req if isinstance(req, dict) else {}
        package_mode = load_package_mode()
        package_caps = load_package_capabilities()
        cfg_data["concurrency"] = {
            "download": max(0, int(req.get("download", 2) or 0)),
            "transcode": max(0, int(req.get("transcode", 2) or 0)),
            "asr": max(0, int(req.get("asr", 2) or 0)) if package_caps.get("speech", True) else 0,
            "subtitle": max(0, int(req.get("subtitle", 2) or 0)) if package_caps.get("subtitle", True) else 0,
            "analysis": max(0, int(req.get("analysis", 2) or 0)),
        }
        download_path = str(req.get("downloadPath") or "").strip() or r"D:\Videos"
        try:
            os.makedirs(download_path, exist_ok=True)
        except Exception:
            return JSONResponse({"ok": False, "message": "下载目录不可用，请更换到有权限的路径"})
        cfg_data["downloadPath"] = download_path
        try:
            bind_port = int(req.get("bindPort", cfg_data.get("bindPort", 18080)) or 18080)
        except Exception:
            bind_port = 18080
        cfg_data["bindPort"] = max(1, min(65535, bind_port))
        task_control_raw = req.get("taskControl") if isinstance(req.get("taskControl"), dict) else {}
        cfg_data["taskControl"] = {
            "download": bool(_as_bool(task_control_raw.get("download", True), True) and package_caps.get("download", True)),
            "transcode": bool(_as_bool(task_control_raw.get("transcode", True), True) and package_caps.get("transcode", True)),
            "speech": bool(_as_bool(task_control_raw.get("speech", True), True) and package_caps.get("speech", True)),
            "subtitle": bool(_as_bool(task_control_raw.get("subtitle", True), True) and package_caps.get("subtitle", True)),
            "analysis": bool(_as_bool(task_control_raw.get("analysis", True), True) and package_caps.get("analysis", True)),
        }
        speech_model = str(req.get("speechModel") or "medium").strip().lower()
        if speech_model not in {"small", "medium", "large", "large-v3"}:
            speech_model = "medium"
        cfg_data["speechModel"] = speech_model
        speech_prompt_mode = str(req.get("speechPromptMode") or "off").strip().lower()
        if speech_prompt_mode not in {"off", "auto", "custom"}:
            speech_prompt_mode = "off"
        cfg_data["speechPromptMode"] = speech_prompt_mode
        speech_vad_mode = str(req.get("speechVadMode") or "builtin").strip().lower()
        if speech_vad_mode not in {"builtin", "silero", "off"}:
            speech_vad_mode = "builtin"
        cfg_data["speechVadMode"] = speech_vad_mode
        cfg_data["speechPromptText"] = str(req.get("speechPromptText") or "")[:400]
        cfg_data["speechWordTimestamps"] = _as_bool(req.get("speechWordTimestamps", True), True)
        cfg_data["speechHallucinationFilter"] = _as_bool(req.get("speechHallucinationFilter", True), True)
        try:
            speech_temperature = float(req.get("speechTemperature", 0.0) or 0.0)
        except Exception:
            speech_temperature = 0.0
        try:
            speech_retry_temperature = float(req.get("speechRetryTemperature", 0.4) or 0.4)
        except Exception:
            speech_retry_temperature = 0.4
        cfg_data["speechTemperature"] = max(0.0, min(1.0, speech_temperature))
        cfg_data["speechRetryTemperature"] = max(0.0, min(1.0, speech_retry_temperature))
        execution_mode = str(req.get("executionMode") or "manual").strip().lower()
        if execution_mode not in {"manual", "auto"}:
            execution_mode = "manual"
        cfg_data["executionMode"] = execution_mode
        cfg_data["enableStreamProxy"] = _as_bool(req.get("enableStreamProxy", False), False)
        cfg_data["publicBaseUrl"] = str(req.get("publicBaseUrl") or "").strip()
        cfg_data["publicHost"] = str(req.get("publicHost") or "").strip()
        cfg_data["publicPort"] = str(req.get("publicPort") or "").strip()
        public_scheme = str(req.get("publicScheme") or "").strip().lower()
        if public_scheme not in {"", "http", "https"}:
            public_scheme = ""
        cfg_data["publicScheme"] = public_scheme
        cfg_data["packageMode"] = package_mode
        cfg_data["packageCapabilities"] = package_caps
        save_monitor_cfg(cfg_data)
        return JSONResponse({"ok": True})

    @router.post("/api/open-folder")
    async def api_open_folder(req: _OpenFolderRequest):
        path = str(req.path or "").strip()
        if not path:
            return JSONResponse({"ok": False}, status_code=400)
        try:
            path = os.path.abspath(path)
            os.makedirs(path, exist_ok=True)
            try:
                os.startfile(path)
            except Exception:
                subprocess.Popen(["explorer", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return JSONResponse({"ok": True})
        except Exception:
            return JSONResponse({"ok": False})

    @router.post("/api/list-folder")
    async def api_list_folder(req: _OpenFolderRequest):
        path = str(req.path or "").strip()
        if not path:
            return JSONResponse({"ok": False}, status_code=400)
        try:
            path = os.path.abspath(path)
            os.makedirs(path, exist_ok=True)
            items = []
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir()
                    except Exception:
                        is_dir = False
                    size = 0
                    if not is_dir:
                        try:
                            size = int(entry.stat().st_size)
                        except Exception:
                            size = 0
                    items.append({"name": entry.name, "isDir": is_dir, "size": size})
            items.sort(key=lambda item: (0 if item["isDir"] else 1, item["name"].lower()))
            return JSONResponse({"ok": True, "items": items})
        except Exception:
            return JSONResponse({"ok": False})

    @router.post("/api/select-folder")
    async def api_select_folder(req: _SelectFolderRequest):
        try:
            import tkinter as tk
            from tkinter import filedialog

            start = str(req.startPath or "").strip() or r"D:\Videos"
            start = os.path.abspath(start)
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(initialdir=start)
            try:
                root.destroy()
            except Exception:
                pass
            selected = str(selected or "").strip()
            if not selected:
                return JSONResponse({"ok": False})
            return JSONResponse({"ok": True, "path": os.path.abspath(selected)})
        except Exception:
            return JSONResponse({"ok": False})

    @router.get("/api/metrics")
    async def api_metrics():
        snap = await metrics.snapshot()
        return JSONResponse({"cpu": snap.cpu_percent, "ram": snap.ram_percent, "disk": snap.disk_percent, "gpu": snap.gpu_percent})

    @router.get("/api/local-ip")
    async def api_local_ip():
        ip = runner._get_local_ip() if runner else "127.0.0.1"
        return JSONResponse({"ok": True, "ip": ip})

    @router.get("/api/version")
    async def api_version():
        payload = dict(service_version_info or {})
        raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else None
        if raw is not None:
            payload["raw"] = raw
        return JSONResponse({"ok": True, **payload})

    @router.get("/api/db/stats")
    async def api_db_stats():
        tasks_cnt = await asyncio.to_thread(lambda: (db.fetch_one("SELECT COUNT(*) AS c FROM edge_stream_task") or {"c": 0})["c"])
        steps_cnt = await asyncio.to_thread(lambda: (db.fetch_one("SELECT COUNT(*) AS c FROM edge_stream_task_step") or {"c": 0})["c"])
        return JSONResponse({"ok": True, "tasks": int(tasks_cnt), "steps": int(steps_cnt), "dbPath": db.path})

    @router.get("/api/db/task/{server_task_id}")
    async def api_db_task(server_task_id: int):
        task = await asyncio.to_thread(
            lambda: db.fetch_one(
                """
SELECT
  id,
  server_task_id,
  task_kind,
  task_type,
  lesson_id,
  lesson_date,
  download_start,
  download_end,
  task_status,
  current_step,
  process_rate,
  nvr_device_id,
  nvr_channel_num,
  nvr_channel_id,
  nvr_ip,
  nvr_port,
  nvr_account,
  nvr_password,
  relate_class,
  relate_lesson,
  created_time,
  updated_time
FROM edge_stream_task
WHERE server_task_id=?
ORDER BY id DESC
LIMIT 1
                """.strip(),
                (int(server_task_id),),
            )
        )
        if task is None:
            return JSONResponse({"ok": False, "message": "not found"}, status_code=404)
        task_db_id = int(task["id"])
        steps = await asyncio.to_thread(
            lambda: [dict(row) for row in db.fetch_all("SELECT step_code, step_status, step_process, start_time, end_time, output_file_path FROM edge_stream_task_step WHERE task_id=? ORDER BY id ASC", (task_db_id,))]
        )
        logs = await asyncio.to_thread(
            lambda: [dict(row) for row in db.fetch_all("SELECT id, step_code, log_level, message, created_time FROM edge_task_log WHERE task_id=? ORDER BY id DESC LIMIT 80", (task_db_id,))]
        )
        return JSONResponse({"ok": True, "task": dict(task), "steps": steps, "logs": logs, "dbPath": db.path})

    @router.get("/api/state")
    async def api_state():
        data = await state.to_public_dict()

        def _query_last_report():
            try:
                row = db.fetch_one(
                    "SELECT last_success_time FROM edge_step_report_state WHERE last_success_time IS NOT NULL AND last_success_time != '' ORDER BY last_success_time DESC LIMIT 1"
                )
                return str(row["last_success_time"]) if row else None
            except Exception:
                return None

        last_report_at = await asyncio.to_thread(_query_last_report)
        if last_report_at:
            data["lastReportAt"] = last_report_at
        return JSONResponse(data)

    return router
