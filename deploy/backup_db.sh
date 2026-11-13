#!/usr/bin/env bash
# Back up the CustomerIntelV1 database (pg_dump custom format) to ~/backups/customerintelv1/.
# Run ON the box. Idempotent. Keeps the newest $KEEP dumps (default 14).
#
#   bash ~/CustomerIntel/deploy/backup_db.sh            # → ~/backups/customerintelv1/customerintel_YYYYmmddTHHMMSSZ.dump
#   KEEP=30 bash ~/CustomerIntel/deploy/backup_db.sh
#
# deploy_ec2.sh runs this before it touches the containers, and installs a
# daily cron line (03:15 UTC). Restore: restore_db.sh. Prove a dump restores: restore_drill.sh.
#
# Off the box: set BACKUP_S3_URI=s3://bucket/prefix in deploy/.env (or the environment) and
# every dump and its checksum is also copied there, using the instance's IAM role. A local dump
# shares a disk with the database it backs up, so without this a lost volume loses both. Retention
# in the bucket is the bucket's lifecycle rule, not this script. A failed upload fails the run:
# cron.log and healthcheck_alert.sh (which watches the age of .last_offbox_ok) both see it.
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
S3_URI="${BACKUP_S3_URI:-$(grep -m1 '^BACKUP_S3_URI=' deploy/.env 2>/dev/null | cut -d= -f2- || true)}"
if [ -n "$S3_URI" ]; then
  aws s3 cp --only-show-errors "$OUT" "${S3_URI%/}/$(basename "$OUT")"
  aws s3 cp --only-show-errors "$OUT.sha256" "${S3_URI%/}/$(basename "$OUT").sha256"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$DIR/.last_offbox_ok"
  echo "backup: copied to ${S3_URI%/}/$(basename "$OUT")"
else
  echo "backup: WARNING BACKUP_S3_URI is not set -- this dump exists only on the database's own disk"
fi
echo "backup: $OUT ($SIZE bytes); kept $(ls -1 "$DIR"/customerintel_*.dump | wc -l) dumps"
