#!/usr/bin/env bash
# Back up the CustomerIntelV1 database (pg_dump custom format) to ~/backups/customerintelv1/.
# Run ON the box. Idempotent. Keeps the newest $KEEP dumps (default 14).
#
#   bash ~/CustomerIntel/deploy/backup_db.sh            # → ~/backups/customerintelv1/customerintel_YYYYmmddTHHMMSSZ.dump
#   KEEP=30 bash ~/CustomerIntel/deploy/backup_db.sh
#
# deploy_ec2.sh runs this before it touches the containers, and installs a
# daily cron line (03:15 UTC). Restore: restore_db.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
KEEP="${KEEP:-14}"
DIR="${BACKUP_DIR:-$HOME/backups/customerintelv1}"
mkdir -p "$DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DIR/customerintel_${STAMP}.dump"
COMPOSE="docker compose -p customerintelv1 --env-file deploy/.env -f deploy/docker-compose.customerintelv1.yml"
$COMPOSE exec -T customerintelv1-postgres pg_dump -U customerintel -d customerintel -Fc > "$OUT"
SIZE=$(stat -c %s "$OUT" 2>/dev/null || stat -f %z "$OUT")
[ "$SIZE" -gt 1024 ] || { echo "backup looks empty ($SIZE bytes): $OUT"; exit 1; }
sha256sum "$OUT" > "$OUT.sha256"
ls -1t "$DIR"/customerintel_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old" "$old.sha256"; done
echo "backup: $OUT ($SIZE bytes); kept $(ls -1 "$DIR"/customerintel_*.dump | wc -l) dumps"
