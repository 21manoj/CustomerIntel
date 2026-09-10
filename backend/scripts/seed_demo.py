"""
Seed CustomerIntelV1 with the protocol-shaped demo tenants and a replay of
live customer 415. Idempotent: a tenant whose domain already exists is
skipped.

    DATABASE_URL=... [DEMO_EXTRACTOR=auto|model|stub|oracle] python scripts/seed_demo.py

v2 manifests submit their communications through the signal engine.
DEMO_EXTRACTOR picks what classifies them (demo/oracle.py): 'auto' (default:
the model when ANTHROPIC_API_KEY is set, else the oracle — the manifest's
labels played back so the narratives seed without a key; not a model
result, and the scorecard says so), 'model', 'stub' (the keyword floor),
'oracle'. The 415 replay is real data and stays on the CSV path untouched.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPLAY = {
    'name': 'Phoenix Data Centers (415 replay)', 'domain': 'phoenix-415-replay.demo', 'vertical': 'dc2_s',
    'fixture': Path(__file__).resolve().parent.parent / 'tests' / 'fixtures' / 'customer415_dc2_s',
    'data_origin': 'synthetic_load_driver_replay',
}


def main():
    from flask import Flask
    from extensions import db
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL']
    db.init_app(app)
    import mcp_server.common as _common
    _common._flask_app = app

    # upload_csv/process_data moved from ONBOARDING_TOOLS to KEYED_TOOLS in the
    # 2026-09-09 cross-tenant fix (any caller could mutate any existing customer_id's
    # data with no key at all). require_auth_if_key_present() only trusts non-HTTP
    # transport (MCP_TRANSPORT != 'http') as implicitly local; this script runs inside
    # the app container, where MCP_TRANSPORT=http is set for the real server, so
    # without this it is indistinguishable from an anonymous HTTP caller — harmless on
    # a box with pre-existing demo tenants (idempotent skip hides it), but a hard
    # failure the first time it runs against a genuinely empty database. This script
    # already has full container/DB access — server-level trust — so present that key
    # explicitly instead of leaning on the onboarding tools' old frictionless loophole.
    import mcp_server.auth as auth
    auth._current_api_key_var.set(os.environ.get('MCP_SERVER_API_KEY', ''))
    import models  # noqa: F401
    from models import Customer

    with app.app_context():
        from utils.schema import migrate; migrate(db.engine)
        from demo.generate import generate, register, load_manifest, MANIFESTS_DIR
        from mcp_server.cs_pulse_onboarding import create_customer, upload_csv, process_data

        for path in sorted(MANIFESTS_DIR.glob('demo_*.json')):
            m = load_manifest(path)
            domain = f"{m['domain_prefix']}.demo"
            if Customer.query.filter_by(domain=domain).first():
                print(f"skip {m['manifest_id']}: {domain} exists")
                continue
            m = dict(m, domain_prefix=m['domain_prefix'])
            files = generate(m)
            reg = register(m, files, name_suffix='', extractor=os.environ.get('DEMO_EXTRACTOR', 'auto'))
            c = db.session.get(Customer, reg['customer_id'])
            c.domain = domain
            db.session.commit()
            sc = reg.get('scorecard')
            print(f"registered {m['manifest_id']} → customer {reg['customer_id']} {reg['status']} "
                  f"coverage={reg['wizard_a']['coverage'] if reg.get('wizard_a') else None}"
                  + (f" extraction[{sc['model_version']}] hit_rate={sc['hit_rate']} unclassified={sc['unclassified']}" if sc else ''))

        if Customer.query.filter_by(domain=REPLAY['domain']).first():
            print(f"skip 415 replay: {REPLAY['domain']} exists")
        else:
            cid = create_customer(data_origin='synthetic_replay', name=REPLAY['name'], domain=REPLAY['domain'], vertical=REPLAY['vertical'],
                                  admin_email=f"admin@{REPLAY['domain']}", admin_name='Replay Admin')['customer_id']
            c = db.session.get(Customer, cid)
            c.data_origin = REPLAY['data_origin']
            db.session.commit()
            for ft in ('account_details.csv', 'kpi_measurements.csv', 'enhanced_qualitative_signals.csv', 'outcomes.csv'):
                upload_csv(cid, ft, (REPLAY['fixture'] / ft).read_text())
            res = process_data(cid)
            print(f"registered 415 replay → customer {cid} {res['status']} steps={len(res['steps_completed'])}")


if __name__ == '__main__':
    main()
