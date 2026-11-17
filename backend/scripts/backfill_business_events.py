#!/usr/bin/env python
"""Rebuild business events for playbook history written before the business_events table existed.

    python scripts/backfill_business_events.py                      # dry run, every tenant: counts only
    python scripts/backfill_business_events.py --customer-id 19     # dry run, one tenant
    python scripts/backfill_business_events.py --apply              # write the events

Rebuilt rows are marked backfilled. Safe to run again: an intervention that already has an event is skipped,
and so is a settings change that already has its event. No model is called and nothing else is changed.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--customer-id', type=int, default=None)
    ap.add_argument('--apply', action='store_true', help='write the events (default is a dry run)')
    args = ap.parse_args(argv)
    from mcp_server.common import get_flask_app
    from playbooks import events
    with get_flask_app().app_context():
        out = events.backfill(args.customer_id, dry_run=not args.apply)
    print(('WROTE' if args.apply else 'DRY RUN, nothing written:'), out['events'], 'event(s)')
    for cid, c in sorted(out['tenants'].items()):
        print(f'  tenant {cid}: ' + ', '.join(f'{k}={v}' for k, v in c.items()))
    if not out['tenants']:
        print('  nothing to backfill')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
