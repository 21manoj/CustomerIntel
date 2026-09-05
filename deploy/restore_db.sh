#!/usr/bin/env bash
# Restore a pg_dump custom-format backup INTO the CustomerIntelV1 database. Destructive:
# drops and recreates the schema first. Stops the app for the duration. Run ON the box.
#
#   bash ~/CustomerIntel/deploy/restore_db.sh ~/backups/customerintelv1/customerintel_20260905T171500Z.dump
#
# After the restore the app boots and runs `alembic upgrade head` (utils/schema.migrate), so a
# dump taken at an older revision is brought forward.
set -euo pipefail
cd "$(dirname "$0")/.."
DUMP="${1:?path to a .dump file}"
[ -f "$DUMP" ] || { echo "no such file: $DUMP"; exit 1; }
if [ -f "$DUMP.sha256" ]; then (cd "$(dirname "$DUMP")" && sha256sum -c "$(basename "$DUMP").sha256"); fi
COMPOSE="docker compose -p customerintelv1 --env-file deploy/.env -f deploy/docker-compose.customerintelv1.yml"
read -r -p "This DROPS the live customerintel schema and restores $DUMP. Type RESTORE to continue: " ans
[ "$ans" = "RESTORE" ] || { echo "aborted"; exit 1; }
$COMPOSE stop customerintelv1-app
$COMPOSE exec -T customerintelv1-postgres psql -U customerintel -d customerintel -v ON_ERROR_STOP=1 \
  -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
$COMPOSE exec -T customerintelv1-postgres pg_restore -U customerintel -d customerintel --no-owner --no-privileges < "$DUMP"
$COMPOSE start customerintelv1-app
echo "restored $DUMP; app restarting (migrations run at boot)"
