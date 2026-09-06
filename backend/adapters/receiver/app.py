"""
Reference receiver (docs/design/adapters.md §2.1, §3).

    POST /hook       the platform's signed payload → 200 accepted | 200 already_received | 401 | 400 | 403 | 413
    GET  /health     {ok, policy, received, platform_host}
    GET  /received   what this receiver has acknowledged (intervention id → when, state of the callbacks)

Ack first, call back after: approve() holds the row in `approved` until we answer; a callback made
inside the request would hit "only a sent one can start". So the response goes out, then a thread
waits callback.initial_delay_seconds and reports `started` (retrying on a non-2xx), then — policy
auto_done — waits auto_done_after_seconds and reports `done`.

Idempotency lives in the JSONL log: every `received` line is reloaded at start, so a restart does
not re-run an intervention the platform (one retry) or a partner's queue delivers again.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from adapters import settings
from playbooks.webhook import verify as verify_signature

logger = logging.getLogger('adapters.receiver')

_sleep = time.sleep          # tests replace this


def _env(name_key: str) -> Optional[str]:
    return os.environ.get(settings.get('receiver', 'env', name_key)) or None


@dataclass
class ReceiverConfig:
    secret: str
    platform_url: str
    platform_key: str
    customer_id: Optional[int] = None                       # when set, payloads for another tenant are refused
    policy: str = field(default_factory=lambda: settings.get('receiver', 'policy', 'default'))
    auto_done_after_seconds: float = field(default_factory=lambda: float(settings.get('receiver', 'policy', 'auto_done_after_seconds')))
    log_path: str = field(default_factory=lambda: settings.get('receiver', 'log_path_default'))
    timestamp_tolerance_seconds: int = field(default_factory=lambda: int(settings.get('receiver', 'timestamp_tolerance_seconds')))
    max_body_bytes: int = field(default_factory=lambda: int(settings.get('receiver', 'max_body_bytes')))
    callback: dict = field(default_factory=lambda: dict(settings.get('receiver', 'callback')))

    def __post_init__(self):
        if not self.secret:
            raise ValueError('a shared secret is required (--secret / CI_RECEIVER_SECRET)')
        if not self.platform_url or not urlparse(self.platform_url).netloc:
            raise ValueError('an absolute platform URL is required (--platform-url / CI_PLATFORM_URL)')
        if not self.platform_key:
            raise ValueError('a platform API key with write scope is required (--key / CI_PLATFORM_KEY)')
        modes = settings.get('receiver', 'policy', 'modes')
        if self.policy not in modes:
            raise ValueError(f'policy must be one of {modes}')
        self.platform_url = self.platform_url.rstrip('/')

    @classmethod
    def from_env(cls, **overrides) -> 'ReceiverConfig':
        """Env first (names in config/adapters.json → receiver.env), explicit overrides win."""
        values = {
            'secret': _env('secret'), 'platform_url': _env('platform_url'), 'platform_key': _env('platform_key'),
            'customer_id': int(_env('customer_id')) if _env('customer_id') else None,
        }
        if _env('policy'):
            values['policy'] = _env('policy')
        if _env('auto_done_after_seconds'):
            values['auto_done_after_seconds'] = float(_env('auto_done_after_seconds'))
        if _env('log_path'):
            values['log_path'] = _env('log_path')
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


class Receiver:
    def __init__(self, cfg: ReceiverConfig):
        self.cfg = cfg
        self.contract = settings.contract()
        self.seen: dict = {}                 # intervention_id → record
        self._lock = threading.Lock()
        self._threads: list = []
        os.makedirs(os.path.dirname(os.path.abspath(self.cfg.log_path)), exist_ok=True)
        self._reload_log()

    # ── the log is the memory ─────────────────────────────────────────

    def _reload_log(self) -> None:
        if not os.path.exists(self.cfg.log_path):
            return
        with open(self.cfg.log_path, encoding='utf-8') as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get('event') == 'received' and ev.get('intervention_id') is not None:
                    self.seen[int(ev['intervention_id'])] = {'received_at': ev.get('at'), 'customer_id': ev.get('customer_id'),
                                                             'playbook_id': ev.get('playbook_id'), 'callbacks': [], 'reloaded': True}
                elif ev.get('event') == 'callback' and int(ev.get('intervention_id', -1)) in self.seen:
                    self.seen[int(ev['intervention_id'])]['callbacks'].append({k: ev.get(k) for k in ('state', 'status', 'http_status', 'at')})

    def _log(self, event: str, **fields) -> None:
        line = {'event': event, 'at': datetime.utcnow().isoformat() + 'Z', **fields}
        with self._lock:
            with open(self.cfg.log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(line, sort_keys=True) + '\n')

    # ── verification ─────────────────────────────────────────────────

    def check(self, headers: dict, body: str) -> Optional[dict]:
        """None when the request is authentic; otherwise {'status', 'reason'} for the refusal."""
        sig = headers.get(self.contract['signature_header'].lower())
        ts = headers.get(self.contract['timestamp_header'].lower())
        if not sig or not ts:
            return {'status': 401, 'reason': 'missing_signature'}
        try:
            ts_int = int(ts)
        except ValueError:
            return {'status': 401, 'reason': 'bad_timestamp'}
        if abs(int(time.time()) - ts_int) > self.cfg.timestamp_tolerance_seconds:
            return {'status': 401, 'reason': 'stale_timestamp'}
        try:
            authentic = verify_signature(self.cfg.secret, ts, body, sig)
        except TypeError:                        # compare_digest refuses a non-ASCII header value
            authentic = False
        if not authentic:
            return {'status': 401, 'reason': 'bad_signature'}
        return None

    # ── the hook ──────────────────────────────────────────────────────

    async def hook(self, request: Request):
        raw = await request.body()
        if len(raw) > self.cfg.max_body_bytes:
            return JSONResponse({'error': 'body_too_large'}, status_code=413)
        body = raw.decode('utf-8', errors='replace')
        headers = {k.lower(): v for k, v in request.headers.items()}
        refusal = self.check(headers, body)
        if refusal:
            self._log('refused', reason=refusal['reason'], client=request.client.host if request.client else None)
            return JSONResponse({'error': refusal['reason']}, status_code=refusal['status'])
        try:
            payload = json.loads(body)
            iid, cid = int(payload['intervention_id']), int(payload['customer_id'])
        except (ValueError, KeyError, TypeError):
            self._log('refused', reason='bad_payload')
            return JSONResponse({'error': 'bad_payload'}, status_code=400)
        if self.cfg.customer_id is not None and cid != self.cfg.customer_id:
            self._log('refused', reason='wrong_tenant', intervention_id=iid, customer_id=cid)
            return JSONResponse({'error': 'wrong_tenant'}, status_code=403)
        with self._lock:
            if iid in self.seen:
                first = self.seen[iid]['received_at']
                replay = True
            else:
                self.seen[iid] = {'received_at': datetime.utcnow().isoformat() + 'Z', 'customer_id': cid,
                                  'playbook_id': (payload.get('playbook') or {}).get('id'), 'callbacks': []}
                replay = False
        if replay:
            self._log('replay', intervention_id=iid, customer_id=cid, first_received_at=first)
            return JSONResponse({'status': 'already_received', 'intervention_id': iid, 'first_received_at': first})
        self._log('received', intervention_id=iid, customer_id=cid, playbook_id=self.seen[iid]['playbook_id'],
                  event_name=payload.get('event'), timestamp=headers.get(self.contract['timestamp_header'].lower()), payload=payload)
        t = threading.Thread(target=self._callbacks, args=(iid, cid), daemon=True, name=f'ci-receiver-callback-{iid}')
        self._threads.append(t)
        t.start()
        return JSONResponse({'status': 'accepted', 'intervention_id': iid, 'policy': self.cfg.policy})

    # ── the callbacks ─────────────────────────────────────────────────

    def _callbacks(self, iid: int, cid: int) -> None:
        pol = settings.get('receiver', 'policy')
        _sleep(float(self.cfg.callback['initial_delay_seconds']))
        started = self.report(iid, cid, 'started', pol['started_note'])
        if started['status'] != 'ok' or self.cfg.policy != 'auto_done':
            return
        _sleep(float(self.cfg.auto_done_after_seconds))
        self.report(iid, cid, 'done', pol['done_note'])

    def report(self, iid: int, cid: int, state: str, note: str, **extra) -> dict:
        """POST /api/interventions/{id}/report with the platform key; retries on a non-2xx (the
        platform may still be committing `sent` when the first call lands). Never raises."""
        import httpx
        cb = self.cfg.callback
        url = f"{self.cfg.platform_url}/api/interventions/{iid}/report"
        body = {'customer_id': cid, 'state': state, 'note': note, **{k: v for k, v in extra.items() if v is not None}}
        headers = {'Authorization': f'Bearer {self.cfg.platform_key}', 'Content-Type': 'application/json'}
        attempts, last_status, last_err = 0, None, None
        for attempt in range(1 + int(cb['retries'])):
            attempts += 1
            try:
                with httpx.Client(timeout=float(cb['timeout_seconds'])) as client:
                    r = client.post(url, json=body, headers=headers)
                last_status = r.status_code
                if 200 <= r.status_code < 300:
                    res = {'status': 'ok', 'state': state, 'http_status': r.status_code, 'attempts': attempts, 'error': None}
                    break
                last_err = f'HTTP {r.status_code}: {r.text[:160]}'
            except Exception as e:
                last_err = f'{type(e).__name__}: {str(e)[:160]}'
            if attempt < int(cb['retries']):
                _sleep(float(cb['retry_delay_seconds']))
        else:
            res = {'status': 'failed', 'state': state, 'http_status': last_status, 'attempts': attempts, 'error': last_err}
            logger.warning('callback %s for intervention #%s failed after %d attempts: %s', state, iid, attempts, last_err)
        res['at'] = datetime.utcnow().isoformat() + 'Z'
        with self._lock:
            if iid in self.seen:
                self.seen[iid]['callbacks'].append(res)
        self._log('callback', intervention_id=iid, customer_id=cid, **res)
        return res

    def drain(self, timeout: float = 30.0) -> None:
        """Wait for the callback threads (tests, shutdown)."""
        for t in list(self._threads):
            t.join(timeout)
        self._threads = [t for t in self._threads if t.is_alive()]

    # ── reads ─────────────────────────────────────────────────────────

    async def health(self, request: Request):
        return JSONResponse({'ok': True, 'policy': self.cfg.policy, 'received': len(self.seen),
                             'platform_host': urlparse(self.cfg.platform_url).hostname, 'customer_id': self.cfg.customer_id,
                             'log_path': self.cfg.log_path, 'signature_scheme': self.contract['signature_scheme']})

    async def received(self, request: Request):
        with self._lock:
            return JSONResponse({'received': {str(k): v for k, v in sorted(self.seen.items())}})


def create_app(cfg: ReceiverConfig) -> Starlette:
    r = Receiver(cfg)
    app = Starlette(routes=[Route('/hook', r.hook, methods=['POST']), Route('/health', r.health, methods=['GET']),
                            Route('/received', r.received, methods=['GET'])])
    app.state.receiver = r
    return app
