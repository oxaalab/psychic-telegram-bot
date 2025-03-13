from __future__ import annotations

import html
import logging
import time
from collections import OrderedDict
from collections.abc import Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.constants import ParseMode
from telegram.error import RetryAfter, BadRequest, Forbidden
from telegram.ext import (
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from chats.repository import (
    add_or_touch_member,
    get_chat_language,
    get_member_first_seen,
    get_member_last_checked,
    mark_checked,
    remove_member,
    set_bot_presence,
    set_chat_language,
    touch_chat,
    pick_stale_members_for_chat,  # NEW
)
from core.db import SessionLocal  # NOTE: SessionLocal() is a factory configured at runtime.
from core.textnorm import sanitize_name
from i18n.messages import available_codes, language_name, t
from welcome.repository import (
    fetch_history_by_user_id,
    fetch_history_by_username,
    fetch_snapshot_before_or_at,
)
from welcome.service import build_welcome_message, record_user_snapshot

from .announce_guard import name_fingerprint, should_announce_persisted

logger = logging.getLogger(__name__)

_ANONYMOUS_ADMIN_USER_ID = 1087968824

_SNAPSHOT_THROTTLE_SECONDS = 300
_CACHE_MAX_USERS = 10000

# --- Dedup guard to prevent double welcomes (NEW_CHAT_MEMBERS + CHAT_MEMBER) ---
_WELCOME_TTL_SECONDS = 30
_WELCOME_GUARD_MAX = 100_000

# --- Opportunistic mini-scan settings (fast & light per chat activity) ---
_OPPORTUNISTIC_SCAN_LIMIT = 3


class _RecentWelcomeGuard(OrderedDict[tuple[int, int], float]):
    """
    TTL-based set: key=(chat_id,user_id) -> last welcome timestamp.
    Used to deduplicate welcomes across multiple update types arriving close together.
    """

    def _cleanup(self, now: float, ttl: float) -> None:
        while self:
            (old_key, ts) = next(iter(self.items()))
            if (now - ts) > ttl:
                self.popitem(last=False)
            else:
                break

    def should_welcome(
        self, chat_id: int, user_id: int, *, ttl: int = _WELCOME_TTL_SECONDS
    ) -> bool:
        now = time.time()
        self._cleanup(now, ttl)
        key = (int(chat_id), int(user_id))
        ts = OrderedDict.get(self, key)
        if ts is not None and (now - ts) < ttl:
            return False
        # mark immediately to avoid racing between handlers
        OrderedDict.__setitem__(self, key, now)
        self.move_to_end(key)
        if len(self) > _WELCOME_GUARD_MAX:
            self.popitem(last=False)
        return True


_welcome_guard = _RecentWelcomeGuard()


class _UserNameCache(OrderedDict[int, tuple[str, str, str, float]]):
    """LRU cache: user_id -> (first, last, username, last_checked_ts)"""

    def __init__(self, maxsize: int):
        super().__init__()
        self._maxsize = maxsize

    @staticmethod
    def _norm(v: str | None) -> str:
        return sanitize_name(v)

    def get_tuple(self, user: User) -> tuple[str, str, str]:
        return (
            self._norm(user.first_name),
            self._norm(user.last_name),
            self._norm(user.username),
        )

    def get_cached(self, user_id: int) -> tuple[str, str, str, float] | None:
        item = OrderedDict.get(self, user_id)
        if item is not None:
            self.move_to_end(user_id)
        return item

    def put(self, user_id: int, fn: str, ln: str, un: str) -> None:
        OrderedDict.__setitem__(self, user_id, (fn, ln, un, time.time()))
        self.move_to_end(user_id)
        if len(self) > self._maxsize:
            self.popitem(last=False)


_name_cache = _UserNameCache(_CACHE_MAX_USERS)


def build_handlers():
    """
    Handler order matters. We place the callback handler BEFORE the generic catch-all.
    """
    return [
        ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER),
        ChatMemberHandler(on_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER),
        CallbackQueryHandler(on_setlang_button, pattern=r"^setlang:"),
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members),
        MessageHandler((~filters.StatusUpdate.ALL) & (~filters.COMMAND), on_any_message),
        CommandHandler("history", cmd_history),
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler("setlang", cmd_setlang),
        TypeHandler(Update, on_any_update),
    ]


