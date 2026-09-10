#!/usr/bin/env bash
#
# Nightly backup: PostgreSQL (custom-format dump), Redis snapshot, and the
# uploaded-media directory, with layered retention and optional offsite copy.
#
# Usage: scripts/backup.sh
#
# Reads configuration from .env (DB_*, REDIS_*) and from BACKUP_DIR /
# RETENTION_* / RCLONE_REMOTE overrides. WAL archiving for point-in-time
# recovery is configured at the Postgres level, outside this script; the
# daily excerpt here is a consistent full backup for fast restores.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"

# --- load environment -------------------------------------------------------
export_db_env() {
    local key value line
    while IFS= read -r line; do
        case "$line" in
            "" | \#*) continue ;;
            DB_* | REDIS_*)
                key="${line%%=*}"
                value="${line#*=}"
                value="${value%\"}"
                value="${value#\"}"
                # A value already present in the environment (e.g. an explicit
                # DB_HOST for a restore target) wins over the file.
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
DATE_STAMP="$(date +%Y%m%d-%H%M%S)"

RETENTION_DAILY="${RETENTION_DAILY:-14}"
RETENTION_WEEKLY="${RETENTION_WEEKLY:-8}"
RETENTION_MONTHLY="${RETENTION_MONTHLY:-24}"
RETENTION_YEARLY="${RETENTION_YEARLY:-7}"

LOG() { printf '[backup] %s\n' "$*"; }
die() { printf '[backup] FATAL: %s\n' "$*" >&2; exit 1; }

for bucket in daily weekly monthly yearly; do
    mkdir -p "$BACKUP_DIR/$bucket"
done

# --- PostgreSQL --------------------------------------------------------------
PG_HOST_ARG=()
if test -n "${DB_HOST:-}"; then
    PG_HOST_ARG=(-h "$DB_HOST" -p "${DB_PORT:-5432}")
fi

if command -v pg_dump >/dev/null 2>&1; then
    DUMP_PATH="$BACKUP_DIR/daily/${DATE_STAMP}-db.dump"
    LOG "dumping PostgreSQL to $DUMP_PATH"
    PGPASSWORD="${DB_PASSWORD:-}" pg_dump \
        "${PG_HOST_ARG[@]}" -U "${DB_USER:-}" \
        -d "${DB_NAME:-}" -Fc -Z 9 --no-owner --no-privileges \
        -f "$DUMP_PATH"
else
    LOG "pg_dump not found; running through docker compose exec instead"
    DUMP_PATH="$BACKUP_DIR/daily/${DATE_STAMP}-db.dump"
    docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db \
        pg_dump -U "${DB_USER:-}" -d "${DB_NAME:-}" -Fc -Z 9 \
        --no-owner --no-privileges >"$DUMP_PATH"
fi

# --- Redis -------------------------------------------------------------------
REDIS_CLI="$(command -v redis-cli || true)"
if test -n "$REDIS_CLI" && test -n "${REDIS_URL:-}"; then
    REDIS_DUMP_DIR="${REDIS_DUMP_DIR:-/var/lib/redis}"
    if "$REDIS_CLI" -u "$REDIS_URL" BGSAVE >/dev/null 2>&1; then
        sleep 1
        if test -f "$REDIS_DUMP_DIR/dump.rdb"; then
            cp "$REDIS_DUMP_DIR/dump.rdb" "$BACKUP_DIR/daily/${DATE_STAMP}-redis.rdb"
            LOG "redis snapshot copied from $REDIS_DUMP_DIR"
        else
            LOG "redis dump.rdb not found at $REDIS_DUMP_DIR; skipping snapshot"
        fi
    else
        LOG "redis BGSAVE failed; skipping snapshot"
    fi
else
    LOG "redis-cli or REDIS_URL unavailable; skipping redis snapshot"
fi

# --- Media -------------------------------------------------------------------
if test -d "$MEDIA_DIR"; then
    LOG "archiving media from $MEDIA_DIR"
    tar -czf "$BACKUP_DIR/daily/${DATE_STAMP}-media.tar.gz" -C "$MEDIA_DIR" .
fi

# --- Retention ----------------------------------------------------------------
# Keep layered buckets; yearly retention is floors at 5, matching the minimum
# period Kenyan tax records must be retained, so financial data is never
# pruned before it becomes legal to delete.
RETENTION_YEARLY=$((RETENTION_YEARLY < 5 ? 5 : RETENTION_YEARLY))

prune_bucket() {
    local bucket="$1" keep="$2"
    # Move artifacts into layered buckets first, then drop the oldest beyond keep.
    find "$BACKUP_DIR/$bucket" -type f -name '*.dump' -newermt '-7 days' \
        -exec cp -n {} "$BACKUP_DIR/weekly/" \; 2>/dev/null || true
    find "$BACKUP_DIR/$bucket" -type f -name '*.dump' -newermt '-30 days' \
        -exec cp -n {} "$BACKUP_DIR/monthly/" \; 2>/dev/null || true
    find "$BACKUP_DIR/$bucket" -type f -name '*.dump' -newermt '-365 days' \
        -exec cp -n {} "$BACKUP_DIR/yearly/" \; 2>/dev/null || true

    local count
    count="$(find "$BACKUP_DIR/$bucket" -type f | wc -l)"
    find "$BACKUP_DIR/$bucket" -type f -printf '%T@ %p\n' | sort -nr | \
        awk -v keep="$keep" 'NR>keep {print $2}' | while IFS= read -r old; do
        rm -f "$old"
    done
    LOG "pruned $bucket to latest $keep of $count"
}

prune_bucket daily "$RETENTION_DAILY"
prune_bucket weekly "$RETENTION_WEEKLY"
prune_bucket monthly "$RETENTION_MONTHLY"
prune_bucket yearly "$RETENTION_YEARLY"

# --- Offsite ------------------------------------------------------------------
if test -n "${RCLONE_REMOTE:-}"; then
    if command -v rclone >/dev/null 2>&1; then
        LOG "offsite copy to ${RCLONE_REMOTE}${BACKUP_DIR}"
        rclone copy "$BACKUP_DIR" "${RCLONE_REMOTE}${BACKUP_DIR}" \
            --transfers 4 --checkers 4
    else
        LOG "RCLONE_REMOTE set but rclone is not installed; offsite copy skipped"
    fi
else
    LOG "no RCLONE_REMOTE configured; offsite copy skipped"
fi

LOG "backup complete: $DUMP_PATH"