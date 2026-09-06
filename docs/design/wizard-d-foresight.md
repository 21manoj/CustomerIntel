# Wizard D — Foresight

*2026-09-05. Design for port-inventory row 3, written before the code and kept true as it was built. Companion to `wizard-a-assessment.md` (journey v3), `playbook-governance-layer.md` §10 (interventions), and the Hindsight lens in `backend/wizards/wizard_b_hindsight.py`. Rule applied: parity is a floor — the old predictor is measured, not copied.*

## 1. What it is for, and who reads it

Foresight is the **forward** lens over an account's journey: at the next decision point (renewal, refresh, or the horizon end) how likely is the account to be retained, how likely to expand, and what ARR is expected at the end of the horizon — **with an interval and a label that says where the number came from**. Hindsight (Wizard B) proves on history; Foresight runs on live accounts. Same two-layer vocabulary: the trailing layer (`kpi_only`) and the leading layer (`qual`, `early_warning`) are read side by side and never blended.

Consumers: (1) the CRO/CFO forecast tile — a revenue-weighted portfolio roll-up with a propagated range; (2) the journey — `journey_json['forecast']`, plus one cited narrative sentence; (3) Ask AI — reads the block and the sentence through the existing contract; (4) later, the playbook layer's exposure-weighted priority.

## 2. What the old one delivered; what the live data supports

Old (`wizard_d_predictor_calibrator.py`, `predictor/`): four GLMM sub-models over a monthly panel, `saas_premium` hardcoded in the panel SQL, the seeds and the cold-start path; CDI seed priors by SaaS profile; CIs a static ±0.05/0.10 self-labelled `placeholder_uncalibrated`; every tenant on the box was `cold_start`. In practice it returned a hand-written seed with a fake interval, for one vertical.

Live box, read tools only (2026-09-05): 46 OUTCOME nodes on 12 tenants, every tenant synthetic (`synthetic_demo` / `synthetic_replay`). Terminal labels (taxonomy buckets `lost` / `protected` / `expansion`): tenant 4 (dc2_s replay, 10 accounts) has 17 positive and **0 lost**; tenants 1, 5, 6, 7, 10, 11 carry one `contraction` each; the rest of tenant 4's 33 outcomes are `at_risk` / `pipeline` (not terminal). One INTERVENTION node per showcase tenant. Every account has a renewal date; tenant 10 has no KPI layer, tenant 5 mixes `none` / `not_yet`. So: **no tenant can unlock a calibrated forecast today**, and the honest output is a template prior that says so with the counts.

Dry run of the shipped engine over the live journeys (read tools only, nothing written, 2026-09-05): tenant 10 and 11 → 2 labelled decisions each (1 retained / 1 not), tenant 4 → 11 (all retained, 0 not) — every block `prior`, `2 of 30` / `11 of 30` on the label line. Tenant 10 (no KPI layer) shows the widened ±0.20 template range; tenant 11 the base ±0.15. Portfolio, prior basis: tenant 11 NRR 0.83 [0.75, 0.91] independent; tenant 4 NRR 0.86 [0.81, 0.90] independent / [0.72, 0.98] correlated. Three of tenant 4's ten accounts and one of each showcase tenant's three sit on arcs whose template puts the whole ARR at risk (see §6).

## 3. Design

