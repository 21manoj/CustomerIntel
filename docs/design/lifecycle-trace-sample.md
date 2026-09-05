*Generated on CustomerIntelV1 by `backend/scripts/lifecycle_trace.py` (real extractor claude-sonnet-5, Ask AI claude-opus-5). Signals-first order: roster → communications through the engine → typed export lane → journey from evidence alone → review → outcome → THEN the KPI file. Re-run any time inside the container. The tenant it creates is synthetic (data_origin='synthetic_demo').*

# Lifecycle trace — Lifecycle Trace (saas_premium) — 2026-09-05T03:35:48.767678Z
Every step shows the tool call, then the rows it wrote (table, id, the fields that matter). Tenant is synthetic (data_origin='synthetic_demo'); the extractor is the real model when ANTHROPIC_API_KEY is set, else the keyword stub.

## Step 1 — create the tenant

```
create_customer(name='Lifecycle Trace f98bf6', domain='trace-f98bf6.demo', vertical='saas_premium', …)  # + data_origin='synthetic_demo'
```

**customers** +1
- customer_id=7 · Lifecycle Trace f98bf6 · vertical via CustomerConfig · data_origin=synthetic_demo

**customer_configs** +1
- config_id=7 · customer 7 vertical=saas_premium

**customer_api_keys** +1
- id=7 · prefix=csp_write_8q scopes=['read', 'write']

**tool_audit_log** +1
- id=21 · mcp/create_customer cust=None key=local allowed 

→ customer_id=7

## Step 2 — the roster only — accounts CSV, no KPIs (signals-first: evidence comes before numbers)

```
upload_csv(cid, 'account_details.csv', <1 account: Harbor Analytics, champion Elena Rossi, sponsor Tom Becker, renewal 2026-08-01>)
```

**csv_uploads** +1
- id=3 · account_details.csv sha=1ca29f8394… rows=1 bytes=364 key=local consumed_by_run=None

**csv_upload_staging** +1
- staging_id=22 · account_details.csv upload_id=3

**tool_audit_log** +1
- id=22 · mcp/upload_csv cust=7 key=local allowed 

## Step 3 — process_data on the roster — nothing to score yet; the run is recorded anyway

```
process_data(cid)
```

**process_runs** +1
- id=3 · pd_e75bd4b2bf514bb3 success mode=auto uploads=[3] counts={'accounts': 1, 'kpi_measurements': 0, 'changed_accounts': 0} steps=['accounts_loaded_1_created_0_updated', 'stakeholders_extracted_from_account_details(4)', 'context_graph_loaded', 'staging_consumed', 'health_scores_auto_0_written', 'wizard_a_1_journeys_0_classified_0_steady_1_unclassified']

**accounts** +1
- account_id=54 · Harbor Analytics arr=1800000.00 status=active arc=None

**context_nodes** +4
- node_id=515 · STAKEHOLDER/champion source=observed platform=account_details_extraction event_id=None role=champion basis=None urgency=None person=None review=None rev=None title='Elena Rossi (VP Data)'
- node_id=516 · STAKEHOLDER/executive_sponsor source=observed platform=account_details_extraction event_id=None role=executive_sponsor basis=None urgency=None person=None review=None rev=None title='Tom Becker (Executive Sponsor)'
- node_id=517 · STAKEHOLDER/csm source=observed platform=account_details_extraction event_id=None role=csm basis=None urgency=None person=None review=None rev=None title='Maya Johnson (Csm)'
- node_id=518 · STAKEHOLDER/cs_manager source=observed platform=account_details_extraction event_id=None role=cs_manager basis=None urgency=None person=None review=None rev=None title='Sam Rivera (Cs Manager)'

**journey_data** +1
- id=54 · account 54 state=unclassified arc=None gen=3.2 episodes=1 live_months=0 narrative_sentences=0

**tool_audit_log** +1
- id=23 · mcp/process_data cust=7 key=local allowed 

→ run_id=pd_e75bd4b2bf514bb3 steps=['accounts_loaded_1_created_0_updated', 'stakeholders_extracted_from_account_details(4)', 'context_graph_loaded', 'staging_consumed', 'health_scores_auto_0_written', 'wizard_a_1_journeys_0_classified_0_steady_1_unclassified']

