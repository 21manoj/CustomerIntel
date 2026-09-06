# UI + RBAC — the first build

*2026-09-06. First UI for CustomerIntel. Decided with the user: one React+TS app with role-based views (not three separate apps); email+password login with RBAC now, SSO/MFA deferred to the governance High list; this design doc before code, matching every other track this session.*

## 1. What this closes

Every subsystem shipped so far (journeys, playbooks/interventions, Wizard D forecasts, Power-of-1/ROI, Wizard C calibrations, adapters) is reachable only through MCP tools or Bearer-keyed HTTP routes — fine for an AI assistant or a partner integration, useless for a CSM or a CFO who wants to open a browser. This build gives humans a session-authenticated app over the same read/write functions, with role- and tenant-scoped visibility.

Two pieces of dead code get removed in the same pass because they are exactly what this replaces: `auth_middleware.py` is a placeholder that says "replace before this ever serves a real request" — this build is that replacement. `approval_queue.py` (424 lines, imported by nothing) duplicated "human-in-the-loop for agent actions," a job `playbooks/governance.py` now does for real; port-inventory already decided it should be retired.

One real bug found while reading the existing code, fixed as part of this build: `create_customer` generates the admin user's password with `secrets.token_urlsafe(16)` and never returns or stores it anywhere — every admin user created since Tier 2A has a permanently unusable account. Fixed by issuing a one-time password-setup link instead (reusing the `User.magic_link_token`/`magic_link_expires_at` columns already on the model, unused until now).

## 2. Auth — session cookies, separate from the Bearer API keys

Two independent auth systems, never conflated:
- **Bearer API keys** (`CustomerApiKey`, `api_key_service.py`) — MCP, partner integrations, the adapters receiver. Unchanged.
- **Session cookies** (new) — a human logged into the browser app. `POST /app/api/auth/login` (email+password, `werkzeug.security.check_password_hash`) sets a signed, httponly, `Secure`, `SameSite=Lax` cookie (`itsdangerous.URLSafeTimedSerializer`, secret from `SESSION_SECRET` env, never in the repo) carrying `{user_id, exp}`; nothing else — the row is looked up fresh on every request so a revoked/deactivated user is locked out immediately, not just at cookie expiry. `POST /app/api/auth/logout` clears it. Login is rate-limited per email+IP using the same limiter shape `api_key_service.py` already has (5 failures / 5 min → 60s block) — the old repo shipped without this on the session layer, a confirmed gap (memory: SOC2 gaps).
- **Password setup / reset — admin-issued only, never self-service.** No mail sender exists in this build. A self-service "request a reset link by email" endpoint that returns the link in its own HTTP response (the only way to hand it back with nothing to send it through) is a live account-takeover hole: anyone who knows an admin's email address gets a valid password-set link for that account, no credentials needed. So there is no anonymous reset route. Instead: `create_customer`'s admin user, and every user an admin invites (`POST /app/api/users`), get a one-time setup token (reuses `User.magic_link_token`/`magic_link_expires_at`, 32 random bytes, SHA-256 stored, 15-minute expiry, single-use) returned ONLY in the response to the authenticated caller who created them — "shown once," the same pattern `onboard_tenant.py` already uses for API keys — for that admin to relay out of band. A locked-out user is unblocked the same way: an admin calls `POST /app/api/users/{id}/reset-password`, which issues a fresh token and returns it to the admin, not the user. The only public, unauthenticated route is `POST /app/api/auth/set-password` (token + new password), which only ever consumes a token an admin already holds. Wiring a real mail provider (and, with it, a safe self-service flow) is a later, separate decision.

## 3. RBAC — reuse the columns already on `User`, don't invent a new model

`User.role` (`admin | cro | cfo | csm`), `User.allowed_customer_ids`, `User.allowed_account_ids` already exist, ported in Tier 2A, unused until now.
- `admin` — every tenant, every account, plus Users/Settings (invite, deactivate, change role, playbook + webhook config).
- `cro` / `cfo` / `csm` — scoped to `allowed_customer_ids` (NULL = every tenant the platform key can see, for a single-tenant deployment this is moot; for a multi-tenant admin console it matters) and, within a tenant, `allowed_account_ids` (NULL = all accounts).
- A decorator `auth.require_session(role=None)` on every `/app/api/*` route: 401 with no valid session, 403 if `role` is given and doesn't match; the route handler itself applies the account/customer scoping by filtering its query through `current_user().allowed_customer_ids/allowed_account_ids`, the same shape `CustomerApiKey.has_account_access` already does for keys.
- Every `/app/api/*` request is a `tool_audit_log` row too (`surface='ui'`, `tool=<route name>`, `key_kind='user'`, a new column-free encoding: `key_prefix` carries `user:<user_id>` since the audit row has no user_id column and adding one is unnecessary for a first build — filterable by that prefix).

## 4. The routes — a session-authenticated mirror of the existing service layer, not a new business layer