def _norm(v: str | None) -> str:
    return sanitize_name(v)


def _is_group(chat) -> bool:
    return bool(chat and getattr(chat, "type", "") in {"group", "supergroup"})


async def _is_admin(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    *,
    relax_if_unknown: bool = False,
) -> bool:
    try:
        if int(user_id) == _ANONYMOUS_ADMIN_USER_ID:
            return True

        member = None
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
        except RetryAfter as e:
            logger.warning(
                "get_chat_member rate-limited in chat %s (user %s): %s", chat_id, user_id, e
            )
        except Exception as e:
            logger.debug("get_chat_member failed chat=%s user=%s: %s", chat_id, user_id, e)

        if member is not None:
            status = getattr(member, "status", None)
            status_value = getattr(status, "value", status)
            if status_value in ("administrator", "creator", "owner"):
                return True
            perms = (
                "can_manage_chat",
                "can_change_info",
                "can_delete_messages",
                "can_invite_users",
                "can_restrict_members",
                "can_promote_members",
                "can_pin_messages",
                "can_manage_topics",
            )
            if any(bool(getattr(member, p, False)) for p in perms):
                return True

        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            for a in admins or []:
                u = getattr(a, "user", None)
                if u and u.id == user_id:
                    return True
            if int(user_id) == _ANONYMOUS_ADMIN_USER_ID and any(
                bool(getattr(a, "is_anonymous", False)) for a in (admins or [])
            ):
                return True
        except Exception as e:
            logger.debug("get_chat_administrators failed chat=%s: %s", chat_id, e)

        return relax_if_unknown
    except Exception as e:
        logger.debug("Unexpected error in _is_admin (chat=%s user=%s): %s", chat_id, user_id, e)
        return relax_if_unknown


def _diff_snap(prev: dict | None, curr_fn: str, curr_ln: str, curr_un: str) -> list[tuple[str, str, str]]:
    if not prev:
        return []
    # Sanitize previous values before comparison to avoid false diffs.
    prev_fn = _norm(prev.get("first_name"))
    prev_ln = _norm(prev.get("last_name"))
    prev_un = _norm(prev.get("username"))
    changes: list[tuple[str, str, str]] = []
    if prev_fn != curr_fn:
        changes.append(("first", prev_fn, curr_fn))
    if prev_ln != curr_ln:
        changes.append(("last", prev_ln, curr_ln))
    if prev_un != curr_un:
        changes.append(("username", prev_un, curr_un))
    return changes


def _render_history_block(snapshots: list[dict], lang: str) -> str:
    if not snapshots:
        return t(lang, "commands.history.no_user_id")

    label_first = t(lang, "labels.first")
    label_last = t(lang, "labels.last")
    label_username = t(lang, "labels.username")
    none_text = t(lang, "general.none", default="(none)")

    lines: list[str] = []
    for i, s in enumerate(snapshots, 1):
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


def _chunked(seq: Iterable[str], size: int) -> Iterable[list[str]]:
    buf: list[str] = []
    for item in seq:
        buf.append(item)
        if len(buf) == size:
            yield buf
            buf = []
    if buf:
        yield buf


def _build_lang_keyboard() -> InlineKeyboardMarkup:
    codes = sorted(set(available_codes()))
    rows: list[list[InlineKeyboardButton]] = []
    for chunk in _chunked(codes, 2):
        row = [
            InlineKeyboardButton(
                text=f"{language_name(code)} ({code})",
                callback_data=f"setlang:{code}",
            )
            for code in chunk
        ]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def _send_language_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    current_lang: str,
) -> None:
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=t(current_lang, "setup.choose_language"),
            reply_markup=_build_lang_keyboard(),
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Failed to send language selection prompt to chat_id=%s", chat_id)


