#!/usr/bin/env bash
# Deploy / update CustomerIntelV1 on the EC2 box. Idempotent. Run ON the box:
#
#   git clone https://github.com/21manoj/CustomerIntel.git ~/CustomerIntel   (first time)
#   cp ~/CustomerIntel/deploy/.env.customerintelv1.example ~/CustomerIntel/deploy/.env && edit
#   bash ~/CustomerIntel/deploy/deploy_ec2.sh [--no-tests] [--no-seed]
#
# Steps: git pull → backup (pg_dump, + daily cron once) → build+up (own compose project, own Postgres,
# joins Caddy's network) → wait for /health (the app migrates at boot) → schema_check → add the Caddy site
# once + reload → run the full test suite inside the container against customerintel_test → rebuild stale
# journeys → seed demo tenants (idempotent).
set -euo pipefail
cd "$(dirname "$0")/.."
RUN_TESTS=1; RUN_SEED=1
for a in "$@"; do case "$a" in --no-tests) RUN_TESTS=0;; --no-seed) RUN_SEED=0;; esac; done

[ -f deploy/.env ] || { echo "deploy/.env missing (copy .env.customerintelv1.example)"; exit 1; }
git pull --ff-only
export GIT_SHA="$(git rev-parse --short HEAD)" BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sed -i "s/^GIT_SHA=.*/GIT_SHA=$GIT_SHA/; s/^BUILD_TIME=.*/BUILD_TIME=$BUILD_TIME/" deploy/.env

COMPOSE="docker compose -p customerintelv1 --env-file deploy/.env -f deploy/docker-compose.customerintelv1.yml"

# backup before anything changes (pg_dump, keeps 14) — and a daily cron line, once
if docker ps --format '{{.Names}}' | grep -q '^customerintelv1-postgres$'; then
  bash deploy/backup_db.sh
fi
CRON_LINE="15 3 * * * /bin/bash $HOME/CustomerIntel/deploy/backup_db.sh >> $HOME/backups/customerintelv1/cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'deploy/backup_db.sh'; echo "$CRON_LINE" ) | crontab -

$COMPOSE up -d --build

echo "waiting for /health ..."
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8101/health >/dev/null 2>&1; then break; fi
  sleep 3
  [ "$i" = 30 ] && { echo "app did not become healthy"; $COMPOSE logs --tail 50 customerintelv1-app; exit 1; }
done
curl -sS http://127.0.0.1:8101/health; echo

# the app ran `alembic upgrade head` at boot; prove the DB is at head and matches the models
$COMPOSE exec -T customerintelv1-app python scripts/schema_check.py

# Caddy site (once)
CADDYFILE="$HOME/caddy/Caddyfile"
if ! grep -q "customerintelv1.3-218-251-181.sslip.io" "$CADDYFILE"; then
  printf '\n' >> "$CADDYFILE"; cat deploy/Caddyfile.customerintelv1.snippet >> "$CADDYFILE"
  echo "added Caddy site"
fi
docker exec -w /etc/caddy cspulse-caddy caddy reload --config /etc/caddy/Caddyfile 2>/dev/null \
  || docker exec cspulse-caddy caddy reload --config /etc/caddy/Caddyfile

if [ "$RUN_TESTS" = 1 ]; then
  echo "running the test suite inside the container (customerintel_test) ..."
  $COMPOSE exec -T -e DATABASE_URL="$($COMPOSE exec -T customerintelv1-app printenv TEST_DATABASE_URL)" \
    customerintelv1-app python -m pytest tests/ -q -p no:warnings | tail -3
fi

echo "rebuilding stale journeys (generator_version behind) ..."
$COMPOSE exec -T customerintelv1-app python scripts/rebuild_stale_journeys.py

if [ "$RUN_SEED" = 1 ]; then
  echo "seeding demo tenants ..."
  $COMPOSE exec -T customerintelv1-app python scripts/seed_demo.py
fi

echo "done: https://customerintelv1.3-218-251-181.sslip.io/health  (sha $GIT_SHA)"
