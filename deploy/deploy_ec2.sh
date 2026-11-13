#!/usr/bin/env bash
# Deploy / update CustomerIntelV1 on the EC2 box. Idempotent. Run ON the box:
#
#   git clone https://github.com/21manoj/CustomerIntel.git ~/CustomerIntel   (first time)
#   cp ~/CustomerIntel/deploy/.env.customerintelv1.example ~/CustomerIntel/deploy/.env && edit
#   bash ~/CustomerIntel/deploy/deploy_ec2.sh [--no-tests] [--seed]
#
# Steps: git pull → backup (pg_dump, + daily cron once; + the 5-minute healthcheck_alert.sh cron) →
# BUILD the new images, without touching what is running → run the full test suite in a throwaway
# container of the NEW image against customerintel_test; a failure stops here, the old release still
# serving → up (own compose project, own Postgres, joins Caddy's network; a one-shot
# customerintelv1-migrate service runs Alembic to head before the app and the signal-enrichment worker
# start -- backend scaling plan step 3, docker-compose.customerintelv1.yml has the full writeup) →
# wait for /health → schema_check → verify the built SPA actually serves → rewrite the Caddy site
# block + reload → rebuild stale journeys → record the release.
#
# Every release's images are tagged with its git sha (the newest 5 are kept), so the one before can be
# put back: `bash deploy/rollback_ec2.sh` lists them, `bash deploy/rollback_ec2.sh <sha>` switches.
#
# --seed  also (re)creates the synthetic demo tenants (idempotent). Off by default: a deploy to a
#         box holding customer data should not write demo tenants beside it unless asked to.
#
# DEPLOY_MODE in deploy/.env picks the Caddy topology (see docker-compose.customerintelv1.yml):
#   shared (default)    an existing Caddy elsewhere on the box (outside this compose project)
#                        fronts this app; pinned to the one box this topology has always run on.
#   standalone           this compose project runs its own Caddy (the 'caddy' service, profile
#                        'standalone') — no other stack on the box required. CADDY_SITE defaults
#                        to customerintelv1.<box's own public IP, dashed>.sslip.io; set CADDY_SITE
#                        in .env to override.
set -euo pipefail
cd "$(dirname "$0")/.."
RUN_TESTS=1; RUN_SEED=0
for a in "$@"; do case "$a" in --no-tests) RUN_TESTS=0;; --seed) RUN_SEED=1;; --no-seed) RUN_SEED=0;; esac; done

[ -f deploy/.env ] || { echo "deploy/.env missing (copy .env.customerintelv1.example)"; exit 1; }
git pull --ff-only
export GIT_SHA="$(git rev-parse --short HEAD)" BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sed -i "s/^GIT_SHA=.*/GIT_SHA=$GIT_SHA/; s/^BUILD_TIME=.*/BUILD_TIME=$BUILD_TIME/" deploy/.env

DEPLOY_MODE="$(grep -m1 '^DEPLOY_MODE=' deploy/.env | cut -d= -f2- || true)"
DEPLOY_MODE="${DEPLOY_MODE:-shared}"

