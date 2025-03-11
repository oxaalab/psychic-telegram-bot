from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.error import InvalidToken, RetryAfter, TelegramError
from telegram.ext import Application, ApplicationBuilder

from api.admin import router as admin_router
from api.health import router as health_router
from core.config import AppConfig
from health.db import check_db


def _plausible_token(token: str) -> bool:
    if not token:
        return False
    up = token.upper()
    if "REPLACE_WITH_YOUR_REAL" in up or up.startswith("REPLACE"):
        return False
    return bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token))


def _norm_updates(upds: Iterable[str] | None) -> list[str]:
    return sorted(set((up or "").strip() for up in (upds or []) if (up or "").strip()))


async def _ensure_webhook(
    application: Application,
    target_url: str,
    secret_token: str | None,
    allowed_updates: list[str],
    log: logging.Logger,
    max_retries: int = 5,
) -> bool:
    """
    Ensure the bot webhook is configured exactly as desired.

    FIXES INCLUDED:
    - Compare URL + allowed_updates and ALWAYS reset when a secret token is configured
      (older code skipped if URL matched, leaving Telegram unaware of the secret header).
    - Keep allowed_updates to a conservative set that’s widely supported by Bot API.
    """
    want_allowed = _norm_updates(allowed_updates)

    needs_set = True
    try:
        info = await application.bot.get_webhook_info()
        current_url = getattr(info, "url", "") or ""
        have_allowed = _norm_updates(getattr(info, "allowed_updates", None))

        if (
            current_url == target_url
            and want_allowed == have_allowed
            and not secret_token
        ):
            needs_set = False
            log.info(
                "Webhook already configured: %s (allowed=%s, pending=%s).",
                target_url,
                have_allowed or "[]",
                getattr(info, "pending_update_count", 0),
            )
        else:
            log.info(
                "Webhook (re)configuration required: "
                "url_match=%s, allowed_match=%s, secret_present=%s",
                current_url == target_url,
                want_allowed == have_allowed,
                bool(secret_token),
            )
    except Exception as e:
        log.warning("Failed to fetch current webhook info (%s); will set webhook.", e)

    if not needs_set:
        return True

    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            await application.bot.set_webhook(
                url=target_url,
                secret_token=secret_token,
                allowed_updates=allowed_updates,
                drop_pending_updates=False,
            )
            log.info("Webhook set to %s (allowed=%s)", target_url, want_allowed or "[]")
            return True
        except RetryAfter as e:
            wait_s = getattr(e, "retry_after", 1)
            log.warning(
                "Telegram rate limit on setWebhook (RetryAfter=%ss), attempt %s/%s; sleeping…",
                wait_s,
                attempt,
                max_retries,
            )
            await asyncio.sleep(max(1, int(wait_s)))
        except TelegramError as e:
            log.warning(
                "setWebhook failed (attempt %s/%s): %s; retrying in %.1fs…",
                attempt,
                max_retries,
                e,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        except Exception:
            log.exception(
                "Unexpected error during setWebhook (attempt %s/%s); retrying in %.1fs…",
                attempt,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    log.error("Giving up on setting webhook after %s attempts.", max_retries)
    return False


def create_app(config: AppConfig) -> FastAPI:
    if not config.public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL is empty; set it in environment.")

    app = FastAPI(title="psychic-telegram-bot")

    app.state.bot_ready = False
    app.state.bot_error: str | None = None
    app.state.db_ok = False
    app.state.db_latency_ms = None
    app.state.webhook_url = (
        f"{config.public_base_url}/{config.webhook_secret_path.strip('/')}"
    )

    log = logging.getLogger("psychic")

    application: Application | None = None
    if _plausible_token(config.bot_token):
        application = ApplicationBuilder().token(config.bot_token).build()

        from tgbot.handlers import build_handlers, error_handler

        for h in build_handlers():
            application.add_handler(h)
        application.add_error_handler(error_handler)
    else:
        app.state.bot_ready = False
        app.state.bot_error = "missing_or_placeholder_token"
        log.warning("Telegram token missing/placeholder; API-only mode.")

    app.include_router(health_router, tags=["health"])
    app.include_router(admin_router, prefix="/admin", tags=["admin"])

    WEBHOOK_PATH = "/" + config.webhook_secret_path.strip("/")

    app.include_router(health_router, prefix=WEBHOOK_PATH, tags=["health"])

    @app.on_event("startup")
    async def _on_startup():
        log.info("Starting up: DB check + Telegram webhook setup.")
        db_health = await check_db()
        app.state.db_ok = bool(db_health.get("ok", False))
        app.state.db_latency_ms = db_health.get("latency_ms")
        if not app.state.db_ok:
            log.error("DB health FAILED: %s", db_health.get("error"))
            raise RuntimeError("DB health check failed")

        if application is None:
            return
        try:
            await application.initialize()
            me = await application.bot.get_me()
            log.info("Bot authorized as @%s (id=%s)", me.username, me.id)

            allowed_updates = [
                "message",
                "edited_message",
                "callback_query",
                "chat_member",
                "my_chat_member",
                "chat_join_request",
            ]
            app.state.webhook_allowed_updates = allowed_updates

            webhook_ok = await _ensure_webhook(
                application=application,
                target_url=app.state.webhook_url,
                secret_token=config.webhook_secret_token,
                allowed_updates=allowed_updates,
                log=log,
            )
            if not webhook_ok:
                app.state.bot_ready = False
                app.state.bot_error = "webhook_set_failed"
                log.error("Proceeding without Telegram webhook (API-only mode).")
                return

            await application.start()
            app.state.bot_ready = True
            app.state.bot_error = None
            log.info("Webhook ready at %s; application started.", app.state.webhook_url)

            if config.scan_enabled:
                from tgbot.scanner import schedule_scanner

                schedule_scanner(application, config)
                log.info(
                    "Name-change scanner scheduled: every %ss, batch=%s",
                    config.scan_interval_secs,
                    config.scan_batch_size,
                )
            else:
                log.info("Name-change scanner disabled by SCAN_ENABLED=false")

        except InvalidToken:
            app.state.bot_ready = False
            app.state.bot_error = "invalid_token"
            log.warning("Telegram rejected token; API-only mode.")
        except Exception:
            app.state.bot_ready = False
            app.state.bot_error = "init_failed"
            log.exception("Telegram initialization failed; API-only mode.")

    @app.on_event("shutdown")
    async def _on_shutdown():
        if application is None:
            return
        try:
            await application.stop()
            await application.shutdown()
        except Exception:
            pass

    @app.post(WEBHOOK_PATH)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ):
        if config.webhook_secret_token:
            if (
                not x_telegram_bot_api_secret_token
                or x_telegram_bot_api_secret_token != config.webhook_secret_token
            ):
                raise HTTPException(
                    status_code=401, detail="Invalid webhook secret token"
                )

        if not app.state.bot_ready or application is None:
            raise HTTPException(status_code=503, detail="Bot not ready (API-only mode)")

        data = await request.json()
        try:
            upd_type = next(iter(data.keys() - {"update_id"}), "unknown")
        except Exception:
            upd_type = "unknown"
        logging.getLogger("psychic.webhook").debug("Incoming update type: %s", upd_type)

        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"ok": True}

    return app
