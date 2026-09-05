*Generated on CustomerIntelV1 by `backend/scripts/lifecycle_trace.py` (sha 9fda126, real extractor claude-sonnet-5, Ask AI claude-opus-5). Re-run any time: `python scripts/lifecycle_trace.py --out trace.md` inside the container. The tenant it creates is synthetic (data_origin='synthetic_demo').*

# Lifecycle trace — Lifecycle Trace (saas_premium) — 2026-09-05T03:01:23.644815Z
Every step shows the tool call, then the rows it wrote (table, id, the fields that matter). Tenant is synthetic (data_origin='synthetic_demo'); the extractor is the real model when ANTHROPIC_API_KEY is set, else the keyword stub.

## Step 1 — create the tenant

```
create_customer(name='Lifecycle Trace 0eab78', domain='trace-0eab78.demo', vertical='saas_premium', …)  # + data_origin='synthetic_demo'
```

**customers** +1
- customer_id=6 · Lifecycle Trace 0eab78 · vertical via CustomerConfig · data_origin=synthetic_demo

**customer_configs** +1
- config_id=6 · customer 6 vertical=saas_premium

**customer_api_keys** +1
- id=6 · prefix=csp_write_YF scopes=['read', 'write']

**tool_audit_log** +1
- id=8 · mcp/create_customer cust=None key=local allowed 

→ customer_id=6

## Step 2 — upload the roster (accounts CSV)

```
upload_csv(cid, 'account_details.csv', <1 account: Harbor Analytics, champion Elena Rossi, renewal 2026-12-01>)
```

**csv_uploads** +1
- id=1 · account_details.csv sha=0354956769… rows=1 bytes=364 key=local consumed_by_run=None

**csv_upload_staging** +1
- staging_id=20 · account_details.csv upload_id=1

**tool_audit_log** +1
- id=9 · mcp/upload_csv cust=6 key=local allowed 

## Step 3 — upload KPI measurements (3 months; one blank value)

```
upload_csv(cid, 'kpi_measurements.csv', <8 rows, Jan–Mar 2026>)
```

**csv_uploads** +1
- id=2 · kpi_measurements.csv sha=f67dd2ae72… rows=8 bytes=594 key=local consumed_by_run=None

**csv_upload_staging** +1
- staging_id=21 · kpi_measurements.csv upload_id=2

**tool_audit_log** +1
- id=10 · mcp/upload_csv cust=6 key=local allowed 

## Step 4 — process_data — ingest, health with provenance, journey, run record

```
process_data(cid)
```

**process_runs** +1
- id=2 · pd_696378a4cd1f4f56 success mode=auto uploads=[1, 2] counts={'kpi_rows_skipped_blank': 1, 'accounts': 1, 'kpi_measurements': 7, 'changed_accounts': 1} steps=['accounts_loaded_1_created_0_updated', 'stakeholders_extracted_from_account_details(4)', 'kpis_loaded_7_blank_skipped_1', 'context_graph_loaded', 'staging_consumed', 'health_scores_auto_3_written', 'wizard_a_1_journeys_0_classified_0_steady_1_unclassified']

**accounts** +1
- account_id=53 · Harbor Analytics arr=1800000.00 status=at_risk arc=None

**kpi_measurements** +7
- kpi_id=63569 · P1-KPI1 2026-01-01 value=62.00 upload_id=2
- kpi_id=63570 · P1-KPI2 2026-01-01 value=55.00 upload_id=2
- kpi_id=63571 · P2-KPI1 2026-01-01 value=70.00 upload_id=2
- kpi_id=63572 · P1-KPI1 2026-02-01 value=58.00 upload_id=2
- kpi_id=63573 · P2-KPI1 2026-02-01 value=64.00 upload_id=2
- kpi_id=63574 · P1-KPI1 2026-03-01 value=49.00 upload_id=2
- kpi_id=63575 · P2-KPI1 2026-03-01 value=55.00 upload_id=2

