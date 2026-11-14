// The platform console — the operator's view across every tenant (docs/design/platform-console.md).
// Shown only to users with a platform_role; the server refuses everyone else on every route here,
// so this file gates for tidiness, not for security.
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import * as api from '../api/client'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import type {
  AttentionItem,
  AttentionSeverity,
  PlatformAuditEntry,
  PlatformOverview,
  PlatformTenantDetail,
  PlatformTenantRow,
  TenantStatus,
} from '../api/types'

const SEVERITY_STYLE: Record<AttentionSeverity, string> = {
  critical: 'border-red-200 bg-red-50 text-red-800',
  warning: 'border-amber-200 bg-amber-50 text-amber-800',
  info: 'border-slate-200 bg-slate-50 text-slate-600',
}

const STATUS_STYLE: Record<TenantStatus, string> = {
  active: 'bg-emerald-50 text-emerald-700',
  suspended: 'bg-red-50 text-red-700',
  expired: 'bg-amber-50 text-amber-700',
}

const HEALTH_ORDER = ['critical', 'at_risk', 'healthy']
const HEALTH_STYLE: Record<string, string> = {
  critical: 'bg-red-500',
  at_risk: 'bg-amber-400',
  healthy: 'bg-emerald-500',
}

function when(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function day(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : '—'
}

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong — try again.'
}

function useLoad<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const reload = useCallback(() => {
    setError(null)
    load()
      .then(setData)
      .catch((e) => setError(errorText(e)))
  }, [load])
  useEffect(reload, [reload])
  return { data, error, reload }
}

