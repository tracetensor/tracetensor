#!/bin/sh
# Restore a TraceTensor Postgres dump produced by db_backup.sh.
# DESTRUCTIVE: drops and recreates the objects in the target database.
#
#   DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db" \
#     scripts/db_restore.sh backups/tracetensor-YYYYMMDD-HHMMSS.dump
set -eu

URL="${DATABASE_URL:?set DATABASE_URL (postgresql+asyncpg://…)}"
PG_URL="$(printf '%s' "$URL" | sed -e 's/+asyncpg//' -e 's/+psycopg2//')"

DUMP="${1:?path to a .dump file}"
[ -f "$DUMP" ] || { echo "no such dump: $DUMP" >&2; exit 1; }

pg_restore --clean --if-exists --no-owner --dbname "$PG_URL" "$DUMP"
echo "restored from: $DUMP"
