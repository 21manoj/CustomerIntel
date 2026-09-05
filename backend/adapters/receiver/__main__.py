"""python -m adapters.receiver — run the reference receiver (see adapters/receiver/app.py)."""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from adapters import settings                                   # noqa: E402
from adapters.receiver.app import ReceiverConfig, create_app    # noqa: E402


def main(argv=None) -> None:
    env = settings.get('receiver', 'env')
    p = argparse.ArgumentParser(description='CustomerIntelV1 reference webhook receiver')
    p.add_argument('--host', default=settings.get('receiver', 'default_host'))
    p.add_argument('--port', type=int, default=int(os.environ.get(env['port']) or settings.get('receiver', 'default_port')))
    p.add_argument('--secret', help=f"shared webhook secret (env {env['secret']})")
    p.add_argument('--platform-url', help=f"the platform's base URL (env {env['platform_url']})")
    p.add_argument('--key', help=f"platform API key with write scope for the tenant (env {env['platform_key']})")
    p.add_argument('--customer-id', type=int, help=f"refuse payloads for any other tenant (env {env['customer_id']})")
    p.add_argument('--policy', choices=settings.get('receiver', 'policy', 'modes'), help=f"auto_done | manual (env {env['policy']})")
    p.add_argument('--auto-done-after', type=float, help=f"seconds before reporting done (env {env['auto_done_after_seconds']})")
    p.add_argument('--log', help=f"JSONL event log path (env {env['log_path']})")
    a = p.parse_args(argv)
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    cfg = ReceiverConfig.from_env(secret=a.secret, platform_url=a.platform_url, platform_key=a.key, customer_id=a.customer_id,
                                  policy=a.policy, auto_done_after_seconds=a.auto_done_after, log_path=a.log)
    import uvicorn
    logging.getLogger('adapters.receiver').info('receiver on %s:%d → %s (policy %s, log %s)', a.host, a.port,
                                                cfg.platform_url, cfg.policy, cfg.log_path)
    uvicorn.run(create_app(cfg), host=a.host, port=a.port, log_level='info')


if __name__ == '__main__':
    main()
