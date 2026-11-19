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
  // 'operator' | 'viewer' for people who may use the platform console; null for everyone else.
  // Separate from `role` on purpose — a tenant admin is not a platform operator.
  platform_role: 'operator' | 'viewer' | null
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

/** The roster's use_cases column, as utils/csv_ingest.parse_use_cases() emits it:
 *  always objects, never bare strings — a ';'-separated cell is normalised to
 *  [{name}] on ingest. Typing this `string[]` crashed AccountDetail on any tenant
 *  that actually declared use cases. */
export interface UseCase {
  name: string
  product?: string
  status?: string
  owner?: string
  target_date?: string
}

export interface PortfolioRow {
  account_id: number
  account_name: string
  revenue: number | null
  use_cases: UseCase[]
  contract_type: string | null
  arc_type: string | null
  state: string | null
  arc_confidence: number | null
  current_phase: string | null
  current_phase_label?: string | null   // what to show: 'critical' when the intervention band has no intervention in it
  lifecycle?: 'active' | 'closed' | null
  build_error?: { error: string; at: string; note: string } | null
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

// backend/playbooks/webhook.py's delivery dict shape (DELIVERED = 'delivered';
// also 'not_configured' | 'failed'). Was untyped Record<string, unknown> on Intervention.
export interface Delivery {
  status: 'delivered' | 'not_configured' | 'failed' | string
  url_host?: string | null
  http_status?: number | null
  attempts?: number
  error?: string | null
  slack?: { status: string } & Record<string, unknown>
}

// Why-panel B3 — backend/playbooks/governance.py's evaluate() return shape.
export interface PlaybookSkip {
  account_id: number
  playbook_id: string
  reason: string
  intervention_id?: number
}

export interface PlaybookEvaluation {
  customer_id: number
  vertical: string
  dry_run: boolean
  playbooks_considered: string[]
  disabled: string[]
  proposed: Record<string, unknown>[]
  auto_approved: Record<string, unknown>[]
  skipped: PlaybookSkip[]
  accounts_evaluated: number
  status: string
  note?: string
}

// Why-panel A2 — journeys/journey_builder.py's OUTCOME episode meta.linked_evidence:
// which SIGNAL node(s) an outcome was linked to, and how honestly that link is tiered
// (utils/provenance.py's observed/inferred/synthetic + the edge-only 'unknown' tier).
export interface LinkedEvidence {
  node_id: number
  derivation: string | null
  evidence_tier: string | null
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
  label?: string | null
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
  use_cases: UseCase[]
  contract_type: string | null
  renewal_date: string | null
  refresh_date: string | null
  champion: string | null
  executive_sponsor: string | null
  csm: string | null
  attributes: Record<string, unknown>
}

// journeys/journey_builder.py detect_phases()/detect_phases_from_evidence() — one
// contiguous segment of a named phase, with the episode that triggered the transition.
export interface Phase {
  name: 'baseline' | 'deterioration' | 'intervention' | 'resolution' | string
  entered_at: string
  exited_at: string | null
  months: number
  health_start: number | null
  health_end: number | null
  trigger_episode_id: string | null
  label?: string | null   // what to show; 'critical' when the intervention band has no intervention in it
  basis?: 'evidence' | string
  negative_signals?: number
  positive_signals?: number
}

// journeys/journey_builder.py leading_series() — one scored (or live) month's
// trailing (kpi_only) vs leading (qual) reading and their divergence.
export interface LeadingVsTrailingPoint {
  month: string
  kpi_only: number | null
  qual: number | null
  divergence: number | null
  early_warning: 'early_warning' | 'recovery_watch' | 'aligned' | 'leading_only' | null
  signal_count: number
  live: boolean
  unreviewed_count: number
  contributing_episode_ids: string[]
  roles: Record<string, number>
}

export interface LeadingVsTrailing {
  series: LeadingVsTrailingPoint[]
  first_leading_warning_at: string | null
  first_trailing_warning_at: string | null
  lead_days: number | null
  window_days?: number
  divergence_warning_pts?: number
  note?: string
}

// journeys/journey_builder.py counterfactual_hooks() — health + outcomes in the
// 90 days either side of an observed decision/intervention episode.
export interface CounterfactualHook {
  episode_id: string
  date: string
  title: string
  health_before: { n: number; mean: number | null; last: number | null }
  health_after: { n: number; mean: number | null; last: number | null }
  outcomes_after: { episode_id: string; bucket: string | null; revenue: number | null }[]
}

// utils/story_arc_loader.py expected_path() — the arc template's typical trajectory,
// a prior overlay (week-relative, not this account's dates).
export interface ExpectedPathPhase {
  phase_id: string | null
  name: string | null
  starts_week: number
  duration_weeks: number
  health_start: number | null
  health_end: number | null
  description?: string | null
}

export interface ExpectedPath {
  arc_type: string
  arc_name?: string | null
  source: string
  note: string
  phases: ExpectedPathPhase[]
}

// get_journey(customer_id, account_id, compact=False) — journey_json plus the added
// account/evidence/open_review_count/origin fields.
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
  current_phase_label?: string | null   // what to show: 'critical' when the intervention band has no intervention in it
  lifecycle?: { status: 'active' | 'closed'; closed_at?: string; title?: string; subtype?: string; by?: string } | null
  phases: Phase[]
  phases_basis: string | null
  data_coverage: DataCoverage
  episodes: Episode[]
  leading_vs_trailing: LeadingVsTrailing
  counterfactual_hooks: CounterfactualHook[]
  expected_path: ExpectedPath | null
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
  delivery: Delivery | null
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
  // Steps and autonomy (playbooks/governance.py row_view). Absent on rows from an older backend.
  approval_policy?: { by_policy: boolean; level: number; reason: string; review_afterwards: boolean } | null
  agent_plan?: AgentPlan | null
  steps?: InterventionStep[]
  step?: (InterventionStep & { number: number; of: number }) | null
  awaiting_approval?: boolean
  review?: { required: boolean; reviewed_at: string | null; reviewed_by: string | null; pending: boolean }
}

