from __future__ import annotations

import html

from telegram import User

from core.db import SessionLocal
from i18n.messages import t

from .formatter import display_name
from .repository import (
    fetch_history_by_user_id,
    insert_name_snapshot_if_new,
    upsert_user,
)


async def record_user_snapshot(session, tg_user: User) -> None:
    """
    Upsert user row and create a new name snapshot if needed.
    This is the ONLY source of history: what our bot observes going forward.
    """
    await upsert_user(
        session,
        tg_user.id,
        tg_user.is_bot,
        getattr(tg_user, "language_code", None),
    )
    await insert_name_snapshot_if_new(
        session,
        tg_user.id,
        tg_user.first_name,
        tg_user.last_name,
        tg_user.username,
    )
    await session.commit()


def _format_history_verbose(history: list[dict], lang: str) -> str:
    """
    Build a verbose, chronological list of snapshots using localized labels:
    1. First: X; Last: Y; Username: @z — 2024-01-01 10:00:00 UTC
    """
    label_first = t(lang, "labels.first")
    label_last = t(lang, "labels.last")
    label_username = t(lang, "labels.username")
    none_text = t(lang, "general.none", default="(none)")

    lines: list[str] = []
    for i, s in enumerate(history, 1):
        fn = html.escape(s.get("first_name") or "")
        ln = html.escape(s.get("last_name") or "")
        un_raw = s.get("username") or ""
        un = f"@{html.escape(un_raw)}" if un_raw else none_text
        when = (s.get("seen_at") or "unknown") + " UTC"
        lines.append(
            f"{i}. <b>{label_first}:</b> {fn or none_text}; "
            f"<b>{label_last}:</b> {ln or none_text}; "
            f"<b>{label_username}:</b> {un} — {when}"
        )
    return "\n".join(lines)


async def build_welcome_message(tg_user: User, lang: str) -> str:
    """
    Build a welcome message using ONLY the local database (no external imports),
    showing a verbose chronological history (First, Last, Username), localized.
    """
    async with SessionLocal() as session:
        history = await fetch_history_by_user_id(session, tg_user.id)

    history = history or []

    mention_text = html.escape(
        tg_user.full_name or tg_user.first_name or tg_user.username or "there"
    )
    mention = f'<a href="tg://user?id={tg_user.id}">{mention_text}</a>'
    header = t(lang, "join.welcome_header", mention=mention)

    none_text = t(lang, "general.none", default="(none)")

    if not history:
        current = display_name(
            tg_user.first_name, tg_user.last_name, tg_user.username, none_text=none_text
        )
        return f"{header}\n{t(lang, 'join.first_time', current=current)}"

    body = _format_history_verbose(history, lang)

    current = history[-1]
    current_disp = display_name(
        current.get("first_name"),
        current.get("last_name"),
        current.get("username"),
        none_text=none_text,
    )

    return (
        f"{header}\n"
        f"{t(lang, 'join.history_intro')}\n"
        f"{body}\n\n"
        f"{t(lang, 'current_name', name=current_disp)}"
    )
