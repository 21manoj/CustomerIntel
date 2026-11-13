#!/usr/bin/env bash
# Watch the stack from the box and tell someone when it is not healthy. Run ON the box, from cron
# (deploy_ec2.sh installs the line: every 5 minutes).
#
# Checks:  /health says status=ok (DB reachable, signal worker not stale)   the web container serves
#          root disk under DISK_ALERT_PCT (default 85)                      newest dump under 26h old
#          newest OFF-BOX copy under 26h old, when BACKUP_S3_URI is set
#
# Sends to whichever of these is set in deploy/.env (both, if both):
#   ALERT_SNS_TOPIC_ARN   aws sns publish, with the instance's IAM role (email/SMS subscribers)
#   ALERT_WEBHOOK_URL     POST {"text": "..."} -- a Slack incoming webhook takes exactly this
# With neither set it still logs the problem and exits non-zero, so cron.log shows it.
#
# One message when a problem starts, a repeat every ALERT_REPEAT_MINUTES (default 60) while it
# lasts, one when it clears. Not a message every five minutes.
#
# What this cannot see: the box itself being gone (a spot reclaim, a dead disk) or the public
# hostname failing while loopback works. That needs a check from OUTSIDE the box -- a Route 53
# health check on https://<site>/health matching "status":"ok", alarming to the same SNS topic.
set -uo pipefail
cd "$(dirname "$0")/.."
env_val() { grep -m1 "^$1=" deploy/.env 2>/dev/null | cut -d= -f2- || true; }
SNS_ARN="${ALERT_SNS_TOPIC_ARN:-$(env_val ALERT_SNS_TOPIC_ARN)}"
WEBHOOK="${ALERT_WEBHOOK_URL:-$(env_val ALERT_WEBHOOK_URL)}"
S3_URI="${BACKUP_S3_URI:-$(env_val BACKUP_S3_URI)}"
SERVER_KEY="$(env_val MCP_SERVER_API_KEY)"
DISK_ALERT_PCT="${DISK_ALERT_PCT:-85}"
REPEAT_MIN="${ALERT_REPEAT_MINUTES:-60}"
BACKUP_MAX_AGE_H="${BACKUP_MAX_AGE_HOURS:-26}"
DIR="${BACKUP_DIR:-$HOME/backups/customerintelv1}"
STATE="$DIR/.alert_state"
mkdir -p "$DIR"
HOST="$(hostname)"
NOW="$(date +%s)"
PROBLEMS=()

# --- the app, the database and the worker, as /health sees them
HEALTH="$(curl -sS -m 15 -H "Authorization: Bearer $SERVER_KEY" http://127.0.0.1:8101/health 2>&1)" \
  || PROBLEMS+=("API is not answering on /health: ${HEALTH:0:120}")
if [ ${#PROBLEMS[@]} -eq 0 ]; then
  DETAIL="$(printf '%s' "$HEALTH" | python3 -c '
import json, sys
try:
    h = json.load(sys.stdin)
except Exception:
    print("/health did not return JSON"); sys.exit(0)
if h.get("status") == "ok":
    sys.exit(0)
w = h.get("worker") or {}
why = []
if not h.get("db"): why.append("database unreachable")
if w.get("stale"): why.append("signal worker stale (last pass %ss ago, last error: %s)" % (w.get("age_seconds"), w.get("last_error")))
print("/health status is %s: %s" % (h.get("status"), "; ".join(why) or "no reason given"))
' 2>&1)"
  [ -z "$DETAIL" ] || PROBLEMS+=("$DETAIL")
fi

# --- the browser app
curl -fsS -m 10 -o /dev/null http://127.0.0.1:8102/index.html 2>/dev/null \
  || PROBLEMS+=("web container is not serving index.html")

# --- disk
DISK_PCT="$(df --output=pcent / | tail -1 | tr -dc '0-9')"
[ "${DISK_PCT:-0}" -lt "$DISK_ALERT_PCT" ] || PROBLEMS+=("root disk is ${DISK_PCT}% full (alert at ${DISK_ALERT_PCT}%)")

# --- backups: the newest dump, and the newest off-box copy
age_hours() { echo $(( (NOW - $(stat -c %Y "$1")) / 3600 )); }
NEWEST="$(ls -1t "$DIR"/customerintel_*.dump 2>/dev/null | head -1)"
if [ -z "$NEWEST" ]; then
  PROBLEMS+=("no database backup exists in $DIR")
elif [ "$(age_hours "$NEWEST")" -ge "$BACKUP_MAX_AGE_H" ]; then
  PROBLEMS+=("newest database backup is $(age_hours "$NEWEST")h old (limit ${BACKUP_MAX_AGE_H}h)")
fi
if [ -n "$S3_URI" ]; then
  if [ ! -f "$DIR/.last_offbox_ok" ]; then
    PROBLEMS+=("no backup has been copied off the box yet (BACKUP_S3_URI is set)")
  elif [ "$(age_hours "$DIR/.last_offbox_ok")" -ge "$BACKUP_MAX_AGE_H" ]; then
    PROBLEMS+=("newest off-box backup copy is $(age_hours "$DIR/.last_offbox_ok")h old (limit ${BACKUP_MAX_AGE_H}h)")
  fi
fi

send() {   # $1 subject, $2 body
  local sent=0
  if [ -n "$SNS_ARN" ]; then
    aws sns publish --region "$(echo "$SNS_ARN" | cut -d: -f4)" --topic-arn "$SNS_ARN" \
      --subject "${1:0:99}" --message "$2" >/dev/null && sent=1
  fi
  if [ -n "$WEBHOOK" ]; then
    python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1] + "\n" + sys.argv[2]}))' "$1" "$2" \
      | curl -fsS -m 15 -X POST -H 'content-type: application/json' --data-binary @- "$WEBHOOK" >/dev/null && sent=1
  fi
  [ "$sent" = 1 ]
}

# state file: "<ok|failing> <epoch of last message sent>"
PREV=ok; LAST_SENT=0
if [ -f "$STATE" ]; then read -r PREV LAST_SENT < "$STATE" || true; fi
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [ ${#PROBLEMS[@]} -eq 0 ]; then
  if [ "$PREV" = failing ]; then
    send "CustomerIntel RECOVERED on $HOST" "All checks pass again as of $STAMP." || echo "$STAMP could not send the recovery message"
    echo "$STAMP recovered"
  fi
  echo "ok 0" > "$STATE"
  exit 0
fi

BODY="$(printf -- '- %s\n' "${PROBLEMS[@]}")"
echo "$STAMP FAILING:"; echo "$BODY"
if [ "$PREV" != failing ] || [ $(( NOW - ${LAST_SENT:-0} )) -ge $(( REPEAT_MIN * 60 )) ]; then
  if send "CustomerIntel PROBLEM on $HOST" "$BODY"$'\n\n'"Checked $STAMP by deploy/healthcheck_alert.sh"; then
    LAST_SENT="$NOW"
  else
    echo "$STAMP no alert was delivered (set ALERT_SNS_TOPIC_ARN or ALERT_WEBHOOK_URL in deploy/.env, or the send failed)"
  fi
fi
echo "failing ${LAST_SENT:-0}" > "$STATE"
exit 1
