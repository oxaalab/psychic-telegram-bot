from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from core.textnorm import sanitize_name
from chats.repository import set_last_announced_fp

_ANNOUNCE_TTL_SECONDS = 300
_MAX_ENTRIES = 100_000


def _norm(v: str | None) -> str:
    return sanitize_name(v)


def name_fingerprint(first_name: str, last_name: str, username: str) -> str:
    """
    Stable fingerprint for a user's current visible identity fields.
    If any of these change (after canonicalization), the fingerprint changes.
    """
    return "\x1f".join((_norm(first_name), _norm(last_name), _norm(username)))


@dataclass
class _Entry:
    fp: str
    ts: float


class _LRU(OrderedDict[tuple[int, int], _Entry]):
    maxsize: int = _MAX_ENTRIES

    def set(self, key: tuple[int, int], entry: _Entry) -> None:
        OrderedDict.__setitem__(self, key, entry)
        self.move_to_end(key)
        if len(self) > self.maxsize:
            self.popitem(last=False)

    def get_move(self, key: tuple[int, int]) -> _Entry | None:
        item = OrderedDict.get(self, key)
        if item is not None:
            self.move_to_end(key)
        return item


_store: _LRU = _LRU()


def should_announce(
    chat_id: int,
    user_id: int,
    fingerprint: str,
    *,
    ttl: int = _ANNOUNCE_TTL_SECONDS,
) -> bool:
    """
    True if we should announce a change for this (chat,user) with this fingerprint now.
    Suppresses repeats for the same fingerprint within TTL.
    Allows immediate re-announce if the fingerprint actually changed.
    """
    key = (int(chat_id), int(user_id))
    now = time.time()
    prev = _store.get_move(key)
    if prev is not None:
        if prev.fp == fingerprint and (now - prev.ts) < ttl:
            return False
    _store.set(key, _Entry(fp=fingerprint, ts=now))
    return True


async def should_announce_persisted(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    fingerprint: str,
    *,
    memory_ttl: int = _ANNOUNCE_TTL_SECONDS,
) -> bool:
    """
    Combined guard:
      1) Fast in-memory TTL (avoid bursts from concurrent handlers in this process)
      2) Atomic persisted DB guard (never repeat for the same fp across processes)
    """
    if not should_announce(chat_id, user_id, fingerprint, ttl=memory_ttl):
        return False

    # Atomic conditional update: returns True only if we actually changed the fp.
    changed = await set_last_announced_fp(session, chat_id, user_id, fingerprint)
    return bool(changed)
