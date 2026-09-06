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
