#!/usr/bin/env python3
"""
QSIM Signal Engine — Data Cleanup.

Ensures no QSIM data persists when the feature is disabled.
Called on:
  1. App startup when FEATURE_SIGNAL_ENGINE=false
  2. When a customer's signal_engine toggle is turned off
  3. Manual cleanup via API

QSIM signals are identified by source_type IN ('slack', 'email', 'transcript', 'manual')
— these are the 4 ingestion channels that only QSIM uses. Pre-existing signals
(health_check, meeting, observation, etc.) are never touched.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Source types that are exclusively QSIM-ingested
QSIM_SOURCE_TYPES = ('slack', 'email', 'transcript', 'manual')


def purge_qsim_signals_for_customer(customer_id: int, db_session=None) -> int:
    """Delete all QSIM-ingested signals for a customer.

    Only removes signals with source_type in QSIM_SOURCE_TYPES.
    Pre-existing qualitative signals (health_check, meeting, etc.) are untouched.

    Returns count of deleted signals.
    """
    try:
        from extensions import db
        from sqlalchemy import text

        if db_session is None:
            db_session = db.session

        # Get account IDs for this customer
        acct_rows = db_session.execute(text(
            "SELECT account_id FROM accounts WHERE customer_id = :cid"
        ), {'cid': customer_id})
        account_ids = [r[0] for r in acct_rows]

        if not account_ids:
            return 0

        # Delete QSIM signals (identified by source_type)
        placeholders = ','.join(str(a) for a in account_ids)
        result = db_session.execute(text(f"""
            DELETE FROM qualitative_signals
            WHERE account_id IN ({placeholders})
              AND source_type IN ('slack', 'email', 'transcript', 'manual')
            RETURNING signal_id
        """))
        deleted = list(result)
        db_session.commit()

        count = len(deleted)
        if count > 0:
            logger.info(
                "QSIM cleanup: purged %d signals for customer %d", count, customer_id
            )
        return count

    except Exception as e:
        logger.warning("QSIM cleanup failed for customer %d: %s", customer_id, e)
        return 0


def purge_all_qsim_signals() -> int:
    """Purge ALL QSIM-ingested signals across all customers.

    Called on app startup when FEATURE_SIGNAL_ENGINE=false globally.
    Safety measure: ensures no orphaned QSIM data exists when the
    feature is completely disabled.
    """
    try:
        from extensions import db
        from sqlalchemy import text

        result = db.session.execute(text("""
            DELETE FROM qualitative_signals
            WHERE source_type IN ('slack', 'email', 'transcript', 'manual')
            RETURNING signal_id
        """))
        deleted = list(result)
        db.session.commit()

        count = len(deleted)
        if count > 0:
            logger.info("QSIM startup cleanup: purged %d orphaned signals (toggle OFF)", count)
        return count

    except Exception as e:
        logger.warning("QSIM startup cleanup failed: %s", e)
        return 0


def purge_alert_records_for_customer(customer_id: int) -> int:
    """Delete alert records for a customer when signal engine is disabled."""
    try:
        from extensions import db
        from sqlalchemy import text

        result = db.session.execute(text(
            "DELETE FROM alert_records WHERE customer_id = :cid RETURNING id"
        ), {'cid': customer_id})
        deleted = list(result)
        db.session.commit()
        return len(deleted)
    except Exception:
        return 0
