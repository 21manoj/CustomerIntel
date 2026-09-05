# Wizard C — weight calibration from logged outcomes

*2026-09-05. Port-inventory row 5. Rule applied: parity is a floor; the old subsystem is measured, then rebuilt on the evidence spine.*

## 1. What it is for, who consumes it

The health score is `Σ pillar_weight × pillar_score`, pillar score `Σ kpi_weight × kpi_score` (`utils/generic_scorer.py`). Those weights come from the vertical catalog unless `CustomerConfig.pillar_weights` overrides them. Wizard C exists to give a tenant's weights a basis in the tenant's **own logged outcomes**, and to answer the CFO's question — *why is Product Adoption weighted 30%?* — with rows that can be drilled into: which outcomes, what the KPIs looked like before them, how big the effect was, who approved the change and when.

Consumers: the scorer (through `CustomerConfig`, after a human approves), and anyone reading a health row's provenance (`HealthScore.weight_source`).

## 2. What the old one did, and what the live box supports

Old `wizards/wizard_c_weight_calibrator_db.py`: label = latest HealthScore ≥ 70 (success) / < 50 (fail) — a rollup of the same KPIs it then correlates, so it mostly rediscovers the base weights (memory: `roadmap_wizard_c_learn_from_context_graph`). It wrote weights straight into `CustomerConfig`, unapproved, with no record of the evidence.

Live CustomerIntelV1, read tools only (`list_journeys`, `get_evidence`, `get_journey`), 2026-09-05:

| tenant | vertical | accounts | KPI layer | logged OUTCOME nodes | accounts with one |
|---|---|---|---|---|---|
| 1, 2, 3 | demo | 12 | present | 1 each | 1 |
| 4 (replay) | dc2_s | 10 | present (Jul-25 → Mar-26) | **33** (23 positive-bucket, 10 at_risk, 0 lost) | 10 |
| 5 | demo | 6 | none / not_yet | 2 | 2 |
| 6, 7 | demo | 1 | — | 1 | 1 |
| 8, 9 | — | 0 | — | 0 | 0 |
| 10 (showcase, signals-only) | saas_premium | 3 | none | 3 | 2 |
| 11 (showcase) | saas_premium | 3 | present (May → Aug-26) | 3 | 2 |

So the minimum-outcomes gate (§3) opens live on tenant 4 only — a replay, labelled as such by its `data_origin`. Every other tenant gets `insufficient_outcomes` with the counts. That is the true state and the tool says it; it is not hidden behind a fallback to HealthScore labels (the old "cold-start fallback" would reintroduce the circularity).

## 3. The method (simple, explainable, no black box)

**Sample** = one logged OUTCOME node (observed, not review-rejected, in a taxonomy revenue bucket). **Label** from the bucket: `lost`, `at_risk` → negative; `protected`, `expansion`, `pipeline` → positive (bucket names from `taxonomy_base.json`; the mapping is config). Outcomes outside the buckets are counted as `unbucketed`, never labelled.

**Features** per sample: for every catalog KPI, the account's mean measured value over the `feature_window_days` before the outcome date, scored with the catalog's own `score_kpi` so every KPI is on the same 0–100 scale (and `higher_is_better` is already folded in). Pillar feature = the weight_l1-weighted mean of its KPI scores, the scorer's L2. A sample with no KPI row in its window is `unfeatured` and reported — a tenant with outcomes but no KPI layer (live tenants 5, 10) sees exactly that.

**Effect** per KPI and per pillar: `n_pos`, `n_neg` (samples where the feature exists), mean score before positive vs before negative outcomes, `effect_pts` = difference in points, `d` = effect / pooled sd. **Direction**: `discriminates` (healthier before good outcomes), `inverse` (healthier before bad ones — flagged, the weight goes *down*), `flat`. **Confidence** is a tier from config (`none / low / medium / high`) that needs both `n_pos` and `n_neg` ≥ a minimum *and* |d| above a threshold; a KPI with 12 positive samples and 1 negative is `none`, whatever its d. No p-values are claimed.