async def _handle_bot_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Persist presence; seed scanner with current admins; prompt admins for language.
    """
    chat = update.effective_chat
    if not chat or not _is_group(chat):
        return

    mycm = update.my_chat_member
    if not mycm:
        return

    new_status = getattr(mycm.new_chat_member, "status", None)
    old_status = getattr(mycm.old_chat_member, "status", None)
    new_val = getattr(new_status, "value", new_status)
    old_val = getattr(old_status, "value", old_status)

    title = getattr(chat, "title", None)
    ctype = getattr(chat, "type", None)

    try:
        async with SessionLocal() as session:
            await set_bot_presence(
                session,
                chat.id,
                title=title,
                chat_type=ctype,
                status=(new_val or "unknown"),
            )
    except Exception:
        logger.exception("Failed to persist bot presence for chat_id=%s", chat.id)

    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        if admins:
            async with SessionLocal() as session:
                for adm in admins:
                    u = getattr(adm, "user", None)
                    if not u or getattr(u, "is_bot", False):
                        continue
                    await add_or_touch_member(session, chat.id, u.id)
                    try:
                        await record_user_snapshot(session, u)
                    except Exception:
                        logger.debug("record_user_snapshot failed for admin %s in chat %s", u.id, chat.id)
                    try:
                        await mark_checked(session, chat.id, u.id)
                    except Exception:
                        logger.debug("mark_checked failed for admin %s in chat %s", u.id, chat.id)
                await session.commit()
            logger.info("Seeded %s admin(s) for scanning in chat_id=%s (baselined)", len(admins), chat.id)
    except Exception as e:
        logger.debug("Could not seed admins for chat_id=%s: %s", chat.id, e)

    if (new_val in ("member", "administrator")) and (old_val in ("left", "kicked")):
        async with SessionLocal() as session:
            current_lang = await get_chat_language(session, chat.id)
        await _send_language_prompt(context, chat.id, current_lang)


async def _announce_change_if_needed(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_to_message_id: int | None,
    user: User,
    changes: list[tuple[str, str, str]],
    lang: str,
) -> None:
    if not changes:
        return
    try:
        mention_text = html.escape(user.full_name or user.first_name or user.username or "user")
        mention = f'<a href="tg://user?id={user.id}">{mention_text}</a>'
        arrow = t(lang, "general.arrow", default="→")
        none_text = t(lang, "general.none", default="(none)")

        change_lines: list[str] = []
        for key, old, new in changes:
            label = t(lang, f"labels.{key}")
            old_disp = f"@{html.escape(old)}" if (key == "username" and old) else (html.escape(old) or none_text)
            new_disp = f"@{html.escape(new)}" if (key == "username" and new) else (html.escape(new) or none_text)
            change_lines.append(f"• <b>{label}</b>: {old_disp} {arrow} {new_disp}")

        async with SessionLocal() as session:
            full_history = await fetch_history_by_user_id(session, user.id) or []
        history_header = t(lang, "changes.history_intro")
        history_block = _render_history_block(full_history, lang)

        text_msg = (
            f"{t(lang, 'changes.announcement', mention=mention)}\n"
            + "\n".join(change_lines)
            + f"\n\n{history_header}\n"
            + history_block
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=text_msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_to_message_id=reply_to_message_id,
            allow_sending_without_reply=True,
        )
    except Exception:
        logger.exception("Failed to announce change for user_id=%s", user.id)


def _should_skip(user: User) -> bool:
    fn, ln, un = _name_cache.get_tuple(user)
    cached = _name_cache.get_cached(user.id)
    now = time.time()
    if cached:
        c_fn, c_ln, c_un, c_ts = cached
        if fn == c_fn and ln == c_ln and un == c_un and (now - c_ts) < _SNAPSHOT_THROTTLE_SECONDS:
            return True
    return False


def _update_cache(user: User) -> tuple[str, str, str]:
    fn, ln, un = _name_cache.get_tuple(user)
    _name_cache.put(user.id, fn, ln, un)
    return fn, ln, un


async def _opportunistic_scan_stale(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, limit: int = _OPPORTUNISTIC_SCAN_LIMIT
) -> None:
    """
    Lightweight on-demand scan for a few stale members in this chat. This complements the
    periodic scanner and helps catch silent renames without waiting for a user to speak.
    """
    try:
        async with SessionLocal() as session:
            pairs = await pick_stale_members_for_chat(session, chat_id, limit)
        if not pairs:
            return

        for cid, uid, last_checked_at in pairs:
            try:
                member = await context.bot.get_chat_member(cid, uid)
            except Forbidden:
                # User/chat inaccessible: mark row & continue.
                async with SessionLocal() as s:
                    try:
                        await remove_member(s, cid, uid)
                        await mark_checked(s, cid, uid)
                        await s.commit()
                    except Exception:
                        logger.exception("opportunistic: cleanup failed for chat=%s user=%s", cid, uid)
                continue
            except BadRequest:
                async with SessionLocal() as s:
                    await mark_checked(s, cid, uid)
                    await s.commit()
                continue
            except RetryAfter:
                # Back off silently; next activity will retry.
                continue
            except Exception as e:
                logger.debug("opportunistic: get_chat_member failed chat=%s user=%s: %s", cid, uid, e)
                continue

            user = member.user
            if not user or user.is_bot:
                async with SessionLocal() as s:
                    await mark_checked(s, cid, uid)
                    await s.commit()
                continue

            curr_fn = _norm(user.first_name)
            curr_ln = _norm(user.last_name)
            curr_un = _norm(user.username)

            async with SessionLocal() as session:
                prev_for_chat = await fetch_snapshot_before_or_at(session, user.id, last_checked_at)
                if not prev_for_chat:
                    first_seen_at = await get_member_first_seen(session, cid, uid)
                    if first_seen_at and first_seen_at != "1970-01-01 00:00:01":
                        prev_for_chat = await fetch_snapshot_before_or_at(session, user.id, first_seen_at)

                await record_user_snapshot(session, user)
                await mark_checked(session, cid, uid)
                lang = await get_chat_language(session, cid)
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
                if not await should_announce_persisted(s, cid, uid, fp):
                    continue
                await s.commit()

            try:
                await _announce_change_if_needed(
                    context=context,
                    chat_id=cid,
                    reply_to_message_id=None,
                    user=user,
                    changes=diffs,
                    lang=lang,
                )
            except RetryAfter:
                pass
            except Exception:
                logger.exception("opportunistic: failed to announce change chat=%s user=%s", cid, uid)
    except Exception:
        logger.debug("opportunistic scan error", exc_info=True)


# ---------------------------
# Language selection handlers
# ---------------------------

async def on_setlang_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle inline keyboard clicks like 'setlang:en'.
    Admin-only in groups. Replies with a short confirmation alert.
    """
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("setlang:"):
        return

    code = query.data.split(":", 1)[1].strip().lower()
    chat = update.effective_chat
    user = update.effective_user

    # Get current language to localize possible errors.
    async with SessionLocal() as session:
        cur_lang = await get_chat_language(session, chat.id if chat else 0)

    if _is_group(chat) and (not user or not await _is_admin(context, chat.id, user.id)):
        await query.answer(t(cur_lang, "commands.setlang.only_admin"), show_alert=True)
        return

    codes = set(available_codes())
    available = ", ".join(sorted(codes))
    if code not in codes:
        await query.answer(
            t(cur_lang, "commands.setlang.unknown", lang_code=code, available=available),
            show_alert=True,
        )
        return

    async with SessionLocal() as session:
        await set_chat_language(session, chat.id if chat else 0, code, title=getattr(chat, "title", None))

    # Confirm in the newly selected language.
    await query.answer(t(code, "commands.setlang.ok", name=language_name(code), lang_code=code), show_alert=True)

    # Best effort: hide the keyboard after selection.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def cmd_setlang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setlang <code> — Admin-only in groups. Accepts codes from i18n/locales.
    """
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    async with SessionLocal() as session:
        cur_lang = await get_chat_language(session, chat.id if chat else 0)

    # Admin gate only for group chats.
    if _is_group(chat) and user and not await _is_admin(context, chat.id, user.id):
        await msg.reply_text(t(cur_lang, "commands.setlang.only_admin"))
        return

    codes = set(available_codes())
    available = ", ".join(sorted(codes))

    if not context.args:
        await msg.reply_text(t(cur_lang, "commands.setlang.usage", available=available))
        return

    code = (context.args[0] or "").strip().lower()
    if code not in codes:
        await msg.reply_text(t(cur_lang, "commands.setlang.unknown", lang_code=code, available=available))
        return

    async with SessionLocal() as session:
        await set_chat_language(session, chat.id if chat else 0, code, title=getattr(chat, "title", None))

    await msg.reply_text(t(code, "commands.setlang.ok", name=language_name(code), lang_code=code))


# ---------------------------
# Activity/name-change logic
# ---------------------------

async def on_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Capture name changes on non-message activity (edited messages, reactions, etc.).
    We explicitly skip message-like updates and membership/callback updates handled elsewhere,
    to avoid double-processing and spam.
    """

    if (
        (getattr(update, "message", None) is not None)
        or (getattr(update, "edited_message", None) is not None)
        or (getattr(update, "channel_post", None) is not None)
        or (getattr(update, "edited_channel_post", None) is not None)
        or (getattr(update, "callback_query", None) is not None)
        or (getattr(update, "chat_member", None) is not None)
        or (getattr(update, "my_chat_member", None) is not None)
    ):
        return

    user: User | None = update.effective_user
    chat = update.effective_chat
    if not user or user.is_bot or not chat or not _is_group(chat):
        return

    if _should_skip(user):
        # Even if we skip this user, opportunistically scan a few stale members in this chat.
        await _opportunistic_scan_stale(context, chat.id)
        return

    async with SessionLocal() as session:
        await touch_chat(session, chat.id, getattr(chat, "title", None), getattr(chat, "type", None))
        await add_or_touch_member(session, chat.id, user.id)
        last_checked_at = await get_member_last_checked(session, chat.id, user.id)
        lang = await get_chat_language(session, chat.id)

        prev_for_chat = await fetch_snapshot_before_or_at(session, user.id, last_checked_at)
        if not prev_for_chat:
            first_seen_at = await get_member_first_seen(session, chat.id, user.id)
            if first_seen_at and first_seen_at != "1970-01-01 00:00:01":
                prev_for_chat = await fetch_snapshot_before_or_at(session, user.id, first_seen_at)

        await record_user_snapshot(session, user)
        await mark_checked(session, chat.id, user.id)
        await session.commit()

    curr_fn, curr_ln, curr_un = _update_cache(user)
    changes = _diff_snap(prev_for_chat, curr_fn, curr_ln, curr_un)
    if not changes:
        await _opportunistic_scan_stale(context, chat.id)
        return

    fp = name_fingerprint(curr_fn, curr_ln, curr_un)
    async with SessionLocal() as s:
        if not await should_announce_persisted(s, chat.id, user.id, fp):
            await s.commit()
            await _opportunistic_scan_stale(context, chat.id)
            return
        await s.commit()

    await _announce_change_if_needed(
        context=context,
        chat_id=chat.id,
        reply_to_message_id=None,
        user=user,
        changes=changes,
        lang=lang,
    )
    # After announcing this user, still scan another few stale users opportunistically.
    await _opportunistic_scan_stale(context, chat.id)


