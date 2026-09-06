// Types for the /app/api/* contract — hand-written from the actual route/service
// return shapes (backend/app_api/http.py, backend/journeys/read.py, backend/roi/priorities.py),
// not generated. Extend as later pages need more of each shape; don't front-load fields nothing reads.

export type Role = 'admin' | 'cro' | 'cfo' | 'csm'

export interface SessionUser {
  user_id: number
  customer_id: number | null
  email: string
  name: string | null
  role: Role
  allowed_customer_ids: number[] | null
  allowed_account_ids: number[] | null
}

export interface OriginBlock {
  data_origin: string
  label: string
  synthetic: boolean
  disclosure: string
}

export interface PriorityBlock {
  lens: string
  secondary_lens: string | null
  risk_factor: number
  opportunity_factor: number
  revenue_weighted: number
  basis: string
  pending_approvals: number
  cited_episodes: number
}

export interface ForecastRow {
  status: 'forecast' | 'not_run' | string
  basis?: string
  labels?: { n: number; needed: number }
  p_retain?: number
  p_retain_low?: number
  p_retain_high?: number
  p_expand?: number
  expected_arr_end?: number
  expected_arr_low?: number
  expected_arr_high?: number
  horizon_days?: number
  decision_point?: string | null
  stale?: boolean
  run_id?: number
}

export interface DataCoverage {
  kpi_layer: 'present' | 'stale' | 'not_yet' | 'none' | string
  months_scored: number
  evidence_count: number
  last_evidence_at: string | null
  contract_shape: string
}

export interface PortfolioRow {
  account_id: number
  account_name: string
  revenue: number | null
  use_cases: string[]
  contract_type: string | null
  arc_type: string | null
  state: string | null
  arc_confidence: number | null
  current_phase: string | null
  last_scored_month: string | null
  live_months: number
  last_evidence_at: string | null
  latest: {
    month: string | null
    kpi_only: number | null
    qual: number | null
    early_warning: boolean | null
    roles: string[] | null
  }
  data_coverage: DataCoverage
  phases_basis: string | null
  first_leading_warning_at: string | null
  lead_days: number | null
  episodes: number
  open_review_count: number
  priority: PriorityBlock | null
  forecast: ForecastRow | null
  updated_at: string | null
}

export type PortfolioResponse = OriginBlock & { accounts: PortfolioRow[] }

export interface ApiError {
  error: string
}

// Shape for /app/api/interventions — hand-written from backend/playbooks/governance.py's
// row_view() (per-row fields) and list_interventions() (adds account_name + the by_playbook
// rollup; account_name IS present on the list response, no client-side join needed).

export type InterventionState = 'proposed' | 'approved' | 'sent' | 'closed'
export type ClosedState = 'done' | 'failed' | 'cancelled'

export interface InterventionTrigger {
  episode_ids: string[] | null
  node_ids: number[] | null
  roles: string[] | null
  quote: string | null
  evaluated_as_of: string | null
}

export interface InterventionOutcome {
  node_id: number
  in_window: boolean | null
  expected: boolean | null
  outcome_type?: string
  revenue?: number | null
  occurred_at?: string | null
  bucket?: string
}

// ── Account detail / Journey (backend/journeys/read.py get_journey, journey_builder.py) ──

export interface Episode {
  episode_id: string
  date: string
  kind: string
  subtype: string | null
  role: string | null
  polarity: number
  source: string
  title: string
  evidence_node_ids: number[]
  sentiment: number | null
  revenue: number | null
  revenue_bucket: string | null
  meta: Record<string, unknown>
}

export interface ArcBlock {
  state: string
  arc_type: string | null
  confidence: number | null
  confidence_semantics: string | null
  matched_rule: string | null
  supporting_episode_ids: string[]
  contradicting_evidence: string[]
  alternatives: { arc_type: string; present: string[]; missing: string[] }[]
  observed_roles: string[]
  evidence_scope: string
  reason?: string
}

export interface NarrativeSentence {
  text: string
  cites: string[]
  template: string
}

export interface NarrativeChapter {
  phase: string
  from: string | null
  to: string | null
  sentences: NarrativeSentence[]
}

export interface OmittedNote {
  reason: string
  template?: string
  note?: string
  cites?: string[]
}

export interface Narrative {
  generator: string
  citation_rule: string
  chapters: NarrativeChapter[]
  omitted: OmittedNote[]
  validated: boolean
  sentence_count: number
  cited_episode_ids: string[]
}

