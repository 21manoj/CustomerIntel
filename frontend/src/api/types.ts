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
