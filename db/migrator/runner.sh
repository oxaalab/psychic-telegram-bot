#!/usr/bin/env bash
set -euo pipefail

log() { printf '[migrator] %s\n' "$*" >&2; }
esc_sql() { printf "%s" "$1" | sed "s/'/''/g"; }

: "${DB_HOST:?DB_HOST is required}"
: "${DB_PORT:?DB_PORT is required}"
: "${DB_NAME:?DB_NAME is required}"

: "${DB_ADMIN_USER:?DB_ADMIN_USER is required}"
: "${DB_ADMIN_PASSWORD:?DB_ADMIN_PASSWORD is required}"

: "${APP_DB_USER:?APP_DB_USER is required}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"

MYSQL_BIN="mysql"
if ! command -v mysql >/dev/null 2>&1; then
    if command -v mariadb >/dev/null 2>&1; then MYSQL_BIN="mariadb"; fi
fi

MYSQL_ARGS_ADMIN=( -h "${DB_HOST}" -P "${DB_PORT}" -u "${DB_ADMIN_USER}" --protocol=TCP --default-character-set=utf8mb4 )
mysql_exec_admin()     { MYSQL_PWD="${DB_ADMIN_PASSWORD}" "${MYSQL_BIN}" "${MYSQL_ARGS_ADMIN[@]}" -N -s -e "$1"; }
mysql_exec_db_admin()  { MYSQL_PWD="${DB_ADMIN_PASSWORD}" "${MYSQL_BIN}" "${MYSQL_ARGS_ADMIN[@]}" -D "${DB_NAME}" -N -s -e "$1"; }

TMP_FILES=()
cleanup_tmp() {
    for f in "${TMP_FILES[@]:-}"; do
        [[ -f "$f" ]] && rm -f "$f" || true
    done
}
trap cleanup_tmp EXIT

log "Waiting for DB ${DB_HOST}:${DB_PORT} as admin user '${DB_ADMIN_USER}' ..."
for i in $(seq 1 60); do
    if mysql_exec_admin "SELECT 1" >/dev/null 2>&1; then
        break
    fi
    sleep 2
    if [[ $i -eq 60 ]]; then
        log "ERROR: DB not reachable after 120s"
        exit 1
    fi
done
log "DB reachable."

log "Ensuring database '${DB_NAME}' exists ..."
mysql_exec_admin "CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"


log "Ensuring app user '${APP_DB_USER}' exists with privileges on '${DB_NAME}' ..."
user_esc="$(esc_sql "${APP_DB_USER}")"
pass_esc="$(esc_sql "${APP_DB_PASSWORD}")"

mysql_exec_admin "CREATE USER IF NOT EXISTS '${user_esc}'@'%' IDENTIFIED BY '${pass_esc}';"
mysql_exec_admin "ALTER USER '${user_esc}'@'%' IDENTIFIED BY '${pass_esc}';"
mysql_exec_admin "GRANT ALL ON \`${DB_NAME}\`.* TO '${user_esc}'@'%'; FLUSH PRIVILEGES;"

log "Ensuring schema_migrations table exists …"
mysql_exec_db_admin "CREATE TABLE IF NOT EXISTS schema_migrations (
  name VARCHAR(255) NOT NULL PRIMARY KEY,
  checksum CHAR(64) NOT NULL,
  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"


prepare_sql_for_db() {
    local filepath="$1"
    local tmp
    tmp="$(mktemp)"
    TMP_FILES+=("$tmp")
    awk -v db="$DB_NAME" '
    BEGIN { in_cd=0 }
    {
      line=$0
      low=tolower(line)

      # If we are inside a multi-line CREATE DATABASE statement, keep skipping
      if (in_cd) {
        if (index(low, ";") > 0) { in_cd=0 }  # end of CREATE DATABASE ... ;
        next
      }

      # Start of CREATE DATABASE – skip this and following lines until semicolon
      if (match(low, /^[[:space:]]*create[[:space:]]+database\b/)) {
        print "-- [migrator] " line "  (ignored: DB is managed by runner)"
        in_cd=1
        next
      }

      # Normalize any USE statements to the target DB
      if (match(low, /^[[:space:]]*use[[:space:]]+/)) {
        print "USE `" db "`;"
        next
      }

      # Otherwise, pass the line through
      print line
    }
    ' "$filepath" > "$tmp"
    printf '%s\n' "$tmp"
}

compute_checksum() {
    local file="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$file" | awk '{print $1}'
    else
        # macOS/Alpine fallback
        shasum -a 256 "$file" | awk '{print $1}'
    fi
}

apply_sql_file() {
    local filepath="$1"
    local name="$2"
    
    local to_apply
    to_apply="$(prepare_sql_for_db "$filepath")"
    
    local checksum
    checksum="$(compute_checksum "$to_apply")"
    
    local existing
    existing="$(mysql_exec_db_admin "SELECT checksum FROM schema_migrations WHERE name='${name}'" || true)"
    
    if [[ -z "$existing" ]]; then
        log "Applying ${name} (first time) …"
        MYSQL_PWD="${DB_ADMIN_PASSWORD}" "${MYSQL_BIN}" "${MYSQL_ARGS_ADMIN[@]}" -D "${DB_NAME}" < "$to_apply"
        mysql_exec_db_admin "INSERT INTO schema_migrations (name, checksum) VALUES ('${name}', '${checksum}')"
        log "Applied ${name}"
        elif [[ "$existing" != "$checksum" ]]; then
        log "Reapplying changed migration ${name} (checksum updated) …"
        MYSQL_PWD="${DB_ADMIN_PASSWORD}" "${MYSQL_BIN}" "${MYSQL_ARGS_ADMIN[@]}" -D "${DB_NAME}" < "$to_apply"
        mysql_exec_db_admin "UPDATE schema_migrations SET checksum='${checksum}', applied_at=CURRENT_TIMESTAMP WHERE name='${name}'"
        log "Updated ${name}"
    else
        log "Up-to-date: ${name}"
    fi
}


if [[ -f "/migrations/schema.sql" ]]; then
    apply_sql_file "/migrations/schema.sql" "schema.sql"
else
    log "No schema.sql provided – skipping baseline."
fi

shopt -s nullglob
mapfile -t files < <(ls -1 /migrations/*.sql 2>/dev/null | sort)
for f in "${files[@]}"; do
    base="$(basename "$f")"
    [[ "$base" == "schema.sql" ]] && continue
    apply_sql_file "$f" "$base"
done

log "All migrations complete."
