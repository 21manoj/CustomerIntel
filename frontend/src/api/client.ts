// Thin fetch wrapper over /app/api/* — session-cookie auth (credentials: 'include'),
// same functions the design doc's route table names. No business logic here, just
// the HTTP call + typed response.
import type {
  InviteUserResponse,
  PlaybookConfigResponse,
  PortfolioResponse,
  Role,
  SessionUser,
  SetupTokenResponse,
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
  kill_switch?: boolean
}) {
  return request<TenantPlaybookConfig>('/app/api/playbooks/config', { method: 'POST', body: JSON.stringify(payload) })
}
