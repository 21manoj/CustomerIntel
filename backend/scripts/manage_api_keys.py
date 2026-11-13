"""
Operator tool for customer API keys. Runs on the box against the database, so it
needs no API key of its own (and the server key never has to leave the box):

    docker exec customerintelv1-app python scripts/manage_api_keys.py list   --customer-id 14 [--all]
    docker exec customerintelv1-app python scripts/manage_api_keys.py revoke --customer-id 14 --key-id 31
    docker exec customerintelv1-app python scripts/manage_api_keys.py revoke --customer-id 14 --all-keys
    docker exec customerintelv1-app python scripts/manage_api_keys.py rotate --customer-id 14 --key-id 31
    docker exec customerintelv1-app python scripts/manage_api_keys.py create --customer-id 14 --name "Pilot app" --scopes read,write

A leaked key: `rotate` it (the replacement is printed once; the old key stops
working immediately), then hand the new key to the tenant. `revoke` when no
replacement is wanted. Every change is written to the tool audit log.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _audit(action: str, customer_id: int, detail: str) -> None:
    from mcp_server import audit
    audit.record('cli', action, customer_id, key_kind='local', outcome='allowed', detail=detail)


def _print_keys(keys: list) -> None:
    if not keys:
        print('  (no keys)')
        return
    for k in keys:
        print(f"  id={k['id']:<5} {k['key_prefix']}…  active={str(k['is_active']):<5} scopes={','.join(k['scopes'] or [])}"
              f"  name={k['name']!r}  last_used={k['last_used_at'] or 'never'}  expires={k['expires_at'] or 'never'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('list'); p.add_argument('--customer-id', type=int, required=True)
    p.add_argument('--all', action='store_true', help='include revoked keys')
    p = sub.add_parser('revoke'); p.add_argument('--customer-id', type=int, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--key-id', type=int)
    g.add_argument('--all-keys', action='store_true', help="revoke every active key of this customer")
    p = sub.add_parser('rotate'); p.add_argument('--customer-id', type=int, required=True)
    p.add_argument('--key-id', type=int, required=True)
    p = sub.add_parser('create'); p.add_argument('--customer-id', type=int, required=True)
    p.add_argument('--name', required=True); p.add_argument('--scopes', default='read,write')
    args = ap.parse_args(argv)

    from mcp_server.common import get_flask_app
    with get_flask_app().app_context():
        from extensions import db
        from models import Customer
        import api_key_service as svc
        cid = args.customer_id
        cust = db.session.get(Customer, cid)
        if not cust:
            print(f'customer {cid} not found', file=sys.stderr)
            return 2
        print(f'customer {cid}: {cust.customer_name}')
        if args.cmd == 'list':
            _print_keys(svc.list_api_keys(cid, include_revoked=args.all))
        elif args.cmd == 'revoke':
            ids = [args.key_id] if args.key_id else [k['id'] for k in svc.list_api_keys(cid)]
            if not ids:
                print('  no active keys to revoke')
            for kid in ids:
                if not svc.revoke_api_key(kid, customer_id=cid):
                    print(f'  key {kid} not found for customer {cid}', file=sys.stderr)
                    return 2
                _audit('revoke_api_key', cid, f'revoked key id={kid}')
                print(f'  revoked key id={kid}')
        elif args.cmd == 'rotate':
            try:
                full_key, new = svc.rotate_api_key(args.key_id, customer_id=cid)
            except ValueError as e:
                print(f'  {e}', file=sys.stderr)
                return 2
            _audit('rotate_api_key', cid, f'rotated key id={args.key_id} -> id={new.id} prefix={new.key_prefix}')
            print(f'  key id={args.key_id} revoked; replacement id={new.id} (shown once, store it now):')
            print(f'  {full_key}')
        elif args.cmd == 'create':
            scopes = [s.strip() for s in args.scopes.split(',') if s.strip()]
            try:
                full_key, rec = svc.generate_api_key(cid, created_by=None, name=args.name, scopes=scopes)
            except ValueError as e:
                print(f'  {e}', file=sys.stderr)
                return 2
            _audit('create_api_key', cid, f'created key id={rec.id} prefix={rec.key_prefix} scopes={scopes}')
            print(f'  created key id={rec.id} scopes={scopes} (shown once, store it now):')
            print(f'  {full_key}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