## Step 4 — week 2 — an email arrives (free text; the extractor types it against the tenant vocabulary)

```
submit_signal(cid, aid, 'Elena skipped the QBR again and has not replied to two follow-ups…', source_type='email', occurred_at='2026-01-14T09:30:00Z', participants=[{'name':'Elena Rossi'}])
```

**qualitative_signals** +1
- id=256 · b5f7c26c… source=email type=email occurred=2026-01-14 09:30:00 node=519 review=True urgency=critical model=claude-sonnet-5 extractions=3

**context_nodes** +3
- node_id=519 · SIGNAL/qbr_no_show source=observed platform=email event_id=b5f7c26c-9f5e-4cd7-992b-8284f8297319 role=engagement_decline basis=llm_extraction urgency=medium person=Elena Rossi review=None rev=None title='Elena skipped the QBR again this quarter'
- node_id=520 · SIGNAL/champion_disengagement source=observed platform=email event_id=b5f7c26c-9f5e-4cd7-992b-8284f8297319 role=champion_change basis=llm_extraction urgency=critical person=Elena Rossi review=None rev=None title="hasn't replied to two follow-ups from Maya; her team lead sa"
- node_id=521 · SIGNAL/competitor_evaluation source=observed platform=email event_id=b5f7c26c-9f5e-4cd7-992b-8284f8297319 role=commercial_pressure basis=llm_extraction urgency=high person=Elena Rossi review=None rev=None title='heads down on a platform review'

**tool_audit_log** +1
- id=24 · mcp/submit_signal cust=7 key=local allowed 

→ queued · subtypes=['qbr_no_show', 'champion_disengagement', 'competitor_evaluation'] · person=Elena Rossi

## Step 5 — week 5 — a support ticket

```
submit_signal(cid, aid, 'The Salesforce sync keeps dropping records…', source_type='ticket', occurred_at='2026-02-05T16:10:00Z', source_ref='ZD-8821')
```

**qualitative_signals** +1
- id=257 · 16215aba… source=ticket type=ticket occurred=2026-02-05 16:10:00 node=522 review=False urgency=high model=claude-sonnet-5 extractions=2

**context_nodes** +2
- node_id=522 · SIGNAL/integration_bug source=observed platform=ticket event_id=ZD-8821 role=product_friction basis=llm_extraction urgency=high person=None review=None rev=None title='the Salesforce sync keeps dropping records'
- node_id=523 · SIGNAL/workflow_friction source=observed platform=ticket event_id=ZD-8821 role=product_friction basis=llm_extraction urgency=high person=None review=None rev=None title='the ops team has stopped trusting the workflow; they are exp'

**tool_audit_log** +1
- id=25 · mcp/submit_signal cust=7 key=local allowed 

→ queued · subtypes=['integration_bug', 'workflow_friction'] · event_id=ticket's own ref

## Step 6 — week 7 — a CRM activity with a declared subtype (structured path: no model call)

```
submit_signal(cid, aid, 'Elena Rossi accepted a role at another company; last day March 6', source_type='crm_activity', signal_type='champion_departure', occurred_at='2026-02-20T10:00:00Z', participants=[{'name':'Elena Rossi','role':'VP Data'}], source_ref='crm:evt:7781')
```

**qualitative_signals** +1
- id=258 · 4b7a61e9… source=crm_activity type=champion_departure occurred=2026-02-20 10:00:00 node=524 review=False urgency=critical model=None extractions=0

**context_nodes** +1
- node_id=524 · SIGNAL/champion_departure source=observed platform=crm_activity event_id=crm:evt:7781 role=champion_change basis=declared_subtype urgency=critical person=Elena Rossi review=None rev=None title='Elena Rossi accepted a role at another company; last day Mar'

**tool_audit_log** +1
- id=26 · mcp/submit_signal cust=7 key=local allowed 

→ queued · basis=declared_subtype · role=champion_change · urgency floor applies

## Step 7 — week 10 — a meeting note carrying three signals of two polarities