COMPOSE="docker compose -p customerintelv1 --env-file deploy/.env -f deploy/docker-compose.customerintelv1.yml"
if [ "$DEPLOY_MODE" = standalone ]; then
  export CADDY_NET_EXTERNAL=false
  export CADDY_NET_NAME="${CADDY_NET_NAME:-customerintelv1-caddy-net}"
  COMPOSE="$COMPOSE --profile standalone"
  CADDY_SITE="$(grep -m1 '^CADDY_SITE=' deploy/.env | cut -d= -f2- || true)"
  if [ -z "$CADDY_SITE" ]; then
    IMDS_TOKEN="$(curl -fsS -m 5 -X PUT http://169.254.169.254/latest/api/token \
      -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
    PUBLIC_IP="$(curl -fsS -m 5 -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
      http://169.254.169.254/latest/meta-data/public-ipv4)"
    [ -n "$PUBLIC_IP" ] || { echo "standalone mode: could not determine this box's public IP via IMDS, and CADDY_SITE is not set in deploy/.env"; exit 1; }
    CADDY_SITE="customerintelv1.${PUBLIC_IP//./-}.sslip.io"
  fi
  echo "standalone mode: this box fronts itself with its own Caddy at $CADDY_SITE"
  # Must exist as a regular file BEFORE the first `up`, or Docker creates a directory
  # at this bind-mount path instead (compose does not create missing bind sources as
  # files). Re-rendered again after the app is confirmed healthy, below, so a routing
  # change in the template also reaches an already-running box.
  mkdir -p deploy/caddy
  sed "s|\${CADDY_SITE}|$CADDY_SITE|" deploy/Caddyfile.standalone.template > deploy/caddy/Caddyfile
else
  CADDY_SITE="customerintelv1.3-218-251-181.sslip.io"
fi

# backup before anything changes (pg_dump, keeps 14) — and a daily cron line, once
if docker ps --format '{{.Names}}' | grep -q '^customerintelv1-postgres$'; then
  bash deploy/backup_db.sh
fi
CRON_LINE="15 3 * * * /bin/bash $HOME/CustomerIntel/deploy/backup_db.sh >> $HOME/backups/customerintelv1/cron.log 2>&1"
ALERT_CRON_LINE="*/5 * * * * /bin/bash $HOME/CustomerIntel/deploy/healthcheck_alert.sh >> $HOME/backups/customerintelv1/alert.log 2>&1"
mkdir -p "$HOME/backups/customerintelv1"
if command -v crontab >/dev/null 2>&1; then
  # `crontab -l` exits 1 when the user has no crontab yet — under set -e/pipefail that silently killed the deploy (2026-09-05)
  EXISTING="$(crontab -l 2>/dev/null || true)"
  TMP_CRON="$(mktemp)"
  { printf '%s\n' "$EXISTING" | grep -v -e 'deploy/backup_db.sh' -e 'deploy/healthcheck_alert.sh' || true
    echo "$CRON_LINE"; echo "$ALERT_CRON_LINE"; } | grep -v '^$' > "$TMP_CRON"
  crontab "$TMP_CRON" && rm -f "$TMP_CRON"
  echo "cron: daily backup scheduled (03:15 UTC); health and backup checks every 5 minutes"
  grep -q -E '^(ALERT_SNS_TOPIC_ARN|ALERT_WEBHOOK_URL)=.+' deploy/.env \
    || echo "WARNING: no ALERT_SNS_TOPIC_ARN / ALERT_WEBHOOK_URL in deploy/.env — problems are logged to alert.log but nobody is told"
  grep -q -E '^BACKUP_S3_URI=.+' deploy/.env \
    || echo "WARNING: no BACKUP_S3_URI in deploy/.env — backups exist only on this box's own disk"
else
  echo "WARNING: no crontab on this host — daily backup NOT scheduled (Amazon Linux 2023: sudo dnf -y install cronie && sudo systemctl enable --now crond, then redeploy)"
fi

# --- build first, test the build, and only then replace what is running --------------------
# The images a release ran with are kept under its git sha. .last_good_sha names the release
# that last finished this script; on the first run under this scheme the images now serving
# are tagged 'bootstrap' so there is something to go back to.
IMAGES="customerintelv1-app customerintelv1-web"
LAST_GOOD="$(cat deploy/.last_good_sha 2>/dev/null || true)"
if [ -z "$LAST_GOOD" ] && docker image inspect customerintelv1-app:latest >/dev/null 2>&1; then
  LAST_GOOD=bootstrap
  for img in $IMAGES; do docker tag "$img:latest" "$img:bootstrap" 2>/dev/null || true; done
  echo bootstrap > deploy/.last_good_sha
  echo bootstrap >> deploy/.deployed_shas
