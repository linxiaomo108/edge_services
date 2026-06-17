from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


_BJT = timezone(timedelta(hours=8))


def bjt_now_iso() -> str:
    return datetime.now(_BJT).isoformat(timespec="seconds")


def ensure_bjt_iso(value: str | None) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_BJT).isoformat(timespec="seconds")
    return dt.astimezone(_BJT).isoformat(timespec="seconds")


def register_sqlite_bjt_functions(conn: sqlite3.Connection) -> sqlite3.Connection:
    def _sqlite_bjt_now() -> str:
        return bjt_now_iso()

    def _sqlite_strftime(fmt: str | None, value: str | None) -> str | None:
        if str(value or "").strip().lower() == "now":
            if str(fmt or "") == "%Y-%m-%dT%H:%M:%SZ":
                return bjt_now_iso()
            try:
                return datetime.now(_BJT).strftime(str(fmt or ""))
            except Exception:
                return bjt_now_iso()
        return None

    conn.create_function("bjt_now", 0, _sqlite_bjt_now)
    conn.create_function("strftime", 2, _sqlite_strftime)
    return conn


_TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "edge_stream_task": ("created_time", "updated_time"),
    "edge_stream_task_step": ("start_time", "end_time", "created_time", "updated_time"),
    "edge_lesson_lock": ("created_time", "updated_time"),
    "edge_node_info": ("created_time",),
    "edge_lesson_output": ("created_time", "updated_time"),
    "edge_task_log": ("created_time",),
    "edge_step_report_state": (
        "last_reported_start_time",
        "last_reported_end_time",
        "last_attempt_time",
        "last_success_time",
        "updated_time",
    ),
    "edge_nvr_channel_map": ("last_success_time", "created_time", "updated_time"),
    "edge_nvr_device": ("last_online_time", "created_time", "updated_time"),
    "edge_nvr_channel": ("created_time", "updated_time"),
    "edge_nvr_rtsp_probe_cache": ("verified_at", "created_time", "updated_time"),
    "edge_mobile_record_task": (
        "start_time",
        "max_end_time",
        "manual_stop_time",
        "auto_stop_time",
        "finish_time",
        "created_time",
        "updated_time",
    ),
    "edge_mobile_record_event": ("created_time",),
}


_TABLE_KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    "edge_stream_task": ("id",),
    "edge_stream_task_step": ("id",),
    "edge_lesson_lock": ("lesson_id",),
    "edge_node_info": ("id",),
    "edge_lesson_output": ("id",),
    "edge_task_log": ("id",),
    "edge_step_report_state": ("id",),
    "edge_nvr_channel_map": ("id",),
    "edge_nvr_device": ("id",),
    "edge_nvr_channel": ("id",),
    "edge_nvr_rtsp_probe_cache": ("id",),
    "edge_mobile_record_task": ("id",),
    "edge_mobile_record_event": ("id",),
}


def _migrate_existing_timestamps(conn: sqlite3.Connection) -> None:
    changed = False
    for table, columns in _TIMESTAMP_COLUMNS.items():
        key_cols = _TABLE_KEY_COLUMNS.get(table, ())
        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
        for row in rows:
            updates: list[str] = []
            params: list[Any] = []
            for col in columns:
                if col not in row.keys():
                    continue
                converted = ensure_bjt_iso(row[col])
                if converted and converted != row[col]:
                    updates.append(f'"{col}"=?')
                    params.append(converted)
            if not updates:
                continue
            if not key_cols or any(k not in row.keys() for k in key_cols):
                continue
            where_parts = [f'"{k}"=?' for k in key_cols]
            params.extend(row[k] for k in key_cols)
            conn.execute(f'UPDATE "{table}" SET {", ".join(updates)} WHERE {" AND ".join(where_parts)}', tuple(params))
            changed = True
    if changed:
        conn.commit()


@dataclass(frozen=True)
class DbConfig:
    path: str