export interface ForecastLabels {
  n: number
  positive?: number
  negative?: number
  needed: number
  per_class_needed?: number
  stratum?: string | null
  stratum_n?: number
  stratum_used?: string | null
  gate?: string
}

export interface ForecastInterval {
  p: number
  low: number
  high: number
  prior_p?: number
  interval_level?: number
  interval_semantics?: string
  mode?: string
  size_share_of_arr?: number
}

export interface ForecastRevenue {
  arr: number
  arr_known: boolean
  expected_arr_end: number
  low: number
  high: number
  nrr_contribution?: number
  nrr_contribution_low?: number
  nrr_contribution_high?: number
  loss_severity?: number
  loss_severity_basis?: string
  formula?: string
}

export interface DecisionPoint {
  at: string | null
  kind: string | null
  status: string
  days_to: number | null
  inside_horizon: boolean
}

// The full Foresight block (as read straight off the journey) — richer than the
// portfolio row's reduced ForecastRow above (see journeys/read.py _forecast_row).
export interface Forecast {
  status: 'forecast' | 'not_run' | string
  generator?: string
  lens?: string
  run_id?: number
  as_of?: string
  horizon_days?: number
  horizon_end?: string
  basis?: string
  basis_note?: string
  note?: string
  labels?: ForecastLabels
  decision_point?: DecisionPoint
  retention?: ForecastInterval
  expansion?: ForecastInterval
  revenue?: ForecastRevenue
  drivers?: { factor: string; key: string; value: unknown; note?: string }[]
  cites?: string[]
  stale?: boolean
  stale_reason?: string | null
  separation?: string
}

export interface EvidencePerson {
  name: string | null
  title: string | null
  role: string | null
  unresolved: boolean | null
}

export interface EvidenceProvenance {
  source: string | null
  source_platform: string | null
  source_event_id: string | null
  source_ref: string | null
  signal_id: string | null
  origin_platform: string | null
  evidence_tier: string | null
  tier: number | null
  classification_basis: string | null
  llm_model_version: string | null
  original_subtype: string | null
}

// One evidence node as journeys/read.py's evidence_view() shapes it — keyed by
// string(node_id) in Journey.evidence.
export interface EvidenceView {
  node_id: number
  account_id: number
  node_type: string
  subtype: string | null
  role: string | null
  occurred_at: string | null
  quote: string | null
  title: string | null
  sentiment: string | number | null
  sentiment_score: string | number | null
  polarity_conflict: boolean | null
  effective_urgency: string | null
  person: EvidencePerson | null
  provenance: EvidenceProvenance
  confidence: number | null
  requires_review: boolean
  review: Record<string, unknown> | null
  use_case: string | null
  attributes: Record<string, unknown> | null
}

export interface AccountBlock {
  use_cases: string[]
  contract_type: string | null
  renewal_date: string | null
  refresh_date: string | null
  champion: string | null
  executive_sponsor: string | null
  csm: string | null
  attributes: Record<string, unknown>
}

// get_journey(customer_id, account_id, compact=False) — journey_json plus the added
// account/evidence/open_review_count/origin fields. Not exhaustive: journey_json also
// carries phases/features/summary/etc that this page doesn't render; typed loosely
// where we only pass them through.
export type Journey = OriginBlock & {
  version: string
  account_id: number
  account_name: string
  vertical: string
  as_of: string
  last_scored_month: string | null
  live_months: string[]
  last_evidence_at: string | null
  arc: ArcBlock
  state: string
  current_phase: string | null
  phases: Record<string, unknown>[]
  phases_basis: string | null
  data_coverage: DataCoverage
  episodes: Episode[]
  leading_vs_trailing: { series?: Record<string, unknown>[]; first_leading_warning_at?: string | null; lead_days?: number | null }
  counterfactual_hooks: Record<string, unknown>[]
  forecast: Forecast | null
  narrative: Narrative | null
  account: AccountBlock
  evidence: Record<string, EvidenceView>
  open_review_count: number
  generated_at: string | null
}

// ── Interventions (backend/playbooks/governance.py row_view/list_interventions) ──

export interface InterventionNote {
  at: string
  by: string
  transition: string
  note?: string | null
}