**health_scores** +3
- health_score_id=595 · 2026-01-01 score=72.06 status=healthy weights={'P1': 0.25, 'P2': 0.25} source=customer_config codes_used=['P1-KPI1', 'P1-KPI2', 'P2-KPI1'] dropped=[] catalog=3.1 taxonomy=0.1 scorer=2.0 input_upload=2 run=2 qual=None early_warning=None
- health_score_id=596 · 2026-02-01 score=67.20 status=at_risk weights={'P1': 0.25, 'P2': 0.25} source=customer_config codes_used=['P1-KPI1', 'P2-KPI1'] dropped=[] catalog=3.1 taxonomy=0.1 scorer=2.0 input_upload=2 run=2 qual=None early_warning=None
- health_score_id=597 · 2026-03-01 score=60.60 status=at_risk weights={'P1': 0.25, 'P2': 0.25} source=customer_config codes_used=['P1-KPI1', 'P2-KPI1'] dropped=[] catalog=3.1 taxonomy=0.1 scorer=2.0 input_upload=2 run=2 qual=None early_warning=None

**context_nodes** +4
- node_id=506 · STAKEHOLDER/champion source=observed platform=account_details_extraction event_id=None role=champion basis=None urgency=None person=None review=None rev=None title='Elena Rossi (VP Data)'
- node_id=507 · STAKEHOLDER/executive_sponsor source=observed platform=account_details_extraction event_id=None role=executive_sponsor basis=None urgency=None person=None review=None rev=None title='Tom Becker (Executive Sponsor)'
- node_id=508 · STAKEHOLDER/csm source=observed platform=account_details_extraction event_id=None role=csm basis=None urgency=None person=None review=None rev=None title='Maya Johnson (Csm)'
- node_id=509 · STAKEHOLDER/cs_manager source=observed platform=account_details_extraction event_id=None role=cs_manager basis=None urgency=None person=None review=None rev=None title='Sam Rivera (Cs Manager)'

**journey_data** +1
- id=53 · account 53 state=unclassified arc=None gen=3.2 episodes=2 live_months=0 narrative_sentences=1

**tool_audit_log** +1
- id=11 · mcp/process_data cust=6 key=local allowed 

→ run_id=pd_696378a4cd1f4f56 status=success steps=['accounts_loaded_1_created_0_updated', 'stakeholders_extracted_from_account_details(4)', 'kpis_loaded_7_blank_skipped_1', 'context_graph_loaded', 'staging_consumed', 'health_scores_auto_3_written', 'wizard_a_1_journeys_0_classified_0_steady_1_unclassified']

## Step 5 — a structured signal — declared taxonomy subtype, no model call

```
submit_signal(cid, aid, 'Elena Rossi accepted a role at another company; last day March 27', source_type='crm_activity', signal_type='champion_departure', occurred_at='2026-03-20T10:00:00Z', participants=[{'name':'Elena Rossi','role':'VP Data'}], source_ref='crm:evt:7781')
```

**qualitative_signals** +1
- id=254 · 4f0402d2… source=crm_activity type=champion_departure occurred=2026-03-20 10:00:00 node=510 review=False urgency=critical model=None extractions=0

**context_nodes** +1
- node_id=510 · SIGNAL/champion_departure source=observed platform=crm_activity event_id=crm:evt:7781 role=champion_change basis=declared_subtype urgency=critical person=Elena Rossi review=None rev=None title='Elena Rossi accepted a role at another company; last day Mar'

**tool_audit_log** +1
- id=12 · mcp/submit_signal cust=6 key=local allowed 

→ queued · evidence={'signal_id': '4f0402d2-5ca1-46c4-8f60-fcfec1ee135f', 'account_id': 53, 'node_id': 510, 'node_ids': [510], 'subtypes': ['champion_departure'], 'subtype': 'champion_departure', 'role': 'champion_change', 'basis': 'declared_subtype', 'polarity_conflict': False, 'person': 'Elena Rossi', 'person_unresolved': False}

## Step 6 — a free-text signal — the extractor types it against the tenant vocabulary

```
submit_signal(cid, aid, <meeting note: half the seats idle since March, procurement wants a true-down; Tom Becker asked about the analytics module for EMEA>, source_type='meeting', occurred_at='2026-04-03T15:00:00Z')
```

