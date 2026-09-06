"""
The human UI's backend — session-authenticated routes over the same
service functions the Bearer-keyed /api/* routes call (docs/design/ui-rbac.md).

    auth       session cookies, password hashing, one-time setup tokens, RBAC
    users      invite / list / patch / reset-password (admin only)
    http       route registration, mounted in server.py

Never duplicates business logic: every handler here calls straight into
journeys/playbooks/roi/wizards, the same functions the MCP tools and the
Bearer HTTP routes use.
"""
