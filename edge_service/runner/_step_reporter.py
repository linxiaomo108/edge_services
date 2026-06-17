from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..constants import StepStatus
from ..db import ensure_bjt_iso
from ..monitor_config import load_monitor_cfg as _load_monitor_cfg
from ..utils import get_lesson_dir as _get_lesson_dir, load_download_path as _load_download_path, resolve_lesson_date as _resolve_lesson_date


class StepReporterMixin:
    """上报相关方法：构建/发送步骤上报 payload，管理脏状态，触发上报。"""

    def _step_report_runtime_state(self) -> dict[str, Any]:
        state = getattr(self, "_step_report_runtime", None)
        if isinstance(state, dict):
            return state
        state = {"inflight": set(), "last_logged": {}, "log_interval_sec": 20.0}
        setattr(self, "_step_report_runtime", state)
        return state

    def _report_base_url(self) -> str:
        cfg = _load_monitor_cfg() or {}
        public_base_url = str(cfg.get("publicBaseUrl") or "").strip().rstrip("/")
        if public_base_url:
            return public_base_url
        public_host = str(cfg.get("publicHost") or "").strip()
        if public_host:
            public_scheme = str(cfg.get("publicScheme") or "").strip().lower() or "http"
            public_port = str(cfg.get("publicPort") or "").strip()
            if public_port:
                return f"{public_scheme}://{public_host}:{public_port}".rstrip("/")
            return f"{public_scheme}://{public_host}".rstrip("/")
        ip = self._get_local_ip()
        port = self._cfg.bind_port
        runtime = self._step_report_runtime_state()
        now_sec = time.monotonic()
        last_warn_sec = float(runtime.get("last_local_base_url_warn_sec") or 0.0)
        if now_sec - last_warn_sec >= 60.0:
            runtime["last_local_base_url_warn_sec"] = now_sec
            self._log.warning(
                "步骤产物上报 URL 未配置公网地址，已回退到本机地址 baseUrl=http://%s:%s enableStreamProxy=%s publicBaseUrl=%s publicHost=%s publicPort=%s",
                ip,
                port,
                cfg.get("enableStreamProxy"),
                public_base_url,
                public_host,
                str(cfg.get("publicPort") or "").strip(),
            )
        return f"http://{ip}:{port}"

    def _build_file_url(self, local_path: str) -> str:
        encoded = urllib.parse.quote(str(local_path), safe="")
        return f"{self._report_base_url()}/api/files?path={encoded}"

    def _build_task_video_url(self, server_task_id: int, step_code: str) -> str | None:
        step = str(step_code or "").strip().upper()
        if step == "DOWNLOAD":
            return f"{self._report_base_url()}/api/task/video?id={int(server_task_id)}-DOWNLOAD&source=source"
        if step == "TRANSCODE":
            return f"{self._report_base_url()}/api/task/video?id={int(server_task_id)}-TRANSCODE&source=hls"
        return None

    def _extract_report_output_urls(self, value: Any) -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except Exception:
                data = None
            if isinstance(data, dict):
                urls: list[str] = []
                for item in data.values():
                    urls.extend(self._extract_report_output_urls(item))
                return urls
        if text.startswith("http://") or text.startswith("https://"):
            return [text]
        return []

    def _replace_report_output_url_base(self, value: Any, old_base_url: str, new_base_url: str) -> tuple[Any, list[dict[str, str]]]:
        old_base = str(old_base_url or "").strip().rstrip("/")
        new_base = str(new_base_url or "").strip().rstrip("/")
        if not old_base or not new_base:
            return value, []
        text = str(value or "").strip()
        if not text:
            return value, []
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except Exception:
                data = None
            if isinstance(data, dict):
                changed = False
                replacements: list[dict[str, str]] = []
                new_data: dict[str, Any] = {}
                for key, item in data.items():
                    new_item, item_replacements = self._replace_report_output_url_base(item, old_base, new_base)
                    new_data[key] = new_item
                    if item_replacements:
                        changed = True
                        replacements.extend(item_replacements)
                if changed:
                    return json.dumps(new_data, ensure_ascii=False), replacements
                return value, []
        if text.startswith(old_base):
            new_value = new_base + text[len(old_base):]
            return new_value, [{"oldUrl": text, "newUrl": new_value}]
        return value, []

    def _report_output_url_hosts(self, value: Any) -> str:
        hosts: list[str] = []
        seen: set[str] = set()
        for url in self._extract_report_output_urls(value):
            try:
                host = urllib.parse.urlsplit(str(url)).netloc
            except Exception:
                host = ""
            if host and host not in seen:
                seen.add(host)
                hosts.append(host)
        return ",".join(hosts)

    def _normalize_report_url_hosts(self, hosts: list[Any]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in hosts or []:
            text = str(raw or "").strip()
            if not text:
                continue
            if "://" in text:
                try:
                    text = urllib.parse.urlsplit(text).netloc
                except Exception:
                    text = ""
            text = text.strip().strip("/")
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result

    def _report_url_host_from_config_value(self, base_url: Any = None) -> str:
        base_text = str(base_url or "").strip()
        if base_text:
            try:
                return urllib.parse.urlsplit(base_text if "://" in base_text else f"http://{base_text}").netloc.strip()
            except Exception:
                return ""
        return ""

    def _report_base_url_from_config_value(self, base_url: Any = None) -> str:
        base_text = str(base_url or "").strip().rstrip("/")
        if base_text:
            return base_text if "://" in base_text else f"http://{base_text}"
        return ""

    def _configured_report_backfill_rules(self) -> list[dict[str, Any]]:
        cfg = _load_monitor_cfg() or {}
        report_backfill = cfg.get("reportBackfill") if isinstance(cfg.get("reportBackfill"), dict) else {}
        if not bool(report_backfill.get("enabled")):
            return []
        raw_rules = report_backfill.get("urlHostReplacements") if isinstance(report_backfill.get("urlHostReplacements"), list) else []
        rules: list[dict[str, Any]] = []
        for raw in raw_rules:
            if not isinstance(raw, dict):
                continue
            old_base_url = self._report_base_url_from_config_value(raw.get("oldBaseUrl"))
            new_base_url = self._report_base_url_from_config_value(raw.get("newBaseUrl"))
            old_host = self._report_url_host_from_config_value(old_base_url)
            new_host = self._report_url_host_from_config_value(new_base_url)
            if not old_host or not new_host or not new_base_url:
                continue
            steps = raw.get("stepCodes") if isinstance(raw.get("stepCodes"), list) else None
            rules.append({
                "oldBaseUrl": old_base_url,
                "oldHost": old_host,
                "newBaseUrl": new_base_url,
                "newHost": new_host,
                "stepCodes": [str(step or "").strip().upper() for step in (steps or []) if str(step or "").strip()],
            })
        return rules

    def _get_step_db_info(self, task_db_id: int, step_code: str) -> dict[str, Any]:
        """从数据库获取步骤的详细信息，用于上报"""
        with self._db.connect() as conn:
            task_row = conn.execute(
                "SELECT server_task_id, task_type, lesson_id, lesson_date FROM edge_stream_task WHERE id=? LIMIT 1",
                (int(task_db_id),),
            ).fetchone()
            if task_row is None:
                return {}
            step_row = conn.execute(
                "SELECT step_status, step_process, start_time, end_time, output_file_path FROM edge_stream_task_step WHERE task_id=? AND step_code=? LIMIT 1",
                (int(task_db_id), str(step_code)),
            ).fetchone()
            if step_row is None:
                return {}
            return {
                "server_task_id": int(task_row["server_task_id"]),
                "task_type": int(task_row["task_type"] or 0),
                "lesson_id": int(task_row["lesson_id"] or 0),
                "lesson_date": str(task_row["lesson_date"] or ""),
                "step_status": int(step_row["step_status"] or 0),
                "step_process": int(step_row["step_process"] or 0),
                "start_time": str(step_row["start_time"] or "") or None,
                "end_time": str(step_row["end_time"] or "") or None,
                "output_file_path": str(step_row["output_file_path"] or "") or None,
            }

    def _list_step_artifacts(self, task_db_id: int) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            task_row = conn.execute(
                "SELECT server_task_id, lesson_id FROM edge_stream_task WHERE id=? LIMIT 1",
                (int(task_db_id),),
            ).fetchone()
            if task_row is None:
                return []
            rows = conn.execute(
                "SELECT file_type, file_path, file_size FROM edge_lesson_output WHERE lesson_id=? AND server_task_id=?",
                (int(task_row["lesson_id"] or 0), int(task_row["server_task_id"] or 0)),
            ).fetchall()
            return [
                {
                    "file_type": str(r["file_type"] or ""),
                    "file_path": str(r["file_path"] or ""),
                    "file_size": int(r["file_size"] or 0),
                }
                for r in (rows or [])
            ]

    def _get_task_report_context(self, task_db_id: int) -> dict[str, Any]:
        raw = self._load_task_raw_by_dbid(int(task_db_id)) or {}
        lesson_id = str(raw.get("lessonId") or "").strip()
        if not lesson_id:
            return {}
        lesson_date = _resolve_lesson_date(raw.get("lessonDate"), raw.get("lessonStartAt"))
        lesson_dir = _get_lesson_dir(lesson_date, lesson_id, Path(_load_download_path()))
        return {
            "raw": raw,
            "lesson_dir": lesson_dir,
        }

    def _has_required_report_artifact(self, step_code: str, artifacts: list[dict[str, Any]]) -> bool:
        step = str(step_code or "").strip().upper()
        if step == "SPEECH":
            return any(a.get("file_type") == "speech_report_html" and a.get("file_path") for a in artifacts)
        if step == "SUBTITLE":
            return any(a.get("file_type") == "subtitle_srt" and a.get("file_path") for a in artifacts)
        if step == "ANALYSIS":
            return any(a.get("file_type") in {"report_web_html", "report_h5_html"} and a.get("file_path") for a in artifacts)
        return True

    def _discover_step_artifacts_from_disk(self, task_db_id: int, step_code: str) -> list[dict[str, Any]]:
        ctx = self._get_task_report_context(task_db_id)
        lesson_dir = ctx.get("lesson_dir")
        if not lesson_dir or not Path(lesson_dir).exists():
            return []
        lesson_dir = Path(lesson_dir)
        step = str(step_code or "").strip().upper()
        artifacts: list[dict[str, Any]] = []

        def _append(fp: Path, file_type: str) -> None:
            if fp.exists() and fp.is_file():
                artifacts.append({
                    "path": str(fp),
                    "sizeBytes": int(fp.stat().st_size),
                    "stepCode": step,
                    "fileType": file_type,
                })

        teacher_video = next((f for f in sorted(lesson_dir.glob("teacher_*.mp4")) if f.is_file() and f.stat().st_size > 0 and ".part" not in f.name.lower() and "_nosub" not in f.name.lower() and "_sub_tmp" not in f.name.lower() and "_temp_embed" not in f.name.lower()), None)
        if step == "SPEECH":
            if teacher_video is not None:
                _append(teacher_video.parent / f"{teacher_video.stem}.zh.srt", "subtitle_srt")
                _append(teacher_video.parent / f"{teacher_video.stem}.zh.vtt", "subtitle_vtt")
                _append(teacher_video.parent / f"{teacher_video.stem}.speech.html", "speech_report_html")
            else:
                speech_html = next((f for f in sorted(lesson_dir.glob("teacher_*.speech.html")) if f.is_file()), None)
                speech_srt = next((f for f in sorted(lesson_dir.glob("teacher_*.zh.srt")) if f.is_file()), None)
                speech_vtt = next((f for f in sorted(lesson_dir.glob("teacher_*.zh.vtt")) if f.is_file()), None)
                if speech_srt is not None:
                    _append(speech_srt, "subtitle_srt")
                if speech_vtt is not None:
                    _append(speech_vtt, "subtitle_vtt")
                if speech_html is not None:
                    _append(speech_html, "speech_report_html")
            return artifacts
        if step == "SUBTITLE":
            if teacher_video is not None:
                _append(teacher_video.parent / f"{teacher_video.stem}.zh.srt", "subtitle_srt")
            else:
                srt = next((f for f in sorted(lesson_dir.glob("*.zh.srt")) if f.is_file() and f.stat().st_size >= 0), None)
                if srt is not None:
                    _append(srt, "subtitle_srt")
            return artifacts
        if step == "ANALYSIS":
            report_dir = lesson_dir / "report"
            _append(report_dir / "report_web.html", "report_web_html")
            _append(report_dir / "report_h5.html", "report_h5_html")
            _append(report_dir / "report_data.json", "report_data")
            return artifacts
        return []

    def _ensure_step_artifacts_for_report(self, task_db_id: int, step_code: str) -> bool:
        step = str(step_code or "").strip().upper()
        if step not in {"SPEECH", "SUBTITLE", "ANALYSIS"}:
            return False
        existing = self._list_step_artifacts(task_db_id)
        if self._has_required_report_artifact(step, existing):
            return False
        ctx = self._get_task_report_context(task_db_id)
        raw = ctx.get("raw") or {}
        if not raw:
            return False
        artifacts = self._discover_step_artifacts_from_disk(task_db_id, step)
        if not artifacts:
            return False
        self._save_artifacts(task_db_id, SimpleNamespace(raw=raw), artifacts)
        try:
            self._replicate_same_lesson_artifacts(task_db_id, artifacts)
        except Exception:
            self._log.debug("replicate_same_lesson_artifacts failed task=%s step=%s", self._server_task_id_for_db_log(task_db_id), step, exc_info=True)
        return True

    def _build_final_output_fields(self, task_db_id: int, server_task_id: int, step_code: str, output_path: str | None) -> tuple[str | None, int | None, str | None, int | None]:
        step = str(step_code or "").strip().upper()
        self._ensure_step_artifacts_for_report(task_db_id, step)
        artifacts = self._list_step_artifacts(task_db_id)
        if step == "DOWNLOAD":
            if not output_path:
                return None, None, "mp4", None
            return self._build_file_url(output_path), self._calc_video_size_mb(output_path, step), "mp4", None
        if step == "TRANSCODE":
            if not output_path:
                return None, None, "m3u8", None
            return (
                self._build_file_url(output_path),
                self._calc_video_size_mb(output_path, step),
                "m3u8",
                self._calc_shard_num(step, output_path),
            )
        if step == "SPEECH":
            report = next((a for a in artifacts if a.get("file_type") == "speech_report_html" and a.get("file_path")), None)
            url = self._build_file_url(report["file_path"]) if report else None
            return url, 1, "html", None
        if step == "SUBTITLE":
            subtitle = next((a for a in artifacts if a.get("file_type") == "subtitle_srt" and a.get("file_path")), None)
            url = self._build_file_url(subtitle["file_path"]) if subtitle else None
            return url, 1, "srt", None
        if step == "ANALYSIS":
            web = next((a for a in artifacts if a.get("file_type") == "report_web_html" and a.get("file_path")), None)
            h5 = next((a for a in artifacts if a.get("file_type") == "report_h5_html" and a.get("file_path")), None)
            payload: dict[str, str] = {}
            if web:
                payload["web"] = self._build_file_url(web["file_path"])
            if h5:
                payload["h5"] = self._build_file_url(h5["file_path"])
            return (json.dumps(payload, ensure_ascii=False) if payload else None), 2, "html", None
        if not output_path:
            return None, None, None, None
        return self._build_file_url(output_path), self._calc_video_size_mb(output_path, step), self._calc_video_format(step, output_path), self._calc_shard_num(step, output_path)

    def _calc_video_size_mb(self, file_path: str | None, step_code: str = "") -> int | None:
        if not file_path:
            return None
        p = Path(file_path)
        if step_code == "TRANSCODE":
            if p.is_file():
                p = p.parent
            if p.is_dir():
                total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                return max(1, int(total / (1024 * 1024))) if total > 0 else None
            return None
        if p.is_file():
            return max(1, int(p.stat().st_size / (1024 * 1024)))
        if p.is_dir():
            total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            return max(1, int(total / (1024 * 1024))) if total > 0 else None
        return None

    def _calc_video_format(self, step_code: str, file_path: str | None) -> str | None:
        if step_code == "DOWNLOAD":
            return "mp4"
        if step_code == "TRANSCODE":
            return "m3u8"
        if step_code == "SPEECH":
            return "html"
        if step_code == "SUBTITLE":
            return "srt"
        if step_code == "ANALYSIS":
            return "html"
        return None

    def _calc_shard_num(self, step_code: str, file_path: str | None) -> int | None:
        if step_code != "TRANSCODE" or not file_path:
            return None
        p = Path(file_path)
        if p.is_dir():
            return len(list(p.glob("*.ts")))
        d = p.parent if p.is_file() else p
        if d.exists():
            return len(list(d.glob("*.ts")))
        return None

    def _build_step_report_payload(self, task_db_id: int, step_code: str, *, is_final: bool = False) -> dict[str, Any] | None:
        info = self._get_step_db_info(task_db_id, step_code)
        if not info:
            return None
        start_time = ensure_bjt_iso(info.get("start_time"))
        end_time = ensure_bjt_iso(info.get("end_time"))
        output_path = info.get("output_file_path")
        effective_final = bool(is_final) or int(info.get("step_status") or 0) in (StepStatus.SUCCESS, StepStatus.FAILED)
        output_url = None
        video_size = None
        video_format = None
        shard_num = None
        if effective_final:
            output_url, video_size, video_format, shard_num = self._build_final_output_fields(task_db_id, int(info["server_task_id"]), step_code, output_path)
        return {
            "server_task_id": int(info["server_task_id"]),
            "step_code": str(step_code),
            "step_status": int(info.get("step_status") or 0),
            "step_process": int(info.get("step_process") or 0),
            "start_time": start_time,
            "end_time": end_time,
            "output_file_url": output_url,
            "video_size": video_size,
            "video_format": video_format,
            "video_shard_num": shard_num,
            "effective_final": effective_final,
        }

    def _mark_step_report_dirty(self, task_db_id: int, step_code: str, error: str = "", record_attempt: bool = False) -> None:
        with self._db.connect() as conn:
            if record_attempt:
                conn.execute(
                    """
INSERT INTO edge_step_report_state(task_id, step_code, report_dirty, last_attempt_time, last_error, updated_time)
VALUES (?,?,1,strftime('%Y-%m-%dT%H:%M:%SZ','now'),?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))
ON CONFLICT(task_id, step_code) DO UPDATE SET
  report_dirty=1,
  last_attempt_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  last_error=excluded.last_error,
  updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    """.strip(),
                    (int(task_db_id), str(step_code), str(error or "")[:500]),
                )
            else:
                conn.execute(
                    """
INSERT INTO edge_step_report_state(task_id, step_code, report_dirty, last_error, updated_time)
VALUES (?,?,1,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))
ON CONFLICT(task_id, step_code) DO UPDATE SET
  report_dirty=1,
  last_error=excluded.last_error,
  updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    """.strip(),
                    (int(task_db_id), str(step_code), str(error or "")[:500]),
                )
            conn.commit()

    def _mark_step_report_success(self, task_db_id: int, step_code: str, payload: dict[str, Any]) -> None:
        with self._db.connect() as conn:
            conn.execute(
                """
INSERT INTO edge_step_report_state(task_id, step_code, report_dirty, last_success_time, last_success_payload, updated_time)
VALUES (?,?,0,strftime('%Y-%m-%dT%H:%M:%SZ','now'),?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))
ON CONFLICT(task_id, step_code) DO UPDATE SET
  report_dirty=0,
  last_success_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  last_success_payload=excluded.last_success_payload,
  updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                """.strip(),
                (int(task_db_id), str(step_code), json.dumps(payload, ensure_ascii=False)),
            )
            conn.commit()

    def _is_step_report_up_to_date(self, task_db_id: int, step_code: str, payload: dict[str, Any]) -> bool:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT report_dirty, last_success_payload FROM edge_step_report_state WHERE task_id=? AND step_code=? LIMIT 1",
                (int(task_db_id), str(step_code)),
            ).fetchone()
        if r is None:
            return False
        if int(r["report_dirty"] or 0) == 1:
            return False
        last = str(r["last_success_payload"] or "")
        if not last:
            return False
        try:
            last_d = json.loads(last)
        except Exception:
            return False
        for key in ("step_status", "step_process", "start_time", "end_time", "output_file_url", "video_size", "video_format", "video_shard_num"):
            if last_d.get(key) != payload.get(key):
                return False
        return True

    def _list_completed_step_reports_for_backfill(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                """
SELECT s.task_id, s.step_code
FROM edge_stream_task_step s
JOIN edge_stream_task t ON t.id=s.task_id
WHERE t.task_kind='CourseTask'
  AND s.step_code IN ('SPEECH','SUBTITLE','ANALYSIS')
  AND s.step_status=?
ORDER BY COALESCE(s.end_time,''), s.task_id, s.step_code
LIMIT ?
                """.strip(),
                (int(StepStatus.SUCCESS), int(limit)),
            ).fetchall()
        return [{"task_id": int(r["task_id"]), "step_code": str(r["step_code"] or "")} for r in (rows or [])]

    def _list_dirty_step_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT task_id, step_code FROM edge_step_report_state WHERE report_dirty=1 ORDER BY updated_time ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [{"task_id": int(r["task_id"]), "step_code": str(r["step_code"] or "")} for r in (rows or [])]

    def _list_report_url_rewrite_candidates(self, rules: list[dict[str, Any]], limit: int = 200) -> list[dict[str, Any]]:
        valid_rules = [rule for rule in (rules or []) if rule.get("oldHost") and rule.get("oldBaseUrl") and rule.get("newBaseUrl")]
        if not valid_rules:
            return []
        clauses = ["COALESCE(r.last_success_payload,'')<>''"]
        params: list[Any] = []
        host_clauses = []
        for rule in valid_rules:
            host = str(rule.get("oldHost") or "").strip()
            host_clauses.append("r.last_success_payload LIKE ?")
            params.append(f"%{host}%")
        clauses.append("(" + " OR ".join(host_clauses) + ")")
        params.append(int(limit))
        with self._db.connect() as conn:
            rows = conn.execute(
                f"""
SELECT r.task_id, r.step_code, r.last_success_time, r.last_success_payload,
       t.server_task_id, t.task_kind, t.task_type, t.lesson_id
FROM edge_step_report_state r
JOIN edge_stream_task t ON t.id=r.task_id
JOIN edge_stream_task_step s ON s.task_id=r.task_id AND UPPER(s.step_code)=UPPER(r.step_code)
WHERE {" AND ".join(clauses)}
  AND s.step_status=?
ORDER BY COALESCE(r.last_success_time,''), r.task_id, r.step_code
LIMIT ?
                """.strip(),
                tuple(params[:-1] + [int(StepStatus.SUCCESS), params[-1]]),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows or []:
            try:
                payload = json.loads(str(row["last_success_payload"] or "") or "{}") or {}
            except Exception:
                payload = {}
            old_value = payload.get("output_file_url") or payload.get("outputFileUrl") or ""
            step_code = str(row["step_code"] or "").strip().upper()
            matched_rule: dict[str, Any] | None = None
            replacement_details: list[dict[str, str]] = []
            new_value: Any = old_value
            for rule in valid_rules:
                rule_steps = set(rule.get("stepCodes") or [])
                if rule_steps and step_code not in rule_steps:
                    continue
                candidate_value, details = self._replace_report_output_url_base(old_value, str(rule.get("oldBaseUrl") or ""), str(rule.get("newBaseUrl") or ""))
                if details:
                    matched_rule = rule
                    replacement_details = details
                    new_value = candidate_value
                    break
            if not matched_rule or not replacement_details:
                continue
            result.append({
                "task_id": int(row["task_id"]),
                "step_code": step_code,
                "server_task_id": int(row["server_task_id"] or 0),
                "task_kind": str(row["task_kind"] or ""),
                "task_type": int(row["task_type"] or 0),
                "lesson_id": int(row["lesson_id"] or 0),
                "last_success_time": str(row["last_success_time"] or ""),
                "old_base_url": str(matched_rule.get("oldBaseUrl") or ""),
                "new_base_url": str(matched_rule.get("newBaseUrl") or ""),
                "old_output_file_url": old_value,
                "new_output_file_url": new_value,
                "replacement_details": replacement_details,
                "last_success_payload": payload,
            })
        return result

    def _normalize_manual_report_task_type(self, value: Any) -> tuple[int, str]:
        text = str(value or "").strip().lower()
        if text in {"teacher", "1"}:
            return 1, "teacher"
        if text in {"student", "2"}:
            return 2, "student"
        if text in {"ppt", "3"}:
            return 3, "ppt"
        if text in {"view", "0"}:
            return 0, "view"
        raise ValueError(f"不支持的 taskType: {value}")

    def _find_force_report_transcode_target(self, lesson_id: str, task_type: int) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                """
SELECT t.id AS task_db_id, t.server_task_id, t.lesson_id, t.task_type,
       s.step_status, s.step_process, s.output_file_path, s.end_time
FROM edge_stream_task t
JOIN edge_stream_task_step s ON s.task_id=t.id
WHERE t.task_kind='CameraTask'
  AND t.lesson_id=?
  AND t.task_type=?
  AND s.step_code='TRANSCODE'
ORDER BY CASE WHEN s.step_status=2 THEN 0 ELSE 1 END,
         COALESCE(s.end_time,'' ) DESC,
         t.id DESC
LIMIT 1
                """.strip(),
                (str(lesson_id or "").strip(), int(task_type)),
            ).fetchone()
            return dict(row) if row is not None else None

    async def force_backfill_transcode_reports(self, targets: list[dict[str, Any]]) -> dict[str, Any]:
        scanned = 0
        reported = 0
        failed = 0
        missing = 0
        items: list[dict[str, Any]] = []
        for target in (targets or []):
            lesson_id = str((target or {}).get("lessonId") or "").strip()
            raw_task_types = (target or {}).get("taskTypes")
            if not lesson_id:
                continue
            task_values = raw_task_types if isinstance(raw_task_types, list) and raw_task_types else ["teacher", "student"]
            for raw_task_type in task_values:
                scanned += 1
                try:
                    task_type, task_type_name = self._normalize_manual_report_task_type(raw_task_type)
                except Exception as exc:
                    failed += 1
                    items.append({
                        "lessonId": lesson_id,
                        "taskType": str(raw_task_type),
                        "ok": False,
                        "message": str(exc),
                    })
                    continue
                row = await asyncio.to_thread(self._find_force_report_transcode_target, lesson_id, task_type)
                if row is None:
                    missing += 1
                    items.append({
                        "lessonId": lesson_id,
                        "taskType": task_type_name,
                        "ok": False,
                        "message": "未找到对应的本地转码任务",
                    })
                    continue
                task_db_id = int(row.get("task_db_id") or 0)
                server_task_id = int(row.get("server_task_id") or 0)
                try:
                    await asyncio.to_thread(self._mark_step_report_dirty, task_db_id, "TRANSCODE", "manual_force_backfill_transcode")
                    await self._do_step_report(task_db_id, "TRANSCODE", is_final=True)
                    state_row = await asyncio.to_thread(
                        self._db.fetch_one,
                        "SELECT report_dirty, last_error, last_success_time FROM edge_step_report_state WHERE task_id=? AND step_code=? LIMIT 1",
                        (int(task_db_id), "TRANSCODE"),
                    )
                    is_ok = state_row is not None and int(state_row["report_dirty"] or 0) == 0
                    if is_ok:
                        reported += 1
                    else:
                        failed += 1
                    items.append({
                        "lessonId": lesson_id,
                        "taskType": task_type_name,
                        "taskDbId": task_db_id,
                        "serverTaskId": server_task_id,
                        "stepStatus": int(row.get("step_status") or 0),
                        "stepProcess": int(row.get("step_process") or 0),
                        "outputFilePath": str(row.get("output_file_path") or ""),
                        "ok": bool(is_ok),
                        "message": "ok" if is_ok else str(state_row["last_error"] or "转码信息补充上报失败") if state_row is not None else "转码信息补充上报失败",
                        "lastSuccessTime": str(state_row["last_success_time"] or "") if state_row is not None else "",
                    })
                except Exception as exc:
                    failed += 1
                    items.append({
                        "lessonId": lesson_id,
                        "taskType": task_type_name,
                        "taskDbId": task_db_id,
                        "serverTaskId": server_task_id,
                        "ok": False,
                        "message": str(exc),
                    })
        return {
            "scanned": scanned,
            "reported": reported,
            "failed": failed,
            "missing": missing,
            "items": items,
        }

    async def preview_report_url_rewrite_backfill(self, limit: int = 200) -> dict[str, Any]:
        rules = await asyncio.to_thread(self._configured_report_backfill_rules)
        rows = await asyncio.to_thread(self._list_report_url_rewrite_candidates, rules, limit)
        items = [self._report_url_rewrite_item(row, "pending", "待更新") for row in rows]
        return {
            "enabled": bool(rules),
            "configuredRules": rules,
            "scanned": len(items),
            "items": items,
        }

    def _report_url_rewrite_item(self, row: dict[str, Any], status: str, message: str) -> dict[str, Any]:
        return {
            "taskDbId": int(row.get("task_id") or 0),
            "serverTaskId": int(row.get("server_task_id") or 0),
            "taskKind": str(row.get("task_kind") or ""),
            "taskType": int(row.get("task_type") or 0),
            "lessonId": int(row.get("lesson_id") or 0),
            "stepCode": str(row.get("step_code") or "").strip().upper(),
            "oldBaseUrl": str(row.get("old_base_url") or ""),
            "newBaseUrl": str(row.get("new_base_url") or ""),
            "oldOutputFileUrl": row.get("old_output_file_url"),
            "newOutputFileUrl": row.get("new_output_file_url"),
            "replacementDetails": row.get("replacement_details") or [],
            "lastSuccessTime": str(row.get("last_success_time") or ""),
            "status": status,
            "message": message,
        }

    def _update_step_report_success_payload_only(self, task_db_id: int, step_code: str, payload: dict[str, Any]) -> None:
        with self._db.connect() as conn:
            conn.execute(
                """
UPDATE edge_step_report_state
SET last_success_payload=?, last_success_time=strftime('%Y-%m-%dT%H:%M:%SZ','now'), last_error='', updated_time=strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE task_id=? AND step_code=?
                """.strip(),
                (json.dumps(payload, ensure_ascii=False), int(task_db_id), str(step_code)),
            )
            conn.commit()

    async def execute_report_url_rewrite_backfill(self, limit: int = 200) -> dict[str, Any]:
        rules = await asyncio.to_thread(self._configured_report_backfill_rules)
        rows = await asyncio.to_thread(self._list_report_url_rewrite_candidates, rules, limit)
        updated = 0
        failed = 0
        items: list[dict[str, Any]] = []
        for row in rows:
            task_db_id = int(row.get("task_id") or 0)
            step_code = str(row.get("step_code") or "").strip().upper()
            payload = dict(row.get("last_success_payload") or {})
            payload["output_file_url"] = row.get("new_output_file_url")
            self._log.info(
                "历史 URL 修复准备 taskDbId=%s serverTaskId=%s step=%s oldBaseUrl=%s newBaseUrl=%s oldOutputFileUrl=%s newOutputFileUrl=%s",
                task_db_id,
                int(row.get("server_task_id") or 0),
                step_code,
                row.get("old_base_url"),
                row.get("new_base_url"),
                row.get("old_output_file_url"),
                row.get("new_output_file_url"),
            )
            try:
                ok = await self._client.report_step_update(
                    task_id=int(payload["server_task_id"]),
                    step_code=str(payload["step_code"]),
                    step_status=int(payload["step_status"]),
                    step_process=int(payload["step_process"]),
                    start_time=payload.get("start_time"),
                    end_time=payload.get("end_time"),
                    video_size=payload.get("video_size"),
                    video_format=payload.get("video_format"),
                    video_shard_num=payload.get("video_shard_num"),
                    output_file_url=payload.get("output_file_url"),
                )
                if not ok:
                    failed += 1
                    self._log.warning("历史 URL 修复中心上报失败 taskDbId=%s serverTaskId=%s step=%s", task_db_id, int(row.get("server_task_id") or 0), step_code)
                    items.append(self._report_url_rewrite_item(row, "failed", "中心上报失败，本地未更新"))
                    continue
                await asyncio.to_thread(self._update_step_report_success_payload_only, task_db_id, step_code, payload)
                updated += 1
                self._log.info("历史 URL 修复完成 taskDbId=%s serverTaskId=%s step=%s", task_db_id, int(row.get("server_task_id") or 0), step_code)
                items.append(self._report_url_rewrite_item(row, "updated", "已更新"))
            except Exception as exc:
                failed += 1
                self._log.warning("历史 URL 修复异常 taskDbId=%s serverTaskId=%s step=%s error=%s", task_db_id, int(row.get("server_task_id") or 0), step_code, exc, exc_info=True)
                items.append(self._report_url_rewrite_item(row, "failed", str(exc)))
        return {
            "enabled": bool(rules),
            "configuredRules": rules,
            "scanned": len(rows),
            "updated": updated,
            "failed": failed,
            "items": items,
        }

    def _fire_step_report(self, task_db_id: int, step_code: str, *, is_final: bool = False) -> None:
        """异步触发一次步骤上报（不阻塞当前线程）"""
        try:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            asyncio.run_coroutine_threadsafe(
                self._do_step_report(task_db_id, step_code, is_final=is_final), loop
            )
        except Exception:
            self._log.debug("fire_step_report failed", exc_info=True)

    def _fire_same_lesson_reports(self, task_db_id: int, step_code: str, *, is_final: bool = False) -> None:
        """为同课次下的其他 CourseTask 也触发上报"""
        try:
            with self._db.connect() as conn:
                t = conn.execute("SELECT lesson_id, task_kind FROM edge_stream_task WHERE id=?", (int(task_db_id),)).fetchone()
                if t is None or str(t["task_kind"]) != "CourseTask":
                    return
                lesson_id = int(t["lesson_id"])
                others = conn.execute(
                    "SELECT id, task_type FROM edge_stream_task WHERE lesson_id=? AND task_kind='CourseTask' AND id!=?",
                    (lesson_id, int(task_db_id)),
                ).fetchall()
            for r in (others or []):
                if int(r["task_type"] or 0) == 3:
                    with self._db.connect() as conn:
                        if not self._is_type3_course_visible(conn, lesson_id):
                            continue
                oid = int(r["id"])
                self._fire_step_report(oid, step_code, is_final=is_final)
        except Exception:
            self._log.debug("fire_same_lesson_reports failed", exc_info=True)

    def _queue_step_state_report(self, task_db_id: int, step_code: str, *, is_final: bool = False, include_same_lesson: bool = False) -> None:
        step = str(step_code or "").strip().upper()
        if not step:
            return
        try:
            self._mark_step_report_dirty(int(task_db_id), step)
        except Exception:
            self._log.debug("mark_step_report_dirty failed task=%s step=%s", self._server_task_id_for_db_log(task_db_id), step, exc_info=True)
        self._fire_step_report(int(task_db_id), step, is_final=is_final)
        if include_same_lesson and step in {"SPEECH", "SUBTITLE", "ANALYSIS"}:
            self._fire_same_lesson_reports(int(task_db_id), step, is_final=is_final)

    def _should_defer_final_step_report(self, task_db_id: int, step_code: str) -> bool:
        step = str(step_code or "").strip().upper()
        try:
            with self._db.connect() as conn:
                row = conn.execute("SELECT task_kind FROM edge_stream_task WHERE id=? LIMIT 1", (int(task_db_id),)).fetchone()
                if row is None:
                    return False
                task_kind = str(row["task_kind"] or "")
                if task_kind == "CourseTask" and step in {"SPEECH", "SUBTITLE", "ANALYSIS"}:
                    return True
                if task_kind == "CameraTask" and step == "TRANSCODE":
                    return True
                return False
        except Exception:
            return False

    async def _on_course_step_complete(
        self,
        task_db_id: int,
        task,
        step_code: str,
        step_artifacts: list[dict[str, Any]] | None,
    ) -> None:
        step = str(step_code or "").strip().upper()
        try:
            if step_artifacts:
                await asyncio.to_thread(self._save_artifacts, task_db_id, task, step_artifacts)
                try:
                    await asyncio.to_thread(self._replicate_same_lesson_artifacts, task_db_id, step_artifacts)
                except Exception:
                    self._log.debug("replicate_same_lesson_artifacts failed task=%s step=%s", self._server_task_id_for_db_log(task_db_id), step, exc_info=True)
            else:
                await asyncio.to_thread(self._mark_step_report_dirty, task_db_id, step)
            await self._do_step_report(task_db_id, step, is_final=True)
            try:
                self._fire_same_lesson_reports(task_db_id, step, is_final=True)
            except Exception:
                pass
        except Exception:
            self._log.warning("course step complete report failed task=%s step=%s", self._server_task_id_for_db_log(task_db_id), step, exc_info=True)

    async def _do_step_report(self, task_db_id: int, step_code: str, *, is_final: bool = False) -> None:
        runtime = self._step_report_runtime_state()
        inflight = runtime["inflight"]
        report_key = (int(task_db_id), str(step_code).strip().upper(), bool(is_final))
        if report_key in inflight:
            return
        inflight.add(report_key)
        try:
            payload = await asyncio.to_thread(self._build_step_report_payload, task_db_id, step_code, is_final=is_final)
            if not payload:
                return
            up_to_date = await asyncio.to_thread(self._is_step_report_up_to_date, task_db_id, step_code, payload)
            if up_to_date:
                return

            ok = await self._client.report_step_update(
                task_id=int(payload["server_task_id"]),
                step_code=str(payload["step_code"]),
                step_status=int(payload["step_status"]),
                step_process=int(payload["step_process"]),
                start_time=payload.get("start_time"),
                end_time=payload.get("end_time"),
                video_size=payload.get("video_size"),
                video_format=payload.get("video_format"),
                video_shard_num=payload.get("video_shard_num"),
                output_file_url=payload.get("output_file_url"),
            )
            if not ok:
                await asyncio.to_thread(self._mark_step_report_dirty, task_db_id, step_code, "step_update_failed", True)
                self._log.warning(
                    "步骤上报失败 task=%s step=%s status=%s process=%s%%",
                    payload["server_task_id"], step_code, payload["step_status"], payload["step_process"],
                )
                return
            await asyncio.to_thread(self._mark_step_report_success, task_db_id, step_code, payload)
            log_key = (
                int(payload["server_task_id"]),
                str(step_code).strip().upper(),
                bool(payload.get("effective_final")),
            )
            now_sec = time.monotonic()
            last_logged = runtime["last_logged"]
            log_interval_sec = float(runtime.get("log_interval_sec") or 20.0)
            if bool(payload.get("effective_final")) or (now_sec - float(last_logged.get(log_key) or 0.0)) >= log_interval_sec:
                last_logged[log_key] = now_sec
                output_file_url = payload.get("output_file_url")
                output_url_hosts = self._report_output_url_hosts(output_file_url)
                self._log.info(
                    "上报 task=%s step=%s status=%s process=%s%% final=%s outputUrlHosts=%s outputFileUrl=%s",
                    payload["server_task_id"],
                    step_code,
                    payload["step_status"],
                    payload["step_process"],
                    payload.get("effective_final"),
                    output_url_hosts,
                    str(output_file_url or "")[:500],
                )
        except Exception as e:
            await asyncio.to_thread(self._mark_step_report_dirty, task_db_id, step_code, str(e), True)
            self._log.warning("步骤上报失败", exc_info=True)
        finally:
            inflight.discard(report_key)

    async def backfill_completed_step_reports(self, limit: int = 200) -> dict[str, Any]:
        rows = await asyncio.to_thread(self._list_completed_step_reports_for_backfill, limit)
        scanned = 0
        repaired = 0
        reported = 0
        for r in (rows or []):
            task_db_id = int(r["task_id"])
            step_code = str(r["step_code"] or "").strip().upper()
            scanned += 1
            rebuilt = await asyncio.to_thread(self._ensure_step_artifacts_for_report, task_db_id, step_code)
            if rebuilt:
                repaired += 1
            await asyncio.to_thread(self._mark_step_report_dirty, task_db_id, step_code)
            await self._do_step_report(task_db_id, step_code, is_final=True)
            state_row = await asyncio.to_thread(
                lambda _task_id=task_db_id, _step=step_code: self._db.fetch_one(
                    "SELECT report_dirty FROM edge_step_report_state WHERE task_id=? AND step_code=? LIMIT 1",
                    (int(_task_id), str(_step)),
                )
            )
            if state_row is not None and int(state_row["report_dirty"] or 0) == 0:
                reported += 1
        return {
            "scanned": scanned,
            "repaired": repaired,
            "reported": reported,
        }

    async def _flush_pending_reports(self) -> None:
        rows = await asyncio.to_thread(self._list_dirty_step_reports, 20)
        for r in (rows or []):
            await self._do_step_report(int(r["task_id"]), str(r["step_code"]), is_final=False)
