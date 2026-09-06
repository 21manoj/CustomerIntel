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
export type ReportState = 'started' | 'done' | 'failed' | 'cancelled'

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
}

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
