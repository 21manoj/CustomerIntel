// Thin fetch wrapper over /app/api/* — session-cookie auth (credentials: 'include'),
// same functions the design doc's route table names. No business logic here, just
// the HTTP call + typed response.
import type {
  PortfolioResponse,
  ReviewQueueResponse,
  ReviewSignalRequest,
  ReviewSignalResult,
  SessionUser,
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
