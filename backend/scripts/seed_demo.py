"""
Seed CustomerIntelV1 with the three protocol-shaped demo tenants and a
replay of live customer 415. Idempotent: a tenant whose domain already
exists is skipped.

    DATABASE_URL=... python scripts/seed_demo.py
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
    import models  # noqa: F401
    from models import Customer

    with app.app_context():
        db.create_all()
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
            reg = register(m, files, name_suffix='')
            c = db.session.get(Customer, reg['customer_id'])
            c.domain = domain
            db.session.commit()
            print(f"registered {m['manifest_id']} → customer {reg['customer_id']} {reg['status']} "
                  f"coverage={reg['wizard_a']['coverage'] if reg.get('wizard_a') else None}")

        if Customer.query.filter_by(domain=REPLAY['domain']).first():
            print(f"skip 415 replay: {REPLAY['domain']} exists")
        else:
            cid = create_customer(name=REPLAY['name'], domain=REPLAY['domain'], vertical=REPLAY['vertical'],
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
