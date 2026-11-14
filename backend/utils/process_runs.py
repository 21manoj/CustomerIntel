"""Close out data-processing runs that were interrupted.

process_data writes its ProcessRun row as 'running' and updates it when it
finishes or fails. If the process is stopped part-way (a deploy, a restart,
an out-of-memory kill) nothing ever updates the row, and it would say
'running' for ever. close_abandoned_runs marks such rows failed, with the
reason, once they are older than platform.attention.process_run_stuck_minutes
(config/app_api.json). The signal worker calls it on every pass.

A run that is genuinely slow and finishes after being closed here simply
overwrites the row with its real result.
"""
from datetime import datetime, timedelta
from typing import Optional

INTERRUPTED_PREFIX = 'Interrupted:'


def close_abandoned_runs(now: Optional[datetime] = None) -> int:
    """Mark stale 'running' ProcessRun rows failed. Returns how many. Needs an app context."""
    from app_api import settings
    from extensions import db
    from models import ProcessRun

    now = now or datetime.utcnow()
    minutes = int(settings.get('platform', 'attention')['process_run_stuck_minutes'])
    rows = ProcessRun.query.filter(ProcessRun.status == 'running',
                                   ProcessRun.started_at < now - timedelta(minutes=minutes)).all()
    for r in rows:
        r.status = 'failed'
        r.errors = (r.errors or []) + [
            f"{INTERRUPTED_PREFIX} the run started {r.started_at.isoformat()} and never finished "
            f"(the process was stopped part-way). Closed automatically after {minutes} minutes."]
        r.finished_at = now
    if rows:
        db.session.commit()
    return len(rows)
