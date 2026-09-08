#!/usr/bin/env bash
# Deploy / update CustomerIntelV1 on the EC2 box. Idempotent. Run ON the box:
#
#   git clone https://github.com/21manoj/CustomerIntel.git ~/CustomerIntel   (first time)
#   cp ~/CustomerIntel/deploy/.env.customerintelv1.example ~/CustomerIntel/deploy/.env && edit
#   bash ~/CustomerIntel/deploy/deploy_ec2.sh [--no-tests] [--no-seed]
#
# Steps: git pull → backup (pg_dump, + daily cron once) → build+up (own compose project, own Postgres,
# the frontend image builds itself, joins Caddy's network) → wait for /health (the app migrates at boot) →
# schema_check → verify the built SPA actually serves → rewrite the Caddy site block + reload → run the
# full test suite inside the container against customerintel_test → rebuild stale journeys → seed demo
# tenants (idempotent).
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
if command -v crontab >/dev/null 2>&1; then
  # `crontab -l` exits 1 when the user has no crontab yet — under set -e/pipefail that silently killed the deploy (2026-09-05)
  EXISTING="$(crontab -l 2>/dev/null || true)"
  TMP_CRON="$(mktemp)"
  { printf '%s\n' "$EXISTING" | grep -v 'deploy/backup_db.sh' || true; echo "$CRON_LINE"; } | grep -v '^$' > "$TMP_CRON"
  crontab "$TMP_CRON" && rm -f "$TMP_CRON"
  echo "cron: daily backup scheduled (03:15 UTC)"
else
  echo "WARNING: no crontab on this host — daily backup NOT scheduled (Amazon Linux 2023: sudo dnf -y install cronie && sudo systemctl enable --now crond, then redeploy)"
fi

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

# The site block's API prefix list is a copy of what server.py registers; prove it still
# covers every route BEFORE rewriting the Caddyfile. An uncovered route does not 404 — it
# gets answered with the SPA's index.html, HTTP 200 and an HTML body where JSON was expected.
$COMPOSE exec -T customerintelv1-app python scripts/check_caddy_routes.py --snippet - \
  < deploy/Caddyfile.customerintelv1.snippet

# The built SPA actually serves, before Caddy is pointed at it. "the container is up"
# is not the same claim: a bad base path or a missing asset still gives a running Caddy
# and a blank page, so check the real bytes — index.html, the hashed bundle it names,
# and the history fallback a deep link depends on.
echo "verifying the frontend build ..."
for i in $(seq 1 20); do
  curl -fsS http://127.0.0.1:8102/index.html >/dev/null 2>&1 && break
  sleep 2
  [ "$i" = 20 ] && { echo "frontend container never served index.html"; $COMPOSE logs --tail 50 customerintelv1-web; exit 1; }
