from __future__ import annotations

import os
from dataclasses import dataclass


def _get_bool(env_key: str, default: bool) -> bool:
    raw = os.getenv(env_key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int(env_key: str, default: int) -> int:
    raw = os.getenv(env_key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_id_list(env_key: str) -> tuple[int, ...]:
    raw = os.getenv(env_key)
    if not raw:
        return ()
    out: list[int] = []
    for part in raw.replace(",", " ").split():
        try:
            out.append(int(part))
        except ValueError:
            continue
    return tuple(out)


def _get_opt(env_key: str) -> str | None:
    """Return a trimmed string or None if the env var is not set / empty."""
    raw = os.getenv(env_key)
    if raw is None:
        return None
    s = raw.strip()
    return s or None


@dataclass(frozen=True)
class AppConfig:
    bot_token: str
    public_base_url: str
    webhook_secret_path: str
    webhook_secret_token: str | None

    database_url: str

    log_level: str

    allowed_chat_ids: tuple[int, ...]

    server_host: str
    server_port: int

    scan_enabled: bool
    scan_interval_secs: int
    scan_batch_size: int

    scan_first_delay_secs: int
    scan_max_rps: int
    scan_retry_after_leeway_secs: int


def load_config() -> AppConfig:
    return AppConfig(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        webhook_secret_path=os.getenv("WEBHOOK_SECRET_PATH", "webhook").strip("/"),
        webhook_secret_token=_get_opt("WEBHOOK_SECRET_TOKEN"),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        allowed_chat_ids=_get_id_list("ALLOWED_CHAT_IDS"),
        server_host=(_get_opt("APP_HOST") or _get_opt("HOST") or "0.0.0.0"),
        server_port=_get_int("APP_PORT", _get_int("PORT", 50042)),
        scan_enabled=_get_bool("SCAN_ENABLED", True),
        scan_interval_secs=_get_int("SCAN_INTERVAL_SECS", 60),
        scan_batch_size=_get_int("SCAN_BATCH_SIZE", 100),
        scan_first_delay_secs=_get_int("SCAN_FIRST_DELAY_SECS", 5),
        scan_max_rps=_get_int("SCAN_MAX_RPS", 15),
        scan_retry_after_leeway_secs=_get_int("SCAN_RETRY_AFTER_LEEWAY_SECS", 1),
    )
