import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { ApiError, getReviewQueue, reviewSignal } from '../api/client'
import type { ReviewDecision, ReviewQueueItem, ReviewQueueResponse } from '../api/types'
import { useAuth } from '../auth/AuthContext'

const PER_PAGE = 25

// backend/signal_engine/review.py URGENCY_ORDER — color by how close to the top.
const URGENCY_STYLES: Record<string, string> = {
  critical: 'bg-red-50 text-red-700',
  high: 'bg-amber-50 text-amber-700',
  medium: 'bg-blue-50 text-blue-700',
  low: 'bg-slate-100 text-slate-600',
}

function urgencyBadge(urgency: string | null) {
  const cls = (urgency && URGENCY_STYLES[urgency]) || 'bg-slate-100 text-slate-600'
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>{urgency ?? 'unknown'}</span>
}

function formatDate(value: string | null) {
  if (!value) return '—'
  return new Date(value).toLocaleDateString()
}

// Inline form for one row's decision. `accept`/`reject` fire immediately (with an
// optional note); `reclassify` needs a subtype first — see backend/signal_engine/review.py
// DECISIONS and review_signal(): reclassify requires `subtype`, node_id disambiguates a
// signal with several evidence nodes (not exposed here since review-queue rows are 1:1
// with a signal_id, not a node).
function RowActions({
  item,
  onSubmit,
}: {
  item: ReviewQueueItem
  onSubmit: (decision: ReviewDecision, extra: { subtype?: string; note?: string }) => Promise<void>
}) {
  const [mode, setMode] = useState<ReviewDecision | null>(null)
  const [subtype, setSubtype] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function fire(decision: ReviewDecision, extra: { subtype?: string; note?: string } = {}) {
    setSubmitting(true)
    setError(null)
    try {
      await onSubmit(decision, extra)
      setMode(null)
      setSubtype('')
      setNote('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Review failed — try again.')
    } finally {
      setSubmitting(false)
    }
  }

  function handleReclassifySubmit(e: FormEvent) {
    e.preventDefault()
    if (!subtype.trim()) return
    fire('reclassify', { subtype: subtype.trim(), note: note.trim() || undefined })
  }

  if (mode === 'reclassify') {
    return (
      <form onSubmit={handleReclassifySubmit} className="flex flex-col gap-2">
        <input
          type="text"
          required
          autoFocus
          placeholder="new subtype (taxonomy)"
          value={subtype}
          onChange={(e) => setSubtype(e.target.value)}
          className="w-44 rounded-md border border-slate-300 px-2 py-1 text-xs"
        />
        <input
          type="text"
          placeholder="note (optional)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          className="w-44 rounded-md border border-slate-300 px-2 py-1 text-xs"
        />
        <div className="flex gap-2">
          <button
            type="submit"
            disabled={submitting}
            className="rounded-md bg-slate-900 px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
          >
            {submitting ? 'Saving…' : 'Save'}
          </button>
          <button
            type="button"
            onClick={() => setMode(null)}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-600"
          >
            Cancel
          </button>
        </div>
        {error && <p className="text-xs text-red-600">{error}</p>}
      </form>
    )
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex gap-2">
        <button
          disabled={submitting}
          onClick={() => fire('accept')}
          className="rounded-md bg-emerald-600 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
        >
          Accept
        </button>
        <button
          disabled={submitting}
          onClick={() => fire('reject')}
          className="rounded-md bg-red-600 px-2 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50"
        >
          Reject
        </button>
        <button
          disabled={submitting}
          onClick={() => setMode('reclassify')}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          Reclassify
        </button>
      </div>
      {item.node_id == null && (
        <p className="text-[11px] text-amber-700">no evidence node yet — decision will fail until ingestion finishes</p>
      )}
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  )
}

