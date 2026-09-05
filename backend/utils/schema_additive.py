"""
Additive schema changes for tables that already exist on a deployed box.
db.create_all() creates missing TABLES only; columns added to an existing
model need an ALTER. No Alembic in this build yet — this is the ledger.
Run at boot (server.build_asgi_app) — idempotent.
"""
from __future__ import annotations

ADDITIVE_COLUMNS = {
    'kpi_measurements': {'upload_id': 'INTEGER'},
    'csv_upload_staging': {'upload_id': 'INTEGER'},
    'health_scores': {
        'kpi_weights': 'JSON', 'kpi_codes_used': 'JSON', 'kpi_codes_dropped': 'JSON', 'weight_source': 'VARCHAR(24)',
        'catalog_version': 'VARCHAR(20)', 'taxonomy_version': 'VARCHAR(20)', 'scorer_version': 'VARCHAR(20)',
        'input_upload_id': 'INTEGER', 'process_run_id': 'INTEGER',
    },
}


def ensure_additive_columns(engine) -> None:
    from sqlalchemy import text
    with engine.connect() as conn:
        for table, cols in ADDITIVE_COLUMNS.items():
            for name, ctype in cols.items():
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ctype}'))
        conn.commit()
