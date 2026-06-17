from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response


def build_app() -> FastAPI:
    target = os.getenv("EDGE_MONITOR_TARGET") or "http://localhost:18080"
    target = target.rstrip("/")

    base_dir = Path(__file__).resolve().parent.parent
    page_path = base_dir / "monitor_ui" / "node_detail.html"

    app = FastAPI()
    client = httpx.AsyncClient(timeout=30.0)

    @app.on_event("shutdown")
    async def _shutdown():
        await client.aclose()

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html = page_path.read_text(encoding="utf-8", errors="ignore")
        return HTMLResponse(html)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy_api(path: str, request: Request):
        url = f"{target}/api/{path}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}}
        body = await request.body()
        resp = await client.request(request.method, url, params=request.query_params, headers=headers, content=body)
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() in {"content-type"}}
        return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)

    return app

