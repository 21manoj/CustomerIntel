"""
Lifecycle trace — walk one tenant through the whole platform and print, at
every step, the exact rows written and their ids. Reads like a ledger, so
the lifecycle can be READ rather than inferred.

    python scripts/lifecycle_trace.py [--out trace.md] [--name "Lifecycle Trace"] [--vertical saas_premium]

Creates a fresh synthetic tenant (data_origin='synthetic_demo'), then:
  1 create_customer · 2 upload accounts · 3 upload KPIs (one blank value)
  4 process_data (ingest → health with provenance → journey → run row)
  5 structured signal (declared subtype) · 6 free-text signal (extractor or stub)
  7 exact duplicate · 8 human review (reject) · 9 outcome logged + linked
  10 journey + narrative · 11 Ask AI · 12 what Hindsight needs · 13 audit tail
Every step lists the tables it touched: +rows, ids, and the fields that matter.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['MCP_TRANSPORT'] = 'stdio'          # in-process tool calls are the local, trusted path (the container exports http for the server)

ACCOUNTS = (
    "source_account_id,account_name,industry,region,arr,csm_name,csm_email,csm_manager,executive_sponsor,"
    "primary_champion_name,primary_champion_title,tier,employee_count,products,contract_end,renewal_date\n"
    "ACC-1,Harbor Analytics,Software,North America,1800000,Maya Johnson,maya@vendor.test,Sam Rivera,Tom Becker,"
    "Elena Rossi,VP Data,Enterprise,900,,2026-12-01,2026-12-01\n"
)
KPIS = (
    "source_account_id,kpi_code,kpi_name,pillar,measured_at,value,target,weight,status\n"
    "ACC-1,P1-KPI1,DAU rate,P1,2026-01-01,62,70,0.3,warning\n"
    "ACC-1,P1-KPI2,Feature adoption breadth,P1,2026-01-01,55,65,0.2,warning\n"
    "ACC-1,P2-KPI1,Exec sponsor engagement,P2,2026-01-01,70,80,0.3,ok\n"
    "ACC-1,P1-KPI1,DAU rate,P1,2026-02-01,58,70,0.3,warning\n"
    "ACC-1,P1-KPI2,Feature adoption breadth,P1,2026-02-01,,65,0.2,warning\n"      # blank → skipped, counted
    "ACC-1,P2-KPI1,Exec sponsor engagement,P2,2026-02-01,64,80,0.3,warning\n"
    "ACC-1,P1-KPI1,DAU rate,P1,2026-03-01,49,70,0.3,critical\n"
    "ACC-1,P2-KPI1,Exec sponsor engagement,P2,2026-03-01,55,80,0.3,critical\n"
)

TABLES = [  # (model name, id attr, formatter)
    ('Customer', 'customer_id', lambda r: f"{r.customer_name} · vertical via CustomerConfig · data_origin={r.data_origin}"),
    ('CustomerConfig', 'id', lambda r: f"customer {r.customer_id} vertical={r.vertical}"),
    ('CustomerApiKey', 'id', lambda r: f"prefix={r.key_prefix} scopes={r.scopes}"),
    ('CsvUpload', 'id', lambda r: f"{r.file_type} sha={r.sha256[:10]}… rows={r.row_count} bytes={r.byte_count} key={r.key_kind} consumed_by_run={r.process_run_id}"),
    ('CsvUploadStaging', 'staging_id', lambda r: f"{r.file_type} upload_id={r.upload_id}"),
    ('ProcessRun', 'id', lambda r: f"{r.run_id} {r.status} mode={r.mode} uploads={r.upload_ids} counts={r.counts} steps={r.steps}"),
    ('Account', 'account_id', lambda r: f"{r.account_name} arr={r.revenue} status={r.account_status} arc={r.arc_type}"),
    ('KPIMeasurement', 'kpi_id', lambda r: f"{r.kpi_code} {r.measured_at.date()} value={r.value} upload_id={r.upload_id}"),
    ('HealthScore', 'health_score_id', lambda r: f"{r.measurement_month} score={r.health_score} status={r.health_status} weights={r.pillar_weights} source={r.weight_source} codes_used={r.kpi_codes_used} dropped={r.kpi_codes_dropped} catalog={r.catalog_version} taxonomy={r.taxonomy_version} scorer={r.scorer_version} input_upload={r.input_upload_id} run={r.process_run_id} qual={r.qual_score} early_warning={r.early_warning}"),
    ('QualitativeSignal', 'id', lambda r: f"{r.signal_id[:8]}… source={r.source_type} type={r.signal_type} occurred={r.occurred_at} node={r.cg_node_id} review={r.requires_review} urgency={r.effective_urgency} model={r.llm_model_version} extractions={len(r.extractions or [])}"),
    ('ContextNode', 'node_id', lambda r: f"{r.node_type}/{r.node_subtype} source={r.source} platform={r.source_platform} event_id={r.source_event_id} role={(r.properties or {}).get('role')} basis={(r.properties or {}).get('classification_basis')} urgency={(r.properties or {}).get('effective_urgency')} person={(r.properties or {}).get('stakeholder_name')} review={((r.properties or {}).get('review') or {}).get('status')} rev={r.revenue_impact} title={(r.title or '')[:60]!r}"),
    ('ContextEdge', 'edge_id', lambda r: f"{r.from_node_id} -{r.edge_type}-> {r.to_node_id} by={r.created_by}"),
    ('JourneyData', 'id', lambda r: f"account {r.account_id} state={r.journey_json.get('state')} arc={(r.journey_json.get('arc') or {}).get('arc_type')} gen={r.generator_version} episodes={len(r.journey_json.get('episodes', []))} live_months={len(r.journey_json.get('live_months', []))} narrative_sentences={(r.journey_json.get('narrative') or {}).get('sentence_count')}"),
    ('SignalReview', 'id', lambda r: f"{r.decision} node={r.node_id} signal={r.signal_id[:8]}… by={r.reviewer} was_flagged={r.was_flagged} note={r.note!r}"),
    ('WizardRun', 'id', lambda r: f"{r.wizard} {r.status} {r.run_id}"),
    ('ToolAuditLog', 'id', lambda r: f"{r.surface}/{r.tool} cust={r.customer_id} key={r.key_kind} {r.outcome} {r.detail or ''}"),
]


class Ledger:
    def __init__(self, app, out):
        self.app, self.out, self.n = app, out, 0
        import models
        self.models = []
        for name, _idattr, fmt in TABLES:
            if not hasattr(models, name):
                continue
            m = getattr(models, name)
            pk = list(m.__table__.primary_key.columns)[0].name       # never guess the id column
            self.models.append((m, pk, fmt, name))
        self.high = self._snapshot()

    def _snapshot(self):
        from sqlalchemy import func
        from extensions import db
        with self.app.app_context():
            return {name: (db.session.query(func.max(getattr(m, idattr))).scalar() or 0) for m, idattr, _, name in self.models}

    def write(self, line=''):
        print(line)
        self.out.write(line + '\n')

    def step(self, title, call, fn, filter_customer=None):
        self.n += 1
        self.write(f"\n## Step {self.n} — {title}\n")
        self.write(f"```\n{call}\n```")
        with self.app.app_context():
            result = fn()
        after = self._snapshot()
        wrote = False
        with self.app.app_context():
            for m, idattr, fmt, name in self.models:
                lo, hi = self.high[name], after[name]
                if hi <= lo:
                    continue
                col = getattr(m, idattr)
                rows = m.query.filter(col > lo, col <= hi).order_by(col).all()
                if filter_customer is not None and hasattr(m, 'customer_id'):
                    rows = [r for r in rows if getattr(r, 'customer_id', None) in (None, filter_customer)]
                if not rows:
                    continue
                wrote = True
                self.write(f"\n**{m.__tablename__}** +{len(rows)}")
                for r in rows[:14]:
                    self.write(f"- {idattr}={getattr(r, idattr)} · {fmt(r)}")
                if len(rows) > 14:
                    self.write(f"- … {len(rows) - 14} more")
        if not wrote:
            self.write("\n_(no new rows — read-only step)_")
        self.high = after
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='lifecycle_trace.md')
    ap.add_argument('--name', default='Lifecycle Trace')
    ap.add_argument('--vertical', default='saas_premium')
    args = ap.parse_args()

    from mcp_server.common import get_flask_app
    app = get_flask_app()
    with app.app_context():
        import models  # noqa: F401 — metadata for create_all
        from extensions import db
        db.create_all()
    out = open(args.out, 'w', encoding='utf-8')
    L = Ledger(app, out)
    tag = uuid.uuid4().hex[:6]
    L.write(f"# Lifecycle trace — {args.name} ({args.vertical}) — {datetime.utcnow().isoformat()}Z")
    L.write("Every step shows the tool call, then the rows it wrote (table, id, the fields that matter). "
            "Tenant is synthetic (data_origin='synthetic_demo'); the extractor is the real model when ANTHROPIC_API_KEY is set, else the keyword stub.")

    from mcp_server.cs_pulse_onboarding import (create_customer, upload_csv, process_data, submit_signal, review_signal,
                                                 log_outcome, get_journey, get_review_queue, ask, list_journeys)

    def _create():
        r = create_customer(name=f'{args.name} {tag}', domain=f'trace-{tag}.demo', vertical=args.vertical,
                            admin_email=f'trace_{tag}@demo.test', admin_name='Trace')
        from models import Customer
        from extensions import db
        c = db.session.get(Customer, r['customer_id']); c.data_origin = 'synthetic_demo'; db.session.commit()
        return r
    r = L.step('create the tenant', f"create_customer(name='{args.name} {tag}', domain='trace-{tag}.demo', vertical='{args.vertical}', …)  # + data_origin='synthetic_demo'", _create)
    cid = r['customer_id']
    L.write(f"\n→ customer_id={cid}")

    L.step('upload the roster (accounts CSV)', "upload_csv(cid, 'account_details.csv', <1 account: Harbor Analytics, champion Elena Rossi, renewal 2026-12-01>)",
           lambda: upload_csv(cid, 'account_details.csv', ACCOUNTS), cid)
    L.step('upload KPI measurements (3 months; one blank value)', "upload_csv(cid, 'kpi_measurements.csv', <8 rows, Jan–Mar 2026>)",
           lambda: upload_csv(cid, 'kpi_measurements.csv', KPIS), cid)
    pd = L.step('process_data — ingest, health with provenance, journey, run record', "process_data(cid)",
                lambda: process_data(cid), cid)
    L.write(f"\n→ run_id={pd['run_id']} status={pd['status']} steps={pd['steps_completed']}")
    with app.app_context():
        from models import Account
        aid = Account.query.filter_by(customer_id=cid).first().account_id

    s1 = L.step('a structured signal — declared taxonomy subtype, no model call',
                "submit_signal(cid, aid, 'Elena Rossi accepted a role at another company; last day March 27', source_type='crm_activity', signal_type='champion_departure', occurred_at='2026-03-20T10:00:00Z', participants=[{'name':'Elena Rossi','role':'VP Data'}], source_ref='crm:evt:7781')",
                lambda: submit_signal(cid, aid, 'Elena Rossi accepted a role at another company; last day March 27', source_type='crm_activity',
                                      signal_type='champion_departure', occurred_at='2026-03-20T10:00:00Z',
                                      participants=[{'name': 'Elena Rossi', 'role': 'VP Data'}], source_ref='crm:evt:7781'), cid)
    L.write(f"\n→ {s1['status']} · evidence={s1.get('evidence')}")
    s2 = L.step('a free-text signal — the extractor types it against the tenant vocabulary',
                "submit_signal(cid, aid, <meeting note: half the seats idle since March, procurement wants a true-down; Tom Becker asked about the analytics module for EMEA>, source_type='meeting', occurred_at='2026-04-03T15:00:00Z')",
                lambda: submit_signal(cid, aid, "Meeting notes 3 Apr. Half the seats haven't logged in since March and procurement wants to true-down at renewal. "
                                                "On the positive side Tom Becker asked what adding the analytics module for the EMEA team would cost.",
                                      source_type='meeting', occurred_at='2026-04-03T15:00:00Z'), cid)
    L.write(f"\n→ {s2['status']} · subtypes={(s2.get('evidence') or {}).get('subtypes')} · basis={(s2.get('evidence') or {}).get('basis')}")
    s3 = L.step('the same note again — exact duplicate within the window', "submit_signal(cid, aid, <same text>, source_type='email', occurred_at='2026-04-04T09:00:00Z')",
                lambda: submit_signal(cid, aid, "Meeting notes 3 Apr. Half the seats haven't logged in since March and procurement wants to true-down at renewal. "
                                                "On the positive side Tom Becker asked what adding the analytics module for the EMEA team would cost.",
                                      source_type='email', occurred_at='2026-04-04T09:00:00Z'), cid)
    L.write(f"\n→ {s3['status']} (duplicate_of={s3.get('duplicate_of', '')[:8]}…) — nothing written, by design")

    q = L.step('what is waiting for a human', "get_review_queue(cid)", lambda: get_review_queue(cid), cid)
    L.write(f"\n→ {q['total']} signal(s) flagged requires_review")
    with app.app_context():
        from models import ContextNode
        nodes = ContextNode.query.filter_by(account_id=aid, node_type='SIGNAL').order_by(ContextNode.node_id).all()
        target = next((n for n in nodes if (n.properties or {}).get('signal_id') == s2['signal_id']), nodes[-1])
        target_id, target_sig = target.node_id, (target.properties or {}).get('signal_id')
    rv = L.step('a human decision — reject one extracted node (kept for audit, excluded from the journey)',
                f"review_signal(cid, '{target_sig[:8]}…', 'reject', node_id={target_id}, note='the seat comment was about a sandbox tenant', reviewer='vp-cs@demo.test')",
                lambda: review_signal(cid, target_sig, 'reject', node_id=target_id, note='the seat comment was about a sandbox tenant', reviewer='vp-cs@demo.test'), cid)
    L.write(f"\n→ nodes={rv['nodes']} audit_ids={rv['audit_ids']} journeys_rebuilt={rv['journeys_rebuilt']}")

    oc = L.step('the decision the measurement chain hangs on — an outcome, linked to the signal that preceded it',
                f"log_outcome(cid, aid, 'contraction', '2026-06-15', revenue=300000, note='Renewed at 12 fewer seats', linked_signal_ids=['{s1['signal_id'][:8]}…'], decided_by='ae@demo.test', source_ref='SO-4410')",
                lambda: log_outcome(cid, aid, 'contraction', '2026-06-15', revenue=300000, note='Renewed at 12 fewer seats',
                                    linked_signal_ids=[s1['signal_id']], decided_by='ae@demo.test', source_ref='SO-4410'), cid)
    L.write(f"\n→ {oc['status']} bucket={oc['bucket']} revenue={oc['revenue']} linked={oc['linked_signal_node_ids']} clamped={oc['evidence_clamped']}")

    j = L.step('the journey as the read surface serves it (with the evidence index and the narrative)', "get_journey(cid, aid)", lambda: get_journey(cid, aid), cid)
    L.write(f"\n→ arc={j['arc']['arc_type']} state={j['state']} phases={[p['name'] for p in j['phases']]} live_months={len(j['live_months'])} evidence_index={len(j['evidence'])} open_reviews={j['open_review_count']}")
    L.write("\n**Narrative** (every sentence cites the episode ids it was built from):")
    for ch in j['narrative']['chapters']:
        for s in ch['sentences']:
            L.write(f"- [{ch['phase']}] {s['text']}  `{s['cites']}`")
    for o in j['narrative']['omitted']:
        L.write(f"- _omitted_ ({o.get('template') or o['reason']}): {o.get('note') or o.get('cites')}")
    L.write("\n**Leading vs trailing** (month · kpi_only · qual · label · roles):")
    for s in j['leading_vs_trailing']['series']:
        L.write(f"- {s['month']} · {s['kpi_only']} · {s['qual']} · {s['early_warning']} · {s['roles']}")

    a = L.step('Ask AI over the contract (cited; absence of evidence is an answer)', "ask(cid, 'Why is Harbor Analytics at risk and what did we decide?', account_id=aid)",
               lambda: ask(cid, 'Why is Harbor Analytics at risk and what did we decide?', account_id=aid), cid)
    L.write(f"\n→ model={a.get('model')} sentences={len(a.get('sentences', []))} unsupported={len(a.get('unsupported', []))} gaps={len(a.get('evidence_gaps', []))}")
    for s in a.get('sentences', [])[:6]:
        L.write(f"- {s['text']}  `{s['cites']}`")

    L.step('portfolio row (what a CRO sees first)', "list_journeys(cid)", lambda: list_journeys(cid), cid)
    L.write("\n## What Hindsight needs from here\n")
    L.write("Wizard B (`trigger_wizard(cid, 'b')`) needs ≥5 journeys with outcomes to derive pattern profiles and run the lead-time backtest; "
            "on a one-account trace it reports 'insufficient'. On the seeded demo tenants it runs inside process_data (WizardRun rows). "
            "The intervention step (playbook governance layer) is the gap in this trace: today an intervention is only a signal with role "
            "'intervention'; the design doc adds the record (requested → approved → dispatched → receipt) and its measurement.")

    L.step('the audit trail of everything above', "GET /api/audit?customer_id=cid  (in-process calls are recorded as key_kind=local)", lambda: None, cid)
    out.close()
    print(f"\nwritten: {args.out}  (customer_id={cid}, account_id={aid})")


if __name__ == '__main__':
    main()
