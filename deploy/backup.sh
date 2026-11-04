#!/usr/bin/env bash
#
# Consistent backup of the SQLite database plus uploaded photos and exports.
# Run as the bot user or as root from cron/systemd.
#
# Environment overrides:
#   APP_DIR        project directory (default /opt/knowledge-library)
#   DATA_DIR       persistent data directory (default /var/lib/knowledge-library)
#   BACKUP_DIR     where backups are written (default $APP_DIR/backups)
#   RETENTION_DAYS how long to keep backups (default 14)
#   PYTHON         python interpreter (default python3)

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/knowledge-library}"
DATA_DIR="${DATA_DIR:-/var/lib/knowledge-library}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
PYTHON="${PYTHON:-python3}"

DB="$DATA_DIR/knowledge_library.db"
if [ ! -f "$DB" ]; then
    echo "Database not found: $DB" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
DB_BACKUP="$BACKUP_DIR/knowledge_library-$STAMP.db"

# Use SQLite's online backup API so the copy is consistent while the bot runs.
"$PYTHON" - "$DB" "$DB_BACKUP" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
destination = sqlite3.connect(sys.argv[2])
with destination:
    source.backup(destination)
destination.close()
source.close()
PY
chmod 600 "$DB_BACKUP"

ITEMS=""
if [ -d "$DATA_DIR/document_photos" ]; then
    ITEMS="$ITEMS document_photos"
fi
if [ -d "$DATA_DIR/exports" ]; then
    ITEMS="$ITEMS exports"
fi
if [ -n "$ITEMS" ]; then
    # shellcheck disable=SC2086
    tar -czf "$BACKUP_DIR/files-$STAMP.tar.gz" -C "$DATA_DIR" $ITEMS
    chmod 600 "$BACKUP_DIR/files-$STAMP.tar.gz"
fi

# Remove backups older than the retention window.
find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'knowledge_library-*.db' -o -name 'files-*.tar.gz' \) \
    -mtime +"$RETENTION_DAYS" -delete

echo "Backup complete: $DB_BACKUP"