async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user: User | None = update.effective_user
    if not user or user.is_bot or not chat or not _is_group(chat):
        return

    if _should_skip(user):
        await _opportunistic_scan_stale(context, chat.id)
        return

    async with SessionLocal() as session:
        await touch_chat(session, chat.id, getattr(chat, "title", None), getattr(chat, "type", None))
        await add_or_touch_member(session, chat.id, user.id)
        last_checked_at = await get_member_last_checked(session, chat.id, user.id)
        lang = await get_chat_language(session, chat.id)

        prev_for_chat = await fetch_snapshot_before_or_at(session, user.id, last_checked_at)
        if not prev_for_chat:
            first_seen_at = await get_member_first_seen(session, chat.id, user.id)
            if first_seen_at and first_seen_at != "1970-01-01 00:00:01":
                prev_for_chat = await fetch_snapshot_before_or_at(session, user.id, first_seen_at)

        await record_user_snapshot(session, user)
        await mark_checked(session, chat.id, user.id)
        await session.commit()

    curr_fn, curr_ln, curr_un = _update_cache(user)
    changes = _diff_snap(prev_for_chat, curr_fn, curr_ln, curr_un)
    if not changes:
        await _opportunistic_scan_stale(context, chat.id)
        return

    fp = name_fingerprint(curr_fn, curr_ln, curr_un)
    async with SessionLocal() as s:
        if not await should_announce_persisted(s, chat.id, user.id, fp):
            await s.commit()
            await _opportunistic_scan_stale(context, chat.id)
            return
        await s.commit()

    await _announce_change_if_needed(
        context=context,
        chat_id=chat.id,
        reply_to_message_id=getattr(msg, "message_id", None),
        user=user,
        changes=changes,
        lang=lang,
    )
    await _opportunistic_scan_stale(context, chat.id)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    chat = update.effective_chat
    if not chat or not msg or not msg.new_chat_members:
        return

    async with SessionLocal() as session:
        await touch_chat(session, chat.id, getattr(chat, "title", None), getattr(chat, "type", None))
        lang = await get_chat_language(session, chat.id)

    for new_user in msg.new_chat_members:
        if new_user.is_bot:
            continue

        # Dedup welcome across handlers
        if not _welcome_guard.should_welcome(chat.id, new_user.id):
            continue

        async with SessionLocal() as session:
            await add_or_touch_member(session, chat.id, new_user.id)
            await record_user_snapshot(session, new_user)
            await mark_checked(session, chat.id, new_user.id)
            await session.commit()

        _update_cache(new_user)

        text_msg = await build_welcome_message(new_user, lang)
        await context.bot.send_message(
            chat_id=chat.id,
            text=text_msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_to_message_id=msg.message_id,
            allow_sending_without_reply=True,
        )

    # After welcomes, proactively scan a couple stale members.
    await _opportunistic_scan_stale(context, chat.id)