New `backend/app_api/` package, routes at `/app/api/*`, each handler calling the SAME functions the Bearer-keyed `/api/*` routes call (`journeys.read`, `playbooks.governance`, `roi.priorities/power_of_1/measured`, `wizards.wizard_d_foresight`, `wizards.wizard_c_calibration`) — no logic is duplicated, only the auth wrapper differs. `backend/app_api/http.py` registers alongside the others in `server.py`; `backend/app_api/auth.py` holds the session/RBAC code; `backend/app_api/users.py` the invite/list/role-change handlers.

| Route | Backing call | Role |
|---|---|---|
| `POST /app/api/auth/login`, `/logout`, `/set-password` | new | any / none (`set-password` needs a valid token, not a session) |
| `GET /app/api/me` | session user + role + scoping | any |
| `GET /app/api/portfolio?customer_id=` | `journeys.read.list_journeys` + `roi.priorities` + `wizard_d_foresight` latest, merged per row | any |
| `GET /app/api/accounts/{id}` | `journeys.read.get_journey` (full, not compact) | any, account-scoped |
| `GET /app/api/interventions`, `POST .../{id}/approve`, `POST .../{id}/report` | `playbooks.governance.*` | any read; csm/admin approve |
| `GET /app/api/roi`, `/roi/power-of-1` | `roi.measured` / `roi.power_of_1` | cfo/cro/admin |
| `GET /app/api/calibrations`, `POST .../{id}/approve|reject` | `wizards.wizard_c_calibration.*` | admin |
| `GET /app/api/review-queue`, `POST /app/api/review` | `signal_engine.ingest_api.review_queue` / `signal_engine.review.review_signal` | csm/admin |
| `GET/POST /app/api/playbooks/config` | `playbooks.definitions.playbooks_for_customer` / `configure_tenant` | admin |
| `GET /app/api/users`, `POST /app/api/users` (invite → returns a one-time setup link), `PATCH /app/api/users/{id}` (role/active/scoping), `POST /app/api/users/{id}/reset-password` (returns a fresh one-time link) | new | admin |

## 5. Frontend — Vite + React + TypeScript + Tailwind, one app

`frontend/` (new top-level dir, own `package.json`, not inside `backend/`). Build output is static; served by Caddy directly (a second site block) or by Starlette's `StaticFiles` mounted in `server.py` under `/` — decided at build time based on what's simpler on the box, not a design fork.

Pages, shared shell (nav + role-aware menu):
- **Login** — email/password, "forgot password" → request-reset.
- **Portfolio** (default landing for every role) — one row per account: health/arc/state, forecast (basis + range), priority (lens + score), open interventions, revenue. Filters by tenant (admin only) and account status.
- **Account / Journey Canvas** — the evidence timeline (episodes incl. interventions), phases, arc with citations, forecast block, counterfactual hooks, narrative, open + closed interventions with an approve/report action for csm/admin.
- **Interventions** — cross-account queue: proposed (approve), sent (report), closed; stuck/delivery-problem flags surfaced (from `list_interventions`).
- **ROI & Power-of-1** (cfo/cro/admin) — priorities ranked list, Po1 $/point with its basis chain shown (never a bare number), realized-vs-exposure per playbook.
- **Review Queue** (csm/admin) — pending signal reviews, accept/reject/reclassify.
- **Calibrations** (admin) — Wizard C proposals, evidence, before/after, approve/reject.
- **Settings** (admin) — Users (invite/role/active/scoping), Playbooks (webhook URL+secret masked, disabled playbooks, automation level, kill switch — `configure_playbooks`), data-origin disclosure banner shown platform-wide (every page footer: `data_origin` label from `origin_block`).

No component library beyond Tailwind + a couple of small deps (charting: reuse whatever the `dataviz` skill's palette conventions are, decided at build time, not a fork worth asking about). TypeScript types generated by hand from the route response shapes (no OpenAPI codegen in this build — the routes are few enough).

## 6. What is explicitly NOT in this build

SSO, MFA, email delivery for setup/reset links (an admin relays them out of band — fine for the number of humans on this box today; a mail provider needs its own self-service-reset security review, not bolted on here), a settings page for the data-origin/economics config files (still edited on disk), mobile layout beyond basic responsiveness, real-time updates (polling only). All on the governance High list or later, not silently dropped — named here so they don't get assumed done.

## 7. Order of work

1. Backend: `app_api/auth.py` (session, RBAC decorator, rate limit), `app_api/users.py`, wire `create_customer`'s admin user + new invites through the setup-link flow, delete `approval_queue.py` + the `auth_middleware.py` placeholder, tests, deploy.
2. Frontend scaffold: Vite/React/TS/Tailwind, API client + types, shell/nav, Login + Portfolio pages, served and reachable end to end.
3. Remaining pages (Account/Journey Canvas, Interventions, ROI/Po1, Review Queue, Calibrations, Settings) — independent files behind the fixed API contract from step 1, safe to build in parallel once 1–2 are live.