class Db:
    def __init__(self, cfg: DbConfig) -> None:
        self._path = str(cfg.path)

    @property
    def path(self) -> str:
        return self._path

    def connect(self) -> sqlite3.Connection:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        register_sqlite_bjt_functions(conn)
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(edge_stream_task)").fetchall()}
            if "lesson_date" not in cols:
                conn.execute("ALTER TABLE edge_stream_task ADD COLUMN lesson_date TEXT;")
            if "room_name" not in cols:
                conn.execute("ALTER TABLE edge_stream_task ADD COLUMN room_name TEXT;")
            if "other_tasks_id" not in cols:
                conn.execute("ALTER TABLE edge_stream_task ADD COLUMN other_tasks_id TEXT;")
            if "grade" not in cols:
                conn.execute("ALTER TABLE edge_stream_task ADD COLUMN grade TEXT;")
            if "subject" not in cols:
                conn.execute("ALTER TABLE edge_stream_task ADD COLUMN subject TEXT;")
            step_cols = {r["name"] for r in conn.execute("PRAGMA table_info(edge_stream_task_step)").fetchall()}
            if "finalize_pending" not in step_cols:
                conn.execute("ALTER TABLE edge_stream_task_step ADD COLUMN finalize_pending INTEGER DEFAULT 0;")
            if "finalize_src_path" not in step_cols:
                conn.execute("ALTER TABLE edge_stream_task_step ADD COLUMN finalize_src_path TEXT;")
            if "finalize_dst_path" not in step_cols:
                conn.execute("ALTER TABLE edge_stream_task_step ADD COLUMN finalize_dst_path TEXT;")
            if "finalize_action" not in step_cols:
                conn.execute("ALTER TABLE edge_stream_task_step ADD COLUMN finalize_action TEXT;")
            if "finalize_error" not in step_cols:
                conn.execute("ALTER TABLE edge_stream_task_step ADD COLUMN finalize_error TEXT;")
            if "finalize_retry_count" not in step_cols:
                conn.execute("ALTER TABLE edge_stream_task_step ADD COLUMN finalize_retry_count INTEGER DEFAULT 0;")
            lesson_output_cols = {r["name"] for r in conn.execute("PRAGMA table_info(edge_lesson_output)").fetchall()}
            if "updated_time" not in lesson_output_cols:
                conn.execute("ALTER TABLE edge_lesson_output ADD COLUMN updated_time TEXT;")
            conn.execute(
                "UPDATE edge_lesson_output SET updated_time=COALESCE(NULLIF(updated_time,''), NULLIF(created_time,''), strftime('%Y-%m-%dT%H:%M:%SZ','now')) WHERE updated_time IS NULL OR updated_time=''"
            )
            report_cols = {r["name"] for r in conn.execute("PRAGMA table_info(edge_step_report_state)").fetchall()}
            if "last_success_payload" not in report_cols:
                conn.execute("ALTER TABLE edge_step_report_state ADD COLUMN last_success_payload TEXT;")
            channel_map_cols = {r["name"] for r in conn.execute("PRAGMA table_info(edge_nvr_channel_map)").fetchall()}
            if "record_type" not in channel_map_cols:
                conn.execute("ALTER TABLE edge_nvr_channel_map ADD COLUMN record_type INTEGER DEFAULT 255;")
            if "consecutive_fail_count" not in channel_map_cols:
                conn.execute("ALTER TABLE edge_nvr_channel_map ADD COLUMN consecutive_fail_count INTEGER DEFAULT 0;")
            if "last_fail_time" not in channel_map_cols:
                conn.execute("ALTER TABLE edge_nvr_channel_map ADD COLUMN last_fail_time TEXT;")
            nvr_channel_cols = {r["name"] for r in conn.execute("PRAGMA table_info(edge_nvr_channel)").fetchall()}
            if nvr_channel_cols and "activated_at" not in nvr_channel_cols:
                conn.execute("ALTER TABLE edge_nvr_channel ADD COLUMN activated_at REAL;")
            record_cols = {r["name"] for r in conn.execute("PRAGMA table_info(edge_mobile_record_task)").fetchall()}
            if record_cols and "callback_last_error" not in record_cols:
                conn.execute("ALTER TABLE edge_mobile_record_task ADD COLUMN callback_last_error TEXT;")
            _migrate_existing_timestamps(conn)
            conn.commit()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, tuple(params))
            conn.commit()

    def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return list(cur.fetchall())

    def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self.connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.fetchone()

    def cleanup_task_logs(self, *, retention_days: int = 30, batch_size: int = 5000) -> int:
        cutoff = (datetime.now(_BJT) - timedelta(days=max(1, int(retention_days)))).isoformat(timespec="seconds")
        total_deleted = 0
        with self.connect() as conn:
            while True:
                cur = conn.execute(
                    """
DELETE FROM edge_task_log
WHERE id IN (
  SELECT id FROM edge_task_log
  WHERE created_time < ?
  ORDER BY id
  LIMIT ?
)
                    """.strip(),
                    (cutoff, max(1, int(batch_size))),
                )
                deleted = int(cur.rowcount or 0)
                if deleted <= 0:
                    break
                total_deleted += deleted
                conn.commit()
        return total_deleted


_SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS edge_stream_task (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  server_task_id INTEGER NOT NULL,
  lesson_id INTEGER NOT NULL,
  lesson_date TEXT,
  room_name TEXT,
  school_area_code TEXT,
  branch_school_code TEXT,
  task_type INTEGER NOT NULL,
  task_kind TEXT NOT NULL,
  nvr_device_id INTEGER,
  nvr_channel_num INTEGER,
  nvr_channel_id TEXT,
  nvr_ip TEXT,
  nvr_port INTEGER,
  nvr_account TEXT,
  nvr_password TEXT,
  relate_class TEXT,
  relate_lesson TEXT,
  grade TEXT,
  subject TEXT,
  download_start TEXT,
  download_end TEXT,
  task_status INTEGER DEFAULT 0,
  current_step TEXT,
  process_rate INTEGER DEFAULT 0,
  retry_count INTEGER DEFAULT 0,
  execute_node_id TEXT,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
""".strip(),
    """
CREATE TABLE IF NOT EXISTS edge_stream_task_step (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  step_code TEXT NOT NULL,
  is_lesson_level INTEGER DEFAULT 0,
  step_status INTEGER DEFAULT 0,
  step_process INTEGER DEFAULT 0,
  start_time TEXT,
  end_time TEXT,
  video_size INTEGER,
  video_format TEXT,
  video_shard_num INTEGER,
  output_file_path TEXT,
  finalize_pending INTEGER DEFAULT 0,
  finalize_src_path TEXT,
  finalize_dst_path TEXT,
  finalize_action TEXT,
  finalize_error TEXT,
  finalize_retry_count INTEGER DEFAULT 0,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
""".strip(),
    """
CREATE TABLE IF NOT EXISTS edge_lesson_lock (
  lesson_id INTEGER PRIMARY KEY,
  speech_done INTEGER DEFAULT 0,
  subtitle_done INTEGER DEFAULT 0,
  analysis_done INTEGER DEFAULT 0,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
""".strip(),
    """
CREATE TABLE IF NOT EXISTS edge_node_info (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT,
  branch_school_code TEXT,
  ip TEXT,
  gpu_info TEXT,
  cpu_info TEXT,
  version TEXT,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
""".strip(),
    """
CREATE TABLE IF NOT EXISTS edge_lesson_output (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lesson_id INTEGER NOT NULL,
  server_task_id INTEGER,
  file_type TEXT,
  file_path TEXT,
  file_size INTEGER,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
""".strip(),
    """
CREATE TABLE IF NOT EXISTS edge_task_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  step_code TEXT,
  log_level TEXT,
  message TEXT,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
""".strip(),
    """
CREATE TABLE IF NOT EXISTS edge_step_report_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  step_code TEXT NOT NULL,
  report_dirty INTEGER DEFAULT 1,
  last_reported_status INTEGER,
  last_reported_process INTEGER,
  last_reported_start_time TEXT,
  last_reported_end_time TEXT,
  last_reported_output_file_url TEXT,
  last_reported_video_size INTEGER,
  last_reported_video_format TEXT,
  last_reported_video_shard_num INTEGER,
  last_attempt_time TEXT,
  last_success_time TEXT,
  last_success_payload TEXT,
  last_error TEXT,
  updated_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE(task_id, step_code)
);
""".strip(),
    """
CREATE TABLE IF NOT EXISTS edge_nvr_channel_map (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nvr_device_id INTEGER NOT NULL,
  web_channel_num INTEGER NOT NULL,
  sdk_channel INTEGER NOT NULL,
  record_type INTEGER DEFAULT 255,
  success_count INTEGER DEFAULT 0,
  consecutive_fail_count INTEGER DEFAULT 0,
  last_success_time TEXT,
  last_fail_time TEXT,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE(nvr_device_id, web_channel_num)
);
""".strip(),
    "CREATE INDEX IF NOT EXISTS idx_edge_task_lesson ON edge_stream_task(lesson_id);",
    "CREATE INDEX IF NOT EXISTS idx_edge_task_status ON edge_stream_task(task_status);",
    "CREATE INDEX IF NOT EXISTS idx_edge_step_task ON edge_stream_task_step(task_id);",
    "CREATE INDEX IF NOT EXISTS idx_edge_task_server_id ON edge_stream_task(server_task_id);",
    "CREATE INDEX IF NOT EXISTS idx_edge_task_kind ON edge_stream_task(task_kind);",
    "CREATE INDEX IF NOT EXISTS idx_edge_step_code ON edge_stream_task_step(step_code);",
    "CREATE INDEX IF NOT EXISTS idx_edge_task_log_task_step_id ON edge_task_log(task_id, step_code, id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_edge_task_log_task_step_msg_id ON edge_task_log(task_id, step_code, message, id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_edge_step_report_dirty ON edge_step_report_state(report_dirty, updated_time);",
    "CREATE INDEX IF NOT EXISTS idx_edge_nvr_channel_map_device ON edge_nvr_channel_map(nvr_device_id, web_channel_num);",
    "CREATE INDEX IF NOT EXISTS idx_edge_task_room_name ON edge_stream_task(room_name);",
    "CREATE INDEX IF NOT EXISTS idx_edge_task_server_id_class ON edge_stream_task(server_task_id, relate_class);",
    "CREATE INDEX IF NOT EXISTS idx_edge_task_relate_class ON edge_stream_task(relate_class);",
    "CREATE INDEX IF NOT EXISTS idx_edge_step_task_status_end ON edge_stream_task_step(task_id, step_status, end_time);",
    "CREATE INDEX IF NOT EXISTS idx_edge_step_status_end ON edge_stream_task_step(step_status, end_time);",
    """
CREATE TABLE IF NOT EXISTS edge_nvr_device (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nvr_device_id INTEGER NOT NULL UNIQUE,
  campus_code TEXT,
  ip_address TEXT,
  port INTEGER DEFAULT 554,
  account TEXT,
  device_model TEXT,
  serial_number TEXT,
  sdk_start_channel INTEGER,
  sdk_start_dchan INTEGER,
  ip_chan_num INTEGER,
  chan_num INTEGER,
  online_status INTEGER DEFAULT 0,
  last_online_time TEXT,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
""".strip(),
    """
CREATE TABLE IF NOT EXISTS edge_nvr_channel (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nvr_device_id INTEGER NOT NULL,
  channel_num INTEGER NOT NULL,
  channel_id TEXT,
  channel_name TEXT,
  sdk_channel INTEGER,
  rtsp_channel INTEGER,
  stream_status INTEGER DEFAULT 0,
  activated_at REAL,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE(nvr_device_id, channel_num)
);
""".strip(),
    """
CREATE TABLE IF NOT EXISTS edge_nvr_rtsp_probe_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nvr_device_id INTEGER NOT NULL,
  channel_num INTEGER NOT NULL,
  provider TEXT NOT NULL,
  ip_address TEXT NOT NULL,
  account TEXT,
  requested_profile TEXT NOT NULL,
  actual_profile TEXT NOT NULL,
  rtsp_port INTEGER NOT NULL,
  rtsp_url TEXT NOT NULL,
  verified_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  success_count INTEGER DEFAULT 0,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE(nvr_device_id, channel_num, provider, ip_address, account, requested_profile)
);
""".strip(),
    "CREATE INDEX IF NOT EXISTS idx_edge_nvr_device_id ON edge_nvr_device(nvr_device_id);",
    "CREATE INDEX IF NOT EXISTS idx_edge_nvr_channel_device ON edge_nvr_channel(nvr_device_id, channel_num);",
    "CREATE INDEX IF NOT EXISTS idx_edge_nvr_rtsp_probe_cache_lookup ON edge_nvr_rtsp_probe_cache(nvr_device_id, channel_num, provider, ip_address, account, requested_profile);",
    """
CREATE TABLE IF NOT EXISTS edge_mobile_record_task (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL UNIQUE,
  classroom_id TEXT NOT NULL,
  classroom_name TEXT,
  nvr_device_id INTEGER,
  nvr_channel_num INTEGER,
  nvr_channel_id TEXT,
  nvr_ip TEXT,
  nvr_port INTEGER,
  nvr_account TEXT,
  provider TEXT,
  record_user_id TEXT NOT NULL,
  record_user_name TEXT,
  status TEXT NOT NULL,
  estimated_duration_seconds INTEGER DEFAULT 0,
  extend_duration_seconds INTEGER DEFAULT 0,
  start_time TEXT,
  max_end_time TEXT,
  manual_stop_time TEXT,
  auto_stop_time TEXT,
  finish_time TEXT,
  stop_reason TEXT,
  output_dir TEXT,
  m3u8_path TEXT,
  play_url TEXT,
  segment_count INTEGER DEFAULT 0,
  file_size INTEGER DEFAULT 0,
  duration_seconds REAL DEFAULT 0,
  codec TEXT,
  ffmpeg_pid INTEGER,
  callback_url TEXT,
  callback_status TEXT,
  callback_retry_count INTEGER DEFAULT 0,
  callback_last_error TEXT,
  error_message TEXT,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
""".strip(),
    """
CREATE TABLE IF NOT EXISTS edge_mobile_record_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT,
  payload TEXT,
  created_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
""".strip(),
    "CREATE INDEX IF NOT EXISTS idx_edge_mobile_record_task_status ON edge_mobile_record_task(status);",
    "CREATE INDEX IF NOT EXISTS idx_edge_mobile_record_task_classroom ON edge_mobile_record_task(classroom_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_edge_mobile_record_task_user ON edge_mobile_record_task(record_user_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_edge_mobile_record_event_task ON edge_mobile_record_event(task_id, id DESC);",
]
