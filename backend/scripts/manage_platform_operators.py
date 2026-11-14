"""
Who may use the platform console. Runs on the box against the database — this is
the ONLY way a platform role is granted or removed; no HTTP route writes it, so
nobody can promote themselves from a browser.

    docker exec customerintelv1-app python scripts/manage_platform_operators.py list
    docker exec customerintelv1-app python scripts/manage_platform_operators.py create --email ops@example.com --name "Ops Person" [--role operator|viewer]
    docker exec customerintelv1-app python scripts/manage_platform_operators.py grant  --email someone@tenant.com [--role operator|viewer]
    docker exec customerintelv1-app python scripts/manage_platform_operators.py revoke --email ops@example.com
    docker exec customerintelv1-app python scripts/manage_platform_operators.py reset  --email ops@example.com

`create` makes a person who belongs to no tenant and prints a one-time
password-setup token (valid for the minutes in config/app_api.json). `grant`
gives the console to someone who already has a tenant login; their tenant access
is unchanged. `reset` prints a fresh setup token for an existing operator.
Every change is written to the tool audit log.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _audit(action: str, detail: str) -> None:
    from mcp_server import audit
    audit.record('cli', f'platform.{action}', None, key_kind='local', outcome='allowed', detail=detail)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    p = sub.add_parser('create'); p.add_argument('--email', required=True); p.add_argument('--name', required=True)
    p.add_argument('--role', default='operator')
    p = sub.add_parser('grant'); p.add_argument('--email', required=True); p.add_argument('--role', default='operator')
    p = sub.add_parser('revoke'); p.add_argument('--email', required=True)
    p = sub.add_parser('reset'); p.add_argument('--email', required=True)
    args = ap.parse_args(argv)

    from mcp_server.common import get_flask_app
    with get_flask_app().app_context():
        from extensions import db
        from models import User
        from app_api import settings
        from app_api.auth import issue_setup_token
        roles = settings.get('platform', 'roles')
        minutes = settings.get('password_setup', 'expiry_minutes')

        if args.cmd == 'list':
            rows = User.query.filter(User.platform_role.isnot(None)).order_by(User.user_id).all()
            if not rows:
                print('  (no platform operators)')
            for u in rows:
                print(f"  user_id={u.user_id:<5} {u.email}  platform_role={u.platform_role}  active={u.active}"
                      f"  tenant={u.customer_id or '-'}  has_password={bool(u.password_hash)}  last_login={u.last_login or 'never'}")
            return 0

        email = args.email.strip().lower()
        role = getattr(args, 'role', None)
        if role is not None and role not in roles:
            print(f'--role must be one of {roles}', file=sys.stderr)
            return 2
        user = User.query.filter_by(email=email).first()

        if args.cmd == 'create':
            if user:
                print(f'{email} already exists (user_id={user.user_id}); use `grant` instead', file=sys.stderr)
                return 2
            if '@' not in email or not args.name.strip():
                print('a valid --email and a --name are required', file=sys.stderr)
                return 2
            # No tenant, no tenant scope, and a tenant role that opens nothing: this person
            # exists only for the console.
            user = User(customer_id=None, user_name=args.name.strip(), email=email, role='platform', active=True,
                        allowed_customer_ids=None, platform_role=role)
            db.session.add(user)
            db.session.flush()
            raw = issue_setup_token(user)
            db.session.commit()
            _audit('operator_create', f'user {user.user_id} ({email}) platform_role={role}')
            print(f'created user_id={user.user_id} {email} platform_role={role}')
            print(f'one-time setup token (valid {minutes} minutes, shown once): {raw}')
            print('set the password on the sign-in page under "I have a token", then sign in.')
            return 0

        if not user:
            print(f'{email} not found', file=sys.stderr)
            return 2
        if args.cmd == 'grant':
            user.platform_role = role
            db.session.commit()
            _audit('operator_grant', f'user {user.user_id} ({email}) platform_role={role}')
            print(f'granted: user_id={user.user_id} {email} platform_role={role}')
        elif args.cmd == 'revoke':
            if not user.platform_role:
                print(f'{email} has no platform role')
                return 0
            user.platform_role = None
            if user.customer_id is None:
                user.active = False            # a console-only person with no console has nothing left to log in to
            db.session.commit()
            _audit('operator_revoke', f'user {user.user_id} ({email})')
            print(f'revoked: user_id={user.user_id} {email}' + (' (login deactivated)' if user.customer_id is None else ''))
        elif args.cmd == 'reset':
            if not user.platform_role:
                print(f'{email} is not a platform operator; tenant users are reset from Settings or the console', file=sys.stderr)
                return 2
            raw = issue_setup_token(user)
            db.session.commit()
            _audit('operator_reset', f'user {user.user_id} ({email})')
            print(f'one-time setup token for {email} (valid {minutes} minutes, shown once): {raw}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
