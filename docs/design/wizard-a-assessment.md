# Wizard A — purpose, value today, and what "capturing the journey" should mean

*Assessment written 2026-09-01, before deciding whether Tier 2A-5 ports Wizard A or rebuilds it. Evidence: the old repo's code (`wizards/wizard_a_journey_db.py`, `utils/arc_classifier.py`, `utils/arc_decision_generator.py`, `utils/arc_edge_generator.py`, `config/story_arcs/`), its consumers (Wizard B's `PatternAnalyzer`, Wizard D's `predictor/features.py`, the journey APIs), and the live EC2 database (166 classified accounts across 11 tenants, read 2026-09-01).*

---

## 1. What Wizard A is for

Wizard A is the step that turns an account's raw rows — health-score history, KPI measurements, and the signal / stakeholder / decision / outcome nodes — into a **journey**: a statement of *what story this account is living, which chapter it's in, and what the causal chain looks like*. It is the bridge between the trailing layer (KPI rollups) and the leading layer (qualitative narrative) that the product is positioned on. Everything downstream that claims to reason about *trajectory* — Wizard B's pattern/counterfactual analysis, Wizard D's NRR forecast, the journey visualizer, arc→playbook triggering — reads Wizard A's output.

Concretely it produces three things:

| Output | Where it lands | Who reads it |
|---|---|---|
| `arc_type`, `arc_phase`, `arc_confidence` | `Account` columns | Wizard B (forecast: arc→playbook map), **Wizard D (one-hot model feature)**, push-intelligence triggers, CRO/CSM surfaces |
| Story-template scaffolding: DECISION nodes + causal edges | `context_nodes` / `context_edges` (`source='synthetic'`, `source_platform='wizard_a'`) | Graph APIs, Mermaid views, the I3 audit; **explicitly excluded** by Wizard B's `count_trustworthy_causal_edges` |
| `journey_json` timeline | `journey_data` | Journey visualizer, Wizard B (`pattern_type`, `events[]`, start/end health) |

The concept is right. A journey object that carries both the quantitative trajectory and the qualitative narrative, with phases and evidence, *is* the differentiator. The assessment below is about the implementation.

## 2. What it actually delivers today

### 2.1 Arc classification: right label roughly half the time, and it has a "safe default" that isn't safe

`classify_arc()` is a first-match rule cascade over seven features (health now, 30/60-day slope, signal-subtype keyword sets, stakeholder-departure flag, P1 delta, days-to-renewal). Live distribution across all 166 classified accounts:

```
competitive_displacement  48   (24 of these are rule 9 — the always-true fallback @ 0.55)
land_and_expand           47
exec_sponsor_change       29
expansion_champion        26
seasonal_surge             8
silent_churn               5
crisis_recovery            3
stalled_deployment         0
```

Three findings:

1. **The fallback invents a story.** Rule 9 is `lambda f: True` → `competitive_displacement @ 0.55`. 14.5% of all live accounts, and **5 of 12 on each datacenter_v1 tenant (42%)**, carry a "competitor is displacing us" arc with zero competitor evidence. That arc then drives which DECISION templates and playbooks get attached. `unclassified` should be a legitimate outcome; a fabricated competitor narrative is not a safe default.
2. **`stalled_deployment` has never fired (0/166)** even though `infrastructure_decay` is one of the most common declared arcs in the load-driver data. Cause: `stakeholder_escalation` and `executive_engagement` sit in `_CHAMPION_LOSS_SIGNALS`, so an infra crisis with an exec escalation matches rule 1b (`exec_sponsor_change`) before rule 3 is reached. On customer 359 both `infrastructure_decay` accounts were labelled `exec_sponsor_change @ 0.80`.
3. **The signal vocabulary is SaaS-shaped.** The datacenter fixture's real signal subtypes — `power_capacity_constraint`, `reliability_sla_breach`, `spot_price_pressure`, `reserved_cluster_idle`, `commitment_ramp_miss`, `silicon_refresh_interest`, `multicloud_diversification` — match **no** keyword set. That is why datacenter tenants fall back 3× more often than dc2_s/SaaS ones. The catalogs already carry per-vertical `pillar_roles`; the classifier has no per-vertical signal roles.

### 2.2 Three classifiers, two written to the graph, contradicting each other on the same account

Already recorded (memory: *roadmap_wizard_b_early_warning_dead_code*): the load-driver's ~32 `story_arc` labels, `classify_arc()`'s 8 canonical arcs, and `_classify_trajectory_with_confidence()`'s 6 health-shape labels are disjoint vocabularies. Live tenant 418, same account, same run:

