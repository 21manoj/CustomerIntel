"""
The platform console — the operator's view across every tenant
(docs/design/platform-console.md).

    access.py    who may come in: users.platform_role, and nothing else
    service.py   what they see (overview, attention list, tenants, audit) and the
                 few things they may do (suspend / reactivate a tenant, revoke a
                 key, deactivate a user, issue a password reset)
    http.py      /app/api/platform/* — session cookie, every route guarded here

Named platform_admin, not platform: `platform` is a standard-library module.
"""
