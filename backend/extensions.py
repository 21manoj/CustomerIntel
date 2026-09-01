from flask_sqlalchemy import SQLAlchemy
from flask import request

db = SQLAlchemy()

def get_customer_id():
    """
    Get customer ID from request headers.

    Returns int for legacy integer IDs, str for UUID-prefixed IDs.
    Falls back to 1 for backward compatibility.
    """
    raw = request.headers.get('X-Customer-ID', '1')
    try:
        return int(raw)
    except (ValueError, TypeError):
        # UUID-style identifier — return as string
        return raw