```
submit_signal(cid, aid, <meeting note: seats idle since January, procurement wants a true-down; Tom Becker asked about the analytics module for EMEA>, source_type='meeting', occurred_at='2026-03-12T15:00:00Z')
```

**qualitative_signals** +1
- id=259 · dcd6c43d… source=meeting type=meeting occurred=2026-03-12 15:00:00 node=525 review=False urgency=high model=claude-sonnet-5 extractions=3

**context_nodes** +3
- node_id=525 · SIGNAL/seat_underutilization source=observed platform=meeting event_id=dcd6c43d-723d-4b61-9b0b-371f45e687a1 role=usage_decline basis=llm_extraction urgency=high person=None review=None rev=None title="Half the seats haven't logged in since January"
- node_id=526 · SIGNAL/seat_reduction_request source=observed platform=meeting event_id=dcd6c43d-723d-4b61-9b0b-371f45e687a1 role=commercial_pressure basis=llm_extraction urgency=high person=None review=None rev=None title='procurement wants to true-down at renewal'
- node_id=527 · SIGNAL/module_upsell_interest source=observed platform=meeting event_id=dcd6c43d-723d-4b61-9b0b-371f45e687a1 role=expansion_intent basis=llm_extraction urgency=high person=Tom Becker review=None rev=None title='Tom Becker asked what adding the analytics module for the EM'

**tool_audit_log** +1
- id=27 · mcp/submit_signal cust=7 key=local allowed 

→ queued · subtypes=['seat_underutilization', 'seat_reduction_request', 'module_upsell_interest'] · basis=llm_extraction

## Step 8 — week 12 — a typed-signals export (the CSM flagged the renewal in Gainsight) takes the structured lane through the SAME engine

```
upload_csv(cid, 'enhanced_qualitative_signals.csv', <1 row: csm_risk_flag, 2026-03-25, source_platform=gainsight>) ; process_data(cid)
```

**csv_uploads** +1
- id=4 · enhanced_qualitative_signals.csv sha=ac570e37e2… rows=1 bytes=280 key=local consumed_by_run=4

**process_runs** +1
- id=4 · pd_49ea55e0e6c24ccf success mode=auto uploads=[4] counts={'accounts': 1, 'kpi_measurements': 0, 'changed_accounts': 0} steps=['signals_queued_1_skipped_0', 'signals_materialized_1_nodes_1_structured_1_extracted_0_unclassified_0', 'context_graph_loaded', 'staging_consumed', 'health_scores_auto_0_written', 'wizard_a_1_journeys_1_classified_0_steady_0_unclassified']

**qualitative_signals** +1
- id=260 · c7_gs_fl… source=csv_import type=csm_risk_flag occurred=2026-03-25 00:00:00 node=528 review=False urgency=low model=None extractions=0

**context_nodes** +1
- node_id=528 · SIGNAL/csm_risk_flag source=observed platform=csv_import event_id=gs_flag_1 role=crm_flag basis=declared_subtype urgency=low person=Maya Johnson review=None rev=None title='CSM marked renewal at risk in Gainsight'

**tool_audit_log** +2
- id=28 · mcp/upload_csv cust=7 key=local allowed 
- id=29 · mcp/process_data cust=7 key=local allowed 

→ the flag is evidence with provenance (origin gainsight) but its role is crm_flag: the human comparator, excluded from the leading score by rule

## Step 9 — the journey from evidence alone — a leading layer with no trailing layer

```
get_journey(cid, aid)
```

**tool_audit_log** +1
- id=30 · mcp/get_journey cust=7 key=local allowed 

→ arc=exec_sponsor_change state=classified live_months=3 (every month is live: no KPI has ever been scored) evidence_index=10

**Leading series, no trailing** (month · kpi_only · qual · label · roles):
- 2026-01-01 · None · 33.33 · leading_only · {'engagement_decline': 1, 'champion_change': 1, 'commercial_pressure': 1}
- 2026-02-01 · None · 21.64 · leading_only · {'engagement_decline': 1, 'champion_change': 2, 'commercial_pressure': 1, 'product_friction': 2}
- 2026-03-01 · None · 33.87 · leading_only · {'product_friction': 2, 'champion_change': 1, 'usage_decline': 1, 'commercial_pressure': 1, 'expansion_intent': 1}

