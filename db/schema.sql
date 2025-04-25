CREATE DATABASE IF NOT EXISTS welcome CHARACTER
SET
  utf8mb4 COLLATE utf8mb4_unicode_ci;

USE welcome;

CREATE TABLE
  IF NOT EXISTS users (
    user_id BIGINT NOT NULL PRIMARY KEY,
    is_bot TINYINT (1) NOT NULL DEFAULT 0,
    language_code VARCHAR(16) NULL,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_users_last_seen (last_seen_at)
  ) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE
  IF NOT EXISTS user_names (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    first_name VARCHAR(64) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
    last_name VARCHAR(64) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
    username VARCHAR(32) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
    seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_names__users FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
    UNIQUE KEY uniq_user_name_combo (user_id, first_name, last_name, username),
    KEY idx_user_names_user_seen (user_id, seen_at),
    KEY idx_user_names_username (username)
  ) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE
  IF NOT EXISTS chats (
    chat_id BIGINT NOT NULL PRIMARY KEY,
    title VARCHAR(255) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
    language_code VARCHAR(8) NOT NULL DEFAULT 'en',
    chat_type VARCHAR(16) NOT NULL DEFAULT '',
    bot_status VARCHAR(16) NOT NULL DEFAULT 'unknown',
    is_active TINYINT (1) NOT NULL DEFAULT 1,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_joined_at TIMESTAMP NULL DEFAULT NULL,
    last_left_at TIMESTAMP NULL DEFAULT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_chats_lang (language_code),
    KEY idx_chats_active (is_active),
    KEY idx_chats_updated (updated_at),
    KEY idx_chats_last_seen (last_seen_at)
  ) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE
  IF NOT EXISTS chat_members (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at TIMESTAMP NOT NULL DEFAULT '1970-01-01 00:00:01',
    last_announced_fp VARCHAR(300) NOT NULL DEFAULT '',
    last_announced_at TIMESTAMP NULL DEFAULT NULL,
    PRIMARY KEY (chat_id, user_id),
    KEY idx_chat_members_last_checked (last_checked_at),
    KEY idx_chat_members_last_seen (last_seen_at),
    KEY idx_chat_members_user (user_id)
  ) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE utf8mb4_unicode_ci;