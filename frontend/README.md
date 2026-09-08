# frontend — the CustomerIntel browser app

React + TypeScript + Vite + Tailwind. One app, role-aware views (admin / cro / cfo / csm).
Design and route table: `docs/design/ui-rbac.md`.

## Develop

```bash
npm install
npm run dev            # http://localhost:5173
```

`vite.config.ts` proxies `/app/api` to `http://localhost:8101` so the browser sees one
origin — the backend's session cookie is httponly and origin-bound, so it will not work
across origins. Point it elsewhere with `VITE_API_PROXY_TARGET`.

You need a backend and a user to log in as:

```bash
cd ../backend
DATABASE_URL=postgresql://.../customerintel_dev .venv/bin/python server.py
.venv/bin/python scripts/dev_seed_ui.py        # throwaway tenant + admin login
```

## Build

```bash
npm run build          # tsc -b && vite build → dist/
```

`dist/` is gitignored — it is built in Docker on the way to the box, never committed.
The API base is same-origin relative (`src/api/client.ts`; `VITE_API_BASE` defaults to
`''`), so a production build needs no environment at all: whatever origin serves the
bundle also answers `/app/api/*`.

## How this gets deployed

`Dockerfile` builds it (`node:22-alpine`, `npm ci && npm run build`) and serves the
`dist/` from `caddy:2-alpine` using the `Caddyfile` in this directory. That image runs as
`customerintelv1-web` in `deploy/docker-compose.customerintelv1.yml`; the box's shared
Caddy sends `/mcp`, `/health`, `/api/*` and `/app/api/*` to the Python app and everything
else here (`deploy/Caddyfile.customerintelv1.snippet`). `deploy/deploy_ec2.sh` builds,
verifies and reloads all of it — there is no separate frontend deploy step, and no Node
on the box.

Two things worth knowing before changing `Caddyfile`:

- **The history fallback is load-bearing.** `src/main.tsx` mounts `BrowserRouter`, so
  `/portfolio`, `/accounts/64`, `/accounts/64/canvas` are client-side routes with no file
  behind them. `try_files {path} /index.html` is what makes a reload or a pasted link
  work instead of 404.
- **`/assets/*` is cached `immutable` while `index.html` is `no-cache`, and that pairing
  is deliberate.** Vite content-hashes the bundles so an old URL never collides with a new
  one; caching `index.html` as well would leave browsers asking for bundles a deploy has
  already deleted.

To exercise the built app against the real serving topology without Docker:

```bash
npm run build
SPA_ROOT=$PWD/dist caddy run --config Caddyfile      # :8080 — the same file the image uses
```

and put a second Caddy in front of it using the site block from
`deploy/Caddyfile.customerintelv1.snippet`, substituting `127.0.0.1:8101` and
`127.0.0.1:8080` for the two container names.