export interface Intervention {
  intervention_id: number
  customer_id: number
  account_id: number
  account_name: string | null
  playbook_id: string
  playbook_version: string | number | null
  action_class: string
  approval_mode: string
  state: InterventionState
  closed_state: ClosedState | null
  urgency: string | number | null
  trigger: InterventionTrigger
  expected_outcome: { types: string[] | null; window_days: number | null }
  exposure_revenue: number | null
  proposed_at: string | null
  proposed_by: string | null
  approved_at: string | null
  approved_by: string | null
  approved_by_key_id: number | null
  sent_at: string | null
  delivery: Record<string, unknown> | null
  delivery_problem: boolean
  started_at: string | null
  last_report_at: string | null
  closed_at: string | null
  closed_by: string | null
  outcome: InterventionOutcome | null
  node_id: number | null
  stuck: boolean
  stuck_days: number | null
  notes: InterventionNote[]
}

export interface PlaybookRollup {
  playbook_id: string
  proposed: number
  approved: number
  sent: number
  closed_done: number
  closed_failed: number
  closed_cancelled: number
  delivery_problems: number
  stuck: number
  outcomes_reported: number
  outcomes_in_window: number
  outcomes_expected: number
  realized_revenue: number
  exposure_revenue: number
  note?: string
}

export interface InterventionsResponse {
  customer_id: number
  count: number
  interventions: Intervention[]
  stuck: number[]
  stuck_after_days: number
  by_playbook: PlaybookRollup[]
  tenant: Record<string, unknown>
}

// ── ROI / Power-of-1 (backend/roi/{priorities,power_of_1,measured}.py) ──
// Every dollar figure on these three endpoints is a `Money` object, never a bare number
// (roi/basis.py's `money()`): value can be null (see `note` for why), basis is the
// weakest link in basis_chain — measured | derived | assumed. Always render basis
// alongside the value; never print money.value alone.

export interface Money {
  value: number | null
  basis: 'measured' | 'derived' | 'assumed' | string
  basis_chain: string[]
  note?: string
}

// -- priorities.py --

export interface PriorityFactorLeading {
  label: string | null
  basis: string
  factor: number
  month: string | null
  qual: number | null
  kpi_only: number | null
  divergence: number | null
}

export interface PriorityFactorUrgency {
  level: string
  factor: number
  evidence_nodes: number
  basis: string
}

export interface PriorityRow {
  account_id: number
  account_name: string
  lens: string
  secondary_lens: string | null
  risk_factor: number
  opportunity_factor: number
  priority_factor: number
  revenue: Money
  revenue_weighted: Money
  factors: {
    phase: { phase: string; factor: number; basis?: string; note?: string }
    leading: PriorityFactorLeading
    urgency: PriorityFactorUrgency
    renewal: { days: number | null; band: string; factor: number }
    weights: Record<string, number>
  }
  opportunity: {
    factor: number
    roles: Record<string, number>
    open_expansion_interventions: number[]
    basis: string
  }
  arc_type: string | null
  state: string | null
  as_of: string | null
  open_interventions: Array<{
    intervention_id: number
    playbook_id: string
    state: string
    action_class: string
    urgency: string | null
    pending_approval: boolean
  }>
  pending_approvals: number
  cites: { episode_ids: string[]; node_ids: number[]; quote: string | null }
}

export interface PrioritiesPortfolio {
  accounts: number
  listed: number
  revenue_total: Money
  revenue_in_protect_lens: Money
  revenue_in_grow_lens: Money
  exposure_weighted: Money
  opportunity_weighted: Money
  by_lens: Record<string, number>
  pending_approvals: number
}

export type PrioritiesResponse = OriginBlock & {
  customer_id: number
  vertical: string
  account_id: number | null
  weights: Record<string, number>
  list_floor: number
  note: string
  status: 'ok' | 'no_journeys' | string
  rows: PriorityRow[]
  listed: PriorityRow[]
  portfolio: PrioritiesPortfolio | null
  hint?: string
}

// -- power_of_1.py --

export interface Po1PillarRow {
  pillar: string
  name: string | null
  weight: number
  weight_source: string
  current_score: number | null
  health_points_per_pillar_point: number
  revenue_per_pillar_point: Money
  revenue_per_one_pct_move: Money
  kpis_in_scope: number
}