first_leading_warning_at=2026-01-01 · first_trailing_warning_at=None (none: there is no trailing layer yet)

## Step 10 — the meeting note forwarded again by email — exact duplicate within the window

```
submit_signal(cid, aid, <same text>, source_type='email', occurred_at='2026-03-13T09:00:00Z')
```

**tool_audit_log** +1
- id=31 · mcp/submit_signal cust=7 key=local allowed 

→ duplicate (duplicate_of=dcd6c43d…) — nothing written, by design

## Step 11 — what is waiting for a human

```
get_review_queue(cid)
```

**tool_audit_log** +1
- id=32 · mcp/get_review_queue cust=7 key=local allowed 

→ 1 signal(s) flagged requires_review

## Step 12 — a human decision — reject one extracted node (kept for audit, excluded from the journey)

```
review_signal(cid, 'dcd6c43d…', 'reject', node_id=525, note='the seat comment was about a sandbox tenant', reviewer='vp-cs@demo.test')
```

**signal_reviews** +1
- id=13 · reject node=525 signal=dcd6c43d… by=vp-cs@demo.test was_flagged=False note='the seat comment was about a sandbox tenant'

**tool_audit_log** +1
- id=33 · mcp/review_signal cust=7 key=local allowed 

→ nodes=[{'node_id': 525, 'subtype': 'seat_underutilization', 'role': 'usage_decline', 'effective_urgency': 'high', 'review': 'rejected'}] audit_ids=[13] journeys_rebuilt=1

## Step 13 — the decision the measurement chain hangs on — an outcome, linked to the signal that preceded it

```
log_outcome(cid, aid, 'contraction', '2026-07-28', revenue=300000, note='Renewed at 12 fewer seats', linked_signal_ids=['4b7a61e9…'], decided_by='ae@demo.test', source_ref='SO-4410')
```

**context_nodes** +1
- node_id=529 · OUTCOME/contraction source=observed platform=manual event_id=outcome:0923f530c7d5 role=None basis=None urgency=None person=None review=None rev=-300000.00 title='Contraction — Harbor Analytics'

**context_edges** +1
- edge_id=40 · 524 -LED_TO-> 529 by=log_outcome

**tool_audit_log** +1
- id=34 · mcp/log_outcome cust=7 key=local allowed 

→ logged bucket=lost revenue=-300000.0 linked=[524] clamped=False

## Step 14 — THEN the KPI file arrives — five months at once, as it does in reality

```
upload_csv(cid, 'kpi_measurements.csv', <10 rows, Jan–May 2026; one blank value>)
```

**csv_uploads** +1
- id=5 · kpi_measurements.csv sha=8b2ec15ce0… rows=10 bytes=693 key=local consumed_by_run=None

**csv_upload_staging** +1
- staging_id=24 · kpi_measurements.csv upload_id=5

**tool_audit_log** +1
- id=35 · mcp/upload_csv cust=7 key=local allowed 

## Step 15 — process_data — the months are scored with provenance; the journey now carries BOTH layers

```
process_data(cid)
```

**process_runs** +1
- id=5 · pd_6917e6695dd44a0a success mode=auto uploads=[5] counts={'kpi_rows_skipped_blank': 1, 'accounts': 1, 'kpi_measurements': 9, 'changed_accounts': 1} steps=['kpis_loaded_9_blank_skipped_1', 'context_graph_loaded', 'staging_consumed', 'health_scores_auto_5_written', 'wizard_a_1_journeys_1_classified_0_steady_0_unclassified']

**kpi_measurements** +9
- kpi_id=63576 · P1-KPI1 2026-01-01 value=74.00 upload_id=5
- kpi_id=63577 · P2-KPI1 2026-01-01 value=78.00 upload_id=5
- kpi_id=63578 · P1-KPI1 2026-02-01 value=71.00 upload_id=5
- kpi_id=63579 · P2-KPI1 2026-02-01 value=72.00 upload_id=5
- kpi_id=63580 · P1-KPI1 2026-03-01 value=64.00 upload_id=5
- kpi_id=63581 · P1-KPI1 2026-04-01 value=48.00 upload_id=5
- kpi_id=63582 · P2-KPI1 2026-04-01 value=50.00 upload_id=5
- kpi_id=63583 · P1-KPI1 2026-05-01 value=30.00 upload_id=5
- kpi_id=63584 · P2-KPI1 2026-05-01 value=30.00 upload_id=5

