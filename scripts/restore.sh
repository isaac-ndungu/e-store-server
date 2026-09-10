#!/usr/bin/env bash
#
# Restore a PostgreSQL custom-format dump (with optional media/redis steps)
# into the database configured in .env.
#
# Usage: scripts/restore.sh [--file backups/daily/20260101-000000-db.dump]
#                           [--media]   # also unpack the matching media tarball
#                           [--redis]   # also install the matching RDB snapshot
#
# This is destructive: without --media/--redis it rebuilds the database from
# the dump, replacing whatever is currently inside. It refuses to target a
# remote host unless ALLOW_DESTRUCTIVE_RESTORE=1 is set explicitly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"

export_db_env() {
    local key value line
    while IFS= read -r line; do
        case "$line" in
            "" | \#*) continue ;;
            DB_*)
                key="${line%%=*}"
                value="${line#*=}"
                value="${value%\"}"
                value="${value#\"}"
                # A value already present in the environment (e.g. the target
                # DB_HOST for a restore) wins over the file.
                if test -z "${!key+x}"; then
                    export "$key=$value"
                fi
                ;;
        esac
    done <"$ENV_FILE"
}

export_db_env || true

BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
MEDIA_DIR="${MEDIA_DIR:-$PROJECT_DIR/media}"

LOG() { printf '[restore] %s\n' "$*"; }
die() { printf '[restore] FATAL: %s\n' "$*" >&2; exit 1; }

PG_HOST="${DB_HOST:-localhost}"
PG_PORT="${DB_PORT:-5432}"

RESTORE_MEDIA=0
RESTORE_REDIS=0
DUMP_FILE=""

while test $# -gt 0; do
    case "$1" in
        --file) DUMP_FILE="$2"; shift 2 ;;
        --media) RESTORE_MEDIA=1; shift ;;
        --redis) RESTORE_REDIS=1; shift ;;
        *) die "unknown option: $1" ;;
    esac
done

if test -z "$DUMP_FILE"; then
    DUMP_FILE="$(ls -t "$BACKUP_DIR"/daily/*-db.dump 2>/dev/null | head -n1 || true)"
fi
test -n "$DUMP_FILE" || die "no dump found; pass --file backups/.../xxx-db.dump"
test -f "$DUMP_FILE" || die "dump not readable: $DUMP_FILE"

case "$PG_HOST" in
    localhost | 127.0.0.1 | "")
        ALLOWED=1 ;;
    *)
        if test "${ALLOW_DESTRUCTIVE_RESTORE:-0}" != "1"; then
            die "refusing to restore into remote host $PG_HOST; " \
                "set ALLOW_DESTRUCTIVE_RESTORE=1 to override"
        fi
        ALLOWED=1 ;;
esac

PG_HOST_ARG=()
if test -n "${DB_HOST:-}"; then
    PG_HOST_ARG=(-h "$DB_HOST" -p "$PG_PORT")
fi

LOG "restoring $DUMP_FILE into $DB_HOST/${DB_NAME}"
if command -v pg_restore >/dev/null 2>&1; then
    PGPASSWORD="${DB_PASSWORD:-}" pg_restore \
        "${PG_HOST_ARG[@]}" -U "${DB_USER:-}" -d "${DB_NAME:-}" \
        --clean --if-exists --no-owner --no-privileges --single-transaction \
        "$DUMP_FILE"
else
    LOG "pg_restore not found; running through docker compose exec instead"
    docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db \
        pg_restore -U "${DB_USER:-}" -d "${DB_NAME:-}" \
        --clean --if-exists --no-owner --no-privileges --single-transaction \
        - <"$DUMP_FILE"
fi

if test "$RESTORE_MEDIA" = "1"; then
    TARBALL="${DUMP_FILE%-db.dump}-media.tar.gz"
    test -f "$TARBALL" || die "media tarball not found: $TARBALL"
    mkdir -p "$MEDIA_DIR"
    LOG "unpacking media from $TARBALL into $MEDIA_DIR"
    tar -xzf "$TARBALL" -C "$MEDIA_DIR"
fi

if test "$RESTORE_REDIS" = "1"; then
    RDB="${DUMP_FILE%-db.dump}-redis.rdb"
    test -f "$RDB" || die "redis snapshot not found: $RDB"
    REDIS_DUMP_DIR="${REDIS_DUMP_DIR:-/var/lib/redis}"
    LOG "installing redis snapshot $RDB into $REDIS_DUMP_DIR/dump.rdb"
    install -m 640 "$RDB" "$REDIS_DUMP_DIR/dump.rdb"
    if command -v redis-cli >/dev/null 2>&1; then
        "${REDIS_CLI:-redis-cli}" FLUSHALL 2>/dev/null || true
        "${REDIS_CLI:-redis-cli}" DEBUG RELOAD >/dev/null 2>&1 || true
    fi
fi

LOG "restore complete"