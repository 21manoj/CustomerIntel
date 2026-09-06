// Thin fetch wrapper over /app/api/* — session-cookie auth (credentials: 'include'),
// same functions the design doc's route table names. No business logic here, just
// the HTTP call + typed response.
import type { CalibrationProposal, CalibrationProposeResult, CalibrationResponse, PortfolioResponse, SessionUser } from './types'

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
