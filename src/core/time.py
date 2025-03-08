from __future__ import annotations

from datetime import datetime


def utcnow() -> datetime:
    return datetime.now(tz=datetime.UTC)
