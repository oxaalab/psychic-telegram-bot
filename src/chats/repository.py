from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_LANG = "en"


async def get_chat_language(session: AsyncSession, chat_id: int) -> str:
    """
    Return persisted language for chat or default 'en' if none.
    """
    sql = text(
        """
        SELECT language_code
        FROM chats
        WHERE chat_id = :cid
        LIMIT 1
        """
    )
    row = (await session.execute(sql, {"cid": chat_id})).first()
    return row[0] if row and row[0] else DEFAULT_LANG


async def set_chat_language(
    session: AsyncSession,
    chat_id: int,
    language_code: str,
    title: str | None = None,
) -> None:
    """
    Persist (upsert) language for chat.
    """
    sql = text(
        """
        INSERT INTO chats (
            chat_id, title, language_code, chat_type, bot_status, is_active,
            created_at, updated_at, last_seen_at
        )
        VALUES (
            :cid, :title, :lang, '', 'unknown', 1,
            UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP()
        )
        ON DUPLICATE KEY UPDATE
            title = VALUES(title),
            language_code = VALUES(language_code),
            updated_at = UTC_TIMESTAMP(),
            last_seen_at = UTC_TIMESTAMP()
        """
    )
    await session.execute(
        sql,
        {"cid": chat_id, "title": (title or "")[:255], "lang": language_code},
    )
    await session.commit()


async def touch_chat(
    session: AsyncSession,
    chat_id: int,
    title: str | None = None,
    chat_type: str | None = None,
) -> None:
    """
    Ensure a row exists for this chat (without changing language), update title/type and bump
    timestamps. Also marks a 'last_seen_at' heartbeat whenever we see activity from the chat.
    """
    sql = text(
        """
        INSERT INTO chats (
            chat_id, title, chat_type, language_code, bot_status, is_active,
            created_at, updated_at, last_seen_at
        )
        VALUES (
            :cid, :title, :ctype, 'en', 'unknown', 1,
            UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP()
        )
        ON DUPLICATE KEY UPDATE
            title = VALUES(title),
            chat_type = CASE
                WHEN VALUES(chat_type) <> '' THEN VALUES(chat_type)
                ELSE chat_type
            END,
            updated_at = UTC_TIMESTAMP(),
            last_seen_at = UTC_TIMESTAMP()
        """
    )
    await session.execute(
        sql,
        {"cid": chat_id, "title": (title or "")[:255], "ctype": (chat_type or "")[:16]},
    )
    await session.commit()


async def set_bot_presence(
    session: AsyncSession,
    chat_id: int,
    *,
    title: str | None,
    chat_type: str | None,
    status: str,
) -> None:
    """
    Upsert the current presence state of the bot in a chat.

    status: Telegram ChatMember status string ('member', 'administrator', 'left', 'kicked', etc.)
    Active when status in {'member','administrator'}; inactive otherwise.
    Records last_joined_at / last_left_at transitions.
    """
    active = 1 if status in ("member", "administrator") else 0
    sql = text(
        """
        INSERT INTO chats (
            chat_id, title, chat_type, language_code, bot_status, is_active,
            created_at, updated_at, last_seen_at, last_joined_at, last_left_at
        )
        VALUES (
            :cid, :title, :ctype, 'en', :status, :active,
            UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP(),
            CASE WHEN :active = 1 THEN UTC_TIMESTAMP() ELSE NULL END,
            CASE WHEN :active = 0 THEN UTC_TIMESTAMP() ELSE NULL END
        )
        ON DUPLICATE KEY UPDATE
            title = VALUES(title),
            chat_type = VALUES(chat_type),
            bot_status = VALUES(bot_status),
            -- transition-aware timestamps
            last_joined_at = IF(
                is_active = 0 AND VALUES(is_active) = 1,
                UTC_TIMESTAMP(),
                last_joined_at
            ),
            last_left_at = IF(
                is_active = 1 AND VALUES(is_active) = 0,
                UTC_TIMESTAMP(),
                last_left_at
            ),
            is_active = VALUES(is_active),
            updated_at = UTC_TIMESTAMP(),
            last_seen_at = UTC_TIMESTAMP()
        """
    )
    await session.execute(
        sql,
        {
            "cid": chat_id,
            "title": (title or "")[:255],
            "ctype": (chat_type or "")[:16],
            "status": status[:16],
            "active": active,
        },
    )
    await session.commit()


async def mark_chat_inactive(
    session: AsyncSession,
    chat_id: int,
    *,
    title: str | None = None,
    chat_type: str | None = None,
    status: str = "left",
) -> None:
    """
    Convenience: mark chat inactive (bot removed). Uses set_bot_presence underneath.
    """
    await set_bot_presence(
        session,
        chat_id,
        title=title,
        chat_type=chat_type,
        status=status,
    )


async def add_or_touch_member(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
) -> None:
    """
    Record that we saw `user_id` in `chat_id` now.
    """
    sql = text(
        """
        INSERT INTO chat_members (chat_id, user_id, first_seen_at, last_seen_at)
        VALUES (:cid, :uid, UTC_TIMESTAMP(), UTC_TIMESTAMP())
        ON DUPLICATE KEY UPDATE
            last_seen_at = UTC_TIMESTAMP()
        """
    )
    await session.execute(sql, {"cid": chat_id, "uid": user_id})
    await session.commit()


