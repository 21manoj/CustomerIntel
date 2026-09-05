# Demo narratives — proving the early-warning mechanism before we have partner data

*2026-09-02. Companion to `wizard-a-assessment.md` §7. There is no design partner yet, so demos run on constructed tenants. The rule that keeps this credible: a demo proves the **mechanism** (behavioral → financial, with evidence, measured by the platform's own backtest) and says so; it never presents a constructed lead time as a measured one.*

---

## 1. What a demo must land — the outcomes

Six things the buyer should walk away having *seen*, not been told. Each scenario below hits all six.

| # | Outcome | What's on screen |
|---|---|---|
| O1 | **Lead time, measured by the platform** | The Hindsight backtest table for this tenant: *N past events, behavioral composite crossed a median D days before the financial decision in K of N, F false alarms / 100 account-months; trailing KPI health crossed D′ days before — the behavioral layer bought D − D′ days.* |
| O2 | **A live account in the same pattern, warned now** | Foresight: an account whose behavioral composite crossed the threshold this month, with the arc hypothesis, the phase, and days-to-renewal. |
| O3 | **Evidence, one click deep** | Every warning opens to the episodes that produced it — the signals, the stakeholder change, the KPI inflection — dated, with source platform. No black box. |
| O4 | **Dollars, CFO-framed** | ARR exposed on the warned accounts; Power-of-1 impact of moving the leading composite by one point; cost of inaction vs playbook cost. |
| O5 | **Action closes the loop** | Recommended playbook in the approval queue, CSM capacity check, and — for the historical case — what the intervention actually changed vs the arc's expected path. |
| O6 | **The platform tells the truth about itself** | Coverage stat (*"11 of 14 accounts classified with cited evidence; 3 unclassified"*), at least one visible false alarm with its resolution, cold-start labelling where calibration doesn't exist yet, and the banner: *synthetic demo tenant — lead times constructed to illustrate the mechanism; your measured numbers come from your own history in week 1 of onboarding.* |

O6 is not a disclaimer to hide in a footer. It is the differentiator against every "AI-powered" CS tool that shows a confident number and can't say where it came from.

## 2. Scenario design rules

Constructed tenants have to look like real portfolios, or the buyer's first question is "why does every account tell a story?"

- **12–15 accounts per tenant; 2–3 carry the featured story.** The rest are background: stable, mildly noisy, one genuinely `unclassified`, one false alarm (behavioral dip that recovered on its own, no financial event).
- **Behavioral onset precedes the financial event by 60–80 days *with noise*** — not a straight ramp. Signal cadence is irregular; KPIs have week-to-week jitter; one signal contradicts the trend.
- **Three timelines per featured account**, so O1's comparators exist in the data: behavioral composite crossing, trailing KPI health crossing, and the CRM's own risk flag (always last).
- **Financial events carry a decision date**, not just a contract end.
- **Every constructed signal is `data_origin`-tagged synthetic** at the tenant level; the load-driver's manifest mode must stamp it (open backlog item) before these tenants are built.
- **Vertical-native vocabulary.** Datacenter scenarios use the datacenter signal set (`reserved_cluster_idle`, `commitment_ramp_miss`, `multicloud_diversification`, `reliability_sla_breach`, `power_capacity_constraint`, `reservation_expansion_interest`, `silicon_refresh_interest`, `funding_raised`) — the very signals the old classifier couldn't read. That's a deliberate proof point for Wizard A v2's signal roles.

## 3. The three scenarios

### Scenario A — Silent displacement (datacenter_v1, H1 retention)

*"They didn't complain. They just started moving workloads."*

**Account:** Meridian AI, $4.1M ARR GPU-cloud commitment, renewal in 95 days at demo time.

| Day | Layer | What happens |
|---|---|---|
| T−78 | behavioral | `reserved_cluster_idle` rises 12% → 31% over three weeks; utilization KPI (P2) still inside target band |
| T−70 | behavioral | QBR postponed by the customer; champion (Head of ML Infra) login cadence halves |
| T−62 | behavioral | Support ticket mentions "validating a second provider for training jobs" → `multicloud_diversification` |
| T−55 | behavioral | `commitment_ramp_miss` — consumed 68% of committed capacity vs 85% plan; **behavioral composite crosses the warning threshold** |
| T−41 | trailing | Trailing KPI health crosses into at-risk (utilization + realized $/GPU-hr both below target) |
| T−14 | CRM | CSM marks renewal "at risk" after a pricing-pressure email |
| T | financial | Renewal signed at −$1.4M (contraction), decision date recorded |

**Platform shows:** Hindsight — this tenant's history has 9 similar events; behavioral led the decision by median 66 days, trailing by 38, CRM flag by 12. Foresight — a second account, Helix Compute, is at day −58 of the same pattern *today*: warning with the four episodes cited, arc hypothesis `silent_churn` @ evidence-cited confidence, $2.9M exposed, playbook "workload-retention review + reserved-pricing restructure" queued with cost and Po1 impact. **Honesty beat:** Quantum Labs shows the same idle-capacity spike at T−60 and recovered in three weeks after a batch-job migration — shown as a resolved false alarm, counted in F.

### Scenario B — Expansion intent the CRM couldn't see (datacenter_v1, H2 growth)

*"They were going to ask for more capacity in April. We knew in January."*

**Account:** Stellar Inference, $3.4M ARR, healthy, no open opportunity.

| Day | Layer | What happens |
|---|---|---|
| T−72 | behavioral | `funding_raised` signal (press/LinkedIn); utilization ramp from 71% → 88% over four weeks |
| T−60 | behavioral | `reservation_expansion_interest` — customer engineer asks about H100 reservation windows in a support thread |
| T−48 | behavioral | `silicon_refresh_interest`; exec engagement event (their CTO joins the monthly review) — **expansion composite crosses** |
| T−35 | trailing | Provisioning-velocity KPI (P6) hits ceiling; capacity KPI flags constraint |
| T−20 | CRM | Opportunity created by the AE after the customer emails asking for a quote |
| T | financial | +$900K expansion booked |

**Platform shows:** the expansion composite fired 52 days before the AE created the opportunity; Po1 on the expansion pillar; CSM capacity view showing who should own the conversation and when. Foresight — Zenith Training at day −50 of the same pattern, with a "capacity conversation" playbook, not a retention one. **Honesty beat:** Vector Dynamics has the utilization ramp but no intent signals — shown as *watch, not warned*, with the coverage stat explaining why.

### Scenario C — Champion departure, intervention, and the counterfactual (saas_premium, H1 with action)

*"The person who bought us left. Here's what we did, and what would have happened if we hadn't."*

**Account:** Northwind Analytics, $1.8M ARR, renewal in 120 days at story start.

| Day | Layer | What happens |
|---|---|---|
| T−82 | behavioral | STAKEHOLDER change: champion (VP Data) leaves — CRM contact update + LinkedIn signal |
| T−66 | behavioral | Engagement decline: weekly syncs lapse, NPS response from the replacement is a 5 |
| T−51 | behavioral | Ticket volume spike from a team that used to self-serve — **composite crosses; arc `exec_sponsor_change`, phase deterioration** |
| T−44 | action | Playbook "exec sponsor rebuild" approved and executed: exec-to-exec intro, value review with the new VP |
| T−30 | behavioral | New champion identified; engagement recovers; sentiment from signals turns positive |
| T−12 | trailing | Trailing KPI health bottoms and turns — *after* the intervention |
| T | financial | Renewal secured at −8% (minor contraction) vs the arc's expected path of full churn |

**Platform shows:** the journey with phases and the transition triggers; the **expected-path overlay** from the `exec_sponsor_change` template (typical: churn at renewal) against the actual path; the intervention's before/after window (counterfactual hook); playbook cost vs ARR protected. **Honesty beat:** the counterfactual is labelled *arc prior from N historical cases, not a prediction for this account*; a second champion-change account, Blue Harbor, recovered with no intervention — shown so the ROI number isn't overstated.

## 4. Why these three

- A and C are H1 across two verticals with different behavioral vocabularies (infra/usage vs stakeholder/engagement); B is H2 — the claim is *churn and expansion*, and most competitors only demo the defensive half.
- Together they exercise every part of Wizard A v2: signal roles (A, B), stakeholder episodes (C), phases with transition triggers (C), expected-path overlays (C), the leading/trailing series and first-dip dates (all), `unclassified` and false alarms (all).
- Each has a Hindsight half (the backtest) and a Foresight half (a live account mid-pattern) — the dual-horizon design made visible.

## 5. What each demo may and may not claim

| May say | May not say |
|---|---|
| "This is how the platform measures lead time — here's the backtest running on this tenant." | "The platform detects churn 60–80 days early." |
| "On this constructed tenant the behavioral layer led by a median 66 days; on your history you'll see your own number in week 1." | Any lead-time figure without the *synthetic* label. |
| "Here are the four signals that produced this warning." | A warning with no cited episodes. |
| "3 of 14 accounts are unclassified; here's why." | 100% coverage. |

## 6. What has to exist for these to run — status 2026-09-02

1. ✅ Wizard A v2 (`journeys/`) — Tier 2A-5.
2. ✅ `evals/lead_time_backtest.py` — H1/H2, trailing + CRM comparators, right-censoring, refutation check, evidence label.
3. ✅ `demo/generate.py` + `demo/manifests/{demo_silent_displacement_dc,demo_expansion_intent_dc,demo_champion_departure_saas}.json` — built inside this repo, not by extending the old load-driver (it derives signals from story phases, never emits loss events, doesn't stamp `data_origin`, targets the old REST path, and lives in the retiring repo). `python -m demo.generate --manifest … --register`.
4. ✅ `HealthScore.qual_score / divergence / early_warning` written by the journey builder.

### What the harness reads back today (synthetic — not evidence)

| Scenario | Leading | Trailing KPI | CSM's own flag | Behavioral layer bought |
|---|---|---|---|---|
| A — Silent displacement (Meridian AI, −$1.4M) | 78 d | 50 d | 14 d | +28 d over trailing, **+64 d over the CSM** |
| B — Expansion intent (Stellar Inference, +$900K) | 68 d | — | — (AE opportunity at 20 d) | |
| C — Champion departure + intervention (Northwind, −8%) | 83 d | — (never crossed: the intervention worked) | 40 d | **+43 d over the CSM** |

Each tenant also carries a live twin (an open story the harness reports as *open*, not a false alarm), a false-alarm account, and an `unclassified` account. Numbers are month-end-dated (the conservative availability rule), which is why a T−104 onset reads as 78 days.

Not yet built: the demo *surfaces* (the Hindsight table, the evidence drill-down, the expected-path overlay, the honesty banner) — those are UI, and the new build has no UI or deployed server yet.

---

## 7. Generator v2 — communications through the engine (2026-09-04)

*Decision (product owner): signals are first-class. The generator no longer writes typed behavioral rows to `enhanced_qualitative_signals.csv`; it authors **communications** — raw text with a source, a time, the people on it — and submits them through the signal engine (`signal_engine.pipeline.ingest` → `process_pending`). The evidence the journey sees is whatever the engine extracted. Code: `demo/manifest_v2.py` (schema + validation), `demo/generate.py` (`register_v2`, `emit_outcomes` seam), `demo/oracle.py`, `demo/scorecard.py`.*

### What a v2 manifest declares

Per account, `communications: [{day, source_type, text, participants:[{name,title}], source_ref?, expected_subtypes:[…], expected_sentiment?}]`. Every expected subtype must exist in the vertical's taxonomy (base + overlay); validation at load fails loudly on an unknown subtype, an unknown source type, unsorted days, a communication with no named participant, duplicate text on one account (the engine would dedup it silently), a v1 event key, or a KPI-layer account without a health curve. The three scenario manifests (§3) were converted by turning each typed signal into a realistic communication with the same day, people and sentiment, labelled with the old type — so the tables in §3 and §6 still hold. Where a text plainly carries a second signal (Stellar's Series C email also reports the utilization ramp), it is labelled with both; same role, same date, same composite.

What stays on CSV: accounts (roster), KPIs, the CSM's own risk flag (`signal_type='csm_risk_flag'`, structured path — declared, never extracted), and for now outcomes. Order of a run: the CSVs are staged and ingested (no scoring), the communications go through the engine, Wizard A builds evidence-only journeys, then outcomes enter through `emit_outcomes()` and **one** `process_data` scores the KPI layer, rebuilds the journeys over all the evidence and runs Wizard B once — evidence lands before the pipeline, as it would for a real tenant. `emit_outcomes()` today rewrites each event's link from the manifest's communication ref to the engine's signal id and stages `outcomes.csv`; when the outcome-logging MCP tool lands that body becomes one call per event and nothing else changes. `"kpis": "none"` makes a signals-only tenant (P1); with no events it never needs `process_data` at all.

### Three extractors, one scorecard

`--extractor auto|model|stub|oracle` (seed_demo: `DEMO_EXTRACTOR`; `auto`, the default, is the model with an API key and the oracle without — so a keyless seed, and the Tier 2B tests that register these manifests, still tell the §3 stories). After processing, every communication's extracted subtypes (the OBSERVED SIGNAL nodes it produced) are compared with its labels: per communication exact / partial / miss, per role precision / recall, subtype-level P/R, unclassified and still-pending counts — written to `demo/out/<tenant>_scorecard.json` beside `<tenant>_labelled.jsonl` (text, source, labels, extraction, `model_version`): the seed of the labelled extraction eval set. The scorecard is labelled with the `model_version` that answered. The oracle plays the manifest's own labels back through the engine — 100% by construction, **not a model result**, and the file says so; it exists so the narratives can be seeded and the journey/backtest path tested without a key.

### What the keyword stub reads (no API key) — reported as the floor it is

| Manifest | Communications | Exact | Unclassified | Subtype P / R | Role-level notes |
|---|---|---|---|---|---|
| A — Silent displacement (dc) | 28 | 0 | 6 | 0.00 / 0.00 (fp 33, fn 28) | usage_decline R 0.75, routine R 0.89, commercial_pressure R 0.33; the stub emits base subtypes (`competitor_mention`, `usage_decline`), never the datacenter overlay words |
| B — Expansion intent (dc) | 28 | 0 | 13 | 0.00 / 0.00 (fp 23, fn 30) | |
| C — Champion departure (saas) | 28 | 0 | 11 | 0.00 / 0.00 (fp 26, fn 29) | |
| D — Signals-only (saas, P1) | 31 | 2 | 16 | 0.06 / 0.03 (fp 16, fn 29) | one of the two exact hits is the labelled "carries no signal" note |

Under the oracle all four score 1.0 / 1.0 and the §6 lead times reproduce exactly (A 78/50/14 d, B 68 d, C 83/—/40 d). Under the real model the numbers are whatever they are; the scorecard is the place they show up, per tenant, per run.

### Signals-only tenant (D) — what builds, what doesn't

Six accounts, 31 communications, no KPI rows, no health scores. The journey builds from evidence alone with the current builder: every month is a live month (`kpi_only` null, label `leading_only`), the leading series carries the composite and role counts, `first_leading_warning_at` is set. Halcyon Health (champion departure → procurement review → seat reduction) classifies as `exec_sponsor_change` through that rule's health-free variant (`needs_or_roles: engagement_decline`). The other five stay `unclassified` even when the evidence is complete: Orchard Retail's expansion story has every expansion-intent and advocacy role but `expansion_champion` / `land_and_expand` require `very_healthy` / `healthy`, which a tenant with no health layer can never satisfy — the open half of P1 (`journeys/arc_classifier.py` RULES). Not patched here.

Also found, not patched (outside this change): `evals/lead_time_backtest._warning_months` does `s['kpi_only'] < at_risk_min` on every month; a live month has `kpi_only=None`, so the backtest raises `TypeError` on a signals-only tenant (and would on any tenant whose evidence runs past its last scored month). `tests/test_demo_v2.py` carries the xfail.

### Tests

`tests/test_demo_manifests.py` (oracle: narratives, engine path, outcome links, D's journeys), `tests/test_demo_v2.py` (validation, v1 still loads and registers on the CSV path, monkeypatched extractor → 100%, stub → misses reported honestly, the evals gap). The customer-415 replay in `scripts/seed_demo.py` is real data and stays on the CSV path untouched.