fi
restore_latest() {   # point :latest back at the last good release (nothing running has changed yet)
  [ -n "$LAST_GOOD" ] || return 0
  for img in $IMAGES; do docker tag "$img:$LAST_GOOD" "$img:latest" 2>/dev/null || true; done
}

$COMPOSE build || { restore_latest; echo "build FAILED — nothing was changed; the running release is untouched"; exit 1; }
for img in $IMAGES; do docker tag "$img:latest" "$img:$GIT_SHA"; done

if [ "$RUN_TESTS" = 1 ]; then
  echo "running the test suite in the NEW image (customerintel_test), before anything is replaced ..."
  $COMPOSE up -d --wait customerintelv1-postgres
  if ! $COMPOSE run --rm --no-deps -T customerintelv1-app \
       sh -c 'DATABASE_URL="$TEST_DATABASE_URL" exec python -m pytest tests/ -q -p no:warnings' | tail -15; then
    restore_latest
    echo "tests FAILED on $GIT_SHA — deploy stopped. The running release (${LAST_GOOD:-none}) was not touched."
    exit 1
  fi
fi

# From here on the running release is being replaced. If a later step fails, say how to go back.
going_live_failed() {
  [ $? -eq 0 ] && return 0
  echo
  echo "DEPLOY FAILED after $GIT_SHA started going live. To put the previous release back:"
  echo "    bash deploy/rollback_ec2.sh ${LAST_GOOD:-<sha>}"
}
trap going_live_failed EXIT

$COMPOSE up -d --no-build

# customerintelv1-app and customerintelv1-worker both depend on
# customerintelv1-migrate completing successfully before they even start
# (compose `condition: service_completed_successfully`) -- so if migration
# failed, `up -d` above already returned with those two never having
# started, and the /health wait below fails loudly instead of silently
# running the app against an unmigrated schema. Always show the migrate
# service's own log here regardless, so a failure's cause is visible in the
# same deploy output that reports it, not one `docker compose logs` away.
echo "--- customerintelv1-migrate ---"
$COMPOSE logs --tail 30 customerintelv1-migrate

echo "waiting for /health ..."
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8101/health >/dev/null 2>&1; then break; fi
  sleep 3
  [ "$i" = 30 ] && { echo "app did not become healthy"; $COMPOSE logs --tail 50 customerintelv1-app; exit 1; }
done
# the operator view (counts, worker detail) needs the server key; the key is never echoed
curl -sS -H "Authorization: Bearer $(grep -m1 '^MCP_SERVER_API_KEY=' deploy/.env | cut -d= -f2-)" http://127.0.0.1:8101/health; echo

# customerintelv1-migrate ran `alembic upgrade head` before the app was allowed to
# start (see above); prove the DB is at head and matches the models
$COMPOSE exec -T customerintelv1-app python scripts/schema_check.py

# The signal-enrichment worker is its own container now (backend scaling plan
# step 3) -- confirm it actually started rather than silently failing to
# depend correctly on migrate/postgres.
if ! $COMPOSE ps --status running customerintelv1-worker | grep -q customerintelv1-worker; then
  echo "customerintelv1-worker is not running"
  $COMPOSE logs --tail 50 customerintelv1-worker
  exit 1
fi
echo "customerintelv1-worker: running"

# The site block's API prefix list is a copy of what server.py registers; prove it still
# covers every route BEFORE rewriting the Caddyfile. An uncovered route does not 404 — it
# gets answered with the SPA's index.html, HTTP 200 and an HTML body where JSON was expected.
# (Same prefix list in both files by construction — the check just needs the current one.)
if [ "$DEPLOY_MODE" = standalone ]; then
  $COMPOSE exec -T customerintelv1-app python scripts/check_caddy_routes.py --snippet - \
    < deploy/caddy/Caddyfile
