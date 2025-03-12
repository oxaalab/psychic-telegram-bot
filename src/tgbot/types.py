from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class NameSnapshot:
    first_name: str
    last_name: str
    username: str
    seen_at: str
