// Thin fetch wrapper over /app/api/* — session-cookie auth (credentials: 'include'),
// same functions the design doc's route table names. No business logic here, just
// the HTTP call + typed response.
import type { Intervention, InterventionsResponse, Journey, PortfolioResponse, ReportState, SessionUser } from './types'

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

export function listInterventions(customerId: number, accountId?: number, state?: string) {
  const params = new URLSearchParams({ customer_id: String(customerId) })
  if (accountId != null) params.set('account_id', String(accountId))
  if (state) params.set('state', state)
  return request<InterventionsResponse>(`/app/api/interventions?${params.toString()}`)
}

export function approveIntervention(interventionId: number, customerId: number, note?: string) {
  return request<Intervention & { payload?: unknown; journeys_rebuilt?: number }>(
    `/app/api/interventions/${interventionId}/approve`,
    { method: 'POST', body: JSON.stringify({ customer_id: customerId, note }) },
  )
}

export function reportIntervention(
  interventionId: number,
  customerId: number,
  state: ReportState,
  opts?: { note?: string; outcomeType?: string; outcomeDate?: string; revenue?: number },
) {
  return request<Intervention & { outcome?: unknown; journeys_rebuilt?: number }>(
    `/app/api/interventions/${interventionId}/report`,
    {
      method: 'POST',
      body: JSON.stringify({
        customer_id: customerId,
        state,
        note: opts?.note,
        outcome_type: opts?.outcomeType,
        outcome_date: opts?.outcomeDate,
        revenue: opts?.revenue,
      }),
    },
  )
}
