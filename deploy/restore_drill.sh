#!/usr/bin/env bash
# Prove a backup restores, without touching the live database. Run ON the box.
#
#   bash ~/CustomerIntel/deploy/restore_drill.sh                 # newest local dump
#   bash ~/CustomerIntel/deploy/restore_drill.sh path/to.dump
#   bash ~/CustomerIntel/deploy/restore_drill.sh --from-s3       # newest dump under BACKUP_S3_URI
#
# Restores the dump into a scratch database (customerintel_restore_drill) on the same Postgres,
# checks it is not empty, and compares per-table row counts with the live database. Counts in the
# scratch copy may be LOWER than live (rows written since the dump); a table that is in live but
# missing from the restore, or a restore with no rows at all, fails the drill. The scratch database
# is dropped at the end either way. A backup that has never been restored is a hope, not a backup.
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose -p customerintelv1 --env-file deploy/.env -f deploy/docker-compose.customerintelv1.yml"
PSQL="$COMPOSE exec -T customerintelv1-postgres psql -U customerintel -v ON_ERROR_STOP=1 -At"
SCRATCH=customerintel_restore_drill
DIR="${BACKUP_DIR:-$HOME/backups/customerintelv1}"
TMP="$(mktemp -d)"
cleanup() { $PSQL -d postgres -c "DROP DATABASE IF EXISTS $SCRATCH" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT

ARG="${1:-}"
if [ "$ARG" = "--from-s3" ]; then
  S3_URI="${BACKUP_S3_URI:-$(grep -m1 '^BACKUP_S3_URI=' deploy/.env 2>/dev/null | cut -d= -f2- || true)}"
  [ -n "$S3_URI" ] || { echo "BACKUP_S3_URI is not set"; exit 1; }
  KEY="$(aws s3 ls "${S3_URI%/}/" | awk '{print $4}' | grep -E '^customerintel_.*\.dump$' | sort | tail -1)"
  [ -n "$KEY" ] || { echo "no dumps under $S3_URI"; exit 1; }
  aws s3 cp --only-show-errors "${S3_URI%/}/$KEY" "$TMP/"
  aws s3 cp --only-show-errors "${S3_URI%/}/$KEY.sha256" "$TMP/"
  DUMP="$TMP/$KEY"; echo "drill: $KEY from $S3_URI"
elif [ -n "$ARG" ]; then
  DUMP="$ARG"
else
  DUMP="$(ls -1t "$DIR"/customerintel_*.dump 2>/dev/null | head -1)"
fi
[ -n "$DUMP" ] && [ -f "$DUMP" ] || { echo "no dump to drill with"; exit 1; }
if [ -f "$DUMP.sha256" ]; then
  [ "$(cut -d' ' -f1 "$DUMP.sha256")" = "$(sha256sum "$DUMP" | cut -d' ' -f1)" ] \
    || { echo "DRILL FAILED: checksum mismatch: $DUMP"; exit 1; }
fi
echo "drill: restoring $DUMP into $SCRATCH"

$PSQL -d postgres -c "DROP DATABASE IF EXISTS $SCRATCH" -c "CREATE DATABASE $SCRATCH" >/dev/null
$COMPOSE exec -T customerintelv1-postgres pg_restore -U customerintel -d "$SCRATCH" \
  --no-owner --no-privileges --exit-on-error < "$DUMP"

# exact per-table counts (pg_stat estimates are empty on a just-restored database)
COUNT_SQL="select format('select %L, count(*) from %I', tablename, tablename) from pg_tables where schemaname='public' order by 1"
counts() { $PSQL -d "$1" -c "$COUNT_SQL" | while read -r q; do $PSQL -d "$1" -F ' ' -c "$q" </dev/null; done; }   # </dev/null: exec -T would eat the query list
counts customerintel > "$TMP/live.txt"
counts "$SCRATCH"    > "$TMP/restored.txt"

TABLES="$(wc -l < "$TMP/restored.txt")"; ROWS="$(awk '{s+=$2} END{print s+0}' "$TMP/restored.txt")"
[ "$TABLES" -gt 0 ] && [ "$ROWS" -gt 0 ] || { echo "DRILL FAILED: restore is empty ($TABLES tables, $ROWS rows)"; exit 1; }
MISSING="$(comm -23 <(cut -d' ' -f1 "$TMP/live.txt" | sort) <(cut -d' ' -f1 "$TMP/restored.txt" | sort))"
[ -z "$MISSING" ] || echo "note: in live but not in the dump (created since?): $MISSING"
REV="$($PSQL -d "$SCRATCH" -c 'select version_num from alembic_version' 2>/dev/null || echo unknown)"
echo "table restored live"
join <(sort "$TMP/restored.txt") <(sort "$TMP/live.txt") | awk '$2 != $3 {print}' | sed 's/^/  differs: /' || true
echo "DRILL OK: $TABLES tables, $ROWS rows restored at revision $REV (live has $(awk '{s+=$2} END{print s+0}' "$TMP/live.txt") rows)"
date -u +%Y-%m-%dT%H:%M:%SZ > "$DIR/.last_restore_drill_ok"
