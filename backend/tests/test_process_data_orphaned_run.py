"""
_process_data_impl commits a ProcessRun row with status='running' before it
knows whether there is any data to process, then (when a tenant has neither
staged CSVs nor accounts/KPIs already in the DB) raised ToolError("No data
found...") without ever finalizing that row. GET /jobs/{job_id} (the v1
facade) and any stale-job sweep read ProcessRun.status as the source of
truth, so the row was left 'running' forever -- indistinguishable from a
job that is still in flight. Found 2026-09-13 while designing the facade's
job-status endpoint against main.

Fixed by wrapping the body from just after the run row is committed through
the final success-path commit in try/except Exception: any failure -- the
"no data" ToolError included -- rolls back, re-fetches the row by id, and
finalizes it as status='failed' with the exception message recorded in
errors and finished_at stamped, before re-raising the original exception
unchanged.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

from mcp_server.common import get_flask_app                # noqa: E402
app = get_flask_app()

# Registers every model class with db.metadata *before* the fixture's
# db.create_all() runs — create_all only creates tables for models that
# have already been imported somewhere.
from models import Customer, ProcessRun                    # noqa: E402


@pytest.fixture(scope='module')
def customer_with_no_data():
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(
            data_origin='synthetic_test',
            name=f'No Data {tag}', domain=f'no-data-{tag}.test', vertical='datacenter_v1',
            admin_email=f'no_data_{tag}@t.test', admin_name='No Data',
        )['customer_id']
        yield cid
        db.session.remove()
        db.drop_all()


def test_no_data_run_is_finalized_as_failed_not_left_running(customer_with_no_data):
    from mcp_server.cs_pulse_mcp_server import ToolError
    from mcp_server.cs_pulse_onboarding import process_data
    from models import ProcessRun

    cid = customer_with_no_data
    with app.app_context():
        with pytest.raises(ToolError, match='No data found'):
            process_data(cid)

        runs = ProcessRun.query.filter_by(customer_id=cid).all()
        assert len(runs) == 1, f"expected exactly one ProcessRun row, got {len(runs)}"
        run = runs[0]
        assert run.status == 'failed', (
            f"ProcessRun.status was {run.status!r} -- the row was left 'running' "
            f"instead of being finalized on the no-data failure path"
        )
        assert run.finished_at is not None
        assert run.errors and any('No data found' in e for e in run.errors)


def test_second_failed_call_leaves_no_running_rows_behind(customer_with_no_data):
    """Not just the first call: every failed run must finalize, so polling
    GET /jobs never sees a permanently-running row from any retry either."""
    from mcp_server.cs_pulse_mcp_server import ToolError
    from mcp_server.cs_pulse_onboarding import process_data
    from models import ProcessRun

    cid = customer_with_no_data
    with app.app_context():
        with pytest.raises(ToolError, match='No data found'):
            process_data(cid)

        runs = ProcessRun.query.filter_by(customer_id=cid).all()
        assert len(runs) == 2
        assert all(r.status == 'failed' for r in runs)
        assert not any(r.status == 'running' for r in runs)