export default function ReviewQueue() {
  const { user } = useAuth()
  const [customerId, setCustomerId] = useState<number | null>(user?.customer_id ?? null)
  const [accountId, setAccountId] = useState('')
  const [urgency, setUrgency] = useState('')
  const [page, setPage] = useState(1)
  const [data, setData] = useState<ReviewQueueResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const canPickTenant = useMemo(() => user?.role === 'admin', [user])
  const totalPages = data ? Math.max(1, Math.ceil(data.total / PER_PAGE)) : 1

  const load = useCallback(() => {
    if (customerId == null) return
    setLoading(true)
    setError(null)
    getReviewQueue({
      customerId,
      accountId: accountId ? Number(accountId) : undefined,
      urgency: urgency || undefined,
      page,
      perPage: PER_PAGE,
    })
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Failed to load review queue.'))
      .finally(() => setLoading(false))
  }, [customerId, accountId, urgency, page])

  useEffect(() => {
    load()
  }, [load])

  // Filters changing should reset to page 1 rather than stranding the user past the end.
  useEffect(() => {
    setPage(1)
  }, [customerId, accountId, urgency])

  async function handleDecision(item: ReviewQueueItem, decision: ReviewDecision, extra: { subtype?: string; note?: string }) {
    if (customerId == null) return
    await reviewSignal({
      customer_id: customerId,
      signal_id: item.signal_id,
      decision,
      node_id: item.node_id ?? undefined,
      ...extra,
    })
    load()
  }

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-xl font-semibold text-slate-900">Review Queue</h1>
        <div className="flex flex-wrap items-center gap-3 text-sm">
          {canPickTenant && (
            <label className="flex items-center gap-2 text-slate-500">
              Customer ID
              <input
                type="number"
                value={customerId ?? ''}
                onChange={(e) => setCustomerId(e.target.value ? Number(e.target.value) : null)}
                className="w-24 rounded-md border border-slate-300 px-2 py-1 text-sm"
                placeholder="e.g. 1"
              />
            </label>
          )}
          <label className="flex items-center gap-2 text-slate-500">
            Account ID
            <input
              type="number"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className="w-24 rounded-md border border-slate-300 px-2 py-1 text-sm"
              placeholder="all"
            />
          </label>
          <label className="flex items-center gap-2 text-slate-500">
            Urgency
            <select
              value={urgency}
              onChange={(e) => setUrgency(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1 text-sm"
            >
              <option value="">all</option>
              <option value="critical">critical</option>
              <option value="high">high</option>
              <option value="medium">medium</option>
              <option value="low">low</option>
            </select>
          </label>
        </div>
      </div>

      {customerId == null && <p className="text-sm text-slate-500">Enter a customer ID to view its review queue.</p>}

      {loading && <p className="text-sm text-slate-400">Loading…</p>}

      {error && <p className="text-sm text-red-600">{error}</p>}

      {data && (
        <>
          {data.review_queue.length === 0 ? (
            <p className="text-sm text-slate-500">Nothing needs review right now.</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-2">Content</th>
                    <th className="px-4 py-2">Account</th>
                    <th className="px-4 py-2">Type</th>
                    <th className="px-4 py-2">Sentiment</th>
                    <th className="px-4 py-2">Urgency</th>
                    <th className="px-4 py-2">Confidence</th>
                    <th className="px-4 py-2">Source</th>
                    <th className="px-4 py-2">Date</th>
                    <th className="px-4 py-2">Decision</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.review_queue.map((item) => (
                    <tr key={item.signal_id} className="align-top hover:bg-slate-50">
                      <td className="max-w-xs px-4 py-3 text-slate-700">{item.content || '—'}</td>
                      <td className="px-4 py-3 text-slate-600">{item.account_id}</td>
                      <td className="px-4 py-3 text-slate-600">{item.signal_type}</td>
                      <td className="px-4 py-3 text-slate-600">{item.sentiment ?? '—'}</td>
                      <td className="px-4 py-3">{urgencyBadge(item.effective_urgency)}</td>
                      <td className="px-4 py-3 text-slate-600">
                        {item.confidence != null ? `${Math.round(item.confidence * 100)}%` : '—'}
                      </td>
                      <td className="px-4 py-3 text-slate-600">{item.source_type ?? '—'}</td>
                      <td className="px-4 py-3 text-slate-600">{formatDate(item.signal_date)}</td>
                      <td className="px-4 py-3">
                        <RowActions item={item} onSubmit={(decision, extra) => handleDecision(item, decision, extra)} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-4 flex items-center justify-between text-sm text-slate-500">
            <span>
              {data.total} total{data.total > 0 && ` · page ${data.page} of ${totalPages}`}
            </span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="rounded-md border border-slate-300 px-3 py-1 text-xs font-medium text-slate-700 disabled:opacity-40"
              >
                Previous
              </button>
              <button
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                className="rounded-md border border-slate-300 px-3 py-1 text-xs font-medium text-slate-700 disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