export interface Po1KpiRow {
  kpi: string
  name?: string
  pillar: string
  unit?: string
  weight_l1: number
  health_points_per_kpi_point: number
  revenue_per_kpi_score_point: Money
  one_pct_value_move: {
    value_now: number
    value_after: number
    direction: string
    score_now: number
    score_after: number
    score_delta: number
    health_delta: number
    revenue_delta: Money
  } | null
}

export interface BandView {
  health_now: number
  band: string
  measurement_month: string
  revenue_at_risk: Money
  next_band: string | null
  points_to_next_band: number | null
  revenue_protected_if_next_band: Money | null
  pillar_points_to_next_band: Record<string, number> | null
}

export interface AccountPo1 {
  account_id: number
  account_name: string
  revenue: Money
  health_now: number | null
  weight_source: string
  weights_basis: string
  revenue_per_health_point: Money
  pillars: Po1PillarRow[]
  kpis: Po1KpiRow[]
  kpi_scope: string
  measured_kpis: number
  band_view: BandView | null
}

export interface Po1PortfolioPillar {
  pillar: string
  name: string | null
  accounts: number
  weight_sources: Record<string, number>
  current_score_revenue_weighted: number | null
  revenue_per_pillar_point: Money
  revenue_per_one_pct_move: Money
}

export interface Po1PortfolioKpi {
  kpi: string
  name?: string
  pillar: string
  unit?: string
  accounts: number
  measured_accounts: number
  revenue_per_kpi_score_point: Money
  one_pct_value_move_revenue: Money
}

export interface Po1PortfolioBand {
  band: string
  accounts: number
  share_at_risk: number
  revenue: Money
  revenue_at_risk: Money
}

export interface Po1Scenario {
  cs_investment_share_of_revenue: number
  investment: Money
  break_even_health_points: number
  basis: string
}

export interface Po1Portfolio {
  accounts: number
  unscored_accounts: number
  revenue_base: Money
  revenue_per_health_point: Money
  weight_sources: Record<string, number>
  pillars: Po1PortfolioPillar[]
  kpis: Po1PortfolioKpi[]
  bands: Po1PortfolioBand[]
  scenarios: Po1Scenario[]
}

export type PowerOfOneResponse = OriginBlock & {
  customer_id: number
  vertical: string
  account_id: number | null
  economics: {
    file: string
    basis: string
    horizon_months: number
    retention_sensitivity_per_health_point: { value: number; basis: string }
    revenue_at_risk_share_by_band: Record<string, number | string>
  }
  note: string
  status: 'ok' | 'no_accounts' | string
  portfolio: Po1Portfolio | null
  accounts: AccountPo1[]
}

// -- measured.py --

export interface PlaybookRoiRow {
  playbook_id: string
  proposed: number
  approved: number
  sent: number
  closed_done: number
  closed_failed: number
  closed_cancelled: number
  delivery_problems: number
  stuck: number
  outcomes_reported: number
  outcomes_in_window: number
  outcomes_expected: number
  intervention_ids: number[]
  outcome_node_ids: number[]
  realized_revenue: Money
  exposure_revenue: Money
  note: string
}

// pillar === 'unmapped' means the vertical has no pillar for these roles;
// `name` carries the human-readable reason in that case — render it, don't just say "unmapped".
export interface PillarRoiRow {
  pillar: string
  name: string
  interventions: number
  closed_done: number
  outcomes_reported: number
  intervention_ids: number[]
  outcome_node_ids: number[]
  roles: string[]
  realized_revenue: Money
  exposure_revenue: Money
  note: string
}

export interface LedgerBucketRow {
  bucket: string
  outcomes: number
  with_revenue: number
  node_ids: Array<number | null>
  linked_to_interventions: number
  revenue: Money
  linked_revenue: Money
}

export interface Ledger {
  by_bucket: LedgerBucketRow[]
  outside_buckets: { outcomes: number; subtypes: string[] }
  note: string
}

export interface Hindsight {
  status: 'ok' | 'no_run' | string
  hint?: string
  run_id?: string
  generated_at?: string
  evidence_label?: string
  interventions?: {
    basis: string
    n: number
    with_health_lift_share: number
    median_lift_pts: number
    followed_by_protected_or_expansion_share: number
  }
  intervention_rows?: Array<{
    account: string
    date: string
    title: string
    lift_pts: number
    outcomes_after: string[]
    revenue_after_protected: number | null
  }>
  realized_nrr?: Record<string, unknown>
  realized_nrr_basis?: string
  journeys?: number
}

