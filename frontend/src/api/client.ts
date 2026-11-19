// Thin fetch wrapper over /app/api/* — session-cookie auth (credentials: 'include'),
// same functions the design doc's route table names. No business logic here, just
// the HTTP call + typed response.
import type {
  ActivityFilters,
  ActivityResponse,
  AgentPlan,
  AccountGraph,
  AskQuestionsResponse,
  AskResponse,
  AskTurn,
  CalibrationProposal,
  CalibrationProposeResult,
  CalibrationResponse,
  InterventionsResponse,
  InviteUserResponse,
  Journey,
  MeasuredRoiResponse,
  OnboardingCreated,
  OnboardingFileResult,
  OnboardingOptions,
  OnboardingPreflight,
  OnboardingProcessResult,
  OnboardingStatus,
  OnboardingTenantInput,
  RetentionSettings,
  ExposureResponse,
  VerticalInfo,
  PlatformAlerts,
  PlatformAuditPage,
  PlatformKey,
  PlatformOverview,
  PlatformTenantDetail,
  PlatformTenantRow,
  PlatformUser,
  PlaybookConfigResponse,
  PlaybookEvaluation,
  PortfolioResponse,
  PowerOfOneResponse,
  PrioritiesResponse,
  ReviewQueueResponse,
  ReviewSignalRequest,
  ReviewSignalResult,
  Role,
  SessionUser,
  SetupTokenResponse,
  TenantDeleted,
  TenantPlaybookConfig,
  UserView,
} from './types'

const BASE = import.meta.env.VITE_API_BASE ?? ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new ApiError(res.status, body.error ?? `${res.status} ${res.statusText}`)
  }
  return body as T
}

