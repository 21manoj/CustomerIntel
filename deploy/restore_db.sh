#!/usr/bin/env bash
# Restore a pg_dump custom-format backup INTO the CustomerIntelV1 database. Destructive:
# drops and recreates the schema first. Stops the app and the worker for the duration.
# Run ON the box.
#
#   bash ~/CustomerIntel/deploy/restore_db.sh ~/backups/customerintelv1/customerintel_20260905T171500Z.dump
#   bash ~/CustomerIntel/deploy/restore_db.sh s3://bucket/prefix/customerintel_20260905T171500Z.dump
#
# Takes a fresh backup of what is there now first, so a restore of the wrong file can be undone.
# After the restore the one-shot customerintelv1-migrate service runs Alembic to head (the app
# and the worker no longer migrate at boot), so a dump taken at an older revision is brought
# forward before anything serves from it.
#
# To check that a dump restores WITHOUT touching the live database: restore_drill.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="${1:?path or s3:// URI of a .dump file}"
COMPOSE="docker compose -p customerintelv1 --env-file deploy/.env -f deploy/docker-compose.customerintelv1.yml"
if grep -q '^DEPLOY_MODE=standalone' deploy/.env; then
  export CADDY_NET_EXTERNAL=false CADDY_NET_NAME="${CADDY_NET_NAME:-customerintelv1-caddy-net}"
fi

if [[ "$SRC" == s3://* ]]; then
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  aws s3 cp --only-show-errors "$SRC" "$TMP/"
  aws s3 cp --only-show-errors "$SRC.sha256" "$TMP/" || echo "note: no .sha256 beside $SRC"
  DUMP="$TMP/$(basename "$SRC")"
else
  DUMP="$SRC"
fi
[ -f "$DUMP" ] || { echo "no such file: $DUMP"; exit 1; }
if [ -f "$DUMP.sha256" ]; then
  # the .sha256 records the path the dump was written to; compare the digest only
  [ "$(cut -d' ' -f1 "$DUMP.sha256")" = "$(sha256sum "$DUMP" | cut -d' ' -f1)" ] \
    || { echo "checksum mismatch: $DUMP"; exit 1; }
  echo "checksum ok"
fi

read -r -p "This DROPS the live customerintel schema and restores $SRC. Type RESTORE to continue: " ans
[ "$ans" = "RESTORE" ] || { echo "aborted"; exit 1; }

bash deploy/backup_db.sh                                   # the way back, if this was the wrong file
$COMPOSE stop customerintelv1-app customerintelv1-worker
$COMPOSE exec -T customerintelv1-postgres psql -U customerintel -d customerintel -v ON_ERROR_STOP=1 \
  -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
$COMPOSE exec -T customerintelv1-postgres pg_restore -U customerintel -d customerintel \
  --no-owner --no-privileges --exit-on-error < "$DUMP"
$COMPOSE run --rm customerintelv1-migrate
$COMPOSE start customerintelv1-app customerintelv1-worker
echo "waiting for /health ..."
for i in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8101/health >/dev/null 2>&1 && break
  sleep 3
  [ "$i" = 30 ] && { echo "app did not become healthy after the restore"; $COMPOSE logs --tail 50 customerintelv1-app; exit 1; }
done
$COMPOSE exec -T customerintelv1-app python scripts/schema_check.py
echo "restored $SRC; app and worker running"