```
Phoenix Hyperscale   Account.arc_type = seasonal_surge (healthy/stable template)   arc_detection node: crisis  @ 0.67
Meridian Compute     Account.arc_type = competitive_displacement (fallback)        arc_detection node: crisis  @ 0.84
Zenith Data Corp     Account.arc_type = competitive_displacement (fallback)        arc_detection node: declining @ 0.95
```

Wizard B's early-warning rules read the third vocabulary and filter on values from the first, so they have never produced a rule for any customer. The "detects churn 16 weeks early" claim has no working mechanism behind it.

### 2.3 The scaffolding is template fabrication, and the platform already doesn't trust it

For each account, `arc_decision_generator` writes the story template's DECISION nodes (skipping only phases "ahead" of the detected one) and `arc_edge_generator` writes the template's `edge_topology` by **ordinal**: `signal:1 → signal:2 LED_TO "Initial signal preceded escalation"` binds to whatever the first and second observed signals happened to be, regardless of what they say. On customer 359 this produced 21 DECISION nodes and 33 edges — all labelled `synthetic`, all gated by an arc that was wrong or defaulted for 7 of 12 accounts.

The old repo's own reviews already pulled this in the right direction (2026-08-24: `evidence_refs` renamed `narrative_refs`, revenue stripped from template decisions, confidence set to `None`), and Wizard B's NRR evidence counter **excludes** `wizard_a` edges by provenance. So the scaffolding costs graph noise on CRO/CFO surfaces and I3-audit exceptions, while the one consumer that reasons about evidence filters it out.

### 2.4 The journey timeline is HealthScore re-serialized

`journey_json.events[]` has one entry per month: `health_score_after`, `phase` (= health band), `sentiment_value` (= **sign of the health delta**, ±1/0 — not sentiment), and `kpi_snapshot` (the first 10 KPIs in dict order). It contains no signals, stakeholders, decisions, outcomes, dates of real events, or revenue. Wizard B's "sustained negative sentiment" rule (EW002) would, if it ever fired, be measuring health direction twice.

### 2.5 What consumers actually use — and therefore what a misclassification costs

- **Wizard D** (`predictor/features.py`): `arc_type` is one-hot encoded as a model feature alongside `health_slope_1mo/3mo`, `volatility_3mo`, `days_to_renewal` bands. A fabricated arc is a fabricated feature in the NRR forecast.
- **Wizard B**: reads `Account.arc_type` → `config/arc_playbook_map.json` to pick the intervention it costs in the forecast; reads `journey.pattern_type` for the (dead) early-warning rules; reads real `OUTCOME` nodes and `Account.revenue` for realized NRR — the one input it gets right comes from ingest, not from Wizard A.
- **Journey visualizer / APIs**: `pattern_type`, `starting_health`, `ending_health` only.

**Verdict.** Wizard A today delivers (a) a coarse account label that is defaulted or misrouted for a large share of accounts and is nonetheless a predictor feature, (b) graph scaffolding the platform itself flags as untrustworthy, and (c) a timeline that is the health table with different keys. The *intent* is the product's core differentiator; the implementation encodes eight demo storylines and forces every real account into one.

## 3. What "capturing the customer journey experience" should mean

A journey should be an **evidence-bearing sequence of episodes with derived state**, not a template match. Proposed `journey_json` v3 (replaces the monthly health list):

```
journey
├── episodes[]            time-ordered; each: date, kind, source, evidence_node_ids[],
│                         quant_state {health, kpi_only, divergence, pillar_deltas},
│                         qual_state {sentiment (from signals' sentiment_score),
│                                     stakeholder_engagement, open_escalations},
│                         revenue_exposure_at_point
│     kinds: health_transition | signal | stakeholder_change | decision | outcome |
│            renewal_milestone | playbook_step
├── phases[]              baseline → deterioration → intervention → resolution, each with
│                         entered_at / exited_at and trigger_episode_id (what caused the transition);
│                         thresholds from health_thresholds.json, never literals
├── arc                   { arc_type | 'unclassified', confidence, matched_rule,
│                           supporting_episode_ids[], contradicting_evidence[], alternatives[] }
├── leading_vs_trailing   per-phase series of (qual_score − kpi_only_score);
│                         first_leading_dip_at, first_trailing_dip_at  → measurable lead time
├── counterfactual_hooks  for each intervention decision: window before/after with health,
│                         outcomes, revenue — what Wizard B needs to compute "would have warned"
└── features              the shared vector Wizard B and D both consume:
                          slopes, volatility, time_in_phase_days, days_to_renewal_band,
                          signal_counts_by_role, stakeholder_coverage, divergence_now, arc one-hot
```