**Basis.** Every account forecast carries `basis`: `prior` (template + features; used while the tenant has fewer than `calibration.min_labels` labelled decisions or fewer than `min_per_class` of either class) or `calibrated` (the prior updated on the tenant's own logged outcomes). The block always shows `labels: {n, positive, negative, needed}` so a prior is visibly a prior. No point estimate without an interval; no interval without its semantics (`template_range` | `beta_credible`).

**Prior** (`config/wizard_d.json`, no number in code). `p_retain = clamp(base_retention × Π factors)` where the factors are read from the journey: health band of `kpi_only` (or `none` when there is no KPI layer), the latest leading label (`early_warning` / `recovery_watch` / `aligned` / `leading_only` split by net polarity), the arc hypothesis (keyed by arc_type, `steady`, `unclassified`), the current phase, an intervention in flight in the current phase (template lift, labelled — Hindsight's `interventions` block is where the measured lift will come from). If no decision point falls inside the horizon, the retention question is mid-term contraction, not renewal: `p_retain = 1 − midterm_loss_hazard ÷ Π factors` (the same factors, raising the hazard when below 1). `p_expand` the same way with its own factors (expansion-intent roles in the 90-day window). The story-arc template supplies **loss severity** (`arr_at_risk_peak / arr_start`) and the expected-path position (weeks into the arc vs `total_weeks`, template `health_end`) — context on the block, never a probability. Interval: `± interval.half_width_p`, widened for thin evidence and for a missing KPI layer.

**Calibrated** — Beta-binomial, not a regression. Labels: terminal outcomes grouped into decisions per account within `label_window_days`; `lost` in the group → 0, else 1 (expansion label: any `expansion` bucket in the group). The stratum of each label is **point-in-time**: the health band and leading label of the journey series month before the decision — no arc, no leakage. The account's posterior is `Beta(prior_strength·p_prior + positives, prior_strength·(1−p_prior) + negatives)` on its own stratum, or the pooled counts when the stratum has fewer than `min_per_stratum` labels (said on the block). Point = mean; interval = Beta quantiles at `interval_level` (own implementation of the regularized incomplete beta; no scipy in this build). With zero labels the posterior *is* the prior — the gate makes the label honest, the math makes the transition smooth.

**Dollars.** `expected_arr_end = ARR × [p_retain·(1 + p_expand·expansion_size_share) + (1−p_retain)·(1−loss_severity)]`, monotone in both probabilities, so the bounds are the formula at the low and high ends. `nrr_contribution = expected_arr_end − ARR`.

**Portfolio.** `Σ expected_arr_end` with the interval **propagated**: each account's half-width becomes a σ at the configured level; the headline range assumes independence (`sqrt(Σσ²)`), and the perfectly-correlated range (`Σ` of the bounds) is reported beside it as the worst case. Portfolio NRR = expected / Σ ARR, both bounds.

**Absolute separation.** The band comes from `kpi_only`; the leading label from `qual`. They enter as separate factors; nothing here writes a blended score anywhere.

**Vertical-agnostic.** No vertical name in code. `config/wizard_d.json` has a `verticals` override block per key; it is empty and says so — no vertical has a measured base rate yet.

## 4. Storage, tools, embedding

- `forecast_runs` (one row per run: horizon, as_of, basis mix, label counts, portfolio block, config snapshot, generator) and `account_forecasts` (one row per account per run, the full block). Alembic `0003_wizard_d_forecasts`. Immutable history; readers take the latest run.
- `trigger_wizard(customer_id, 'd')` runs Foresight (needs journeys; `skipped` with a reason otherwise). `get_forecast(customer_id, account_id=None)` and `GET /api/forecast` return the latest run: the portfolio block, or one account's block. Both keyed (`KEYED_TOOLS`; trigger is already a write tool). `process_data` runs D after B (config `run_in_process_data`).
- After a run, D writes `journey_json['forecast']` on each JourneyData row and re-renders the narrative (pure over the journey JSON) so the sentence appears without a rebuild; `build_journey` embeds the latest stored forecast on every later rebuild and marks it `stale` when evidence has arrived since the run. Generator `3.7`.
- Narrative: one sentence, template `forecast_statement`, citing the episodes the forecast rests on (arc support, the latest series month's contributors, the latest health transition, interventions in flight, the renewal milestone). No citation → dropped and listed in `omitted`, the standing rule.
- Ask AI: the journey context block gains `forecast`; numbers are read from it, never computed.

## 5. Deviations from the brief, with reasons

- A Beta-binomial stratum update instead of a fitted feature model: with tens of labels a regression is unearned confidence; the update is exact, interpretable, degrades to the prior, and yields a real credible interval.
- Two portfolio ranges (independent, correlated) rather than one: the correlation between accounts is unknown; naming both is the honest propagation.
- D runs inside `process_data` (after B) so the block is populated on every tenant after the next upload — otherwise the journey shows a forecast only for tenants where someone remembered to trigger it (the fires-before-dependency class).

## 6. Known gaps

Calibrated mode has not been exercised on live data (it cannot be, by §2). The intervention factor is a template lift until Hindsight reports a measured one. Expansion size is a single share, not a distribution. No cross-tenant pooling (`pool_scope` is tenant-only in this version). Loss severity is read from the story-arc template's `arr_at_risk_peak / arr_start`, and for `exec_sponsor_change`, `competitive_displacement` and `crisis_recovery` that ratio is 1.0 — a non-retained outcome on those arcs is forecast as a full loss, which is right for churn at renewal and pessimistic for a contraction; the block labels it `story_arc_template`, and a measured severity per arc (from Hindsight's realized outcomes) should replace it.