**qualitative_signals** +1
- id=255 · 1c0e5ecb… source=meeting type=meeting occurred=2026-04-03 15:00:00 node=511 review=False urgency=high model=claude-sonnet-5 extractions=3

**context_nodes** +3
- node_id=511 · SIGNAL/seat_underutilization source=observed platform=meeting event_id=1c0e5ecb-b8df-4a57-b30a-c87266f12c1c role=usage_decline basis=llm_extraction urgency=high person=None review=None rev=None title="Half the seats haven't logged in since March"
- node_id=512 · SIGNAL/seat_reduction_request source=observed platform=meeting event_id=1c0e5ecb-b8df-4a57-b30a-c87266f12c1c role=commercial_pressure basis=llm_extraction urgency=high person=None review=None rev=None title='procurement wants to true-down at renewal'
- node_id=513 · SIGNAL/module_upsell_interest source=observed platform=meeting event_id=1c0e5ecb-b8df-4a57-b30a-c87266f12c1c role=expansion_intent basis=llm_extraction urgency=high person=Tom Becker review=None rev=None title='Tom Becker asked what adding the analytics module for the EM'

**tool_audit_log** +1
- id=13 · mcp/submit_signal cust=6 key=local allowed 

→ queued · subtypes=['seat_underutilization', 'seat_reduction_request', 'module_upsell_interest'] · basis=llm_extraction

## Step 7 — the same note again — exact duplicate within the window

```
submit_signal(cid, aid, <same text>, source_type='email', occurred_at='2026-04-04T09:00:00Z')
```

**tool_audit_log** +1
- id=14 · mcp/submit_signal cust=6 key=local allowed 

→ duplicate (duplicate_of=1c0e5ecb…) — nothing written, by design

## Step 8 — what is waiting for a human

```
get_review_queue(cid)
```

**tool_audit_log** +1
- id=15 · mcp/get_review_queue cust=6 key=local allowed 

→ 0 signal(s) flagged requires_review

## Step 9 — a human decision — reject one extracted node (kept for audit, excluded from the journey)

```
review_signal(cid, '1c0e5ecb…', 'reject', node_id=511, note='the seat comment was about a sandbox tenant', reviewer='vp-cs@demo.test')
```

**signal_reviews** +1
- id=12 · reject node=511 signal=1c0e5ecb… by=vp-cs@demo.test was_flagged=False note='the seat comment was about a sandbox tenant'

**tool_audit_log** +1
- id=16 · mcp/review_signal cust=6 key=local allowed 

→ nodes=[{'node_id': 511, 'subtype': 'seat_underutilization', 'role': 'usage_decline', 'effective_urgency': 'high', 'review': 'rejected'}] audit_ids=[12] journeys_rebuilt=1

## Step 10 — the decision the measurement chain hangs on — an outcome, linked to the signal that preceded it

```
log_outcome(cid, aid, 'contraction', '2026-06-15', revenue=300000, note='Renewed at 12 fewer seats', linked_signal_ids=['4f0402d2…'], decided_by='ae@demo.test', source_ref='SO-4410')
```

**context_nodes** +1
- node_id=514 · OUTCOME/contraction source=observed platform=manual event_id=outcome:970d36fa2555 role=None basis=None urgency=None person=None review=None rev=-300000.00 title='Contraction — Harbor Analytics'

**context_edges** +1
- edge_id=39 · 510 -LED_TO-> 514 by=log_outcome

**tool_audit_log** +1
- id=17 · mcp/log_outcome cust=6 key=local allowed 

→ logged bucket=lost revenue=-300000.0 linked=[510] clamped=False

## Step 11 — the journey as the read surface serves it (with the evidence index and the narrative)

```
get_journey(cid, aid)
```

**tool_audit_log** +1
- id=18 · mcp/get_journey cust=6 key=local allowed 

→ arc=exec_sponsor_change state=classified phases=['baseline', 'deterioration'] live_months=1 evidence_index=4 open_reviews=0

