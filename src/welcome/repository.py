from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.textnorm import sanitize_name


def _nz(v: str | None) -> str:
    return sanitize_name(v)


async def upsert_user(
    session: AsyncSession,
    user_id: int,
    is_bot: bool,
    language_code: str | None,
) -> None:
    sql = text(
        """
        INSERT INTO users (user_id, is_bot, language_code, first_seen_at, last_seen_at)
        VALUES (:user_id, :is_bot, :lang, UTC_TIMESTAMP(), UTC_TIMESTAMP())
        ON DUPLICATE KEY UPDATE
            last_seen_at = UTC_TIMESTAMP(),
            is_bot = VALUES(is_bot),
            language_code = VALUES(language_code)
        """
    )
    await session.execute(
        sql,
        {"user_id": user_id, "is_bot": 1 if is_bot else 0, "lang": language_code},
    )


async def insert_name_snapshot_if_new(
    session: AsyncSession,
    user_id: int,
    first_name: str | None,
    last_name: str | None,
    username: str | None,
    seen_at_override: str | None = None,
) -> None:
    """
    Insert a unique (first,last,username) tuple. If duplicate, bump seen_at to max(existing, new).
    If seen_at_override is provided, store that exact timestamp; otherwise use UTC_TIMESTAMP().

    NOTE: Inputs are sanitized to avoid invisible-character toggling.
    """
    fn = _nz(first_name)[:64]
    ln = _nz(last_name)[:64]
    un = _nz(username)[:32]

    if seen_at_override:
        sql = text(
            """
            INSERT INTO user_names (user_id, first_name, last_name, username, seen_at)
            VALUES (:user_id, :fn, :ln, :un, :ts)
            ON DUPLICATE KEY UPDATE
                seen_at = GREATEST(seen_at, VALUES(seen_at))
            """
        )
        params = {"user_id": user_id, "fn": fn, "ln": ln, "un": un, "ts": seen_at_override}
    else:
        sql = text(
            """
            INSERT INTO user_names (user_id, first_name, last_name, username, seen_at)
            VALUES (:user_id, :fn, :ln, :un, UTC_TIMESTAMP())
            ON DUPLICATE KEY UPDATE
                seen_at = GREATEST(seen_at, VALUES(seen_at))
            """
        )
        params = {"user_id": user_id, "fn": fn, "ln": ln, "un": un}
    await session.execute(sql, params)


async def bulk_import_history(
    session: AsyncSession,
    user_id: int,
    snapshots: list[dict],
) -> int:
    """
    Import many snapshots (dicts with keys: first_name, last_name, username, seen_at).
    Returns number of rows attempted (not necessarily inserted due to duplicates).
    """
    count = 0
    for s in snapshots:
        await insert_name_snapshot_if_new(
            session,
            user_id=user_id,
            first_name=s.get("first_name"),
            last_name=s.get("last_name"),
            username=s.get("username"),
            seen_at_override=(s.get("seen_at") or None),
        )
        count += 1
    return count


async def fetch_history_by_user_id(
    session: AsyncSession,
    user_id: int,
) -> list[dict] | None:
    sql = text(
        """
        SELECT
          first_name,
          last_name,
          username,
          DATE_FORMAT(seen_at, '%Y-%m-%d %H:%i:%s') AS seen_at
        FROM user_names
        WHERE user_id = :uid
        ORDER BY seen_at ASC, id ASC
        """
    )
    rows = (await session.execute(sql, {"uid": user_id})).mappings().all()
    if not rows:
        c = await session.execute(
            text("SELECT 1 FROM users WHERE user_id = :uid"),
            {"uid": user_id},
        )
        if c.scalar() is None:
            return None
        return []
    return [dict(r) for r in rows]


async def fetch_history_by_username(
    session: AsyncSession,
    username: str,
) -> tuple[int, list[dict]] | None:
    sql_user = text(
        """
        SELECT user_id
        FROM user_names
        WHERE username = :un
        ORDER BY seen_at DESC
        LIMIT 1
        """
    )
    row = (await session.execute(sql_user, {"un": _nz(username)})).first()
    if not row:
        return None
    user_id = int(row[0])
    history = await fetch_history_by_user_id(session, user_id)
    return (user_id, history or [])


async def fetch_latest_snapshot(
    session: AsyncSession,
    user_id: int,
) -> dict | None:
    """
    Return the most recent (first_name, last_name, username, seen_at) snapshot for the user,
    or None.
    """
    sql = text(
        """
        SELECT
          first_name,
          last_name,
          username,
          DATE_FORMAT(seen_at, '%Y-%m-%d %H:%i:%s') AS seen_at
        FROM user_names
        WHERE user_id = :uid
        ORDER BY seen_at DESC, id DESC
        LIMIT 1
        """
    )
    row = (await session.execute(sql, {"uid": user_id})).mappings().first()
    return dict(row) if row else None


async def fetch_snapshot_before_or_at(
    session: AsyncSession,
    user_id: int,
    cutoff_ts: str,
) -> dict | None:
    """
    Return the most recent snapshot whose seen_at <= cutoff_ts (UTC), or None if no such snapshot
    exists. cutoff_ts should be 'YYYY-MM-DD HH:MM:SS'.
    """
    sql = text(
        """
        SELECT
          first_name,
          last_name,
          username,
          DATE_FORMAT(seen_at, '%Y-%m-%d %H:%i:%s') AS seen_at
        FROM user_names
        WHERE user_id = :uid
          AND seen_at <= :ts
        ORDER BY seen_at DESC, id DESC
        LIMIT 1
        """
    )
    row = (
        (await session.execute(sql, {"uid": user_id, "ts": cutoff_ts}))
        .mappings()
        .first()
    )
    return dict(row) if row else None
