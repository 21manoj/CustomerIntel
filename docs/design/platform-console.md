# Platform console

The operator's view across every tenant: what is running, what needs attention,
and the few things an operator may do about it. It replaces the "Super Admin
console" of the earlier product (CustomerSuccessAI-DataCenter) — the need is the
same, the design is not.

## What the earlier console did, and what was kept

| Earlier console | Here | Why |
|---|---|---|
| Dashboard: tenant, user and account totals | **Overview**: the same totals, plus system status and an attention list | Totals alone do not tell an operator what to do next |
| Tenant list and tenant detail | **Tenants**, **Tenant detail** | Kept |
| Deactivate / reactivate a tenant | **Suspend / reactivate**, with a required reason | Kept; made reversible and audited |
| User list, deactivate, edit | Deactivate / activate, issue a password reset | Kept. Editing a tenant's users stays with the tenant's own admin (Settings) |
| API keys: list, revoke | List (id and prefix only), revoke | Kept. Issuing a key stays on the box so a raw key never crosses a browser |
| Activity log, export | **Audit log** across tenants, filterable | Kept, on the existing tool audit log |
| Purge, bulk purge, orphan cleanup | Not in the console | Irreversible. A destructive action belongs in a reviewed script on the box, not behind a button |
| Licence tiers, seat limits, feature toggles | Not built | This product has no licence tiers; inventing them here would be a guess |
| Vertical / KPI template authoring | Not built | Catalogs here are reviewed JSON files under version control |
| Load-test runner, backup/restore buttons | Not built | Deploys, backups and restores are box operations with their own runbook |
| Create a tenant | Not yet | Exists as the server-key `create_customer` tool; can be added to the console later |

## Who is an operator

The earlier product made the operator a role value (`super_admin`) and, in
places, a special tenant. Both are avoided here.

* **`users.platform_role`** — `operator` (may act) or `viewer` (read-only), null
  for everyone else. It is a separate column from the tenant `role`, because a
  tenant's own `admin` is not a platform operator. On 2026-09-08 this product
  leaked across tenants precisely because one code path treated tenant
  `role = 'admin'` as "unrestricted"; that mistake is not repeated.
* **No "super tenant" row.** A tenant row is something the product scores,
  processes and reports on. An operator who is not a customer should not appear
  as one. A console-only operator has `customer_id` null and no tenant scope.
* **Granted only on the box**: `scripts/manage_platform_operators.py`
  (`list | create | grant | revoke | reset`). No HTTP route writes the column,
  so nobody can promote themselves — or be promoted by a tenant admin — from a
  browser.
* **The console is the only cross-tenant door.** `platform_role` opens
  `/app/api/platform/*` and nothing else. On the ordinary tenant routes an
  operator is scoped exactly like any other user, which for a console-only
  operator means refused everywhere.

## Routes

All under `/app/api/platform/`, session cookie, guarded by
`platform_admin.access.require_platform`.

| Route | Access |
|---|---|
| `GET overview` — totals, system status, attention list | operator, viewer |
| `GET tenants` | operator, viewer |
| `GET tenants/{id}` — accounts, users, keys, runs, uploads, recent audit | operator, viewer |
| `GET audit?customer_id=&outcome=&tool=&before_id=&limit=` | operator, viewer |
| `POST tenants/{id}/suspend {reason}` / `POST tenants/{id}/reactivate` | operator |
| `POST keys/{id}/revoke` | operator |
| `POST users/{id}/active {active}` | operator |
| `POST users/{id}/reset-password` — one-time token, shown once | operator |

## Suspending a tenant

`customers.suspended_at` / `suspended_reason`. While set:

* the tenant's users cannot sign in, and sessions they already hold stop working
  on their next request (`app_api.auth.tenant_suspended`);
* the tenant's API keys are refused (`api_key_service.validate_api_key`);
* nothing is deleted, and reactivating restores all of it.

An operator is never locked out by a suspension, and cannot suspend the tenant
their own login belongs to.

## The attention list

Each item is a fact read from the database; thresholds live in
`config/app_api.json` → `platform.attention`.

| Item | Severity |
|---|---|
| Signal worker stopped reporting | critical |
| A journey failed to rebuild (the previous build is still served) | critical |
| A tenant's last processing run failed | critical |
| Worker failing passes; last run finished with errors or is stuck | warning |
| Journeys behind the generator version | warning |
| No new data for N days | warning (info for synthetic demo tenants) |
| Tenant past its end date; API key about to expire | warning |
| Many refused calls in the window | warning |
| Active users with no tenant scope | warning |
| Tenant suspended; tenant ending soon; accounts but no data | info |

## What an operator never sees

Key hashes, raw keys, password hashes, setup-token hashes, the server key, and
signal text. Keys appear by id and prefix only. The console shows counts and
status for a tenant's data, not the customer conversations themselves — for
that, an operator would need a tenant login granted by the tenant.

## Audit

Every write, every tenant-detail view, and every refused attempt is written to
the tool audit log as `platform.<action>`, with the operator's user id. Role
changes made by the CLI are logged the same way.

## Tests

`backend/tests/test_platform_admin.py`: every platform route refuses no-session
and every tenant role (including tenant admins); viewers cannot write; no secret
appears in any response; `platform_role` opens nothing on tenant routes;
suspension blocks logins, live sessions and keys and reactivation restores them;
operators cannot manage each other from the browser; every route in
`platform_admin.http.ROUTES` is exercised.