export function login(email: string, password: string) {
  return request<{ user: SessionUser }>('/app/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
}

export function logout() {
  return request<{ status: string }>('/app/api/auth/logout', { method: 'POST' })
}

export function setPassword(token: string, newPassword: string) {
  return request<{ status: string }>('/app/api/auth/set-password', {
    method: 'POST',
    body: JSON.stringify({ token, new_password: newPassword }),
  })
}

export function me() {
  return request<SessionUser>('/app/api/me')
}

export function getPortfolio(customerId: number) {
  return request<PortfolioResponse>(`/app/api/portfolio?customer_id=${customerId}`)
}

export function getAccount(customerId: number, accountId: number) {
  return request<Journey>(`/app/api/accounts/${accountId}?customer_id=${customerId}`)
}

export function getAccountGraph(customerId: number, accountId: number) {
  return request<AccountGraph>(`/app/api/accounts/${accountId}/graph?customer_id=${customerId}`)
}

export function getInterventions(customerId: number, opts?: { accountId?: number; state?: string }) {
  const params = new URLSearchParams({ customer_id: String(customerId) })
  if (opts?.accountId != null) params.set('account_id', String(opts.accountId))
  if (opts?.state) params.set('state', opts.state)
  return request<InterventionsResponse>(`/app/api/interventions?${params.toString()}`)
}

function activityParams(customerId: number, f?: ActivityFilters) {
  const params = new URLSearchParams({ customer_id: String(customerId) })
  if (f?.accountId != null) params.set('account_id', String(f.accountId))
  if (f?.interventionId != null) params.set('intervention_id', String(f.interventionId))
  if (f?.playbookId) params.set('playbook_id', f.playbookId)
  if (f?.eventType) params.set('event_type', f.eventType)
  if (f?.from) params.set('from', f.from)
  if (f?.to) params.set('to', f.to)
  if (f?.limit != null) params.set('limit', String(f.limit))
  if (f?.offset) params.set('offset', String(f.offset))
  return params
}

// The business event log: who did what to which playbook run, and every automation settings change.
export function getActivity(customerId: number, filters?: ActivityFilters) {
  return request<ActivityResponse>(`/app/api/activity?${activityParams(customerId, filters).toString()}`)
}

// The same rows as a CSV file (every row matching the filters, not one page). Fetched with the
// session cookie and handed to the browser as a download.
export async function downloadActivityCsv(customerId: number, filters?: ActivityFilters) {
  const params = activityParams(customerId, { ...filters, limit: undefined, offset: undefined })
  params.set('format', 'csv')
  const res = await fetch(`${BASE}/app/api/activity?${params.toString()}`, { credentials: 'include' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new ApiError(res.status, body.error ?? `${res.status} ${res.statusText}`)
  }
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = `activity_customer_${customerId}.csv`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// Why-panel B3 ("why didn't playbook X fire for this account") — a read-only look at
// what governance.evaluate() would propose or skip right now, and why. Always dry_run
// server-side; nothing here writes an intervention.
export function evaluatePlaybooks(customerId: number, accountId?: number) {
  const params = new URLSearchParams({ customer_id: String(customerId) })
  if (accountId != null) params.set('account_id', String(accountId))
  return request<PlaybookEvaluation>(`/app/api/interventions/evaluate?${params.toString()}`)
}

export function approveIntervention(interventionId: number, customerId: number, note?: string) {
  return request<{ intervention_id: number; state: string }>(`/app/api/interventions/${interventionId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ customer_id: customerId, note }),
  })
}

export function draftIntervention(interventionId: number, customerId: number) {
  return request<{ agent_plan: AgentPlan }>(`/app/api/interventions/${interventionId}/draft`, {
    method: 'POST',
    body: JSON.stringify({ customer_id: customerId }),
  })
}

export function reviewIntervention(interventionId: number, customerId: number, note?: string) {
  return request<{ intervention_id: number; state: string }>(`/app/api/interventions/${interventionId}/review`, {
    method: 'POST',
    body: JSON.stringify({ customer_id: customerId, note }),
  })
}

export function reportIntervention(
  interventionId: number,
  customerId: number,
  opts: { state: string; note?: string; outcomeType?: string; outcomeDate?: string; revenue?: number },
) {
  return request<{ intervention_id: number; state: string }>(`/app/api/interventions/${interventionId}/report`, {
    method: 'POST',
    body: JSON.stringify({
      customer_id: customerId,
      state: opts.state,
      note: opts.note,
      outcome_type: opts.outcomeType,
      outcome_date: opts.outcomeDate,
      revenue: opts.revenue,
    }),
  })
}

export function getReviewQueue(params: {
  customerId: number
  accountId?: number
  urgency?: string
  page?: number
  perPage?: number
}) {
  const q = new URLSearchParams({ customer_id: String(params.customerId) })
  if (params.accountId) q.set('account_id', String(params.accountId))
  if (params.urgency) q.set('urgency', params.urgency)
  if (params.page) q.set('page', String(params.page))
  if (params.perPage) q.set('per_page', String(params.perPage))
  return request<ReviewQueueResponse>(`/app/api/review-queue?${q.toString()}`)
}

export function reviewSignal(body: ReviewSignalRequest) {
  return request<ReviewSignalResult>('/app/api/review', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

// cfo/cro/admin only — the backend 403s every other role on all three (_finance_guard in http.py).
export function getInvestmentPriorities(customerId: number) {
  return request<PrioritiesResponse>(`/app/api/roi/priorities?customer_id=${customerId}`)
}

export function getPowerOfOne(customerId: number) {
  return request<PowerOfOneResponse>(`/app/api/roi/power-of-1?customer_id=${customerId}`)
}

export function getMeasuredRoi(customerId: number) {
  return request<MeasuredRoiResponse>(`/app/api/roi?customer_id=${customerId}`)
}

// Only for a vertical whose money basis is exposure; the backend answers 422 for any other.
export function getExposure(customerId: number) {
  return request<ExposureResponse>(`/app/api/roi/exposure?customer_id=${customerId}`)
}

// What the tenant's vertical declares: capabilities (e.g. money basis) and display labels. Every role.
export function getVertical(customerId: number) {
  return request<VerticalInfo>(`/app/api/vertical?customer_id=${customerId}`)
}

// ── Settings: users (admin) ──

export function getUsers(customerId: number) {
  return request<{ users: UserView[] }>(`/app/api/users?customer_id=${customerId}`)
}

export function inviteUser(payload: {
  customer_id: number
  email: string
  name: string
  role: Role
  allowed_account_ids?: number[]
}) {
  return request<InviteUserResponse>('/app/api/users', { method: 'POST', body: JSON.stringify(payload) })
}

export function patchUser(
  userId: number,
  payload: { role?: Role; active?: boolean; allowed_customer_ids?: number[]; allowed_account_ids?: number[] },
) {
  return request<UserView>(`/app/api/users/${userId}`, { method: 'PATCH', body: JSON.stringify(payload) })
}

export function resetUserPassword(userId: number) {
  return request<SetupTokenResponse>(`/app/api/users/${userId}/reset-password`, { method: 'POST' })
}

// ── Settings: playbook config (admin) ──

export function getPlaybookConfig(customerId: number) {
  return request<PlaybookConfigResponse>(`/app/api/playbooks/config?customer_id=${customerId}`)
}

export function configurePlaybooks(payload: {
  customer_id: number
  webhook_url?: string
  webhook_secret?: string
  disabled_playbooks?: string[]
  automation_level?: number
  automation_levels_by_class?: Record<string, number | null>
  kill_switch?: boolean
}) {
  return request<TenantPlaybookConfig>('/app/api/playbooks/config', { method: 'POST', body: JSON.stringify(payload) })
}

// ── Calibrations (Wizard C, admin) ──

export function getCalibrations(customerId: number, proposalId?: number) {
  const q = new URLSearchParams({ customer_id: String(customerId) })
  if (proposalId != null) q.set('proposal_id', String(proposalId))
  return request<CalibrationResponse>(`/app/api/calibrations?${q.toString()}`)
}

export function proposeCalibration(customerId: number) {
  return request<CalibrationProposeResult>('/app/api/calibrations/propose', {
    method: 'POST',
    body: JSON.stringify({ customer_id: customerId }),
  })
}

export function approveCalibration(proposalId: number, customerId: number, note?: string) {
  return request<CalibrationProposal>(`/app/api/calibrations/${proposalId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ customer_id: customerId, note }),
  })
}

export function rejectCalibration(proposalId: number, customerId: number, note?: string) {
  return request<CalibrationProposal>(`/app/api/calibrations/${proposalId}/reject`, {
    method: 'POST',
    body: JSON.stringify({ customer_id: customerId, note }),
  })
}

// With a customer id the set is that tenant's vertical's own (a vertical may replace a role's questions).
export function getAskQuestions(customerId?: number | null) {
  const q = customerId != null ? `?customer_id=${customerId}` : ''
  return request<AskQuestionsResponse>(`/app/api/ask/questions${q}`)
}

// history: the earlier turns of the SAME chat, held in the browser and replayed with each new
// question so a follow-up ("what about their champion?") can resolve what it refers to. Nothing
// is persisted server-side — closing the page ends the conversation.
export function askQuestion(customerId: number, question: string, accountId?: number, history?: AskTurn[]) {
  return request<AskResponse>('/app/api/ask', {
    method: 'POST',
    body: JSON.stringify({ customer_id: customerId, question, account_id: accountId, history }),
  })
}

// ── Platform console — operators only (users.platform_role); every route is refused for tenant users ──

export function getPlatformOverview() {
  return request<PlatformOverview>('/app/api/platform/overview')
}

export function getPlatformTenants() {
  return request<{ tenants: PlatformTenantRow[] }>('/app/api/platform/tenants')
}

export function getPlatformTenant(customerId: number) {
  return request<PlatformTenantDetail>(`/app/api/platform/tenants/${customerId}`)
}

export function getPlatformAudit(filters: { customerId?: string; outcome?: string; tool?: string; beforeId?: number }) {
  const q = new URLSearchParams()
  if (filters.customerId) q.set('customer_id', filters.customerId)
  if (filters.outcome) q.set('outcome', filters.outcome)
  if (filters.tool) q.set('tool', filters.tool)
  if (filters.beforeId) q.set('before_id', String(filters.beforeId))
  return request<PlatformAuditPage>(`/app/api/platform/audit?${q.toString()}`)
}

export function suspendTenant(customerId: number, reason: string) {
  return request<{ status: string }>(`/app/api/platform/tenants/${customerId}/suspend`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function reactivateTenant(customerId: number) {
  return request<{ status: string }>(`/app/api/platform/tenants/${customerId}/reactivate`, { method: 'POST' })
}

export function deleteTenant(customerId: number, confirmName: string) {
  return request<TenantDeleted>(`/app/api/platform/tenants/${customerId}/delete`, {
    method: 'POST',
    body: JSON.stringify({ confirm_name: confirmName }),
  })
}

export function getPlatformAlerts() {
  return request<PlatformAlerts>('/app/api/platform/alerts')
}

export function addAlertRecipient(protocol: string, endpoint: string) {
  return request<PlatformAlerts>('/app/api/platform/alerts/recipients', {
    method: 'POST',
    body: JSON.stringify({ protocol, endpoint }),
  })
}

export function removeAlertRecipient(id: string) {
  return request<PlatformAlerts>('/app/api/platform/alerts/recipients/remove', {
    method: 'POST',
    body: JSON.stringify({ id }),
  })
}

export function platformRevokeKey(keyId: number) {
  return request<PlatformKey>(`/app/api/platform/keys/${keyId}/revoke`, { method: 'POST' })
}

export function platformSetUserActive(userId: number, active: boolean) {
  return request<PlatformUser>(`/app/api/platform/users/${userId}/active`, {
    method: 'POST',
    body: JSON.stringify({ active }),
  })
}

export function platformResetPassword(userId: number) {
  return request<SetupTokenResponse>(`/app/api/platform/users/${userId}/reset-password`, { method: 'POST' })
}

// ── Platform console: onboarding a tenant — the tenant, its files, the check, the load, the retention settings ──

export function getOnboardingOptions() {
  return request<OnboardingOptions>('/app/api/platform/onboarding/options')
}

export function onboardCreateTenant(input: OnboardingTenantInput) {
  return request<OnboardingCreated>('/app/api/platform/onboarding/tenants', { method: 'POST', body: JSON.stringify(input) })
}

export function getOnboardingStatus(customerId: number) {
  return request<OnboardingStatus>(`/app/api/platform/onboarding/tenants/${customerId}`)
}

// dry_run validates and stages nothing; without it the file replaces whatever was staged under that name.
export function onboardFile(customerId: number, fileType: string, csvContent: string, dryRun: boolean) {
  return request<OnboardingFileResult>(`/app/api/platform/onboarding/tenants/${customerId}/files`, {
    method: 'POST',
    body: JSON.stringify({ file_type: fileType, csv_content: csvContent, dry_run: dryRun }),
  })
}

export function getOnboardingPreflight(customerId: number) {
  return request<OnboardingPreflight>(`/app/api/platform/onboarding/tenants/${customerId}/preflight`)
}

export function onboardProcess(customerId: number, mode?: string) {
  return request<OnboardingProcessResult>(`/app/api/platform/onboarding/tenants/${customerId}/process`, {
    method: 'POST',
    body: JSON.stringify({ mode }),
  })
}

export function getRetentionSettings(customerId: number) {
  return request<RetentionSettings>(`/app/api/platform/onboarding/tenants/${customerId}/settings`)
}

export function saveRetentionSettings(customerId: number, values: Record<string, number | string>, recompute: boolean) {
  return request<RetentionSettings>(`/app/api/platform/onboarding/tenants/${customerId}/settings/save`, {
    method: 'POST',
    body: JSON.stringify({ values, recompute }),
  })
}
