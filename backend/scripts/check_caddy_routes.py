"""
Prove the Caddy site block still covers every HTTP route this server registers.

deploy/Caddyfile.customerintelv1.snippet splits one hostname between two
upstreams: an explicit list of API path prefixes goes to the uvicorn app,
everything else goes to the static SPA. That list is a hand-written copy of
knowledge that actually lives in backend/server.py's route registrations —
exactly the kind of duplicate that goes stale silently. When it does, the
failure is not a 404: a new /api/... route starts being answered with the
SPA's index.html, so callers get HTTP 200 and a page of HTML where they
expected JSON.

So: enumerate the routes off the real ASGI app, parse the prefixes out of the
real snippet, and fail if anything is uncovered. deploy_ec2.sh runs this
inside the container with the snippet on stdin, before it rewrites the
Caddyfile — a mismatch stops the deploy instead of shipping a shadowed route.

    python scripts/check_caddy_routes.py --snippet -   < ../deploy/Caddyfile.customerintelv1.snippet
    python scripts/check_caddy_routes.py --snippet ../deploy/Caddyfile.customerintelv1.snippet

No DB needed (build_asgi_app(create_schema=False) registers routes without
touching Postgres).
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# GET / is the app's JSON descriptor ({"server":..., "mcp":"/mcp"}). The SPA is
# deliberately served there instead — it was always a placeholder, and /health
# carries the same fields. Listed here so the shadow stays a decision on the
# record rather than something this check quietly tolerates.
INTENTIONALLY_SHADOWED = {'/'}


def registered_paths() -> list[str]:
    os.environ.setdefault('MCP_SERVER_API_KEY', 'route-check')
    from server import build_asgi_app

    app = build_asgi_app('postgresql://unused/route-check', create_schema=False)
    paths: list[str] = []

    def walk(node, prefix=''):
        for route in getattr(node, 'routes', []) or []:
            path = getattr(route, 'path', None)
            if path is not None:
                paths.append(prefix + path)
            sub = getattr(route, 'app', None)
            if sub is not None and hasattr(sub, 'routes'):
                walk(sub, prefix + (getattr(route, 'path', '') or ''))

    walk(app.app)      # app is BearerAuthMiddleware; .app is the Starlette app
    return sorted(set(paths))


def snippet_prefixes(text: str) -> list[str]:
    """The `path` arguments of the @backend matcher, e.g. /mcp /api/* /app/api/*."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        m = re.match(r'@backend\s+path\s+(.+)$', stripped)
        if m:
            return m.group(1).split()
    raise SystemExit('check_caddy_routes: no "@backend path ..." line in the snippet — '
                     'has the site block been restructured? Update this check with it.')


def covered(path: str, prefixes: list[str]) -> bool:
    """Caddy's `path` matcher: exact, unless the pattern ends in * (prefix match)."""
    for p in prefixes:
        if p.endswith('*'):
            if path.startswith(p[:-1]):
                return True
        elif path == p:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--snippet', required=True, help='path to Caddyfile.customerintelv1.snippet, or - for stdin')
    args = ap.parse_args()
    text = sys.stdin.read() if args.snippet == '-' else open(args.snippet, encoding='utf-8').read()

    prefixes = snippet_prefixes(text)
    paths = registered_paths()

    uncovered = [p for p in paths if p not in INTENTIONALLY_SHADOWED and not covered(p, prefixes)]
    wrongly_covered = [p for p in INTENTIONALLY_SHADOWED if p in paths and covered(p, prefixes)]

    if uncovered:
        print('FAIL: routes the Caddy site block does not send to the backend —', file=sys.stderr)
        print('      the SPA would answer these with index.html (HTTP 200, HTML body):', file=sys.stderr)
        for p in uncovered:
            print(f'        {p}', file=sys.stderr)
        print(f'      snippet prefixes: {" ".join(prefixes)}', file=sys.stderr)
        print('      fix deploy/Caddyfile.customerintelv1.snippet (@backend path ...), not this script.', file=sys.stderr)
        return 1
    if wrongly_covered:
        print(f'FAIL: {wrongly_covered} is listed as intentionally served by the SPA but the '
              'snippet routes it to the backend — the two disagree.', file=sys.stderr)
        return 1

    shadowed = sorted(INTENTIONALLY_SHADOWED & set(paths))
    print(f'caddy routes ok: {len(paths)} registered, all covered by [{" ".join(prefixes)}]'
          + (f'; served by the SPA on purpose: {" ".join(shadowed)}' if shadowed else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