Principles behind it:

- **One vocabulary.** The 8-arc taxonomy stays (it's a good buyer-facing language) but becomes the *only* arc vocabulary. The trajectory classifier is demoted to a `features.trajectory` value; load-driver labels stay in the load-driver. Wizard B keys off `journey.arc`, which makes its early-warning mechanism reachable for the first time.
- **Evidence-cited or unclassified.** Every arc assignment names the rule and the episodes that satisfied it. No always-true fallback.
- **Per-vertical signal roles**, mirroring the catalogs' `pillar_roles`: `signal_roles: {infra_incident: [...], capacity_pressure: [...], champion_change: [...], expansion_intent: [...], commercial_pressure: [...]}` per vertical, so the datacenter vocabulary above can actually drive classification. Rule sets are written against roles, not literal subtypes.
- **Templates become overlays, not nodes.** `config/story_arcs/` keeps its value as *expected path* content ("in this arc, the next decision is typically X, the chain typically looks like Y") — stored on the journey as `expected_path`, rendered as a dotted overlay, and used to compute "how far off the typical path is this account." Nothing synthetic is written to `context_nodes`/`context_edges`. A DECISION node exists when a decision was actually made (uploaded, or a playbook actually executed).
- **Leading/trailing divergence is the early-warning primitive** for both horizons: Wizard B backtests it on history (Hindsight: "qual dipped on date A, KPI health followed on date B, A→B is the lead time we would have given"), Wizard D reads it as a feature (Foresight). This is the two-layer model made operational and measurable, rather than a claim.

## 4. Enhancement list, ranked by value ÷ effort

| # | Enhancement | Unlocks | Effort |
|---|---|---|---|
| E1 | Single arc vocabulary; trajectory → feature; Wizard B reads `journey.arc` | Wizard B early-warning rules become reachable; contradictory nodes disappear | S |
| E2 | Evidence-cited classifier, `unclassified` allowed, no fallback; fix infra-vs-champion signal overlap | Stops inventing competitor stories for 14–42% of accounts; `stalled_deployment` becomes reachable | S–M |
| E3 | Per-vertical `signal_roles` in the catalogs; rules written against roles | Datacenter/manufacturing/healthcare signals classify instead of falling back | M |
| E4 | Journey v3 episodes from real nodes + health transitions; real sentiment | B's rules measure something; visualizer shows the actual story; counterfactual hooks exist | M |
| E5 | Leading/trailing divergence series + first-dip dates | The "N weeks early" claim becomes computed, backtestable (B) and predictive (D) | M |
| E6 | Templates as `expected_path` overlays; stop writing synthetic DECISION/edge rows | Removes synthetic content from CFO/CRO surfaces and the I3 audit; graph = evidence only | S (delete) + M (overlay render) |
| E7 | Shared `features` vector contract consumed by both B and D | One definition of slope/volatility/time-in-phase; D's arc feature stops being a guess | S |

E1, E2 and E6 are mostly deletion; E4 and E5 are the real build.

## 5. Recommendation for Tier 2A-5

**Do not port `wizard_a_journey_db.py` as-is.** Most of its 1,300 lines are template plumbing (decision generator, edge generator, ordinal ref registry, alias tables) that §3 says to drop. Build Wizard A v2 as:

1. `journeys/journey_builder.py` — episodes + phases + leading/trailing series from `HealthScore`, `KPIMeasurement`, and the graph (E4, E5).
2. `journeys/arc_classifier.py` — rule cascade rewritten against signal roles, evidence-cited, no fallback (E2, E3); the old rule *ideas* (slope bands, health bands, renewal proximity) carry over.
3. `journeys/features.py` — the shared vector (E7).
4. `config/story_arcs/` ported as read-only `expected_path` content (E6); `arc_decision_generator` / `arc_edge_generator` not ported.
5. `JourneyData` model ported with `journey_json` v3 and `generator_version='3.0'`.

**Parity contract changes.** For the first time the target is not "reproduce the old output" — the old output is wrong for a documented share of accounts. Instead: (a) for every account where the old arc was a non-fallback rule match, v2 must either agree or cite the evidence for disagreeing; (b) old fallback assignments must become `unclassified` or an evidence-cited arc; (c) health/phase-derived fields must match the old timeline exactly (they come from the same rows). Ground truth for the check is a *fresh* live tenant, not customer 359 (its files predate items 28/37/38).

## 6. Decisions — recommended answers (2026-09-02)

Deciding criterion, as stated by the user: *a credible AI platform grounded in truth, that can still produce outcomes such as early warning from behavioral signals 60–80 days ahead of the financial signals.* Every answer below is chosen so the lead-time claim is **measured, not asserted**.

**Q1 — arc vocabulary: keep the 8 arcs as the single, cross-vertical vocabulary; put per-vertical `signal_roles` underneath.**
The 8 arcs describe *relationship dynamics* (champion loss, silent churn, expansion, crisis recovery) which are vertical-agnostic — a datacenter buyer and a SaaS buyer both understand "silent churn." What differs by vertical is *which observable signals evidence each dynamic* (`reliability_sla_breach` is a datacenter infra signal the way `system_outage` is a SaaS one). Per-vertical arcs would fragment the one thing the early-warning claim needs most: enough labeled journeys per arc for Wizard B to backtest and Wizard D to fit (D one-hot encodes arc — more arcs = sparser features). Two credibility rules ride along: `unclassified` is a first-class value and the platform *reports its own coverage* ("71% of accounts classified with cited evidence"); and **the arc is never the early-warning mechanism** — it's the narrative wrapper. The warning comes from §3's leading-vs-trailing divergence, which is computed whether or not an arc matched.

**Q2 — synthetic scaffolding: delete outright, no demo flag.**
A truth-grounded platform cannot hold fabricated DECISION nodes in the same table as observed ones and rely on a provenance tag — CFO/CRO rollups, Mermaid views and the I3 audit aggregate across the tag, and reviewers have already caught it (the 2026-08-24 `evidence_refs`→`narrative_refs` rename was the symptom). Story templates keep their value as **expected-path priors**: "accounts in this arc typically show X ~6 weeks before Y" is exactly the kind of prior an early-warning system uses, and *deviation from the expected path* becomes a signal in its own right. Demo tenants already get observed-looking data from the load-driver; they don't need scaffolding. Keeping a flag would keep the code path alive for the next person to flip.

**Q3 — Wizard B rewire: 2A-5 produces the early-warning primitive; 2B consumes it. Schema fixed now.**
Wizard A v2 must write the `leading_vs_trailing` series and `first_leading_dip_at` / `first_trailing_dip_at` per journey — the lead-time *measurement* lives in A. Wizard B (2B) then does what it's for: the Hindsight backtest ("on the last 12 months, the leading composite crossed the warning threshold a median N days before the financial event in K of M churn/contraction cases; false-alarm rate R per 100 account-months"). Moving B's rules into A conflates producing the primitive with proving it. Wizard D reads the same series as a Foresight feature. Fixing the v3 schema now is what stops 2B from re-opening it.

### What makes "60–80 days" credible rather than a slogan

1. **Define the two layers operationally, per vertical.** Behavioral/leading = qualitative signals (stakeholder change, escalation velocity, engagement decline, sentiment) plus the KPIs whose pillar role is `adoption`/`engagement`. Financial/trailing = OUTCOME events (renewal, contraction, churn, expansion booked) plus KPIs whose role is `revenue`/`expansion`. The catalogs' `pillar_roles` already carry this split; `signal_roles` completes it.
2. **Write the leading layer to the columns that already exist for it.** `HealthScore.qual_score`, `divergence`, `early_warning` were designed for exactly this (two-layer indicator model) and are **NULL on every live row** — they have never had a writer. Journey v3's series is that writer.
3. **Report lead time as a measured distribution, per tenant.** Median, IQR, recall, false-alarm rate, sample size — from Wizard B's backtest on the buyer's own history. If a tenant's data supports 45 days, the platform says 45. "60–80 days" is the claim the design *targets*; each tenant gets the number its data proves.
4. **Gate the claim on data origin.** The load-driver *generates* signals from story phases, so a backtest on a synthetic tenant is circular. `Customer.data_origin` (WS-2 2a) already tags synthetic tenants; lead-time statistics on those are labelled "demonstrated on synthetic data," never "measured."
5. **Every warning carries its evidence.** Episode ids, rule/model version, the lead-time distribution it rests on, and `confidence_semantics` (calibrated hit-rate vs rule-match constant). Cold-start tenants (< N labeled outcomes) get warnings labelled *rule-based, uncalibrated* — same honesty Wizard D's `cold_start` already practises.

## 7. Proof protocol — how the hypothesis gets tested on real data

**Starting condition.** No tenant on the platform holds real customer history; every one is load-driver output, and the load-driver *derives* signals from story phases. A lead time measured there is the generator's own parameter read back. So the proof needs (a) a pre-registered protocol and (b) real history. Order matters: protocol first, so the data can't shape the definition.

### 7.1 Pre-register (before any outcome is looked at)

Frozen in a spec + a tagged code version:

- **H1 (retention):** for accounts with a negative financial event at date `T_f` (churn, contraction, non-renewal decision), the *leading composite* first crosses the warning threshold at `T_l` such that median `(T_f − T_l)` ≥ 60 days, recall ≥ 70%, at a false-alarm budget ≤ 5 warnings per 100 account-months.
- **H2 (expansion):** same shape for expansion booked, leading = expansion-intent signals + adoption/engagement KPIs.
- **Leading composite definition:** which signal roles and which pillar roles (`adoption`, `engagement`) count as behavioral; how they aggregate; the threshold. Written once, versioned.
- **Financial event definition:** the *decision* date (renewal declined, contraction signed), not contract end — otherwise lead time is inflated by the notice period.
- **Exclusions:** signals that *are* the financial event in disguise ("customer says they're leaving") — classified as `announcement`, not behavioral, and excluded from the leading composite; any note or annotation written after `T_f`.
- **Refutation criterion, stated up front:** median < 30 days or recall < 50% at the false-alarm budget → the claim becomes whatever was measured, and the "60–80" language is retired until a tenant supports it.

### 7.2 Data requirements (what to ask a design partner for)

Exactly the canonical 4-CSV shape, with dates, 12–24 months deep, ≥ 30 negative financial events overall (power):

| File | Must have |
|---|---|
| `account_details.csv` | ARR, renewal date, CSM, champion at period start |
| `kpi_measurements.csv` | monthly (or weekly) usage/adoption/engagement KPIs, dated |
| `enhanced_qualitative_signals.csv` | tickets, escalations, NPS responses, meeting/email events — **each with the date it occurred**, plus a role tag or enough text to derive one |
| `outcomes.csv` | every renewal/churn/contraction/expansion with the *decision* date and amount |
| optional | the CSM's own risk-flag date from the CRM — the true comparator for "before the CSM would have noticed" |

Ingest as a real tenant (`Customer.data_origin = NULL`). Prerequisite fix: the load-driver's manifest mode doesn't stamp `data_origin` (memory: *backlog_load_driver_data_origin_not_stamped*) — close that first, or synthetic tenants can masquerade as measured.

### 7.3 Leakage and baseline controls

- **Point-in-time features only.** At `T_l` the composite may use nothing dated after `T_l`. Rolling-origin backtest: for each month `m`, compute the composite with data ≤ `m`, then check outcomes in `(m, m+90d]`.
- **Three comparators**, all reported alongside the leading composite: (1) trailing KPI-only health (`kpi_only_score`) crossing its own threshold — the lead time is the *difference* between the two crossings; (2) renewal-proximity naive baseline; (3) the CSM flag date where available.
- **Per-vertical, per-tenant reporting.** Never pooled into one number across tenants.

### 7.4 Metrics

Lead-time distribution (median, IQR, n), recall and precision at the false-alarm budget, AUC at 30/60/90-day horizons, calibration curve. Same table for H1 and H2.

### 7.5 Build order

1. **Harness** `evals/lead_time_backtest.py` — runs against any tenant DB, emits the §7.4 table + the refutation check. Independent of Wizard A v2's UI; only needs the leading/trailing series.
2. **Smoke on a synthetic tenant** — expected to pass trivially; its only purpose is to prove the harness runs. Output labelled *synthetic — not evidence*.
3. **Method validation on a public behavioral-churn dataset** (optional, B2C, e.g. KKBox: daily usage logs preceding subscription cancellation). Proves the *method* finds a lead time where one exists; says nothing about B2B numbers. Labelled as such.
4. **Design-partner history** — the only run that produces a claimable number. One partner → "measured on one tenant"; three across two verticals → the platform can say "measured" in GTM material with the distribution attached.
5. **Wire into Wizard B** (Hindsight backtest per tenant, run at onboarding) so every future tenant gets its own number — the claim stays measured, forever, per customer.

### 7.6 What the platform then says

Not "60–80 days early." Rather: *"On your history: N negative events; the behavioral composite crossed the warning threshold a median D days before the financial decision in K of N (recall R%), with F false alarms per 100 account-months. Trailing KPI health crossed a median D′ days before — the behavioral layer bought you D − D′ days."* If D lands in 60–80, the marketing claim is true and cited. If it doesn't, the platform is still credible — which is the property that makes the claim worth anything when it does hold.