export interface Sensitivity {
  minimum_interventions: number
  qualifying_interventions: number
  pairs: Array<{ intervention_id: number; outcome_node_id: number; lift_pts: number; revenue: number; bucket: string }>
  assumed_revenue_share_per_health_point: { value: number; basis: string }
  note: string
  status: 'ok' | 'insufficient_data' | string
  measured_revenue_per_health_point: Money
}

export type MeasuredRoiResponse = OriginBlock & {
  customer_id: number
  vertical: string
  revenue_base: Money
  interventions: { count: number; stuck: unknown[]; source: string }
  by_playbook: PlaybookRoiRow[]
  by_pillar: PillarRoiRow[]
  ledger: Ledger
  hindsight: Hindsight
  sensitivity: Sensitivity
  note: string
}

// backend/signal_engine/ingest_api.py review_queue() — one QualitativeSignal flagged
// requires_review, truncated to the UI-safe shape the route returns.
export interface ReviewQueueItem {
  signal_id: string
  account_id: number
  signal_type: string
  content: string
  sentiment: string | null
  signal_date: string | null
  source_type: string | null
  intent_signals: string[] | null
  confidence: number | null
  effective_urgency: string | null
  node_id: number | null
}

export interface ReviewQueueResponse {
  review_queue: ReviewQueueItem[]
  total: number
  page: number
}

// backend/signal_engine/review.py DECISIONS — accept clears the flag as-is, reject
// excludes the node from the journey (kept for audit), reclassify re-types it to a
// taxonomy subtype (requires `subtype`; `node_id` disambiguates a multi-node signal).
export type ReviewDecision = 'accept' | 'reject' | 'reclassify'

export interface ReviewSignalRequest {
  customer_id: number
  signal_id: string
  decision: ReviewDecision
  subtype?: string
  node_id?: number
  note?: string
}

export interface ReviewSignalResult {
  signal_id: string
  account_id: number
  decision: ReviewDecision
  nodes: Array<{ node_id: number; subtype: string; role: string | null; effective_urgency: string | null; review: string }>
  audit_ids: number[]
  requires_review: boolean
  journeys_rebuilt: number
}

// ── Settings: users (backend/app_api/users.py) ──────────────────────────

export interface UserView {
  user_id: number
  customer_id: number
  name: string | null
  email: string
  role: Role
  active: boolean
  allowed_customer_ids: number[] | null
  allowed_account_ids: number[] | null
  last_login: string | null
  has_password: boolean
}

export interface SetupTokenResponse {
  setup_token: string
  setup_token_note: string
}

export interface InviteUserResponse extends SetupTokenResponse {
  user: UserView
}

// ── Settings: playbook config (backend/playbooks/definitions.py) ────────

export interface PlaybookDef {
  id: string
  label: string
  trigger: {
    roles: string[]
    roles_match: string
    urgency_floor: string | null
    renewal_within_days: number | null
  }
  action_class: string
  approval: string
  expected_outcome: { types: string[]; window_days: number }
}

export interface TenantPlaybookConfig {
  webhook_url: string | null
  webhook_secret_set: boolean
  slack_webhook_url_set: boolean
  disabled_playbooks: string[]
  automation_level: number
  automation_level_meaning: string
  kill_switch: boolean
}

export interface PlaybookConfigResponse {
  vertical: string
  version: string | null
  source: string | null
  note?: string | null
  playbooks: PlaybookDef[]
  disabled: string[]
  tenant: TenantPlaybookConfig
}
// ── Calibrations (Wizard C) — backend/wizards/wizard_c_calibration.py ──
// Labels come from logged OUTCOME nodes, never from HealthScore (that would be circular).
// Shapes below are hand-verified against a live GET /app/api/calibrations and a live
// POST .../propose response, not guessed from the source alone.

export type CalibrationState = 'proposed' | 'approved' | 'rejected' | 'superseded'
export type ConfidenceTier = 'none' | 'low' | 'medium' | 'high'

export interface CalibrationGate {
  min_outcomes_total: number
  min_outcomes_per_class: number
  min_accounts_with_outcomes: number
}

export interface OutcomeCounts {
  total: number
  positive: number
  negative: number
  unbucketed: number
  rejected: number
  by_bucket: Record<string, number>
  accounts: number
  unfeatured?: number
}

