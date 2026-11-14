"""
Rebuild journeys whose generator_version is behind journeys.wizard_a.GENERATOR_VERSION.
Run by deploy_ec2.sh after the app is healthy; safe to re-run (no-op when nothing is stale).

    python scripts/rebuild_stale_journeys.py [--customer-id N]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--customer-id', type=int, default=None)
    args = ap.parse_args()
    from mcp_server.common import get_flask_app
    from journeys.wizard_a import rebuild_stale_journeys
    with get_flask_app().app_context():
        out = rebuild_stale_journeys(args.customer_id)
    print(f"journeys: {out['stale']} stale → {out['rebuilt']} rebuilt to {out['generator_version']}"
          + (f" (per customer {out['customers']})" if out['customers'] else ''))
    # said loudly, exit 0: a journey that cannot rebuild is stamped with build_error and shown as such on the
    # read surface; it must not roll back a deploy whose app is already healthy
    for f in out.get('failed', []):
        print(f"journeys: FAILED customer {f['customer_id']} account {f['account_id']} ({f['account_name']}): {f['error']}", file=sys.stderr)


if __name__ == '__main__':
    main()
