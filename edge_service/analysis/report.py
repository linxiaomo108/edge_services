from __future__ import annotations

import json
import os
from pathlib import Path


def render_report_html(template_path: str, data: dict, out_path: str) -> str:
    tpl = Path(template_path).read_text(encoding="utf-8", errors="ignore")
    payload = json.dumps(data, ensure_ascii=False)
    html = tpl.replace("{{DATA_JSON}}", payload)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return str(out)


def default_template_path() -> str:
    base = Path(__file__).resolve().parents[2]
    p = base / "assets" / "templates" / "report_template_min.html"
    return str(p)