**Narrative** (every sentence cites the episode ids it was built from):
- [baseline] The leading layer first flagged early_warning in March 2026; the KPI score never fell into the critical band, which is the trailing layer's own warning line.  `['sig:510']`
- [deterioration] Deterioration began in February 2026 with health moved from healthy to at_risk (67.2).  `['hs:596']`
- [deterioration] In March 2026, Elena Rossi accepted a role at another company; last day March 27.  `['sig:510']`
- [live] No KPI upload has arrived since March 2026; what follows is live evidence only.  `['sig:512']`
- [live] On 3 April 2026 a meeting note recorded procurement wants to true-down at renewal, and Tom Becker asked what adding the analytics module for the EMEA team would cost.  `['sig:512', 'sig:513']`
- [live] On 15 June 2026, contraction ($300,000 lost).  `['out:514']`
- [live] The arc hypothesis is exec_sponsor_change (confidence 0.85, rule match constant), supported by 1 cited episode.  `['sig:510']`
- _omitted_ (phase_open_with_trigger): phase 'baseline' from 2026-01-01 has no trigger episode (no evidence before it)
- _omitted_ (rejected_evidence): seat_underutilization rejected by vp-cs@demo.test on 2026-09-05: the seat comment was about a sandbox tenant

**Leading vs trailing** (month · kpi_only · qual · label · roles):
- 2026-01-01 · 72.06 · None · None · {}
- 2026-02-01 · 67.2 · None · None · {}
- 2026-03-01 · 60.6 · 20.0 · early_warning · {'champion_change': 1}
- 2026-04-01 · None · 42.02 · leading_only · {'champion_change': 1, 'commercial_pressure': 1, 'expansion_intent': 1}

## Step 12 — Ask AI over the contract (cited; absence of evidence is an answer)

```
ask(cid, 'Why is Harbor Analytics at risk and what did we decide?', account_id=aid)
```

**tool_audit_log** +1
- id=19 · mcp/ask cust=6 key=local allowed 

→ model=claude-opus-5 sentences=8 unsupported=0 gaps=6
- Harbor Analytics entered deterioration in February 2026 when health moved from healthy to at_risk (67.2), a system-derived health transition with no evidence episode attached.  `['hs:596', 'row:53']`
- The trigger for the risk narrative is observed evidence: on 2026-03-20 a CRM activity recorded that Elena Rossi (champion, VP Data) accepted a role at another company, last day March 27 — evidence_tier observed, requires_review false, effective_urgency critical.  `['sig:510', '510']`
- On 2026-04-03 a meeting note recorded that procurement wants to true-down at renewal (role commercial_pressure, confidence 1, observed, requires_review false), which is the commercial side of the risk.  `['sig:512', '512']`
- The same meeting also carried a positive counterweight: Tom Becker, executive sponsor, asked what adding the analytics module for the EMEA team would cost (role expansion_intent, confidence 1, observed).  `['sig:513', '513']`
- The system's arc hypothesis is exec_sponsor_change at confidence 0.85, whose confidence semantics are rule_match_constant (not a learned probability), supported by sig:510 and explicitly contradicted by the positive expansion_intent role.  `['sig:510', 'row:53']`
- The leading layer first flagged early_warning in March 2026 with qual 20.0 against kpi_only 60.6 (divergence -40.6), and April 2026 is leading_only — qual 42.02 with no KPI upload since March 2026, so the trailing layer never confirmed and lead_days is null.  `['sig:512', 'sig:510', 'row:53']`

## Step 13 — portfolio row (what a CRO sees first)

```
list_journeys(cid)
```

**tool_audit_log** +1
- id=20 · mcp/list_journeys cust=6 key=local allowed 

## What Hindsight needs from here

Wizard B (`trigger_wizard(cid, 'b')`) needs ≥5 journeys with outcomes to derive pattern profiles and run the lead-time backtest; on a one-account trace it reports 'insufficient'. On the seeded demo tenants it runs inside process_data (WizardRun rows). The intervention step (playbook governance layer) is the gap in this trace: today an intervention is only a signal with role 'intervention'; the design doc adds the record (requested → approved → dispatched → receipt) and its measurement.

## Step 14 — the audit trail of everything above

```
GET /api/audit?customer_id=cid  (in-process calls are recorded as key_kind=local)
```

_(no new rows — read-only step)_
