"""
PLACEHOLDER — not a real auth implementation.

The old repo's auth/session layer had confirmed, live gaps (no MFA/SSO
anywhere, tenant isolation enforced per-query rather than centrally,
SESSION_COOKIE_SECURE=false in production) — carrying that layer forward
verbatim would just re-import the same problems into a fresh build. This
stub exists only so approval_queue.py's Flask blueprint (the REST route
wiring, not its ApprovalQueueService business logic, which doesn't touch
this file at all) can be imported and exercised in tests before a real
session/auth strategy is designed.

Replace before this ever serves a real request.
"""


def get_current_customer_id():
    raise NotImplementedError(
        "auth_middleware is a placeholder — no real session/auth layer exists yet."
    )


def get_current_user_id():
    raise NotImplementedError(
        "auth_middleware is a placeholder — no real session/auth layer exists yet."
    )
