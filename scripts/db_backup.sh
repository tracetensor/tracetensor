#!/bin/sh
# Back up the TraceTensor Postgres database to a timestamped custom-format dump,
# then prune dumps older than the retention window.
#
#   DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db" \
#     scripts/db_backup.sh [output_dir]
#
# Env:
#   BACKUP_RETENTION_DAYS  delete dumps older than N days (default 14; 0 = keep all)
#
# Runs both by hand and inside the compose `backup` sidecar (see docker-compose).
# Restore with scripts/db_restore.sh. Test your restores — an untested backup
# does not exist.
set -eu

URL="${DATABASE_URL:?set DATABASE_URL (postgresql+asyncpg://…)}"
# pg_dump speaks libpq URLs — strip the SQLAlchemy async/sync driver suffix.
PG_URL="$(printf '%s' "$URL" | sed -e 's/+asyncpg//' -e 's/+psycopg2//')"

OUTDIR="${1:-./backups}"
RETENTION="${BACKUP_RETENTION_DAYS:-14}"
mkdir -p "$OUTDIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$OUTDIR/tracetensor-$TS.dump"

pg_dump "$PG_URL" --format=custom --no-owner --file "$OUT"
echo "backup written: $OUT ($(du -h "$OUT" | cut -f1))"

# Retention: prune old dumps (skip when RETENTION=0).
if [ "$RETENTION" -gt 0 ] 2>/dev/null; then
    pruned="$(find "$OUTDIR" -name 'tracetensor-*.dump' -type f -mtime +"$RETENTION" -print -delete | wc -l | tr -d ' ')"
    [ "$pruned" -gt 0 ] && echo "pruned $pruned dump(s) older than ${RETENTION}d"
fi