**health_scores** +5
- health_score_id=598 · 2026-01-01 score=80.00 status=healthy weights={'P1': 0.25, 'P2': 0.25} source=customer_config codes_used=['P1-KPI1', 'P2-KPI1'] dropped=[] catalog=3.1 taxonomy=0.1 scorer=2.0 input_upload=5 run=5 qual=33.33 early_warning=early_warning
- health_score_id=599 · 2026-02-01 score=75.71 status=healthy weights={'P1': 0.25, 'P2': 0.25} source=customer_config codes_used=['P1-KPI1', 'P2-KPI1'] dropped=[] catalog=3.1 taxonomy=0.1 scorer=2.0 input_upload=5 run=5 qual=21.64 early_warning=early_warning
- health_score_id=600 · 2026-03-01 score=73.43 status=healthy weights={'P1': 0.25} source=customer_config codes_used=['P1-KPI1'] dropped=[] catalog=3.1 taxonomy=0.1 scorer=2.0 input_upload=5 run=5 qual=36.38 early_warning=early_warning
- health_score_id=601 · 2026-04-01 score=58.53 status=at_risk weights={'P1': 0.25, 'P2': 0.25} source=customer_config codes_used=['P1-KPI1', 'P2-KPI1'] dropped=[] catalog=3.1 taxonomy=0.1 scorer=2.0 input_upload=5 run=5 qual=50.00 early_warning=aligned
- health_score_id=602 · 2026-05-01 score=40.18 status=critical weights={'P1': 0.25, 'P2': 0.25} source=customer_config codes_used=['P1-KPI1', 'P2-KPI1'] dropped=[] catalog=3.1 taxonomy=0.1 scorer=2.0 input_upload=5 run=5 qual=None early_warning=None

**tool_audit_log** +1
- id=36 · mcp/process_data cust=7 key=local allowed 

→ run_id=pd_6917e6695dd44a0a steps=['kpis_loaded_9_blank_skipped_1', 'context_graph_loaded', 'staging_consumed', 'health_scores_auto_5_written', 'wizard_a_1_journeys_1_classified_0_steady_0_unclassified']

## Step 16 — the journey as the read surface serves it — evidence first, numbers confirming later

```
get_journey(cid, aid)
```

**tool_audit_log** +1
- id=37 · mcp/get_journey cust=7 key=local allowed 

→ arc=exec_sponsor_change state=classified phases=['baseline', 'deterioration', 'intervention'] live_months=0 evidence_index=10
→ **first leading warning 2026-01-01 · first trailing warning 2026-05-01 · lead_days 120**

**Leading vs trailing** (month · kpi_only · qual · label · roles):
- 2026-01-01 · 80.0 · 33.33 · early_warning · {'engagement_decline': 1, 'champion_change': 1, 'commercial_pressure': 1}
- 2026-02-01 · 75.71 · 21.64 · early_warning · {'engagement_decline': 1, 'champion_change': 2, 'commercial_pressure': 1, 'product_friction': 2}
- 2026-03-01 · 73.43 · 36.38 · early_warning · {'product_friction': 2, 'champion_change': 1, 'commercial_pressure': 1, 'expansion_intent': 1}
- 2026-04-01 · 58.53 · 50.0 · aligned · {'commercial_pressure': 1, 'expansion_intent': 1}
- 2026-05-01 · 40.18 · None · None · {}

