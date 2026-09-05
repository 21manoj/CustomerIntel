"""
Lifecycle trace — signals first. Walk one tenant through the platform in the
order evidence actually arrives, and print at every step the exact rows
written and their ids, so the lifecycle can be READ rather than inferred.

    python scripts/lifecycle_trace.py [--out trace.md] [--name "Lifecycle Trace"] [--vertical saas_premium]

Creates a fresh synthetic tenant (data_origin='synthetic_demo'), then:
  1 create the tenant · 2 roster only (accounts CSV, no KPIs) · 3 process_data on the roster
  4–7 communications arrive over ten weeks through the engine (email, ticket, CRM note, meeting)
  8 a typed-signals export (the CSM's own risk flag) takes the structured lane through the same engine
  9 the journey from evidence alone — leading layer, no trailing layer yet
  10 exact duplicate · 11 human review · 12 the outcome, linked to the signal that preceded it
  13 THEN the KPI file arrives — process_data scores the months; the journey shows both layers and the lead time
  14 Ask AI · 15 portfolio row · 16 what Hindsight needs
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
    "Elena Rossi,VP Data,Enterprise,900,,2026-08-01,2026-08-01\n"
)
# the CSM's own risk flag, as a Gainsight/ChurnZero-style export would carry it — the structured lane
CRM_FLAGS = (
    "signal_id,source_account_id,signal_date,signal_type,content,sentiment,sentiment_score,stakeholder_name,stakeholder_title,signal_ref,source_platform\n"
    "gs_flag_1,ACC-1,2026-03-25,csm_risk_flag,CSM marked renewal at risk in Gainsight,negative,-0.5,Maya Johnson,CSM,gs_flag_1,gainsight\n"
)
# the KPI file arrives LAST — monthly, and the trailing score crosses its own warning line only in May
KPIS = (
    "source_account_id,kpi_code,kpi_name,pillar,measured_at,value,target,weight,status\n"
    "ACC-1,P1-KPI1,DAU rate,P1,2026-01-01,74,70,0.3,ok\n"
    "ACC-1,P2-KPI1,Exec sponsor engagement,P2,2026-01-01,78,80,0.3,ok\n"
    "ACC-1,P1-KPI1,DAU rate,P1,2026-02-01,71,70,0.3,ok\n"
    "ACC-1,P2-KPI1,Exec sponsor engagement,P2,2026-02-01,72,80,0.3,warning\n"
    "ACC-1,P1-KPI1,DAU rate,P1,2026-03-01,64,70,0.3,warning\n"
    "ACC-1,P2-KPI1,Exec sponsor engagement,P2,2026-03-01,,80,0.3,warning\n"      # blank → skipped, counted
    "ACC-1,P1-KPI1,DAU rate,P1,2026-04-01,48,70,0.3,critical\n"
    "ACC-1,P2-KPI1,Exec sponsor engagement,P2,2026-04-01,50,80,0.3,warning\n"
    "ACC-1,P1-KPI1,DAU rate,P1,2026-05-01,30,70,0.3,critical\n"
    "ACC-1,P2-KPI1,Exec sponsor engagement,P2,2026-05-01,30,80,0.3,critical\n"
)
MEETING = ("Meeting notes 12 Mar. Half the seats haven't logged in since January and procurement wants to true-down at renewal. "
           "On the positive side Tom Becker asked what adding the analytics module for the EMEA team would cost.")

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
        assert models
        from extensions import db
        from utils.schema import migrate; migrate(db.engine)
    out = open(args.out, 'w', encoding='utf-8')
    L = Ledger(app, out)
    tag = uuid.uuid4().hex[:6]
    L.write(f"# Lifecycle trace — {args.name} ({args.vertical}) — {datetime.utcnow().isoformat()}Z")
    L.write("Every step shows the tool call, then the rows it wrote (table, id, the fields that matter). "
            "Tenant is synthetic (data_origin='synthetic_demo'); the extractor is the real model when ANTHROPIC_API_KEY is set, else the keyword stub.")

    from mcp_server.cs_pulse_onboarding import (create_customer, upload_csv, process_data, submit_signal, review_signal,
                                                 log_outcome, get_journey, get_review_queue, ask, list_journeys)

    def _create():
        return create_customer(name=f'{args.name} {tag}', domain=f'trace-{tag}.demo', vertical=args.vertical,
                               admin_email=f'trace_{tag}@demo.test', admin_name='Trace', data_origin='synthetic_demo')
    r = L.step('create the tenant — the data origin is declared here and disclosed on every surface',
               f"create_customer(name='{args.name} {tag}', domain='trace-{tag}.demo', vertical='{args.vertical}', data_origin='synthetic_demo', …)", _create)
    cid = r['customer_id']
    L.write(f"\n→ customer_id={cid}")

    L.step('the roster only — accounts CSV, no KPIs (signals-first: evidence comes before numbers)',
           "upload_csv(cid, 'account_details.csv', <1 account: Harbor Analytics, champion Elena Rossi, sponsor Tom Becker, renewal 2026-08-01>)",
           lambda: upload_csv(cid, 'account_details.csv', ACCOUNTS), cid)
    pd0 = L.step('process_data on the roster — nothing to score yet; the run is recorded anyway', "process_data(cid)", lambda: process_data(cid), cid)
    L.write(f"\n→ run_id={pd0['run_id']} steps={pd0['steps_completed']}")
    with app.app_context():
        from models import Account
        aid = Account.query.filter_by(customer_id=cid).first().account_id

    def _sig(text, **kw):
        return lambda: submit_signal(cid, aid, text, **kw)

    s_a = L.step('week 2 — an email arrives (free text; the extractor types it against the tenant vocabulary)',
                 "submit_signal(cid, aid, 'Elena skipped the QBR again and has not replied to two follow-ups…', source_type='email', occurred_at='2026-01-14T09:30:00Z', participants=[{'name':'Elena Rossi'}])",
                 _sig("Elena skipped the QBR again this quarter and hasn't replied to two follow-ups from Maya; her team lead says she is 'heads down on a platform review'.",
                      source_type='email', occurred_at='2026-01-14T09:30:00Z', participants=[{'name': 'Elena Rossi', 'role': 'VP Data'}]), cid)
    L.write(f"\n→ {s_a['status']} · subtypes={(s_a.get('evidence') or {}).get('subtypes')} · person={(s_a.get('evidence') or {}).get('person')}")
    s_b = L.step('week 5 — a support ticket', "submit_signal(cid, aid, 'The Salesforce sync keeps dropping records…', source_type='ticket', occurred_at='2026-02-05T16:10:00Z', source_ref='ZD-8821')",
                 _sig("Ticket ZD-8821: the Salesforce sync keeps dropping records and the ops team has stopped trusting the workflow; they are exporting to spreadsheets instead.",
                      source_type='ticket', occurred_at='2026-02-05T16:10:00Z', source_ref='ZD-8821'), cid)
    L.write(f"\n→ {s_b['status']} · subtypes={(s_b.get('evidence') or {}).get('subtypes')} · event_id=ticket's own ref")
    s_c = L.step('week 7 — a CRM activity with a declared subtype (structured path: no model call)',
                 "submit_signal(cid, aid, 'Elena Rossi accepted a role at another company; last day March 6', source_type='crm_activity', signal_type='champion_departure', occurred_at='2026-02-20T10:00:00Z', participants=[{'name':'Elena Rossi','role':'VP Data'}], source_ref='crm:evt:7781')",
                 _sig('Elena Rossi accepted a role at another company; last day March 6', source_type='crm_activity', signal_type='champion_departure',
                      occurred_at='2026-02-20T10:00:00Z', participants=[{'name': 'Elena Rossi', 'role': 'VP Data'}], source_ref='crm:evt:7781'), cid)
    L.write(f"\n→ {s_c['status']} · basis={(s_c.get('evidence') or {}).get('basis')} · role={(s_c.get('evidence') or {}).get('role')} · urgency floor applies")
    s_d = L.step('week 10 — a meeting note carrying three signals of two polarities',
                 "submit_signal(cid, aid, <meeting note: seats idle since January, procurement wants a true-down; Tom Becker asked about the analytics module for EMEA>, source_type='meeting', occurred_at='2026-03-12T15:00:00Z')",
                 _sig(MEETING, source_type='meeting', occurred_at='2026-03-12T15:00:00Z'), cid)
    L.write(f"\n→ {s_d['status']} · subtypes={(s_d.get('evidence') or {}).get('subtypes')} · basis={(s_d.get('evidence') or {}).get('basis')}")

    L.step('week 12 — a typed-signals export (the CSM flagged the renewal in Gainsight) takes the structured lane through the SAME engine',
           "upload_csv(cid, 'enhanced_qualitative_signals.csv', <1 row: csm_risk_flag, 2026-03-25, source_platform=gainsight>) ; process_data(cid)",
           lambda: (upload_csv(cid, 'enhanced_qualitative_signals.csv', CRM_FLAGS), process_data(cid))[1], cid)
    L.write("\n→ the flag is evidence with provenance (origin gainsight) but its role is crm_flag: the human comparator, excluded from the leading score by rule")

    j0 = L.step('the journey from evidence alone — a leading layer with no trailing layer', "get_journey(cid, aid)", lambda: get_journey(cid, aid), cid)
    L.write(f"\n→ arc={j0['arc']['arc_type']} state={j0['state']} live_months={len(j0['live_months'])} (every month is live: no KPI has ever been scored) evidence_index={len(j0['evidence'])}")
    L.write("\n**Leading series, no trailing** (month · kpi_only · qual · label · roles):")
    for x in j0['leading_vs_trailing']['series']:
        L.write(f"- {x['month']} · {x['kpi_only']} · {x['qual']} · {x['early_warning']} · {x['roles']}")
    L.write(f"\nfirst_leading_warning_at={j0['leading_vs_trailing']['first_leading_warning_at']} · first_trailing_warning_at={j0['leading_vs_trailing']['first_trailing_warning_at']} (none: there is no trailing layer yet)")

    s_dup = L.step('the meeting note forwarded again by email — exact duplicate within the window', "submit_signal(cid, aid, <same text>, source_type='email', occurred_at='2026-03-13T09:00:00Z')",
                   _sig(MEETING, source_type='email', occurred_at='2026-03-13T09:00:00Z'), cid)
    L.write(f"\n→ {s_dup['status']} (duplicate_of={s_dup.get('duplicate_of', '')[:8]}…) — nothing written, by design")

    q = L.step('what is waiting for a human', "get_review_queue(cid)", lambda: get_review_queue(cid), cid)
    L.write(f"\n→ {q['total']} signal(s) flagged requires_review")
    with app.app_context():
        from models import ContextNode
        nodes = ContextNode.query.filter_by(account_id=aid, node_type='SIGNAL').order_by(ContextNode.node_id).all()
        target = next((n for n in nodes if (n.properties or {}).get('signal_id') == s_d['signal_id']), nodes[-1])
        target_id, target_sig = target.node_id, (target.properties or {}).get('signal_id')
    rv = L.step('a human decision — reject one extracted node (kept for audit, excluded from the journey)',
                f"review_signal(cid, '{target_sig[:8]}…', 'reject', node_id={target_id}, note='the seat comment was about a sandbox tenant', reviewer='vp-cs@demo.test')",
                lambda: review_signal(cid, target_sig, 'reject', node_id=target_id, note='the seat comment was about a sandbox tenant', reviewer='vp-cs@demo.test'), cid)
    L.write(f"\n→ nodes={rv['nodes']} audit_ids={rv['audit_ids']} journeys_rebuilt={rv['journeys_rebuilt']}")

    oc = L.step('the decision the measurement chain hangs on — an outcome, linked to the signal that preceded it',
                f"log_outcome(cid, aid, 'contraction', '2026-07-28', revenue=300000, note='Renewed at 12 fewer seats', linked_signal_ids=['{s_c['signal_id'][:8]}…'], decided_by='ae@demo.test', source_ref='SO-4410')",
                lambda: log_outcome(cid, aid, 'contraction', '2026-07-28', revenue=300000, note='Renewed at 12 fewer seats',
                                    linked_signal_ids=[s_c['signal_id']], decided_by='ae@demo.test', source_ref='SO-4410'), cid)
    L.write(f"\n→ {oc['status']} bucket={oc['bucket']} revenue={oc['revenue']} linked={oc['linked_signal_node_ids']} clamped={oc['evidence_clamped']}")

    L.step('THEN the KPI file arrives — five months at once, as it does in reality', "upload_csv(cid, 'kpi_measurements.csv', <10 rows, Jan–May 2026; one blank value>)",
           lambda: upload_csv(cid, 'kpi_measurements.csv', KPIS), cid)
    pd1 = L.step('process_data — the months are scored with provenance; the journey now carries BOTH layers', "process_data(cid)", lambda: process_data(cid), cid)
    L.write(f"\n→ run_id={pd1['run_id']} steps={pd1['steps_completed']}")

    j = L.step('the journey as the read surface serves it — evidence first, numbers confirming later', "get_journey(cid, aid)", lambda: get_journey(cid, aid), cid)
    lvt = j['leading_vs_trailing']
    L.write(f"\n→ arc={j['arc']['arc_type']} state={j['state']} phases={[p['name'] for p in j['phases']]} live_months={len(j['live_months'])} evidence_index={len(j['evidence'])}")
    L.write(f"→ **first leading warning {lvt['first_leading_warning_at']} · first trailing warning {lvt['first_trailing_warning_at']} · lead_days {lvt['lead_days']}**")
    L.write("\n**Leading vs trailing** (month · kpi_only · qual · label · roles):")
    for x in lvt['series']:
        L.write(f"- {x['month']} · {x['kpi_only']} · {x['qual']} · {x['early_warning']} · {x['roles']}")
    L.write("\n**Narrative** (every sentence cites the episode ids it was built from):")
    for ch in j['narrative']['chapters']:
        for x in ch['sentences']:
            L.write(f"- [{ch['phase']}] {x['text']}  `{x['cites']}`")
    for o in j['narrative']['omitted']:
        L.write(f"- _omitted_ ({o.get('template') or o['reason']}): {o.get('note') or o.get('cites')}")

    a = L.step('Ask AI over the contract (cited; absence of evidence is an answer)', "ask(cid, 'When did we first know Harbor Analytics was at risk, and what did the numbers say at the time?', account_id=aid)",
               lambda: ask(cid, 'When did we first know Harbor Analytics was at risk, and what did the numbers say at the time?', account_id=aid), cid)
    L.write(f"\n→ model={a.get('model')} sentences={len(a.get('sentences', []))} unsupported={len(a.get('unsupported', []))} gaps={len(a.get('evidence_gaps', []))}")
    for x in a.get('sentences', [])[:6]:
        L.write(f"- {x['text']}  `{x['cites']}`")

    L.step('portfolio row (what a CRO sees first)', "list_journeys(cid)", lambda: list_journeys(cid), cid)
    L.write("\n## What Hindsight needs from here\n")
    L.write("Wizard B (`trigger_wizard(cid, 'b')`) needs ≥5 journeys with outcomes to derive pattern profiles and run the lead-time backtest; "
            "on a one-account trace it reports 'insufficient'. On the seeded demo tenants it runs inside process_data (WizardRun rows). "
            "The intervention step is the gap in this trace: today an intervention is only a signal with role 'intervention'; "
            "`playbook-governance-layer.md` adds the record (proposed → approved → sent → closed) and its measurement.")

    L.step('the audit trail of everything above', "GET /api/audit?customer_id=cid  (in-process calls are recorded as key_kind=local)", lambda: None, cid)
    out.close()
    print(f"\nwritten: {args.out}  (customer_id={cid}, account_id={aid})")


if __name__ == '__main__':
    main()
