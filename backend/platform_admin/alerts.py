"""Alerts for the platform console: who is told when something breaks, and what has broken lately.

Three sources, none of them stored by this app:

  recipients   the subscribers of the SNS topic every alert is published to
               (ALERT_SNS_TOPIC_ARN — the same variable deploy/healthcheck_alert.sh
               reads). Operators add and remove them here; AWS sends an email
               recipient a confirmation link, and nothing is delivered to them
               until they click it.
  alarms       CloudWatch alarms whose name starts with platform.alerts.alarm_name_prefix,
               with their recent state changes. These are the checks that run
               OFF the box, so they still fire when the box is down.
  host checks  the log deploy/healthcheck_alert.sh writes every five minutes ON the
               box (containers, disk, backups), mounted read-only at ALERT_LOG_PATH.

The console is only reachable while the box is up, so it complements the
recipients list; it does not replace it.

AWS is reached with the instance's IAM role. Every call is bounded by
platform.alerts.aws_timeout_seconds, and a failure is reported in the response
('error') rather than raised: the rest of the console must load without AWS.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from app_api import settings

_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_PHONE = re.compile(r'^\+[1-9]\d{7,14}$')
_STAMP = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) (.*)$')
PROTOCOLS = ('email', 'sms')


def _cfg() -> dict:
    return settings.get('platform', 'alerts')


def topic_arn() -> Optional[str]:
    return (os.environ.get('ALERT_SNS_TOPIC_ARN') or '').strip() or None


def _clients():
    """(sns, cloudwatch) for the topic's region. Tests replace this."""
    import boto3
    from botocore.config import Config
    t = float(_cfg()['aws_timeout_seconds'])
    conf = Config(connect_timeout=t, read_timeout=t, retries={'max_attempts': 1})
    region = topic_arn().split(':')[3]
    return boto3.client('sns', region_name=region, config=conf), boto3.client('cloudwatch', region_name=region, config=conf)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


# ── recipients ──

def _recipients(sns) -> list:
    out, token = [], None
    while True:
        page = sns.list_subscriptions_by_topic(TopicArn=topic_arn(), **({'NextToken': token} if token else {}))
        for s in page.get('Subscriptions', []):
            arn = s.get('SubscriptionArn') or ''
            confirmed = arn.startswith('arn:')
            out.append({'id': arn if confirmed else None, 'protocol': s.get('Protocol'), 'endpoint': s.get('Endpoint'),
                        'status': 'confirmed' if confirmed else 'pending'})
        token = page.get('NextToken')
        if not token:
            break
    return sorted(out, key=lambda r: (r['status'] != 'confirmed', r['protocol'] or '', r['endpoint'] or ''))


# ── alarms ──

def _alarms(cw) -> tuple:
    prefix, limit = _cfg()['alarm_name_prefix'], int(_cfg()['history_limit'])
    alarms = [{'name': a['AlarmName'], 'state': a.get('StateValue'), 'reason': a.get('StateReason'),
               'since': _iso(a.get('StateUpdatedTimestamp')), 'description': a.get('AlarmDescription'),
               'notifies': bool(a.get('AlarmActions')) and bool(a.get('ActionsEnabled', True))}
              for a in cw.describe_alarms(AlarmNamePrefix=prefix).get('MetricAlarms', [])]
    history = []
    for a in alarms:
        for h in cw.describe_alarm_history(AlarmName=a['name'], HistoryItemType='StateUpdate', MaxRecords=limit,
                                           ScanBy='TimestampDescending').get('AlarmHistoryItems', []):
            history.append({'at': _iso(h.get('Timestamp')), 'alarm': a['name'], 'summary': h.get('HistorySummary')})
    history.sort(key=lambda h: h['at'] or '', reverse=True)
    return alarms, history[:limit]


# ── host checks ──