// One KPI's or pillar's before/after evidence: n/mean per label, standardised effect size (d),
// the confidence tier it earned, and (once a proposal exists) the weight factor it was given.
export interface CalibrationEffect {
  pillar?: string
  name?: string | null
  n_pos: number
  n_neg: number
  accounts_pos: number
  accounts_neg: number
  mean_pos: number | null
  mean_neg: number | null
  effect_pts: number | null
  sd?: number
  d: number | null
  direction: 'no_data' | 'flat' | 'discriminates' | 'inverse'
  confidence: ConfidenceTier
  factor?: number
  current_weight?: number
  proposed_weight?: number
}

export interface CalibrationEvidence {
  pillars: Record<string, CalibrationEffect>
  kpis: Record<string, CalibrationEffect>
}

export interface WeightSet {
  pillar_weights: Record<string, number>
  kpi_weights: Record<string, Record<string, number>>
}

export interface CalibrationAccountImpact {
  account_id: number
  account_name: string
  month: string
  before: number
  before_recomputed: number
  stored_matches_recompute: boolean
  after: number
  delta: number
  band_before: string
  band_after: string
  pillars_before: Record<string, number>
  pillars_after: Record<string, number>
  revenue: number | null
}

export interface CalibrationImpact {
  accounts: CalibrationAccountImpact[]
  summary: {
    accounts_scored: number
    accounts_unscored: number
    mean_delta: number | null
    max_abs_delta: number | null
    band_changes: number
    stored_vs_recompute_mismatches: number
    note: string
  }
}

export interface CalibrationNote {
  at: string
  by: string
  transition: string
  note: string | null
}

// The full row (row_view() in wizard_c_calibration.py) — what GET .../calibrations?proposal_id=
// and approve/reject return, and what a successful propose() merges into its own response.
export interface CalibrationProposal {
  proposal_id: number
  customer_id: number
  vertical: string
  state: CalibrationState
  method_version: string
  catalog_version: string | null
  config_snapshot: Record<string, unknown>
  outcome_counts: OutcomeCounts
  outcome_node_ids: number[]
  current: WeightSet
  proposed: WeightSet
  evidence: CalibrationEvidence
  impact: CalibrationImpact
  proposed_at: string | null
  proposed_by: string | null
  proposed_by_key_id: number | null
  decided_at: string | null
  decided_by: string | null
  decided_by_key_id: number | null
  decision_note: string | null
  applied_config_version: string | null
  recompute: Record<string, unknown> | null
  superseded_by: number | null
  notes: CalibrationNote[]
}

export interface CalibrationProposalSummary {
  proposal_id: number
  state: CalibrationState
  proposed_at: string
  proposed_by: string | null
  decided_at: string | null
  decided_by: string | null
  outcomes: number | null
}

// current_weights() — the weights the scorer applies today, plus where they came from
// (weights_origin: 'vertical_default' | 'customer_config' | 'wizard_c', or 'catalog' when
// no CustomerConfig override exists at all).
export interface InForceWeights extends WeightSet {
  origin: string
  config_version: string | null
  warnings: string[]
}

export interface CalibrationResponse {
  customer_id: number
  vertical: string
  in_force: InForceWeights
  proposal: CalibrationProposal | null
  proposals: CalibrationProposalSummary[]
  count: number
  gate: CalibrationGate
}

// propose() never throws for an ungated tenant — it returns one of these three shapes with
// HTTP 200; a ValueError (unknown customer/vertical) is the only path that becomes an HTTP 400.
export interface CalibrationGateFailure {
  status: 'insufficient_outcomes'
  customer_id: number
  vertical: string
  outcome_counts: OutcomeCounts
  gate: CalibrationGate
  short_by: string[]
  proposal_id: null
  note: string
}

export interface CalibrationNoConfidentEffect {
  status: 'no_confident_effect'
  customer_id: number
  vertical: string
  outcome_counts: OutcomeCounts
  evidence: CalibrationEvidence
  proposal_id: null
  note: string
}

export type CalibrationProposed = CalibrationProposal & { status: 'proposed'; adjusted: number; superseded: number[] }

export type CalibrationProposeResult = CalibrationGateFailure | CalibrationNoConfidentEffect | CalibrationProposed

// backend/config/playbook_governance.json report_states — the only values report() accepts.
export const REPORT_STATES = ['started', 'done', 'failed', 'cancelled'] as const
export type ReportState = (typeof REPORT_STATES)[number]
