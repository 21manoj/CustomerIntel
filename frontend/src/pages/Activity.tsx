// The business event log for a tenant: every playbook event (proposed, approved, sent, reported,
// drafted) and every automation settings change, newest first, with filters and a CSV export.
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError, downloadActivityCsv, getActivity } from '../api/client'
import type { ActivityFilters, ActivityResponse } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { actorText, eventNote, eventTime, stepText } from '../components/EventTimeline'

const PAGE = 50

function money(value: number | null | undefined): string {
  if (value == null) return '—'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(value)
}

export default function Activity() {
  const { user } = useAuth()
  const [params, setParams] = useSearchParams()
  const canPickTenant = user?.role === 'admin'
  const customerId = useMemo(() => {
    const fromUrl = params.get('customer_id')
    return fromUrl && canPickTenant ? Number(fromUrl) : (user?.customer_id ?? (fromUrl ? Number(fromUrl) : null))
  }, [params, user, canPickTenant])

  const accountId = params.get('account_id') ?? ''
  const interventionId = params.get('intervention_id') ?? ''
  const playbookId = params.get('playbook_id') ?? ''
  const eventType = params.get('event_type') ?? ''
  const from = params.get('from') ?? ''
  const to = params.get('to') ?? ''
  const offset = Number(params.get('offset') ?? 0) || 0

  const [data, setData] = useState<ActivityResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)

  const filters: ActivityFilters = useMemo(
    () => ({
      accountId: accountId ? Number(accountId) : undefined,
      interventionId: interventionId ? Number(interventionId) : undefined,
      playbookId: playbookId || undefined,
      eventType: eventType || undefined,
      from: from || undefined,
      to: to || undefined,
    }),
    [accountId, interventionId, playbookId, eventType, from, to],
  )

  useEffect(() => {
    if (customerId == null) return
    let live = true
    setLoading(true)
    setError(null)
    getActivity(customerId, { ...filters, limit: PAGE, offset })
      .then((res) => live && setData(res))
      .catch((err) => live && setError(err instanceof ApiError ? err.message : 'Failed to load activity.'))
      .finally(() => live && setLoading(false))
    return () => {
      live = false
    }
  }, [customerId, filters, offset])

  // Any filter change goes back to the first page.
  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    if (key !== 'offset') next.delete('offset')
    setParams(next, { replace: true })
  }

  async function handleExport() {
    if (customerId == null) return
    setExporting(true)
    setError(null)
    try {
      await downloadActivityCsv(customerId, filters)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The export failed.')
    } finally {
      setExporting(false)
    }
  }

  const hasFilters = Boolean(accountId || interventionId || playbookId || eventType || from || to)
  const playbooks = useMemo(
    () => Array.from(new Set([...(data?.events.map((e) => e.playbook_id).filter((p): p is string => !!p) ?? []), ...(playbookId ? [playbookId] : [])])).sort(),
    [data, playbookId],
  )
  const first = data && data.total > 0 ? data.offset + 1 : 0
  const last = data ? data.offset + data.events.length : 0
  const input = 'rounded-md border border-slate-300 px-2 py-1 text-sm'

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-slate-900">Activity</h1>
        <div className="flex items-center gap-4">
          {canPickTenant && (
            <label className="flex items-center gap-2 text-sm text-slate-500">
              Customer ID
              <input type="number" value={customerId ?? ''} onChange={(e) => setFilter('customer_id', e.target.value)} className={`w-24 ${input}`} placeholder="e.g. 1" />
            </label>
          )}
          <button
            onClick={handleExport}
            disabled={exporting || customerId == null || !data || data.total === 0}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            {exporting ? 'Exporting…' : 'Export CSV'}
          </button>
        </div>
      </div>
      <p className="mb-5 text-sm text-slate-500">
        Every playbook event and automation settings change: what happened, who did it and why. Data loads and review-queue decisions
        are not in this log yet.
      </p>

      <div className="mb-4 flex flex-wrap items-end gap-3 text-sm text-slate-500">
        <label className="flex flex-col gap-1">
          Event
          <select value={eventType} onChange={(e) => setFilter('event_type', e.target.value)} className={input}>
            <option value="">All events</option>
            {data?.event_types.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          Playbook
          <select value={playbookId} onChange={(e) => setFilter('playbook_id', e.target.value)} className={input}>
            <option value="">All playbooks</option>
            {playbooks.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          Account ID
          <input type="number" value={accountId} onChange={(e) => setFilter('account_id', e.target.value)} className={`w-24 ${input}`} placeholder="all" />
        </label>
        <label className="flex flex-col gap-1">
          From
          <input type="date" value={from} onChange={(e) => setFilter('from', e.target.value)} className={input} />
        </label>
        <label className="flex flex-col gap-1">
          To
          <input type="date" value={to} onChange={(e) => setFilter('to', e.target.value)} className={input} />
        </label>
        {hasFilters && (
          <button
            onClick={() => setParams(canPickTenant && customerId != null ? { customer_id: String(customerId) } : {}, { replace: true })}
            className="pb-1 font-medium text-slate-500 hover:text-slate-800"
          >
            Clear filters
          </button>
        )}
      </div>
      {interventionId && <p className="mb-3 text-sm text-slate-600">Showing intervention #{interventionId} only.</p>}

      {customerId == null && <p className="text-sm text-slate-500">Enter a customer ID to view activity.</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}
      {loading && !data && <p className="text-sm text-slate-400">Loading…</p>}

      {data && !error && (
        <>
          {data.events.length === 0 ? (
            <p className="text-sm text-slate-500">{hasFilters ? 'No events match these filters.' : 'No events recorded yet.'}</p>
          ) : (
            <div className={`overflow-x-auto rounded-lg border border-slate-200 bg-white ${loading ? 'opacity-60' : ''}`}>
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-2">When</th>
                    <th className="px-4 py-2">Event</th>
                    <th className="px-4 py-2">Account</th>
                    <th className="px-4 py-2">Playbook</th>
                    <th className="px-4 py-2">By</th>
                    <th className="px-4 py-2 text-right">Exposure</th>
                    <th className="px-4 py-2">Detail</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.events.map((e) => (
                    <tr key={e.id} className="align-top hover:bg-slate-50">
                      <td className="whitespace-nowrap px-4 py-3 text-slate-600">{eventTime(e.at)}</td>
                      <td className="px-4 py-3">
                        <div className="font-medium text-slate-900">{e.event}</div>
                        {e.backfilled && (
                          <div className="text-xs text-slate-400" title="Rebuilt from history recorded before this log existed">
                            rebuilt from history
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {e.account_id != null ? (
                          <Link to={`/accounts/${e.account_id}?customer_id=${data.customer_id}`} className="text-slate-900 hover:underline">
                            {e.account ?? `#${e.account_id}`}
                          </Link>
                        ) : (
                          <span className="text-slate-400">Tenant setting</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-slate-600 [overflow-wrap:anywhere]">
                        {e.playbook_id ?? '—'}
                        {stepText(e) && <div className="mt-0.5 text-xs text-slate-400">{stepText(e)}</div>}
                        {e.intervention_id != null && (
                          <button onClick={() => setFilter('intervention_id', String(e.intervention_id))} className="mt-0.5 block text-xs text-slate-400 hover:text-slate-700">
                            #{e.intervention_id}
                          </button>
                        )}
                      </td>
                      <td className="min-w-[11rem] px-4 py-3 text-slate-600">{actorText(e)}</td>
                      <td className="px-4 py-3 text-right text-slate-900">{money(e.exposure_revenue)}</td>
                      <td className="min-w-[12rem] max-w-xs px-4 py-3 text-slate-600">{eventNote(e) ?? <span className="text-slate-300">—</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-3 flex items-center justify-between text-sm text-slate-500">
            <span>
              {data.total === 0 ? '0 events' : `${first} to ${last} of ${data.total} event${data.total === 1 ? '' : 's'}`}
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setFilter('offset', String(Math.max(0, offset - PAGE)))}
                disabled={offset === 0}
                className="rounded-md border border-slate-300 px-3 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40"
              >
                Newer
              </button>
              <button
                onClick={() => setFilter('offset', String(offset + PAGE))}
                disabled={last >= data.total}
                className="rounded-md border border-slate-300 px-3 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40"
              >
                Older
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
