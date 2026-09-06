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
  state?: string
  note?: string | null
}

export interface Intervention {
  intervention_id: number
  customer_id: number
  account_id: number
  account_name?: string | null
  playbook_id: string
  playbook_version: string | null
  action_class: string
  approval_mode: string
  state: 'proposed' | 'approved' | 'sent' | 'closed' | string
  closed_state: string | null
  urgency: string | null
  trigger: {
    episode_ids: string[] | null
    node_ids: number[] | null
    roles: string[] | null
    quote: string | null
    evaluated_as_of: string | null
  }
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
  outcome: {
    node_id: number
    in_window: boolean | null
    expected: boolean | null
    outcome_type?: string
    revenue?: number | null
    occurred_at?: string | null
    bucket?: string
  } | null
  node_id: number | null
  stuck: boolean
  stuck_days: number | null
  notes: InterventionNote[]
}

export interface InterventionsResponse {
  customer_id: number
  count: number
  interventions: Intervention[]
  stuck: number[]
  stuck_after_days: number
  by_playbook: Record<string, unknown>[]
  tenant: Record<string, unknown>
}

// backend/config/playbook_governance.json report_states — the only values report() accepts.
export const REPORT_STATES = ['started', 'done', 'failed', 'cancelled'] as const
export type ReportState = (typeof REPORT_STATES)[number]