else
  $COMPOSE exec -T customerintelv1-app python scripts/check_caddy_routes.py --snippet - \
    < deploy/Caddyfile.customerintelv1.snippet
fi

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

if [ "$DEPLOY_MODE" = standalone ]; then
  # Single-site file, owned entirely by this compose project — re-render fresh from the
  # template (no multi-site splicing needed, unlike the shared branch below) and (re)start
  # the caddy service so a first-ever deploy picks it up.
  CADDYFILE="deploy/caddy/Caddyfile"
  cp "$CADDYFILE" "$CADDYFILE.bak"
  sed "s|\${CADDY_SITE}|$CADDY_SITE|" deploy/Caddyfile.standalone.template > "$CADDYFILE.new"
  if cmp -s "$CADDYFILE" "$CADDYFILE.new"; then
    rm -f "$CADDYFILE.new"; echo "Caddy site already current"
  else
    cat "$CADDYFILE.new" > "$CADDYFILE"; rm -f "$CADDYFILE.new"      # in place: see shared branch's comment on why
    echo "Caddy site rewritten from the template (previous: $CADDYFILE.bak)"
  fi
  $COMPOSE up -d caddy
  if ! $COMPOSE exec -T caddy caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    echo "Caddyfile FAILED validation — restoring $CADDYFILE.bak and aborting:"
    $COMPOSE exec -T caddy caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile 2>&1 | tail -20
    cat "$CADDYFILE.bak" > "$CADDYFILE"
    exit 1
  fi
  $COMPOSE exec -T caddy caddy reload --config /etc/caddy/Caddyfile
  if [ "$($COMPOSE exec -T caddy md5sum /etc/caddy/Caddyfile | cut -d' ' -f1)" != "$(md5sum "$CADDYFILE" | cut -d' ' -f1)" ]; then
    echo "Caddy container's config still stale after reload — restarting the container"
    $COMPOSE restart caddy >/dev/null
    sleep 3
  fi
else
  # Caddy site — rewritten from the snippet on EVERY deploy. It used to be appended once
  # and never touched again ("if ! grep -q hostname"), which meant a snippet change simply
  # never reached an already-deployed box. awk drops the existing hostname block (brace
  # counted) together with the comment run directly above it, then the current snippet is
  # appended; running it twice is a no-op. Unrelated sites and comments are preserved.
  CADDYFILE="$HOME/caddy/Caddyfile"
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
BASE="https://$CADDY_SITE"
if curl -fsS -m 10 "$BASE/health" >/dev/null 2>&1; then
  curl -fsS -m 10 "$BASE/" | grep -q 'id="root"' \
    && echo "public check ok: / serves the app, /health serves the API" \
    || echo "WARNING: $BASE/health is up but / did not return the app — check the Caddy site block"
else
  echo "note: could not reach $BASE from the box itself (hairpin NAT); verify from a browser"
fi

# This release finished: record it, keep the newest 5 releases' images, drop the rest and the
# untagged layers earlier builds left behind (they were piling up at ~290 MB a deploy).
echo "$GIT_SHA" > deploy/.last_good_sha
{ grep -v -x -e "$GIT_SHA" deploy/.deployed_shas 2>/dev/null || true; echo "$GIT_SHA"; } > deploy/.deployed_shas.new
mv deploy/.deployed_shas.new deploy/.deployed_shas
head -n -5 deploy/.deployed_shas | while read -r old; do
  for img in $IMAGES; do docker rmi "$img:$old" >/dev/null 2>&1 || true; done
done
tail -n 5 deploy/.deployed_shas > deploy/.deployed_shas.new && mv deploy/.deployed_shas.new deploy/.deployed_shas
docker image prune -f >/dev/null 2>&1 || true
echo "release $GIT_SHA recorded; rollback targets: $(tr '\n' ' ' < deploy/.deployed_shas)"

echo "done: $BASE  (app)   $BASE/health  (api, sha $GIT_SHA)"