function Badge({ className, children }: { className: string; children: ReactNode }) {
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${className}`}>{children}</span>
}

function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-slate-900">{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </div>
  )
}

function HealthBar({ health }: { health: Record<string, number> }) {
  const total = Object.values(health).reduce((a, b) => a + b, 0)
  if (!total) return <span className="text-xs text-slate-400">not scored</span>
  return (
    <div className="flex items-center gap-2">
      <div className="flex h-2 w-24 overflow-hidden rounded-full bg-slate-100">
        {HEALTH_ORDER.filter((k) => health[k]).map((k) => (
          <div key={k} className={HEALTH_STYLE[k]} style={{ width: `${(health[k] / total) * 100}%` }} />
        ))}
      </div>
      <span className="text-xs text-slate-500">
        {HEALTH_ORDER.filter((k) => health[k])
          .map((k) => `${health[k]} ${k.replace('_', ' ')}`)
          .join(' · ')}
      </span>
    </div>
  )
}

function AttentionList({ items, showTenant = true }: { items: AttentionItem[]; showTenant?: boolean }) {
  if (!items.length) {
    return <p className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-500">Nothing needs attention.</p>
  }
  return (
    <ul className="space-y-2">
      {items.map((i, n) => (
        <li key={n} className={`rounded-lg border p-3 ${SEVERITY_STYLE[i.severity]}`}>
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-xs font-semibold uppercase tracking-wide">{i.severity}</span>
            <span className="text-sm font-medium">{i.title}</span>
            {showTenant && i.customer_id != null && (
              <Link to={`/platform/tenants/${i.customer_id}`} className="text-sm underline">
                {i.customer_name ?? `tenant ${i.customer_id}`}
              </Link>
            )}
            {i.since && <span className="text-xs opacity-70">since {day(i.since)}</span>}
          </div>
          {i.detail && <p className="mt-1 text-xs opacity-90">{i.detail}</p>}
        </li>
      ))}
    </ul>
  )
}

// ── Overview ──

function Overview({ data }: { data: PlatformOverview }) {
  const { totals: t, system: s } = data
  const w = s.worker
  const workerText = !w.known ? 'never reported' : w.stale ? 'stopped' : w.consecutive_errors ? 'failing' : 'running'
  return (
    <div className="space-y-8">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat
          label="Tenants"
          value={t.tenants}
          sub={Object.entries(t.tenants_by_status)
            .map(([k, v]) => `${v} ${k}`)
            .join(' · ')}
        />
        <Stat label="Accounts" value={t.accounts} sub={<HealthBar health={t.account_health} />} />
        <Stat label="Active users" value={t.active_users} sub={`${t.active_keys} active API keys`} />
        <Stat
          label="Calls, last 24h"
          value={t.calls_24h.allowed + t.calls_24h.denied}
          sub={`${t.calls_24h.denied} refused · model spend 30d $${t.llm_cost_30d_usd.toFixed(2)}`}
        />
      </div>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-slate-900">
          Needs attention
          <span className="ml-2 text-sm font-normal text-slate-500">
            {data.attention_counts.critical} critical · {data.attention_counts.warning} warning · {data.attention_counts.info} info
          </span>
        </h2>
        <AttentionList items={data.attention} />
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-slate-900">System</h2>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 rounded-lg border border-slate-200 bg-white p-4 text-sm md:grid-cols-4">
          <div>
            <dt className="text-xs text-slate-500">Release</dt>
            <dd className="font-mono text-slate-900">{s.git_sha ? s.git_sha.slice(0, 7) : 'unknown'}</dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Signal worker</dt>
            <dd className={w.known && !w.stale && !w.consecutive_errors ? 'text-emerald-700' : 'text-red-700'}>
              {workerText}
              {w.known && <span className="ml-1 text-xs text-slate-500">last pass {when(w.last_pass_at)}</span>}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Journey generator</dt>
            <dd className="text-slate-900">
              v{s.journey_generator_version}
              <span className="ml-1 text-xs text-slate-500">{s.stale_journeys} journeys behind</span>
            </dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Tenants by data</dt>
            <dd className="text-slate-900">
              {Object.entries(t.tenants_by_data_origin)
                .map(([k, v]) => `${v} ${k.replace(/_/g, ' ')}`)
                .join(' · ')}
            </dd>
          </div>
        </dl>
      </section>
    </div>
  )
}

// ── Tenants ──

function Tenants({ rows }: { rows: PlatformTenantRow[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="min-w-full text-sm">
        <thead className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-4 py-2">Tenant</th>
            <th className="px-4 py-2">Status</th>
            <th className="px-4 py-2">Accounts</th>
            <th className="px-4 py-2">Users</th>
            <th className="px-4 py-2">Keys</th>
            <th className="px-4 py-2">Last data</th>
            <th className="px-4 py-2">Last run</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((r) => (
            <tr key={r.customer_id} className="hover:bg-slate-50">
              <td className="px-4 py-2">
                <Link to={`/platform/tenants/${r.customer_id}`} className="font-medium text-slate-900 hover:underline">
                  {r.name}
                </Link>
                <div className="text-xs text-slate-500">
                  #{r.customer_id} · {r.vertical ?? 'no vertical'}
                  {r.data_origin ? ` · ${r.data_origin.replace(/_/g, ' ')}` : ''}
                </div>
              </td>
              <td className="px-4 py-2">
                <Badge className={STATUS_STYLE[r.status]}>{r.status}</Badge>
              </td>
              <td className="px-4 py-2">
                <div className="text-slate-900">{r.accounts}</div>
                <HealthBar health={r.health} />
              </td>
              <td className="px-4 py-2 text-slate-700">{r.active_users}</td>
              <td className="px-4 py-2 text-slate-700">{r.active_keys}</td>
              <td className="px-4 py-2 text-slate-700">{day(r.last_activity_at)}</td>
              <td className="px-4 py-2 text-slate-700">
                {r.last_run ? (
                  <span className={r.last_run.status === 'ok' || r.last_run.status === 'success' ? '' : 'text-amber-700'}>
                    {r.last_run.status} · {day(r.last_run.started_at)}
                  </span>
                ) : (
                  '—'
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Audit ──

function AuditTable({ entries, showTenant = true }: { entries: PlatformAuditEntry[]; showTenant?: boolean }) {
  if (!entries.length) return <p className="text-sm text-slate-500">No entries.</p>
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="min-w-full text-xs">
        <thead className="border-b border-slate-200 text-left uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-3 py-2">When</th>
            {showTenant && <th className="px-3 py-2">Tenant</th>}
            <th className="px-3 py-2">Action</th>
            <th className="px-3 py-2">Caller</th>
            <th className="px-3 py-2">Result</th>
            <th className="px-3 py-2">Detail</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {entries.map((e) => (
            <tr key={e.id}>
              <td className="whitespace-nowrap px-3 py-1.5 text-slate-600">{when(e.at)}</td>
              {showTenant && (
                <td className="px-3 py-1.5 text-slate-700">
                  {e.customer_id != null ? (
                    <Link to={`/platform/tenants/${e.customer_id}`} className="hover:underline">
                      {e.customer_name ?? `#${e.customer_id}`}
                    </Link>
                  ) : (
                    '—'
                  )}
                </td>
              )}
              <td className="px-3 py-1.5 font-mono text-slate-900">{e.tool}</td>
              <td className="px-3 py-1.5 text-slate-600">
                {e.key_kind ?? '—'}
                {e.key_prefix ? ` ${e.key_prefix}…` : ''}
                {e.caller_ip ? ` · ${e.caller_ip}` : ''}
              </td>
              <td className="px-3 py-1.5">
                <Badge className={e.outcome === 'denied' ? 'bg-red-50 text-red-700' : 'bg-slate-100 text-slate-600'}>{e.outcome}</Badge>
              </td>
              <td className="max-w-md px-3 py-1.5 text-slate-600">{e.detail ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Audit({ tenants }: { tenants: PlatformTenantRow[] }) {
  const [customerId, setCustomerId] = useState('')
  const [outcome, setOutcome] = useState('')
  const [tool, setTool] = useState('')
  const [entries, setEntries] = useState<PlatformAuditEntry[]>([])
  const [next, setNext] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const fetchPage = useCallback(
    async (beforeId?: number) => {
      setError(null)
      try {
        const page = await api.getPlatformAudit({ customerId, outcome, tool, beforeId })
        setEntries((prev) => (beforeId ? [...prev, ...page.entries] : page.entries))
        setNext(page.next_before_id)
      } catch (e) {
        setError(errorText(e))
      }
    },
    [customerId, outcome, tool],
  )

  useEffect(() => {
    const t = setTimeout(() => void fetchPage(), 250) // debounce typing in the action filter
    return () => clearTimeout(t)
  }, [fetchPage])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-slate-200 bg-white p-4">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600">Tenant</label>
          <select value={customerId} onChange={(e) => setCustomerId(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">All tenants</option>
            {tenants.map((t) => (
              <option key={t.customer_id} value={t.customer_id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600">Result</label>
          <select value={outcome} onChange={(e) => setOutcome(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">Any</option>
            <option value="allowed">Allowed</option>
            <option value="denied">Refused</option>
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600">Action contains</label>
          <input
            value={tool}
            onChange={(e) => setTool(e.target.value)}
            placeholder="e.g. platform. or upload"
            className="w-56 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </div>
      </div>
      {error && <p className="text-sm text-red-600">{error}</p>}
      <AuditTable entries={entries} />
      {next && (
        <button onClick={() => void fetchPage(next)} className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
          Load older entries
        </button>
      )}
    </div>
  )
}

// ── Page: /platform ──

type Tab = 'overview' | 'tenants' | 'audit'

export default function Platform() {
  const { user } = useAuth()
  const [tab, setTab] = useState<Tab>('overview')
  const loadOverview = useCallback(() => api.getPlatformOverview(), [])
  const loadTenants = useCallback(() => api.getPlatformTenants().then((r) => r.tenants), [])
  const overview = useLoad(loadOverview)
  const tenants = useLoad(loadTenants)

  if (!user?.platform_role) return <Navigate to="/" replace />

  const tabs: { id: Tab; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'tenants', label: `Tenants${tenants.data ? ` (${tenants.data.length})` : ''}` },
    { id: 'audit', label: 'Audit log' },
  ]
  return (
    <div>
      <div className="mb-6 flex items-baseline justify-between">
        <h1 className="text-xl font-semibold text-slate-900">Platform console</h1>
        <span className="text-xs text-slate-500">
          {user.platform_role === 'operator' ? 'Operator — can act' : 'Viewer — read-only'} · everything you open here is logged
        </span>
      </div>
      <div className="mb-6 flex gap-1 border-b border-slate-200">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${
              tab === t.id ? 'border-slate-900 text-slate-900' : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {(overview.error || tenants.error) && <p className="mb-4 text-sm text-red-600">{overview.error ?? tenants.error}</p>}
      {tab === 'overview' && (overview.data ? <Overview data={overview.data} /> : !overview.error && <p className="text-sm text-slate-400">Loading…</p>)}
      {tab === 'tenants' && (tenants.data ? <Tenants rows={tenants.data} /> : !tenants.error && <p className="text-sm text-slate-400">Loading…</p>)}
      {tab === 'audit' && <Audit tenants={tenants.data ?? []} />}
    </div>
  )
}

// ── Page: /platform/tenants/:customerId ──

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mb-8">
      <h2 className="mb-3 text-lg font-semibold text-slate-900">{title}</h2>
      {children}
    </section>
  )
}

const TH = 'px-3 py-2'
const TD = 'px-3 py-1.5'
const ACTION = 'rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50'

export function PlatformTenant() {
  const { user } = useAuth()
  const { customerId } = useParams()
  const cid = Number(customerId)
  const load = useCallback(() => api.getPlatformTenant(cid), [cid])
  const { data, error, reload } = useLoad<PlatformTenantDetail>(load)
  const [reason, setReason] = useState('')
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [token, setToken] = useState<{ email: string; token: string } | null>(null)

  if (!user?.platform_role) return <Navigate to="/" replace />
  const canAct = user.platform_role === 'operator'

  async function act(fn: () => Promise<unknown>, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return
    setActionError(null)
    setBusy(true)
    try {
      await fn()
      reload()
    } catch (e) {
      setActionError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  function handleSuspend(e: FormEvent) {
    e.preventDefault()
    void act(async () => {
      await api.suspendTenant(cid, reason)
      setReason('')
    }, `Suspend ${data?.tenant.name}? Its users are signed out and its API keys stop working until you reactivate it. Nothing is deleted.`)
  }

  if (error) return <p className="text-sm text-red-600">{error}</p>
  if (!data) return <p className="text-sm text-slate-400">Loading…</p>
  const t = data.tenant

  return (
    <div>
      <Link to="/platform" className="text-sm text-slate-500 hover:text-slate-900">
        ← Platform console
      </Link>
      <div className="mb-6 mt-2 flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-slate-900">{t.name}</h1>
        <Badge className={STATUS_STYLE[t.status]}>{t.status}</Badge>
        <span className="text-sm text-slate-500">
          #{t.customer_id} · {t.vertical ?? 'no vertical'}
          {t.data_origin ? ` · ${t.data_origin.replace(/_/g, ' ')}` : ''} · created {day(t.created_at)}
          {t.expires_at ? ` · ends ${day(t.expires_at)}` : ''}
        </span>
      </div>

      {actionError && <p className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{actionError}</p>}
      {token && (
        <div className="mb-4 rounded-md border border-amber-300 bg-amber-50 p-4">
          <p className="mb-2 text-sm font-medium text-amber-900">
            One-time password token for {token.email} — shown once. Pass it to them out of band; they redeem it on the sign-in page under “I have a token”.
          </p>
          <code className="block select-all break-all rounded bg-white px-2 py-1 text-xs text-amber-950">{token.token}</code>
          <button onClick={() => setToken(null)} className="mt-2 text-xs font-medium text-amber-900 underline">
            Dismiss
          </button>
        </div>
      )}

      <div className="mb-8 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Accounts" value={data.accounts.length} />
        <Stat label="Signals" value={data.signals} />
        <Stat
          label="Interventions"
          value={Object.values(data.interventions).reduce((a, b) => a + b, 0)}
          sub={Object.entries(data.interventions)
            .map(([k, v]) => `${v} ${k}`)
            .join(' · ')}
        />
        <Stat label="Model spend, 30d" value={`$${data.llm_30d.cost_usd.toFixed(2)}`} sub={`${data.llm_30d.calls} calls`} />
      </div>

      <Section title="Needs attention">
        <AttentionList items={data.attention} showTenant={false} />
      </Section>

      <Section title="Access">
        <div className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
          {t.status === 'suspended' ? (
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-slate-700">
                Suspended {when(t.suspended_at)} — {t.suspended_reason}
              </span>
              {canAct && (
                <button disabled={busy} onClick={() => void act(() => api.reactivateTenant(cid))} className={ACTION}>
                  Reactivate
                </button>
              )}
            </div>
          ) : canAct ? (
            <form onSubmit={handleSuspend} className="flex flex-wrap items-end gap-3">
              <div className="flex-1">
                <label className="mb-1 block text-xs font-medium text-slate-600">
                  Suspend this tenant — users are signed out and API keys refused; nothing is deleted
                </label>
                <input
                  required
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Reason (recorded in the audit log)"
                  className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </div>
              <button type="submit" disabled={busy} className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50">
                Suspend
              </button>
            </form>
          ) : (
            <span className="text-slate-500">Active. Suspending a tenant needs operator access.</span>
          )}
        </div>
      </Section>

      <Section title={`Accounts (${data.accounts.length})`}>
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="min-w-full text-sm">
            <thead className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className={TH}>Account</th>
                <th className={TH}>Health</th>
                <th className={TH}>Scored</th>
                <th className={TH}>Revenue</th>
                <th className={TH}>Journey</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.accounts.map((a) => (
                <tr key={a.account_id}>
                  <td className={TD}>
                    <span className="font-medium text-slate-900">{a.name}</span>
                    <span className="ml-1 text-xs text-slate-400">#{a.account_id}</span>
                  </td>
                  <td className={TD}>
                    {a.health_score != null ? (
                      <span className="inline-flex items-center gap-1.5">
                        <span className={`h-2 w-2 rounded-full ${HEALTH_STYLE[a.health_status ?? ''] ?? 'bg-slate-300'}`} />
                        {a.health_score.toFixed(0)} <span className="text-xs text-slate-500">{(a.health_status ?? '').replace('_', ' ')}</span>
                      </span>
                    ) : (
                      <span className="text-xs text-slate-400">not scored</span>
                    )}
                  </td>
                  <td className={`${TD} text-slate-600`}>{day(a.health_month)}</td>
                  <td className={`${TD} text-slate-600`}>{a.revenue != null ? `$${Math.round(a.revenue).toLocaleString()}` : '—'}</td>
                  <td className={TD}>
                    {a.journey === 'failed' ? (
                      <Badge className="bg-red-50 text-red-700">rebuild failed</Badge>
                    ) : a.journey === 'none' ? (
                      <span className="text-xs text-slate-400">none</span>
                    ) : (
                      <span className="text-xs text-slate-500">v{a.journey_version}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title={`Users (${data.users.length})`}>
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="min-w-full text-sm">
            <thead className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className={TH}>User</th>
                <th className={TH}>Role</th>
                <th className={TH}>State</th>
                <th className={TH}>Last sign-in</th>
                {canAct && <th className={TH} />}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.users.map((u) => (
                <tr key={u.user_id}>
                  <td className={TD}>
                    <div className="font-medium text-slate-900">{u.name ?? u.email}</div>
                    <div className="text-xs text-slate-500">{u.email}</div>
                  </td>
                  <td className={`${TD} text-slate-700`}>
                    {u.role}
                    {u.platform_role && <span className="ml-1 text-xs text-slate-500">+ platform {u.platform_role}</span>}
                  </td>
                  <td className={TD}>
                    <Badge className={u.active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-600'}>{u.active ? 'active' : 'inactive'}</Badge>
                    {!u.has_password && <span className="ml-1 text-xs text-amber-700">no password set</span>}
                    {u.active && !u.scoped && !u.platform_role && <span className="ml-1 text-xs text-amber-700">no tenant scope</span>}
                  </td>
                  <td className={`${TD} text-slate-600`}>{when(u.last_login)}</td>
                  {canAct && (
                    <td className={`${TD} whitespace-nowrap text-right`}>
                      {!u.platform_role && (
                        <>
                          <button
                            disabled={busy}
                            className={ACTION}
                            onClick={() =>
                              void act(
                                () => api.platformSetUserActive(u.user_id, !u.active),
                                u.active ? `Deactivate ${u.email}? They are signed out immediately.` : undefined,
                              )
                            }
                          >
                            {u.active ? 'Deactivate' : 'Activate'}
                          </button>{' '}
                          <button
                            disabled={busy}
                            className={ACTION}
                            onClick={() =>
                              void act(async () => {
                                const r = await api.platformResetPassword(u.user_id)
                                setToken({ email: u.email, token: r.setup_token })
                              }, `Issue a one-time password token for ${u.email}?`)
                            }
                          >
                            Reset password
                          </button>
                        </>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title={`API keys (${data.keys.length})`}>
        {data.keys.length === 0 ? (
          <p className="text-sm text-slate-500">No keys.</p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="min-w-full text-sm">
              <thead className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className={TH}>Key</th>
                  <th className={TH}>Scopes</th>
                  <th className={TH}>State</th>
                  <th className={TH}>Last used</th>
                  <th className={TH}>Expires</th>
                  {canAct && <th className={TH} />}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.keys.map((k) => (
                  <tr key={k.id}>
                    <td className={TD}>
                      <div className="font-medium text-slate-900">{k.name}</div>
                      <div className="font-mono text-xs text-slate-500">
                        #{k.id} · {k.key_prefix}…
                      </div>
                    </td>
                    <td className={`${TD} text-slate-700`}>
                      {k.scopes.join(', ')}
                      {k.account_restricted && <span className="ml-1 text-xs text-slate-500">(some accounts)</span>}
                    </td>
                    <td className={TD}>
                      <Badge className={k.is_active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-600'}>{k.is_active ? 'active' : 'revoked'}</Badge>
                    </td>
                    <td className={`${TD} text-slate-600`}>{when(k.last_used_at)}</td>
                    <td className={`${TD} text-slate-600`}>{day(k.expires_at)}</td>
                    {canAct && (
                      <td className={`${TD} text-right`}>
                        {k.is_active && (
                          <button
                            disabled={busy}
                            className={ACTION}
                            onClick={() =>
                              void act(
                                () => api.platformRevokeKey(k.id),
                                `Revoke key "${k.name}"? Anything using it stops working on its next call. This cannot be undone — a replacement is issued on the box.`,
                              )
                            }
                          >
                            Revoke
                          </button>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Data processing">
        {data.runs.length === 0 && data.uploads.length === 0 ? (
          <p className="text-sm text-slate-500">No uploads or processing runs recorded.</p>
        ) : (
          <div className="grid gap-4 md:grid-cols-2">
            <div className="rounded-lg border border-slate-200 bg-white p-4">
              <h3 className="mb-2 text-sm font-medium text-slate-700">Recent runs</h3>
              <ul className="space-y-2 text-sm">
                {data.runs.map((r) => (
                  <li key={r.run_id}>
                    <span className={r.error_count ? 'font-medium text-amber-700' : 'font-medium text-slate-900'}>{r.status}</span>
                    <span className="ml-2 text-xs text-slate-500">
                      {when(r.started_at)} · {r.mode ?? 'run'}
                      {r.error_count ? ` · ${r.error_count} error${r.error_count === 1 ? '' : 's'}` : ''}
                    </span>
                    {r.first_error && <div className="text-xs text-slate-500">{r.first_error}</div>}
                  </li>
                ))}
                {data.runs.length === 0 && <li className="text-slate-500">None.</li>}
              </ul>
            </div>
            <div className="rounded-lg border border-slate-200 bg-white p-4">
              <h3 className="mb-2 text-sm font-medium text-slate-700">Recent uploads</h3>
              <ul className="space-y-2 text-sm">
                {data.uploads.map((u) => (
                  <li key={u.id}>
                    <span className="font-medium text-slate-900">{u.file_type}</span>
                    <span className="ml-2 text-xs text-slate-500">
                      {when(u.uploaded_at)} · {u.row_count ?? 0} rows · {u.consumed ? 'processed' : 'waiting'}
                      {u.warnings ? ` · ${u.warnings} warning${u.warnings === 1 ? '' : 's'}` : ''}
                    </span>
                  </li>
                ))}
                {data.uploads.length === 0 && <li className="text-slate-500">None.</li>}
              </ul>
            </div>
          </div>
        )}
      </Section>

      <Section title="Recent activity">
        <AuditTable entries={data.audit} showTenant={false} />
      </Section>
    </div>
  )
}