/** The playbook agent's draft for an intervention. It is wording only: it approves and sends nothing. */
export interface AgentPlan {
  at: string
  by: string
  requested_by: string | null
  model: string
  model_label: string
  rationale: { text: string; cites: string[] } | null
  steps: {
    id: string
    label: string
    action_class: string
    draft: string | null
    cites: string[]
    approval: { by: string; level: number; reason: string; review_afterwards: boolean }
  }[]
  citable: Record<string, string>
  unsupported: { step_id: string | null; text: string; reason: string }[]
  missing_steps: string[]
  gaps: string[]
}

export interface InterventionStep {
  id: string
  label: string
  action_class: string
  state: string
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
  // Autonomy (playbooks/definitions.py tenant_config)
  automation_levels_by_class?: Record<string, number>
  effective_levels?: Record<string, number>
  ceiling?: number
  approved_plans?: Record<string, { version: string | null; by: string | null; at: string | null }>
  autonomy_log?: { at: string; event: string; action_class?: string; from?: number | null; to?: number; reason?: string; by?: string | null; detail?: string }[]
}

export interface PlaybookConfigResponse {
  vertical: string
  version: string | null
  source: string | null
  note?: string | null
  playbooks: PlaybookDef[]
  disabled: string[]
  tenant: TenantPlaybookConfig
  always_human?: { account_flags: string[] }
  autonomy?: { suggested_raises: { action_class: string; from: number; to: number; clean_approvals: number }[] }
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

// ── Ask AI (backend/ask_ai) ──

export interface AskQuestion {
  id: string
  scope: 'account' | 'portfolio'
  text: string
}

// GET /app/api/ask/questions — `questions` is just the caller's own role for non-admin,
// every role for admin (server-side gate, not a UI-only restriction).
export interface AskQuestionsResponse {
  roles: string[]
  questions: Record<string, AskQuestion[]>
}

export interface AskSentence {
  text: string
  cites: string[]
  unverified_numbers?: string[]
}

export interface AskUnsupported {
  text: string
  cites: string[]
  reason: 'no_citation' | 'unresolved_citation' | 'over_max_sentences' | string
  unresolved?: string[]
}

// One earlier turn of the SAME chat, sent back with the next question so a follow-up can
// resolve what it refers to. The browser holds these; the server stores nothing (this is
// in-session continuity, not the separate cross-session memory feature). account_id is the
// account that turn resolved to, and it is re-checked against the user's scope server-side.
export interface AskTurn {
  question: string
  answer: string
  account_id?: number | null
}

// POST /app/api/ask — journeys/ask_ai.answer.ask()'s full return shape.
export type AskResponse = OriginBlock & {
  question: string
  scope: 'account' | 'portfolio'
  scope_detail: Record<string, unknown> & {
    account_id?: number
    account_name?: string
    history_turns?: number
    scope_carried_from_history?: boolean
  }
  answer: string
  sentences: AskSentence[]
  citations: Record<string, unknown>
  unsupported: AskUnsupported[]
  evidence_gaps: string[]
  confidence: number | null
  citation_rule: string
  context_chars: number
  history_turns: number
  model: string
  // What to show for the model: its name, or a plain "no model" line when the fixed-rule fallback answered.
  model_label?: string
  // Plain statements that lead the answer (about what the data cannot answer, or which scope ran).
  notices?: string[]
  generator: string
}

// ── Platform console (/app/api/platform/*) — backend/platform_admin/service.py ──

export type AttentionSeverity = 'critical' | 'warning' | 'info'

export interface AttentionItem {
  severity: AttentionSeverity
  kind: string
  title: string
  detail: string | null
  customer_id: number | null
  customer_name: string | null
  since: string | null
}

export interface PlatformWorkerStatus {
  known: boolean
  last_pass_at?: string | null
  age_seconds?: number
  last_error?: string | null
  consecutive_errors?: number
  stale?: boolean
}

export interface PlatformOverview {
  totals: {
    tenants: number
    tenants_by_status: Record<string, number>
    tenants_by_vertical: Record<string, number>
    tenants_by_data_origin: Record<string, number>
    accounts: number
    active_users: number
    active_keys: number
    signals: number
    account_health: Record<string, number>
    calls_24h: { allowed: number; denied: number }
    llm_cost_30d_usd: number
  }
  system: {
    git_sha: string | null
    build_time: string | null
    journey_generator_version: string
    stale_journeys: number
    worker: PlatformWorkerStatus
    interventions: Record<string, unknown>
    time: string
  }
  attention: AttentionItem[]
  attention_counts: Record<AttentionSeverity, number>
}

export type TenantStatus = 'active' | 'suspended' | 'expired'

export interface PlatformTenantRow {
  customer_id: number
  name: string
  vertical: string | null
  data_origin: string | null
  status: TenantStatus
  suspended_at: string | null
  suspended_reason: string | null
  expires_at: string | null
  created_at: string | null
  accounts: number
  active_users: number
  active_keys: number
  health: Record<string, number>
  last_activity_at: string | null
  last_run: { status: string; started_at: string | null } | null
}

export interface PlatformUser {
  user_id: number
  name: string | null
  email: string
  role: string | null
  active: boolean
  has_password: boolean
  last_login: string | null
  scoped: boolean
  platform_role: string | null
}

export interface PlatformKey {
  id: number
  name: string
  key_prefix: string
  scopes: string[]
  is_active: boolean
  created_at: string | null
  last_used_at: string | null
  expires_at: string | null
  account_restricted: boolean
}

export interface PlatformRun {
  run_id: string
  status: string
  mode: string | null
  started_at: string | null
  finished_at: string | null
  error_count: number
  first_error: string | null
}

export interface PlatformAuditEntry {
  id: number
  at: string | null
  surface: string | null
  tool: string
  customer_id: number | null
  customer_name?: string | null
  key_kind: string | null
  key_id: number | null
  key_prefix: string | null
  caller_ip: string | null
  outcome: string
  detail: string | null
}

export interface PlatformTenantDetail {
  tenant: {
    customer_id: number
    name: string
    vertical: string | null
    data_origin: string | null
    domain: string | null
    status: TenantStatus
    suspended_at: string | null
    suspended_reason: string | null
    expires_at: string | null
    created_at: string | null
  }
  accounts: {
    account_id: number
    name: string
    status: string | null
    revenue: number | null
    health_score: number | null
    health_status: string | null
    health_month: string | null
    journey: 'built' | 'failed' | 'none'
    journey_version: string | null
  }[]
  users: PlatformUser[]
  keys: PlatformKey[]
  runs: PlatformRun[]
  uploads: { id: number; file_type: string; row_count: number | null; uploaded_at: string | null; consumed: boolean; warnings: number }[]
  signals: number
  interventions: Record<string, number>
  llm_30d: { cost_usd: number; calls: number }
  attention: AttentionItem[]
  audit: PlatformAuditEntry[]
}

export interface PlatformAuditPage {
  entries: PlatformAuditEntry[]
  next_before_id: number | null
}

// ── Platform console: alerts and tenant deletion ──

export interface AlertRecipient {
  id: string | null // null while the recipient has not confirmed
  protocol: string
  endpoint: string
  status: 'confirmed' | 'pending'
}

export interface AlertAlarm {
  name: string
  state: string // OK | ALARM | INSUFFICIENT_DATA
  reason: string | null
  since: string | null
  description: string | null
  notifies: boolean
}

export interface HostCheckEvent {
  at: string
  kind: 'failing' | 'recovered' | 'note'
  problems: string[]
}

export interface PlatformAlerts {
  configured: boolean
  topic: string | null
  error: string | null
  recipients: AlertRecipient[]
  alarms: AlertAlarm[]
  history: { at: string | null; alarm: string; summary: string | null }[]
  host_checks: { available: boolean; state: 'ok' | 'failing' | 'unknown'; events: HostCheckEvent[] }
  protocols: string[]
}

export interface TenantDeleted {
  deleted: boolean
  customer_id: number
  name: string
  rows: Record<string, number>
  total_rows: number
  archive: string
}

// The account's evidence nodes and the causal edges between them (GET /app/api/accounts/{id}/graph).
export interface GraphNode {
  node_id: number
  node_type: string
  subtype: string | null
  title: string | null
  quote: string | null
  occurred_at: string | null
  sentiment: string | null
  person: { name: string | null; title: string | null; role: string | null } | null
}

export interface GraphEdge {
  edge_id: number
  from_node_id: number
  to_node_id: number
  edge_type: string
  confidence: number | null
  derivation: string | null
  evidence_tier: string | null
}

export interface AccountGraph {
  account_id: number
  account_name: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  synthetic?: boolean
  disclosure?: string
}

// -- roi/exposure.py: the money view for a vertical whose accounts cost money when they fail --

export interface ExposureNextBand {
  min_health: number
  annual_failure_likelihood: number
  health_points_needed: number
  avoidable_exposure: Money
}

export interface ExposureAccountRow {
  account_id: number
  account_name: string
  tier: string | null
  regulated_flag: boolean
  regulated_note?: string
  contract_value: Money
  failure_cost: Money
  failure_cost_source: string | null
  failure_event: string | null
  health_now: number | null
  measurement_month: string | null
  annual_failure_likelihood: number | null
  expected_annual_exposure: Money | null
  next_band: ExposureNextBand | null
  not_priced: string | null
}

export interface ExposureTierRow {
  tier: string
  accounts: number
  priced_accounts: number
  regulated_flag_accounts: number
  contract_value: Money
  expected_annual_exposure: Money
}

export interface ExposurePlaybookRow {
  playbook_id: string
  interventions: number
  /** approved runs: the ones cost to date counts */
  runs: number
  closed_done: number
  estimated_hours: number
  estimated_cost_per_execution: Money
  estimated_cost_to_date: Money
  exposure_avoided: Money
  closed_with_health_before_and_after: number
}

export interface ExposureResponse {
  customer_id: number
  vertical: string
  account_id: number | null
  synthetic: boolean
  disclosure: string
  money_basis: 'exposure'
  status: 'ok' | 'no_accounts'
  note: string
  economics: { file: string; basis: string; horizon_months: number; tiers: string[] }
  portfolio: {
    accounts: number
    priced_accounts: number
    not_priced: Record<string, number[]>
    regulated_flag_accounts: number
    contract_value: Money
    expected_annual_exposure: Money
    avoidable_exposure_next_band: Money
    by_tier: ExposureTierRow[]
  } | null
  playbooks: {
    status: string
    reason?: string
    rows: ExposurePlaybookRow[]
    playbooks_without_a_cost_entry?: string[]
    estimated_cost_to_date?: Money
    note?: string
  } | null
  accounts: ExposureAccountRow[]
}

// -- /app/api/vertical: what the tenant's vertical declares --

export interface VerticalLabels {
  account: string
  accounts: string
  revenue: string
  recurring_revenue: string
  renewal: string
}

export interface VerticalInfo {
  customer_id: number
  vertical: string
  capabilities: { money_basis: 'revenue' | 'exposure'; revenue_economics: boolean; [k: string]: unknown }
  labels: VerticalLabels
}

// -- /app/api/activity: the business event log (playbook events and automation settings) --

export interface BusinessEvent {
  id: number
  at: string | null
  event_type: string
  event: string
  actor_kind: string | null
  actor: string | null
  account_id: number | null
  account: string | null
  intervention_id: number | null
  playbook_id: string | null
  playbook_version: string | null
  step: string | null
  step_number: number | null
  steps_total: number | null
  action_class: string | null
  exposure_revenue: number | null
  state_before: string | null
  state_after: string | null
  reason: string | null
  delivery_status: string | null
  outcome_type: string | null
  outcome_node_id: number | null
  trigger_key: string | null
  backfilled: boolean
  detail: Record<string, unknown> | null
}

export interface ActivityFilters {
  accountId?: number
  interventionId?: number
  playbookId?: string
  eventType?: string
  from?: string
  to?: string
  limit?: number
  offset?: number
}

export interface ActivityResponse {
  customer_id: number
  total: number
  limit: number
  offset: number
  events: BusinessEvent[]
  event_types: { id: string; label: string }[]
  coverage: string
  read_scope: 'tenant' | 'own_accounts'
}

// ── Platform console: tenant onboarding (backend/platform_admin/onboarding.py) ──

export interface OnboardingTier {
  id: string
  name: string
  description: string | null
  kpi_count: number | null
  prerequisites: unknown
}

export interface OnboardingVertical {
  id: string
  description: string | null
  money_basis: string | null
  tiers: { default: string | null; choices: OnboardingTier[] }
}

export interface OnboardingFileSpec {
  file_type: string
  label: string
  expected: boolean
  what: string | null
  required_columns: string[]
  optional_columns: string[]
}

export interface OnboardingOptions {
  verticals: OnboardingVertical[]
  data_origins: { id: string; label: string; synthetic: boolean; disclosure: string | null }[]
  default_data_origin: string | null
  files: OnboardingFileSpec[]
  max_csv_bytes: number | null
  modes: string[]
  can_write: boolean
}

export interface OnboardingTenantInput {
  name: string
  domain: string
  vertical: string
  tier?: string
  data_origin?: string
  admin_name: string
  admin_email: string
}

export interface OnboardingCreated {
  customer_id: number
  name: string
  domain: string
  vertical: string
  data_origin: string
  disclosure: string | null
  admin_email: string
  admin_setup_token: string
  setup_token_expiry_minutes: number | null
  api_key: string | null
}

export interface OnboardingFileResult {
  status: 'success' | 'validation_error' | 'error'
  file_type: string
  dry_run: boolean
  staged: boolean
  row_count?: number
  columns_found?: string[]
  missing_required?: string[]
  errors?: string[]
  warnings?: string[]
  message?: string
}

export interface PlatformRunView {
  run_id: string
  status: string
  mode: string | null
  started_at: string | null
  finished_at: string | null
  error_count: number
  first_error: string | null
}

export interface RetentionGroup {
  n_accounts: number
  cohort_accounts: number
  new_accounts_excluded: number
  starting_arr: number
  lost: number
  expansion: number
  protected_narrative: number
  net_arr: number
  nrr: number | null
  grr: number | null
  withheld: boolean
  withheld_reason: string | null
  observed_months: number
  amount_coverage: number
  outcomes_without_amount: number
  outcomes_outside_window: number
}

export interface RetentionResult {
  run_id: string
  generated_at: string | null
  basis: string
  window: { start: string; end: string; months: number; anchor: string }
  settings: Record<string, number | string>
  portfolio: RetentionGroup
}

export interface OnboardingStatus {
  tenant: {
    customer_id: number
    name: string
    domain: string
    vertical: string
    data_origin: string
    status: TenantStatus
    created_at: string | null
  }
  admins: { user_id: number; email: string; name: string; password_set: boolean }[]
  files: { file_type: string; label: string; expected: boolean; staged: boolean; row_count: number | null; staged_at: string | null }[]
  missing_expected: string[]
  pending_files: string[]
  accounts: number
  last_run: PlatformRunView | null
  retention: RetentionResult | null
  step: 'upload' | 'check' | 'processing' | 'done'
}

export interface OnboardingPreflight {
  ok: boolean
  summary: string[]
  problems: { severity: 'error' | 'warning' | 'info'; code: string; message: string }[]
  problem_count: number
  error_count: number
  staged_files: string[]
  files?: Record<string, { rows: number; columns_read: string[]; columns_kept_as_attributes: string[]; missing_required_columns: string[] }>
  accounts?: { new: number; already_loaded: number }
}

export interface OnboardingProcessResult {
  status: string
  mode: string | null
  accounts: number | null
  kpi_measurements: number | null
  csv_files_processed: unknown
  steps_completed: string[] | null
  errors: unknown[] | null
  data_warnings: unknown[] | null
  duration_s: number | null
  message: string | null
}

export interface RetentionField {
  key: string
  type: 'int' | 'float' | 'choice'
  min?: number
  max?: number
  choices?: string[]
  label: string
  help: string
  default: number | string
  value: number | string
  overridden: boolean
}

export interface RetentionSettings {
  fields: RetentionField[]
  defaults: Record<string, number | string>
  overrides: Record<string, number | string>
  effective: Record<string, number | string>
  result: RetentionResult | null
  recompute?: { status: string; reason: string | null }
}