async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.my_chat_member:
        await _handle_bot_join(update, context)
        return

    chat = update.effective_chat
    member = update.chat_member
    if not member or not chat:
        return

    new_state = getattr(member, "new_chat_member", None)
    old_state = getattr(member, "old_chat_member", None)
    user = getattr(new_state, "user", None) if new_state else None
    if not user or user.is_bot:
        return

    status = getattr(new_state, "status", None)
    status_value = getattr(status, "value", status)
    old_status = getattr(old_state, "status", None)
    old_val = getattr(old_status, "value", old_status)

    just_joined = status_value in ("member", "administrator") and (old_val in ("left", "kicked"))

    async with SessionLocal() as session:
        await touch_chat(session, chat.id, getattr(chat, "title", None), getattr(chat, "type", None))
        lang = await get_chat_language(session, chat.id)

        if status_value in ("left", "kicked"):
            await remove_member(session, chat.id, user.id)
            await mark_checked(session, chat.id, user.id)
            await session.commit()
            return
        else:
            await add_or_touch_member(session, chat.id, user.id)
            last_checked_at = await get_member_last_checked(session, chat.id, user.id)

            prev_for_chat = await fetch_snapshot_before_or_at(session, user.id, last_checked_at)
            if not prev_for_chat:
                first_seen_at = await get_member_first_seen(session, chat.id, user.id)
                if first_seen_at and first_seen_at != "1970-01-01 00:00:01":
                    prev_for_chat = await fetch_snapshot_before_or_at(session, user.id, first_seen_at)

            await record_user_snapshot(session, user)
            await mark_checked(session, chat.id, user.id)
            await session.commit()

    if _is_group(chat) and just_joined:
        # Dedup welcome across handlers
        if not _welcome_guard.should_welcome(chat.id, user.id):
            return

        text_msg = await build_welcome_message(user, lang)
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=text_msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                allow_sending_without_reply=True,
            )
        except Exception:
            logger.exception("Failed to send welcome message in chat_id=%s", chat.id)
        await _opportunistic_scan_stale(context, chat.id)
        return

    if not _is_group(chat):
        return

    curr_fn, curr_ln, curr_un = _update_cache(user)
    changes = _diff_snap(prev_for_chat, curr_fn, curr_ln, curr_un)
    if not changes:
        await _opportunistic_scan_stale(context, chat.id)
        return

    fp = name_fingerprint(curr_fn, curr_ln, curr_un)
    async with SessionLocal() as s:
        if not await should_announce_persisted(s, chat.id, user.id, fp):
            await s.commit()
            await _opportunistic_scan_stale(context, chat.id)
            return
        await s.commit()

    await _announce_change_if_needed(
        context=context,
        chat_id=chat.id,
        reply_to_message_id=None,
        user=user,
        changes=changes,
        lang=lang,
    )
    await _opportunistic_scan_stale(context, chat.id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if _is_group(chat) and user and not await _is_admin(context, chat.id, user.id):
        return
    async with SessionLocal() as session:
        lang = await get_chat_language(session, chat.id if chat else 0)
    await update.effective_message.reply_text(t(lang, "commands.start"))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if _is_group(chat) and user and not await _is_admin(context, chat.id, user.id):
        return
    async with SessionLocal() as session:
        lang = await get_chat_language(session, chat.id if chat else 0)
    await update.effective_message.reply_text(t(lang, "commands.help"))


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if _is_group(chat) and user and not await _is_admin(context, chat.id, user.id):
        return

    async with SessionLocal() as session:
        lang = await get_chat_language(session, chat.id if chat else 0)

    msg = update.effective_message
    if not msg:
        return

    target_user: User | None = None
    user_id: int | None = None
    username: str | None = None

    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user
        user_id = target_user.id
        username = target_user.username

    if not user_id and context.args:
        arg = context.args[0].strip()
        if arg.startswith("@"):
            username = arg.lstrip("@")
        elif arg.isdigit():
            user_id = int(arg)
        else:
            username = arg

    if user_id:
        async with SessionLocal() as session:
            history = await fetch_history_by_user_id(session, user_id)
        if history is None or not history:
            await msg.reply_text(t(lang, "commands.history.no_user_id"))
            return
        await msg.reply_text(
            _render_history_verbose(user_id, history, target_user, lang),
            parse_mode=ParseMode.HTML,
        )
        return

    if username:
        async with SessionLocal() as session:
            res = await fetch_history_by_username(session, username)
        if not res:
            await msg.reply_text(t(lang, "commands.history.no_username", username=html.escape(username)))
            return
        uid, history = res
        await msg.reply_text(_render_history_verbose(uid, history, None, lang), parse_mode=ParseMode.HTML)
        return

    await msg.reply_text(t(lang, "commands.history.usage"), parse_mode=ParseMode.MARKDOWN)


def _render_history_verbose(
    user_id: int,
    snapshots: list[dict],
    target_user: User | None,
    lang: str,
) -> str:
    if not snapshots:
        return t(lang, "commands.history.no_user_id")

    label_first = t(lang, "labels.first")
    label_last = t(lang, "labels.last")
    label_username = t(lang, "labels.username")
    none_text = t(lang, "general.none", default="(none)")

    lines: list[str] = []
    for i, s in enumerate(snapshots, 1):
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

    cur = snapshots[-1]
    fn = html.escape(cur.get("first_name") or "")
    ln_raw = cur.get("last_name")
    ln = (" " + html.escape(ln_raw)) if ln_raw else ""
    cur_disp = (fn + ln).strip() or (
        f"@{html.escape(cur.get('username'))}" if cur.get("username") else "Unknown"
    )

    title = f'<b>{t(lang, "history.title")}</b> <a href="tg://user?id={user_id}">{cur_disp}</a>:\n'
    return title + "\n".join(lines)


# ---- error handler (needed by core.bot.create_app) ----
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global error handler for PTB. Keeps the traceback in logs and avoids crashing
    the application when an update causes an exception.
    """
    try:
        err = getattr(context, "error", None)
        if err is not None:
            logger.exception(
                "Unhandled exception while processing update: %r (%r)", update, err, exc_info=True
            )
        else:
            logger.exception("Unhandled exception while processing update: %r", update, exc_info=True)
    except Exception:
        # As a last resort, at least log that something went wrong.
        logger.exception("Unhandled exception (no update info available)", exc_info=True)