async def remove_member(session: AsyncSession, chat_id: int, user_id: int) -> None:
    sql = text("DELETE FROM chat_members WHERE chat_id = :cid AND user_id = :uid")
    await session.execute(sql, {"cid": chat_id, "uid": user_id})
    await session.commit()


async def prune_chat_members_for_chat(session: AsyncSession, chat_id: int) -> None:
    sql = text("DELETE FROM chat_members WHERE chat_id = :cid")
    await session.execute(sql, {"cid": chat_id})
    await session.commit()


async def pick_members_for_scan(
    session: AsyncSession,
    limit: int,
) -> list[tuple[int, int, str]]:
    """
    Return up to `limit` (chat_id, user_id, last_checked_at) pairs ordered by least recently
    checked first. last_checked_at is returned as 'YYYY-MM-DD HH:MM:SS' (UTC).
    """
    sql = text(
        """
        SELECT
            chat_id,
            user_id,
            DATE_FORMAT(last_checked_at, '%Y-%m-%d %H:%i:%s') AS last_checked_at
        FROM chat_members
        ORDER BY last_checked_at ASC, last_seen_at DESC
        LIMIT :lim
        """
    )
    rows = (await session.execute(sql, {"lim": limit})).all()
    out: list[tuple[int, int, str]] = []
    for r in rows:
        chat_id = int(r[0])
        user_id = int(r[1])
        lchk: str = str(r[2] or "1970-01-01 00:00:01")
        out.append((chat_id, user_id, lchk))
    return out


async def pick_stale_members_for_chat(
    session: AsyncSession,
    chat_id: int,
    limit: int,
) -> list[tuple[int, int, str]]:
    """
    Return up to `limit` (chat_id, user_id, last_checked_at) for a specific chat,
    ordered by least-recently-checked first. last_checked_at is 'YYYY-MM-DD HH:MM:SS' (UTC).
    """
    sql = text(
        """
        SELECT
            chat_id,
            user_id,
            DATE_FORMAT(last_checked_at, '%Y-%m-%d %H:%i:%s') AS last_checked_at
        FROM chat_members
        WHERE chat_id = :cid
        ORDER BY last_checked_at ASC, last_seen_at DESC
        LIMIT :lim
        """
    )
    rows = (await session.execute(sql, {"cid": chat_id, "lim": limit})).all()
    out: list[tuple[int, int, str]] = []
    for r in rows:
        cid = int(r[0])
        uid = int(r[1])
        lchk: str = str(r[2] or "1970-01-01 00:00:01")
        out.append((cid, uid, lchk))
    return out


async def get_member_last_checked(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
) -> str:
    """
    Return the per-(chat,user) last_checked_at watermark as 'YYYY-MM-DD HH:MM:SS' (UTC).
    Defaults to '1970-01-01 00:00:01' if the row is missing.
    """
    sql = text(
        """
        SELECT DATE_FORMAT(last_checked_at, '%Y-%m-%d %H:%i:%s') AS last_checked_at
        FROM chat_members
        WHERE chat_id = :cid AND user_id = :uid
        LIMIT 1
        """
    )
    row = (await session.execute(sql, {"cid": chat_id, "uid": user_id})).first()
    return row[0] if row and row[0] else "1970-01-01 00:00:01"


async def mark_checked(session: AsyncSession, chat_id: int, user_id: int) -> None:
    sql = text(
        """
        UPDATE chat_members
        SET last_checked_at = UTC_TIMESTAMP()
        WHERE chat_id = :cid AND user_id = :uid
        """
    )
    await session.execute(sql, {"cid": chat_id, "uid": user_id})


async def get_member_first_seen(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
) -> str:
    """
    Return 'YYYY-MM-DD HH:MM:SS' (UTC) for when we first saw this user in this chat.
    Falls back to epoch-like minimum if unknown (so callers can detect 'no baseline').
    """
    sql = text(
        """
        SELECT DATE_FORMAT(first_seen_at, '%Y-%m-%d %H:%i:%s') AS first_seen_at
        FROM chat_members
        WHERE chat_id = :cid AND user_id = :uid
        LIMIT 1
        """
    )
    row = (await session.execute(sql, {"cid": chat_id, "uid": user_id})).first()
    return row[0] if row and row[0] else "1970-01-01 00:00:01"


async def get_last_announced_fp(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
) -> str:
    """
    Return the last announced fingerprint for this (chat_id, user_id), or '' if none.
    """
    sql = text(
        """
        SELECT last_announced_fp
        FROM chat_members
        WHERE chat_id = :cid AND user_id = :uid
        LIMIT 1
        """
    )
    row = (await session.execute(sql, {"cid": chat_id, "uid": user_id})).first()
    return (row[0] or "") if row else ""


async def set_last_announced_fp(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    fp: str,
) -> bool:
    """
    Atomically update the last announced fingerprint + timestamp for this (chat_id, user_id).
    Returns True iff the row was updated (i.e., fingerprint actually changed).
    Assumes the row exists (callers normally ensure via add_or_touch_member/pick_members_for_scan).
    """
    sql = text(
        """
        UPDATE chat_members
        SET last_announced_fp = :fp,
            last_announced_at = UTC_TIMESTAMP()
        WHERE chat_id = :cid AND user_id = :uid
          AND (last_announced_fp IS NULL OR last_announced_fp <> :fp)
        """
    )
    result = await session.execute(
        sql, {"cid": chat_id, "uid": user_id, "fp": fp[:300]}
    )

    try:
        updated = int(getattr(result, "rowcount", 0)) > 0
    except Exception:
        updated = False
    return updated
