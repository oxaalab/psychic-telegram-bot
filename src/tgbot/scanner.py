from __future__ import annotations

import asyncio
import logging
import time

from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import Application, ContextTypes, JobQueue

from chats.repository import (
    get_chat_language,
    get_member_first_seen,
    mark_chat_inactive,
    mark_checked,
    pick_members_for_scan,
    prune_chat_members_for_chat,
    remove_member,
)
from core.config import AppConfig
from core.db import SessionLocal
from core.textnorm import sanitize_name
from welcome.repository import fetch_snapshot_before_or_at
from welcome.service import record_user_snapshot

from .announce_guard import name_fingerprint, should_announce_persisted
from .handlers import _announce_change_if_needed

log = logging.getLogger(__name__)


def _norm(v: str | None) -> str:
    return sanitize_name(v)


def schedule_scanner(application: Application, config: AppConfig) -> None:
    """
    Register a repeating job that scans known chat members and announces name changes.
    If PTB was installed without the job-queue extra, create a JobQueue on the fly.

    Improvements:
    - Start quickly (configurable first delay) instead of waiting a full interval.
    - Soft rate limiting and RetryAfter handling to stay healthy in large groups.
    """
    jq = getattr(application, "job_queue", None)
    if jq is None:
        try:
            jq = JobQueue()
            jq.set_application(application)
            jq.start()
            log.info("JobQueue was not preset; started a new JobQueue instance.")
        except Exception as e:
            log.warning("JobQueue not available; name-change scanner disabled: %s", e)
            return

    min_gap = 1.0 / max(1, int(config.scan_max_rps))
    first_delay = max(1, int(getattr(config, "scan_first_delay_secs", 5)))

    jq.run_repeating(
        callback=_scan_tick,
        interval=max(10, int(config.scan_interval_secs)),
        first=first_delay,
        name="name-change-scanner",
        data={
            "batch_size": int(config.scan_batch_size),
            "min_gap": float(min_gap),
            "retry_leeway": int(config.scan_retry_after_leeway_secs),
            "last_call_ts": 0.0,
        },
    )
    log.info(
        "Name-change scanner scheduled: every %ss (first=%ss), batch=%s, max_rps=%s",
        config.scan_interval_secs,
        first_delay,
        config.scan_batch_size,
        config.scan_max_rps,
    )


async def _scan_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data or {}
    batch_size = int(job_data.get("batch_size", 100))
    min_gap = float(job_data.get("min_gap", 0.0))
    retry_leeway = int(job_data.get("retry_leeway", 1))
    last_call_ts = float(job_data.get("last_call_ts", 0.0))

    bot = context.bot

    async def _respect_ratelimit() -> None:
        nonlocal last_call_ts
        if min_gap <= 0:
            return
        now = time.monotonic()
        sleep_for = (last_call_ts + min_gap) - now
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        last_call_ts = time.monotonic()
        job_data["last_call_ts"] = last_call_ts

    async with SessionLocal() as session:
        pairs = await pick_members_for_scan(session, limit=batch_size)

    if not pairs:
        return

    idx = 0
    while idx < len(pairs):
        chat_id, user_id, last_checked_at = pairs[idx]
        idx += 1

        try:
            await _respect_ratelimit()
            member = await bot.get_chat_member(chat_id, user_id)
        except RetryAfter as e:
            wait_s = max(1, int(getattr(e, "retry_after", 1))) + max(0, retry_leeway)
            log.warning(
                "Scanner: rate-limited (RetryAfter=%ss) on chat=%s user=%s; sleeping...",
                wait_s,
                chat_id,
                user_id,
            )
            await asyncio.sleep(wait_s)
            pairs.append((chat_id, user_id, last_checked_at))
            last_call_ts = time.monotonic()
            job_data["last_call_ts"] = last_call_ts
            continue
        except Forbidden:
            log.info(
                "Scanner: Forbidden for chat %s (likely left). "
                "Pruning members and marking inactive.",
                chat_id,
            )
            async with SessionLocal() as session:
                await prune_chat_members_for_chat(session, chat_id)
                try:
                    await mark_chat_inactive(session, chat_id, status="left")
                except Exception:
                    log.exception(
                        "Scanner: failed to mark chat %s inactive",
                        chat_id,
                    )
            continue
        except BadRequest as e:
            log.debug(
                "Scanner: BadRequest for chat=%s user=%s: %s", chat_id, user_id, e
            )
            async with SessionLocal() as session:
                await mark_checked(session, chat_id, user_id)
                await session.commit()
            continue
        except Exception as e:
            log.warning(
                "Scanner: get_chat_member failed chat=%s user=%s: %s",
                chat_id,
                user_id,
                e,
            )
            continue

        try:
            status = getattr(member, "status", None)
            status_value = getattr(status, "value", status)
        except Exception:
            status_value = None

        if status_value in ("left", "kicked"):
            async with SessionLocal() as session:
                try:
                    await remove_member(session, chat_id, user_id)
                    await mark_checked(session, chat_id, user_id)
                    await session.commit()
                except Exception:
                    log.exception(
                        "Scanner: failed to remove left user chat=%s user=%s",
                        chat_id,
                        user_id,
                    )
            continue

        user = member.user
        if not user or user.is_bot:
            async with SessionLocal() as session:
                await mark_checked(session, chat_id, user_id)
                await session.commit()
            continue

        curr_fn = _norm(user.first_name)
        curr_ln = _norm(user.last_name)
        curr_un = _norm(user.username)

        async with SessionLocal() as session:
            prev_for_chat = await fetch_snapshot_before_or_at(
                session,
                user.id,
                last_checked_at,
            )

            if not prev_for_chat:
                first_seen_at = await get_member_first_seen(session, chat_id, user_id)
                if first_seen_at and first_seen_at != "1970-01-01 00:00:01":
                    prev_for_chat = await fetch_snapshot_before_or_at(
                        session,
                        user.id,
                        first_seen_at,
                    )

            await record_user_snapshot(session, user)
            await mark_checked(session, chat_id, user_id)
            lang = await get_chat_language(session, chat_id)
            await session.commit()

        diffs: list[tuple[str, str, str]] = []
        if prev_for_chat:
            prev_fn = _norm(prev_for_chat.get("first_name"))
            prev_ln = _norm(prev_for_chat.get("last_name"))
            prev_un = _norm(prev_for_chat.get("username"))
            if prev_fn != curr_fn:
                diffs.append(("first", prev_fn, curr_fn))
            if prev_ln != curr_ln:
                diffs.append(("last", prev_ln, curr_ln))
            if prev_un != curr_un:
                diffs.append(("username", prev_un, curr_un))

        if not diffs:
            continue

        fp = name_fingerprint(curr_fn, curr_ln, curr_un)
        async with SessionLocal() as s:
            if not await should_announce_persisted(s, chat_id, user_id, fp):
                continue
            await s.commit()

        try:
            await _respect_ratelimit()
            await _announce_change_if_needed(
                context=context,
                chat_id=chat_id,
                reply_to_message_id=None,
                user=user,
                changes=diffs,
                lang=lang,
            )
        except RetryAfter as e:
            wait_s = max(1, int(getattr(e, "retry_after", 1))) + max(0, retry_leeway)
            log.warning(
                "Scanner: rate-limited sending message (RetryAfter=%ss) chat=%s",
                wait_s,
                chat_id,
            )
            await asyncio.sleep(wait_s)
        except Exception:
            log.exception(
                "Scanner: failed to announce change chat=%s user=%s", chat_id, user.id
            )
