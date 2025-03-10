from __future__ import annotations

from time import perf_counter
from typing import Any

from sqlalchemy import text

from core.db import get_engine


async def check_db() -> dict[str, Any]:
    t0 = perf_counter()
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        dt_ms = (perf_counter() - t0) * 1000.0
        return {"ok": True, "latency_ms": round(dt_ms, 2), "error": None}
    except Exception as e:
        dt_ms = (perf_counter() - t0) * 1000.0
        return {"ok": False, "latency_ms": round(dt_ms, 2), "error": str(e)}
