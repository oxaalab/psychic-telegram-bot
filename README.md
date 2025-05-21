# psychic-telegram-bot

Welcome 👋 — this is a Telegram bot that **welcomes new members** and **announces name changes** in your groups, keeping a local, privacy‑friendly history of:
- first name
- last name
- username

It ships as a small **FastAPI** web app with a **python-telegram-bot (PTB)** worker and a **MySQL/MariaDB** database (async SQLAlchemy 2.0). Multi‑language UI (English & Russian) lives in `src/i18n/locales/`.

> **Python 3.11+ is required** (uses `datetime.UTC`).

---

## Contents

- [psychic-telegram-bot](#psychic-telegram-bot)
  - [Contents](#contents)
  - [Architecture](#architecture)
  - [Runtime workflow](#runtime-workflow)
  - [Features](#features)
  - [Quick start](#quick-start)
    - [1) Create the database schema (MySQL / MariaDB)](#1-create-the-database-schema-mysql--mariadb)
    - [2) Configure environment](#2-configure-environment)
    - [3) Install \& run locally](#3-install--run-locally)
  - [Webhook \& security](#webhook--security)
  - [Bot commands](#bot-commands)
  - [REST Admin API](#rest-admin-api)
  - [Configuration reference](#configuration-reference)
  - [Development](#development)
    - [Fork \& continue on your own](#fork--continue-on-your-own)
    - [Project layout](#project-layout)
    - [Add a new language](#add-a-new-language)
    - [Coding tips](#coding-tips)
  - [Contributing](#contributing)
  - [Contributing](#contributing-1)
  - [Author](#author)
  - [License](#license)

---

## Architecture

```mermaid
flowchart LR
  subgraph Telegram
    U[Users & Chats]
  end

  API[(Telegram Bot API)]

  subgraph App[FastAPI app]
    WH[/POST /<WEBHOOK_SECRET_PATH>/]
    ADM[/Admin API /admin/*/]
    HZ[/GET /healthz/]
    PTB[PTB Application & Handlers]
    JQ[JobQueue Scanner]
  end

  DB[(MySQL / MariaDB)]

  U --- API
  API -- webhook POST --> WH
  WH --> PTB
  ADM --> PTB
  PTB <--> DB
  JQ --> PTB
  HZ --> App
```

- **FastAPI** app bootstraps configuration, checks DB health, and sets the Telegram webhook.
- **PTB Application** processes updates via handlers (messages, membership, callbacks).
- **Scanner** (PTB `JobQueue`) periodically checks a small batch of known members for silent name changes.
- **MySQL/MariaDB** stores users, name snapshots, chats, and membership meta.
- **i18n** strings live in YAML (`en`, `ru` out of the box).

## Runtime workflow

```mermaid
sequenceDiagram
  autonumber
  participant TG as Telegram
  participant WEB as FastAPI Webhook
  participant PTB as PTB Handlers
  participant DB as DB (MySQL/MariaDB)

  TG->>WEB: POST /<secret path> (update)
  WEB->>PTB: Update object
  rect rgb(245,245,245)
    note over PTB: Handlers normalize & persist
    PTB->>DB: upsert users, insert unique (first,last,username)+seen_at
    PTB->>DB: mark chat/member (first/last seen, last_checked)
  end
  alt fields changed & dedup allows
    PTB->>TG: send message — announce change + history
  else no change
    note over PTB: opportunistic mini-scan for a few stale members
  end

  loop every SCAN_INTERVAL_SECS
    PTB->>TG: get_chat_member (soft rate-limited)
    PTB->>DB: compare vs snapshot; record & optionally announce
  end
```

---

## Features

- 🧭 **Welcomes** new members with a localized history summary
- 📝 **Tracks snapshots** of first/last/username (after unicode sanitization) to avoid false diffs
- 🔔 **Announces** changes with per-(chat,user) **in‑memory** + **DB guard** to prevent spam
- 🌐 **Webhook** with optional secret header verification
- 🗣️ **Multi‑language** (English `en`, Russian `ru`) — easy to add more
- 🧰 **Admin REST API** to import/query histories
- 🛡️ **Resilient scanning** with retry/backoff and soft rate limiting

---

## Quick start

> You need: **Python 3.11+**, a **MySQL/MariaDB** instance, and a **Telegram bot token** from BotFather.

### 1) Create the database schema (MySQL / MariaDB)

Copy & run this DDL (utf8mb4, InnoDB). It matches the queries used by the code in `src/`.

```sql
-- Users we have ever seen
CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  is_bot TINYINT(1) NOT NULL DEFAULT 0,
  language_code VARCHAR(8) NULL,
  first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Unique snapshots of (first,last,username) for a user over time
CREATE TABLE IF NOT EXISTS user_names (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  first_name VARCHAR(64) NOT NULL DEFAULT '',
  last_name  VARCHAR(64) NOT NULL DEFAULT '',
  username   VARCHAR(32) NOT NULL DEFAULT '',
  seen_at    DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_user_name (user_id, first_name, last_name, username),
  KEY idx_userid (user_id),
  CONSTRAINT fk_user_names_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Chats the bot interacts with
CREATE TABLE IF NOT EXISTS chats (
  chat_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  title VARCHAR(255) NOT NULL DEFAULT '',
  language_code VARCHAR(8) NOT NULL DEFAULT 'en',
  chat_type VARCHAR(16) NOT NULL DEFAULT '',
  bot_status VARCHAR(16) NOT NULL DEFAULT 'unknown',
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  last_seen_at DATETIME NULL,
  last_joined_at DATETIME NULL,
  last_left_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Membership & scanning metadata
CREATE TABLE IF NOT EXISTS chat_members (
  chat_id BIGINT UNSIGNED NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  last_checked_at DATETIME NOT NULL DEFAULT '1970-01-01 00:00:01',
  last_announced_fp VARCHAR(300) NULL,
  last_announced_at DATETIME NULL,
  PRIMARY KEY (chat_id, user_id),
  KEY idx_last_checked (last_checked_at),
  CONSTRAINT fk_chat_members_chat FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE,
  CONSTRAINT fk_chat_members_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 2) Configure environment

Create `.env` (or export vars in your shell). **Minimal** set:

```dotenv
# Telegram
TELEGRAM_BOT_TOKEN=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijk
PUBLIC_BASE_URL=https://your.domain.tld  # must be publicly reachable over HTTPS
WEBHOOK_SECRET_PATH=telegram-webhook     # path segment only; final URL is PUBLIC_BASE_URL/<path>
WEBHOOK_SECRET_TOKEN=change_me_to_random # optional but recommended; enables secret header check

# Database (MySQL or MariaDB; use aiomysql or asyncmy)
DATABASE_URL=mysql+aiomysql://bot:botpass@127.0.0.1:3306/psychic?charset=utf8mb4

# App
LOG_LEVEL=INFO
APP_HOST=0.0.0.0
APP_PORT=50042

# Scanner (tune for your groups)
SCAN_ENABLED=true
SCAN_INTERVAL_SECS=60
SCAN_BATCH_SIZE=100
SCAN_FIRST_DELAY_SECS=5
SCAN_MAX_RPS=15
SCAN_RETRY_AFTER_LEEWAY_SECS=1

# Optional (present in config, not strictly used by handlers)
ALLOWED_CHAT_IDS=
```

> **Driver note:** For MariaDB/MySQL async access, either `mysql+aiomysql://...` (PyPI: `aiomysql`) or `mysql+asyncmy://...` (PyPI: `asyncmy`) works.

### 3) Install & run locally

```bash
# 1) Python 3.11+ virtualenv
python -m venv .venv
source .venv/bin/activate

# 2) Install runtime deps
pip install "fastapi>=0.115" "uvicorn[standard]>=0.30" \
            "sqlalchemy>=2.0" "aiomysql>=0.2" \
            "python-telegram-bot>=21" "pydantic>=2.8" "PyYAML>=6.0"

# If you prefer asyncmy instead of aiomysql:
# pip install asyncmy

# 3) Export env (or use direnv / dotenv)
export $(grep -v '^#' .env | xargs -d '\n')

# 4) Run the app (main will start uvicorn)
python -m src.main

# Alternatively (explicit uvicorn):
# uvicorn src.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-50042}" --log-level info
```

Now add your bot to a test group and speak — you should see welcomes and history/rename announcements.  
Health endpoint: `GET /healthz`.

---

## Webhook & security

At startup the app:

1. Checks DB (`/src/health/db.py`).
2. If the bot token looks valid, it **sets the webhook** to `PUBLIC_BASE_URL/WEBHOOK_SECRET_PATH`, with a conservative set of `allowed_updates`.
3. Starts PTB `Application` and optionally schedules the scanner.

If `WEBHOOK_SECRET_TOKEN` is set, the webhook requires the header:

```
X-Telegram-Bot-Api-Secret-Token: <WEBHOOK_SECRET_TOKEN>
```

> The endpoint path is exactly `/<WEBHOOK_SECRET_PATH>` (e.g. `/telegram-webhook`).  
> You must serve the app over public **HTTPS** (Telegram requirement). Use a reverse proxy or a tunnel (cloudflare, tailscale, etc.).

---

## Bot commands

- `/start` — brief intro (admin‑only in groups)
- `/help` — list commands (admin‑only in groups)
- `/setlang <code>` — set chat language; admin‑only (codes are taken from `src/i18n/locales/*`)
- `/history` — reply to a user **or** pass `@username` **or** numeric `user_id`

The bot also listens for non‑command activity and membership updates to keep snapshots fresh.

---

## REST Admin API

All endpoints are **unauthenticated** by default — restrict them at the proxy layer if exposing beyond localhost.

- `GET /admin/user/{user_id}/history` → `{"user_id": ..., "history": [...]}`  
- `GET /admin/username/{username}/history` → `{"user_id": ..., "history": [...]}`  
- `POST /admin/import-history` → import many snapshots for a known `user_id`

Example import payload:

```json
{
  "user_id": 123456789,
  "is_bot": false,
  "language_code": "en",
  "items": [
    {"first_name": "Alice", "last_name": "Smith", "username": "alice", "seen_at": "2024-01-01 10:00:00"},
    {"first_name": "Alice", "last_name": "W.", "username": "alicew", "seen_at": "2024-05-10 12:34:56"}
  ]
}
```

---

## Configuration reference

Environment variables are read in `src/core/config.py`:

| Key | Required | Default | Notes |
|---|:--:|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from BotFather |
| `PUBLIC_BASE_URL` | ✅ | — | Public HTTPS base (no trailing slash) |
| `WEBHOOK_SECRET_PATH` | ➖ | `webhook` | Path segment only |
| `WEBHOOK_SECRET_TOKEN` | ➖ | — | If set, webhook verifies secret header |
| `DATABASE_URL` | ✅ | — | e.g. `mysql+aiomysql://user:pass@host:3306/db?charset=utf8mb4` |
| `LOG_LEVEL` | ➖ | `INFO` | `DEBUG`, `INFO`, … |
| `ALLOWED_CHAT_IDS` | ➖ | — | Space/comma separated ints (not enforced by handlers yet) |
| `APP_HOST` / `HOST` | ➖ | `0.0.0.0` | Bind host |
| `APP_PORT` / `PORT` | ➖ | `50042` | Bind port |
| `SCAN_ENABLED` | ➖ | `true` | Enable periodic scanner |
| `SCAN_INTERVAL_SECS` | ➖ | `60` | Periodic scan cadence |
| `SCAN_BATCH_SIZE` | ➖ | `100` | Members per tick |
| `SCAN_FIRST_DELAY_SECS` | ➖ | `5` | Initial delay at startup |
| `SCAN_MAX_RPS` | ➖ | `15` | Soft rate cap for Bot API calls |
| `SCAN_RETRY_AFTER_LEEWAY_SECS` | ➖ | `1` | Extra wait added to RetryAfter |

---

## Development

### Fork & continue on your own

1. **Fork** this repository on GitHub.
2. **Clone** your fork and set upstream:
   ```bash
   git clone https://github.com/<you>/psychic-telegram-bot.git
   cd psychic-telegram-bot
   git remote add upstream https://github.com/<original>/psychic-telegram-bot.git
   ```
3. Create a branch:
   ```bash
   git checkout -b feat/my-change
   ```
4. Set up a **local DB** + `.env` (see [Quick start](#quick-start)), then run:
   ```bash
   python -m src.main
   ```
5. Make changes under `src/…` and test in a Telegram **test group**.
6. Commit and push:
   ```bash
   git commit -m "feat: <what you changed>"
   git push origin feat/my-change
   ```
7. Open a PR to your fork’s `main` (or upstream if contributing back).

### Project layout

```
src/
  api/                # FastAPI routers: /healthz, /admin/*
  core/               # app/bot bootstrap, config, db session factory
  tgbot/              # PTB handlers, announce guards, scanner
  chats/, welcome/    # persistence + domain logic for chats/users
  i18n/               # YAML locales (en, ru); add more here
  main.py             # entry point (starts uvicorn)
```

Key flows to know:

- `core.bot.create_app()` — builds the FastAPI app, PTB application, sets webhook
- `tgbot.handlers.*` — main user/chat event processing; opportunistic mini-scan
- `tgbot.scanner.schedule_scanner()` — periodic scan with rate‑limit & RetryAfter handling
- `welcome.*` — snapshot persistence & welcome message composition
- `chats.repository.*` — chat/membership metadata, last‑checked watermark
- `tgbot.announce_guard` — in‑memory + DB fingerprint gating for announcements

### Add a new language

1. Copy `src/i18n/locales/english.yaml` to `my_lang.yaml` and fill:
   ```yaml
   meta:
     code: xx
     name: My Language
   strings:
     # mirror the keys from english.yaml
   ```
2. On import, `i18n/messages.py` auto‑loads all `*.yml|*.yaml` files.  
3. In chat, use `/setlang xx` to activate.

### Coding tips

- Python **3.11+** (`datetime.UTC`).
- DB engine is created by `core.db.init_db(DATABASE_URL)`; use `SessionLocal()` to get sessions **after** init.
- Name normalization is centralized in `core.textnorm.sanitize_name()` to avoid false diffs from unicode tricks.
- When adding new handlers, prefer **async PTB v21** patterns (`ApplicationBuilder`, `ContextTypes`, etc.).
- If you add more REST endpoints, consider gating them at the proxy level (auth) in production.

---

## Contributing

Contributions are welcome! If you intend to contribute here (not just to your fork):

1. **Discuss** your idea in an issue.
2. **Small PRs** are easier to review. Please keep changes focused.
3. Follow conventional commit style when possible (`feat:`, `fix:`, `docs:`, …).
4. Add/adjust docs in this README where relevant.
5. Ensure basic sanity: project runs locally, `/healthz` returns OK, and the bot can be added to a test group.

> Security fixes and i18n improvements (new languages, better strings) are especially appreciated.

---

## Contributing

Contributions are welcome! If you intend to contribute here (not just to your fork):

1. **Discuss** your idea in an issue.
2. **Small PRs** are easier to review. Please keep changes focused.
3. Follow conventional commit style when possible (`feat:`, `fix:`, `docs:`, …).
4. Add/adjust docs in this README where relevant.
5. Ensure basic sanity: project runs locally, `/healthz` returns OK, and the bot can be added to a test group.

> Security fixes and i18n improvements (new languages, better strings) are especially appreciated.

---

## Author

**Boris Karaoglanov**  
📧 [i@oxaa.dev](mailto:i@oxaa.dev)  
🌐 <https://oxaa.dev>


---

## License

Specify your license here (e.g. MIT). If a `LICENSE` file exists in the repo, that is the source of truth.
