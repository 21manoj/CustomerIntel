#!/usr/bin/env bash
# Put a previous release back. Run ON the box.
#
#   bash ~/CustomerIntel/deploy/rollback_ec2.sh            # list the releases that can be restored
#   bash ~/CustomerIntel/deploy/rollback_ec2.sh <sha>      # switch the app, worker and web to <sha>
#
# deploy_ec2.sh tags each release's images with its git sha and keeps the newest 5. This points
# :latest back at one of them and recreates the containers from it -- no build, so it takes
# seconds and cannot fail on a broken build.
#
# The DATABASE is not rolled back. If the release being abandoned ran a migration the older
# code does not know, the one-shot migrate service fails here and the app does not start: either
# roll forward, or restore the dump deploy_ec2.sh took just before that deploy
# (restore_db.sh; newest files in ~/backups/customerintelv1/). A backup of the current state is
# taken first in any case.
#
# The git checkout is left where it is: the next deploy_ec2.sh builds whatever main then holds.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGES="customerintelv1-app customerintelv1-web"
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "last release that finished deploying: $(cat deploy/.last_good_sha 2>/dev/null || echo unknown)"
  echo "releases with images on this box (oldest first):"
  docker images customerintelv1-app --format '  {{.Tag}}   built {{.CreatedSince}}' | grep -v -e latest -e '<none>' || echo "  none"
  echo "usage: bash deploy/rollback_ec2.sh <sha>"
  exit 0
fi
for img in $IMAGES; do
  docker image inspect "$img:$TARGET" >/dev/null 2>&1 || { echo "no image $img:$TARGET on this box (run with no argument to list)"; exit 1; }
done

COMPOSE="docker compose -p customerintelv1 --env-file deploy/.env -f deploy/docker-compose.customerintelv1.yml"
if grep -q '^DEPLOY_MODE=standalone' deploy/.env; then
  export CADDY_NET_EXTERNAL=false CADDY_NET_NAME="${CADDY_NET_NAME:-customerintelv1-caddy-net}"
  COMPOSE="$COMPOSE --profile standalone"
fi

bash deploy/backup_db.sh
for img in $IMAGES; do docker tag "$img:$TARGET" "$img:latest"; done
if ! $COMPOSE up -d --no-build; then
  echo "--- customerintelv1-migrate ---"; $COMPOSE logs --tail 30 customerintelv1-migrate
  echo "ROLLBACK to $TARGET did not start. If the migrate log above names an unknown revision, the"
  echo "database is ahead of this release: roll forward, or restore the pre-deploy dump (restore_db.sh)."
  exit 1
fi
echo "waiting for /health ..."
for i in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8101/health >/dev/null 2>&1 && break
  sleep 3
  [ "$i" = 30 ] && { echo "app did not become healthy on $TARGET"; $COMPOSE logs --tail 50 customerintelv1-migrate customerintelv1-app; exit 1; }
done
curl -fsS http://127.0.0.1:8101/health; echo
echo "$TARGET" > deploy/.last_good_sha
echo "rolled back to $TARGET"