done
FE_INDEX="$(curl -fsS http://127.0.0.1:8102/)"
printf '%s' "$FE_INDEX" | grep -q 'id="root"' || { echo "frontend: / is not the SPA index.html"; exit 1; }
FE_ASSET="$(printf '%s' "$FE_INDEX" | sed -n 's|.*src="\(/assets/[^"]*\.js\)".*|\1|p' | head -1)"
[ -n "$FE_ASSET" ] || { echo "frontend: index.html names no /assets/*.js bundle"; exit 1; }
curl -fsS -o /dev/null "http://127.0.0.1:8102$FE_ASSET" || { echo "frontend: bundle $FE_ASSET does not resolve"; exit 1; }
# a client-side route (frontend/src/App.tsx) has no file behind it — must fall back to
# index.html, not 404, or every reload and deep link on the deployed app breaks
curl -fsS http://127.0.0.1:8102/accounts/1 | grep -q 'id="root"' \
  || { echo "frontend: SPA history fallback is broken (/accounts/1 did not serve index.html)"; exit 1; }
echo "frontend ok: $FE_ASSET + history fallback"

# Caddy site — rewritten from the snippet on EVERY deploy. It used to be appended once
# and never touched again ("if ! grep -q hostname"), which meant a snippet change simply
# never reached an already-deployed box. awk drops the existing hostname block (brace
# counted) together with the comment run directly above it, then the current snippet is
# appended; running it twice is a no-op. Unrelated sites and comments are preserved.
CADDYFILE="$HOME/caddy/Caddyfile"
CADDY_SITE="customerintelv1.3-218-251-181.sslip.io"
[ -f "$CADDYFILE" ] || { echo "$CADDYFILE missing — is the shared Caddy set up on this box?"; exit 1; }
cp "$CADDYFILE" "$CADDYFILE.bak"
awk -v site="$CADDY_SITE" '
  BEGIN { skip = 0; depth = 0; buf = ""; cbuf = "" }
  skip == 1 {
    o = gsub(/\{/, "{"); c = gsub(/\}/, "}")
    depth += o - c
    if (depth <= 0) skip = 0
    next
  }
  /^[[:space:]]*$/ { buf = buf cbuf $0 "\n"; cbuf = ""; next }
  /^[[:space:]]*#/ { cbuf = cbuf $0 "\n"; next }
  index($0, site) == 1 {
    cbuf = ""                                  # the block owns the comments touching it
    o = gsub(/\{/, "{"); c = gsub(/\}/, "}")
    depth = o - c
    if (depth > 0) skip = 1
    next
  }
  { printf "%s%s", buf, cbuf; buf = ""; cbuf = ""; print }
  END { printf "%s%s", buf, cbuf }
' "$CADDYFILE" \
  | awk 'NF{last=NR} {L[NR]=$0} END{for(i=1;i<=last;i++) print L[i]}' > "$CADDYFILE.new"
printf '\n' >> "$CADDYFILE.new"
cat deploy/Caddyfile.customerintelv1.snippet >> "$CADDYFILE.new"
if cmp -s "$CADDYFILE" "$CADDYFILE.new"; then
  rm -f "$CADDYFILE.new"; echo "Caddy site already current"
else
  # Write IN PLACE (truncate + copy into the existing inode), not `mv`. cspulse-caddy
  # bind-mounts this single file at container start; `mv` replaces the inode at this path,
  # which orphans that bind mount from the new content — found 2026-09-08 the hard way:
  # the host file, `caddy validate`, and `caddy reload` all reported success, but the
  # running container kept serving the OLD file (different md5sum from the host copy)
  # because its mount never followed the rename. `caddy reload` re-reads from the
  # container's (stale) view, so it silently reloaded the config it already had.
  cat "$CADDYFILE.new" > "$CADDYFILE"; rm -f "$CADDYFILE.new"
  echo "Caddy site rewritten from the snippet (previous: $CADDYFILE.bak)"
fi
# Validate before reloading. A reload of a broken config leaves the OLD one running, but
# this Caddy fronts other sites too — fail loudly and put the previous file back rather
# than leave a file on disk that the next `caddy run` would refuse to start with.
if ! docker exec -w /etc/caddy cspulse-caddy caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  echo "Caddyfile FAILED validation — restoring $CADDYFILE.bak and aborting:"
  docker exec -w /etc/caddy cspulse-caddy caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile 2>&1 | tail -20
  cat "$CADDYFILE.bak" > "$CADDYFILE"
  exit 1
fi
docker exec -w /etc/caddy cspulse-caddy caddy reload --config /etc/caddy/Caddyfile 2>/dev/null \
  || docker exec cspulse-caddy caddy reload --config /etc/caddy/Caddyfile
# Belt and suspenders: prove the container's view actually matches what's on disk now,
# since `caddy reload` gave no error the day it silently no-op'd (above). A restart
# re-attaches the bind mount fresh, so it can't have this problem — only pay for it
# on the rare deploy where reload wasn't enough.
if [ "$(docker exec cspulse-caddy md5sum /etc/caddy/Caddyfile | cut -d' ' -f1)" != "$(md5sum "$CADDYFILE" | cut -d' ' -f1)" ]; then
  echo "Caddy container's config still stale after reload — restarting the container"
  docker restart cspulse-caddy >/dev/null
  sleep 3
fi

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

# Through the public hostname, both arms of the site block. Soft: the box reaching its
# own Elastic IP depends on NAT hairpinning, so a failure here is worth printing but is
# not proof the site is down — the loopback checks above already passed.
BASE="https://customerintelv1.3-218-251-181.sslip.io"
if curl -fsS -m 10 "$BASE/health" >/dev/null 2>&1; then
  curl -fsS -m 10 "$BASE/" | grep -q 'id="root"' \
    && echo "public check ok: / serves the app, /health serves the API" \
    || echo "WARNING: $BASE/health is up but / did not return the app — check the Caddy site block"
else
  echo "note: could not reach $BASE from the box itself (hairpin NAT); verify from a browser"
fi

echo "done: $BASE  (app)   $BASE/health  (api, sha $GIT_SHA)"