def host_checks() -> dict:
    """The tail of deploy/healthcheck_alert.sh's log: '<stamp> FAILING:' followed by
    '- problem' lines, or '<stamp> recovered'. A passing check writes nothing."""
    path = os.environ.get('ALERT_LOG_PATH') or _cfg()['host_log_path']
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()[-int(_cfg()['host_log_lines']):]
    except OSError:
        return {'available': False, 'state': 'unknown', 'events': []}
    events = []
    for ln in lines:
        m = _STAMP.match(ln)
        if m and m.group(2).startswith('FAILING'):
            events.append({'at': m.group(1), 'kind': 'failing', 'problems': []})
        elif m and m.group(2).startswith('recovered'):
            events.append({'at': m.group(1), 'kind': 'recovered', 'problems': []})
        elif m:
            events.append({'at': m.group(1), 'kind': 'note', 'problems': [m.group(2)[:300]]})
        elif ln.startswith('- ') and events and events[-1]['kind'] == 'failing':
            events[-1]['problems'].append(ln[2:][:300])
    state = 'ok'
    for e in events:
        if e['kind'] in ('failing', 'recovered'):
            state = 'failing' if e['kind'] == 'failing' else 'ok'
    events.reverse()
    return {'available': True, 'state': state, 'events': events[:int(_cfg()['history_limit'])]}


# ── the console's view ──

def status() -> dict:
    out = {'configured': bool(topic_arn()), 'topic': (topic_arn() or '').rsplit(':', 1)[-1] or None, 'error': None,
           'recipients': [], 'alarms': [], 'history': [], 'host_checks': host_checks(), 'protocols': list(PROTOCOLS)}
    if not out['configured']:
        out['error'] = 'ALERT_SNS_TOPIC_ARN is not set on this deployment, so alerts are not sent to anyone.'
        return out
    try:
        sns, cw = _clients()
        out['recipients'] = _recipients(sns)
        out['alarms'], out['history'] = _alarms(cw)
    except Exception as e:                                   # no role, no permission, no network: say so, keep the page
        out['error'] = f'Could not read alert settings from AWS: {type(e).__name__}: {str(e)[:200]}'
    return out


def _audit(op, action: str, detail: str) -> None:
    from mcp_server import audit
    audit.record('ui', f'platform.{action}', None, key_kind='user', key_record=None, outcome='allowed',
                 detail=f'operator:{op.user_id} {detail}')


def add_recipient(op, protocol, endpoint) -> dict:
    if not topic_arn():
        raise ValueError('alerts are not configured on this deployment (ALERT_SNS_TOPIC_ARN)')
    protocol, endpoint = str(protocol or '').strip().lower(), str(endpoint or '').strip()
    if protocol not in PROTOCOLS:
        raise ValueError(f'protocol must be one of: {", ".join(PROTOCOLS)}')
    if protocol == 'email':
        endpoint = endpoint.lower()
        if not _EMAIL.match(endpoint) or len(endpoint) > 254:
            raise ValueError('enter a valid email address')
    elif not _PHONE.match(endpoint):
        raise ValueError('enter the phone number in international form, e.g. +14155550123')
    sns, _cw = _clients()
    if any(r['protocol'] == protocol and (r['endpoint'] or '').lower() == endpoint.lower() for r in _recipients(sns)):
        raise ValueError(f'{endpoint} is already a recipient')
    sns.subscribe(TopicArn=topic_arn(), Protocol=protocol, Endpoint=endpoint, ReturnSubscriptionArn=True)
    _audit(op, 'alert_recipient_add', f'added {protocol} {endpoint}')
    return status()


def remove_recipient(op, subscription_id) -> dict:
    if not topic_arn():
        raise ValueError('alerts are not configured on this deployment (ALERT_SNS_TOPIC_ARN)')
    sns, _cw = _clients()
    current = _recipients(sns)
    target = next((r for r in current if r['id'] and r['id'] == subscription_id), None)
    if not target:                                           # only this topic's subscriptions can be removed from here
        raise LookupError('no such confirmed recipient on the alerts topic (a pending one lapses by itself after three days)')
    if sum(1 for r in current if r['status'] == 'confirmed') <= 1:
        raise ValueError('this is the only confirmed recipient — add and confirm another before removing it')
    sns.unsubscribe(SubscriptionArn=target['id'])
    _audit(op, 'alert_recipient_remove', f"removed {target['protocol']} {target['endpoint']}")
    return status()
