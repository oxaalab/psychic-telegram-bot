from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request):
    app = request.app
    return {
        "ok": bool(getattr(app.state, "db_ok", False)),
        "bot_ready": bool(getattr(app.state, "bot_ready", False)),
        "bot_error": getattr(app.state, "bot_error", None),
        "db_ok": bool(getattr(app.state, "db_ok", False)),
        "db_latency_ms": getattr(app.state, "db_latency_ms", None),
        "webhook": getattr(app.state, "webhook_url", None),
    }