**Narrative** (every sentence cites the episode ids it was built from):
- [baseline] From January 2026 Harbor Analytics was in baseline on the numbers (80.0) while the evidence already showed commercial pressure: heads down on a platform review, raised by Elena Rossi (champion).  `['sig:521']`
- [baseline] The leading layer first flagged early_warning in January 2026; the KPI score crossed at-risk in May 2026, 120 days later.  `['sig:519', 'sig:520', 'sig:521']`
- [baseline] In February 2026, the Salesforce sync keeps dropping records, the ops team has stopped trusting the workflow; they are exporting to spreadsheets instead, and Elena Rossi accepted a role at another company; last day March 6.  `['sig:522', 'sig:523', 'sig:524']`
- [deterioration] Deterioration began in March 2026 with CSM marked renewal at risk in Gainsight, raised by Maya Johnson (csm).  `['sig:528']`
- [deterioration] In March 2026, procurement wants to true-down at renewal, and Tom Becker asked what adding the analytics module for the EMEA team would cost.  `['sig:526', 'sig:527']`
- [deterioration] In April 2026, health moved from healthy to at_risk (58.53).  `['hs:601']`
- [intervention] Intervention began in May 2026 with health moved from at_risk to critical (40.18).  `['hs:602']`
- [intervention] In July 2026, contraction ($300,000 lost).  `['out:529']`
- [intervention] The arc hypothesis is exec_sponsor_change (confidence 0.85, rule match constant), supported by 2 cited episodes.  `['sig:520', 'sig:524']`
- _omitted_ (rejected_evidence): seat_underutilization rejected by vp-cs@demo.test on 2026-09-05: the seat comment was about a sandbox tenant

## Step 17 — Ask AI over the contract (cited; absence of evidence is an answer)

```
ask(cid, 'When did we first know Harbor Analytics was at risk, and what did the numbers say at the time?', account_id=aid)
```

**tool_audit_log** +1
- id=38 · mcp/ask cust=7 key=local allowed 

→ model=claude-opus-5 sentences=8 unsupported=0 gaps=6
- The first knowable risk was January 2026: the leading layer flagged early_warning at month 2026-01-01, with qual 33.33 against kpi_only 80.0 and a divergence of -46.67 across 3 signals.  `['sig:519', 'sig:520', 'sig:521']`
- All three January signals were dated 2026-01-14 and all three were unreviewed at that point (unreviewed_count 3): Elena Rossi skipped the QBR again this quarter (engagement_decline, confidence 1), she 'hasn't replied to two follow-ups from Maya; her team lead says she is heads down on a platform review' (champion_change, confidence 0.7), and 'heads down on a platform review' read as competitor_evaluation (commercial_pressure, confidence 0.5).  `['sig:519', 'sig:520', 'sig:521', '519', '520', '521']`
- That January evidence is observed tier from email, extracted by llm_extraction, and each node still carries requires_review true — so the earliest warning rests on unreviewed evidence.  `['519', '520', '521']`
- The KPI-only score at that moment was still healthy at 80.0, and the account was formally in the baseline phase from 2026-01-01 with health_start 80.0.  `['sig:521']`
- The trailing numbers did not confirm it until much later: first_trailing_warning_at is 2026-05-01, a lead of 120 days, with kpi_only falling to 75.71 in February, 73.43 in March, 58.53 in April and 40.18 in May.  `['sig:519', 'sig:520', 'sig:521', 'sig:522', 'sig:523', 'sig:524', 'sig:526', 'sig:527']`
- System-derived health transitions came after the signals: healthy → at_risk at 58.53 on 2026-04-01 and at_risk → critical at 40.18 on 2026-05-01.  `['hs:601', 'hs:602']`

## Step 18 — portfolio row (what a CRO sees first)

```
list_journeys(cid)
```

**tool_audit_log** +1
- id=39 · mcp/list_journeys cust=7 key=local allowed 

## What Hindsight needs from here

Wizard B (`trigger_wizard(cid, 'b')`) needs ≥5 journeys with outcomes to derive pattern profiles and run the lead-time backtest; on a one-account trace it reports 'insufficient'. On the seeded demo tenants it runs inside process_data (WizardRun rows). The intervention step is the gap in this trace: today an intervention is only a signal with role 'intervention'; `playbook-governance-layer.md` adds the record (proposed → approved → sent → closed) and its measurement.

## Step 19 — the audit trail of everything above

```
GET /api/audit?customer_id=cid  (in-process calls are recorded as key_kind=local)
```

_(no new rows — read-only step)_