**Gate**: `min_outcomes_total`, `min_outcomes_per_class`, `min_accounts_with_outcomes` (config). Below any of them → `insufficient_outcomes`, the counts, and **no proposal row**. Above the gate but no KPI or pillar at `adjust_from_confidence` → `no_confident_effect`, evidence returned, no proposal row.

**Proposal**: for each KPI/pillar at or above `adjust_from_confidence`, `factor = 1 + gain × clamp(d, ±d_cap)`; others keep their current weight. Renormalise KPI weights within each pillar (cap `max_kpi_weight_within_pillar`) and pillar weights across the pillars the tenant scores (floor `min_pillar_weight`). Current weights = `CustomerConfig` (or the catalog when unset); proposed is always shown next to current, per weight, with its evidence.

**Impact**: every account's latest scored month is re-scored side by side with current and proposed weights (same KPI inputs the pipeline used). Per account before/after/band change and a summary. Computed, stored on the proposal, **never written to health rows**.

## 4. Governance — mirrors `playbooks/governance.py`

One table, `weight_calibrations`: `proposed → approved | rejected`, plus `superseded` when a newer proposal is made while one is open. Every transition is a `tool_audit_log` row (`surface='wizard_c'`, `tool='calibration.<transition>'`) carrying the caller's key kind and id; the row keeps `proposed_by`, `decided_by`, `decided_by_key_id`, the note.

**Approval writes the weights**: `CustomerConfig.pillar_weights` / `kpi_weights`, `customized_by='wizard_c:<id>'`, `config_version` bumped, `weights_origin='wizard_c'`; then a health recompute through the existing pipeline (`_process_data_impl`, mode from config, default `full_recalc` so the stored before/after becomes the actual rows — history is rewritten under one weight set rather than left with a seam; `auto` is the alternative). The recomputed rows carry `weight_source='wizard_c'` and the run id is stored on the calibration.

Wizard C fires **only** on `trigger_wizard(customer_id, 'c')` / `POST /api/calibrations/propose`. Nothing in `process_data_pipeline` calls it (policy: `policy_wizard_c_decoupled_from_process_data`), and a test asserts that.

## 5. `weight_source` — who set the weights, stored, not guessed

New `CustomerConfig.weights_origin` (Alembic `0003_wizard_c_calibration`): `vertical_default` (create_customer copied the vertical's tier default in), `customer_config` (a person set them), `wizard_c` (an approved calibration). The scorer reads it; `HealthScore.weight_source` becomes `lifecycle | vertical_default | customer_config | wizard_c | catalog` (`catalog` = no override row at all, the catalog's `weight_l2` applied directly). The migration back-fills existing rows (`customized_by` set → `customer_config`, else weights present → `vertical_default`). Rows with weights and a NULL origin after that (a direct DB write) read as `customer_config`.

## 6. Deviations from the brief, with reasons

- **`CustomerConfig.kpi_weights` was a dead column** — no scorer read it. Proposing KPI weights that nothing applies would be a guard-never-fires bug, so `generic_scorer` now takes `kpi_weight_overrides` and `vertical_health` passes the config's (nested `{pillar: {code: w}}` or flat). The lifecycle stage's KPI weights, which the pipeline computed and discarded, now reach the scorer the same way.
- **`no_confident_effect` is a third status**, not a proposal of unchanged weights: a proposal without a change has nothing to approve.
- `configure_customer_kpis` is listed in both tool registries but has no implementation in this build; the `customer_config` label therefore has no tool writer yet — noted, not built here.

## 7. Surface

Tools (`mcp_server/cs_pulse_wizard_c.py`, keyed; approve/reject/propose need write scope): `trigger_wizard(cid,'c')`, `get_calibration(cid, proposal_id=None)`, `approve_calibration(cid, proposal_id, note=None)`, `reject_calibration(cid, proposal_id, note=None)`. HTTP (`wizards/wizard_c_http.py`): `GET /api/calibrations`, `POST /api/calibrations/propose`, `POST /api/calibrations/{id}/approve|reject`. Code: `wizards/wizard_c_calibration.py`; numbers: `config/wizard_c.json`.
