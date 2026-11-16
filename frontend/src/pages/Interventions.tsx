import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { ApiError, approveIntervention, draftIntervention, getInterventions, reportIntervention, reviewIntervention } from '../api/client'
import type { Intervention, InterventionsResponse, InterventionState, ReportState } from '../api/types'
import { useAuth } from '../auth/AuthContext'

const STATE_TABS: { value: InterventionState | 'all'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'proposed', label: 'Proposed' },
  { value: 'approved', label: 'Approved' },
  { value: 'sent', label: 'Sent' },
  { value: 'closed', label: 'Closed' },
]

const REPORT_STATES: ReportState[] = ['started', 'done', 'failed', 'cancelled']

function money(value: number | null | undefined): string {
  if (value == null) return '—'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(value)
}

function stateBadge(state: InterventionState) {
  const styles: Record<InterventionState, string> = {
    proposed: 'bg-slate-100 text-slate-600',
    approved: 'bg-blue-50 text-blue-700',
    sent: 'bg-violet-50 text-violet-700',
    closed: 'bg-emerald-50 text-emerald-700',
  }
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${styles[state]}`}>{state}</span>
}

function truncate(text: string | null | undefined, max = 90): string {
  if (!text) return '—'
  return text.length > max ? `${text.slice(0, max)}…` : text
}

interface SummaryStripProps {
  data: InterventionsResponse
}

function SummaryStrip({ data }: SummaryStripProps) {
  const totals = useMemo(() => {
    return data.by_playbook.reduce(
      (acc, p) => ({
        proposed: acc.proposed + p.proposed,
        approved: acc.approved + p.approved,
        sent: acc.sent + p.sent,
        closed: acc.closed + p.closed_done + p.closed_failed + p.closed_cancelled,
        stuck: acc.stuck + p.stuck,
        delivery_problems: acc.delivery_problems + p.delivery_problems,
      }),
      { proposed: 0, approved: 0, sent: 0, closed: 0, stuck: 0, delivery_problems: 0 },
    )
  }, [data])

  const tiles: { label: string; value: number; accent?: string }[] = [
    { label: 'Proposed', value: totals.proposed },
    { label: 'Approved', value: totals.approved },
    { label: 'Sent', value: totals.sent },
    { label: 'Closed', value: totals.closed },
    { label: 'Stuck', value: totals.stuck, accent: totals.stuck > 0 ? 'text-amber-700' : undefined },
    { label: 'Delivery problems', value: totals.delivery_problems, accent: totals.delivery_problems > 0 ? 'text-red-700' : undefined },
  ]

  return (
    <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      {tiles.map((t) => (
        <div key={t.label} className="rounded-lg border border-slate-200 bg-white px-4 py-3">
          <div className={`text-xl font-semibold ${t.accent ?? 'text-slate-900'}`}>{t.value}</div>
          <div className="text-xs text-slate-500">{t.label}</div>
        </div>
      ))}
    </div>
  )
}

interface ApproveFormProps {
  onSubmit: (note: string) => Promise<void>
  onCancel: () => void
}

function ApproveForm({ onSubmit, onCancel }: ApproveFormProps) {
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await onSubmit(note)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to approve.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="mt-2 flex items-center gap-2">
      <input
        type="text"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Optional note"
        className="w-48 rounded-md border border-slate-300 px-2 py-1 text-xs"
      />
      <button
        type="submit"
        disabled={submitting}
        className="rounded-md bg-emerald-600 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
      >
        {submitting ? 'Approving…' : 'Confirm'}
      </button>
      <button type="button" onClick={onCancel} className="text-xs text-slate-500 hover:text-slate-700">
        Cancel
      </button>
      {error && <span className="text-xs text-red-600">{error}</span>}
    </form>
  )
}

interface ReportFormProps {
  onSubmit: (opts: { state: ReportState; note: string; outcomeType: string; revenue: string }) => Promise<void>
  onCancel: () => void
}

function ReportForm({ onSubmit, onCancel }: ReportFormProps) {
  const [state, setState] = useState<ReportState>('done')
  const [note, setNote] = useState('')
  const [outcomeType, setOutcomeType] = useState('')
  const [revenue, setRevenue] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await onSubmit({ state, note, outcomeType, revenue })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to report.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="mt-2 flex flex-wrap items-center gap-2">
      <select
        value={state}
        onChange={(e) => setState(e.target.value as ReportState)}
        className="rounded-md border border-slate-300 px-2 py-1 text-xs"
      >
        {REPORT_STATES.map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <input
        type="text"
        value={outcomeType}
        onChange={(e) => setOutcomeType(e.target.value)}
        placeholder="Outcome type (optional)"
        className="w-40 rounded-md border border-slate-300 px-2 py-1 text-xs"
      />
      <input
        type="number"
        value={revenue}
        onChange={(e) => setRevenue(e.target.value)}
        placeholder="Revenue (optional)"
        className="w-28 rounded-md border border-slate-300 px-2 py-1 text-xs"
      />
      <input
        type="text"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Optional note"
        className="w-40 rounded-md border border-slate-300 px-2 py-1 text-xs"
      />
      <button
        type="submit"
        disabled={submitting}
        className="rounded-md bg-slate-900 px-2 py-1 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50"
      >
        {submitting ? 'Reporting…' : 'Submit'}
      </button>
      <button type="button" onClick={onCancel} className="text-xs text-slate-500 hover:text-slate-700">
        Cancel
      </button>
      {error && <span className="text-xs text-red-600">{error}</span>}
    </form>
  )
}

export default function Interventions() {
  const { user } = useAuth()
  const [customerId, setCustomerId] = useState<number | null>(user?.customer_id ?? null)
  const [accountId, setAccountId] = useState('')
  const [stateFilter, setStateFilter] = useState<InterventionState | 'all'>('all')
  const [data, setData] = useState<InterventionsResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [openAction, setOpenAction] = useState<{ id: number; kind: 'approve' | 'report' } | null>(null)
  const [drafting, setDrafting] = useState<number | null>(null)
  const [draftError, setDraftError] = useState<string | null>(null)

  const canPickTenant = useMemo(() => user?.role === 'admin', [user])
  const canAct = user?.role === 'csm' || user?.role === 'admin'

  function reload() {
    if (customerId == null) return
    setLoading(true)
    setError(null)
    getInterventions(customerId, {
      accountId: accountId ? Number(accountId) : undefined,
      state: stateFilter === 'all' ? undefined : stateFilter,
    })
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Failed to load interventions.'))
      .finally(() => setLoading(false))
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(reload, [customerId, accountId, stateFilter])

  async function handleApprove(id: number, note: string) {
    if (customerId == null) return
    await approveIntervention(id, customerId, note || undefined)
    setOpenAction(null)
    reload()
  }

  async function handleReport(id: number, opts: { state: ReportState; note: string; outcomeType: string; revenue: string }) {
    if (customerId == null) return
    await reportIntervention(id, customerId, {
      state: opts.state,
      note: opts.note || undefined,
      outcomeType: opts.outcomeType || undefined,
      revenue: opts.revenue ? Number(opts.revenue) : undefined,
    })
    setOpenAction(null)
    reload()
  }

  async function handleReview(id: number) {
    if (customerId == null) return
    await reviewIntervention(id, customerId)
    reload()
  }

  async function handleDraft(id: number) {
    if (customerId == null) return
    setDrafting(id)
    setDraftError(null)
    try {
      await draftIntervention(id, customerId)
      reload()
    } catch (e) {
      setDraftError(e instanceof ApiError ? e.message : 'The draft could not be written.')
    } finally {
      setDrafting(null)
    }
  }

  function renderDraft(row: Intervention) {
    const plan = row.agent_plan
    if (!plan) return null
    return (
      <div className="mt-2 rounded-md border border-slate-200 bg-slate-50 p-2 text-xs not-italic text-slate-700">
        <div className="mb-1 font-medium text-slate-500">Draft, not sent. {plan.model_label}</div>
        {plan.gaps.map((g) => (
          <div key={g} className="text-amber-700">{g}</div>
        ))}
        {plan.steps.map((s) => (
          <div key={s.id} className="mt-1">
            <div className="font-medium text-slate-900">
              {s.label} <span className="font-normal text-slate-400">(approved by {s.approval.by})</span>
            </div>
            {s.draft ? (
              <div>
                {s.draft}
                <div className="text-slate-400" title={s.cites.map((c) => plan.citable[c] ?? c).join(' | ')}>
                  Based on: {s.cites.join(', ')}
                </div>
              </div>
            ) : (
              <div className="text-slate-400">No draft for this step.</div>
            )}
          </div>
        ))}
        {plan.unsupported.length > 0 && (
          <div className="mt-1 text-amber-700">
            {plan.unsupported.length} draft{plan.unsupported.length === 1 ? '' : 's'} discarded for citing nothing that was shown.
          </div>
        )}
      </div>
    )
  }

  function renderRow(row: Intervention) {
    const isActionOpen = openAction?.id === row.intervention_id
    const waitingStep = row.state === 'sent' && row.awaiting_approval === true
    return (
      <tr key={row.intervention_id} className="hover:bg-slate-50 align-top">
        <td className="px-4 py-3 font-medium text-slate-900">{row.account_name ?? `#${row.account_id}`}</td>
        <td className="px-4 py-3 text-slate-600">
          {row.playbook_id}
          {row.step && (
            <div className="mt-1 text-xs text-slate-400">
              Step {row.step.number} of {row.step.of}: {row.step.label}
            </div>
          )}
        </td>
        <td className="px-4 py-3">
          {stateBadge(row.state)}
          {row.closed_state && <div className="mt-1 text-xs text-slate-400">{row.closed_state}</div>}
          {row.approved_by?.startsWith('policy:') && <div className="mt-1 text-xs text-slate-400">approved by policy</div>}
        </td>
        <td className="px-4 py-3 text-slate-600">{row.urgency ?? '—'}</td>
        <td className="px-4 py-3 text-right text-slate-900">{money(row.exposure_revenue)}</td>
        <td className="px-4 py-3">
          <div className="flex flex-wrap gap-1">
            {row.stuck && (
              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700">
                stuck{row.stuck_days != null ? ` ${row.stuck_days}d` : ''}
              </span>
            )}
            {row.delivery_problem && (
              <span className="rounded-full bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">delivery problem</span>
            )}
            {waitingStep && (
              <span className="rounded-full bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-700">step awaits approval</span>
            )}
            {row.review?.pending && (
              <span className="rounded-full bg-violet-50 px-2 py-0.5 text-xs font-medium text-violet-700">review due</span>
            )}
            {!row.stuck && !row.delivery_problem && !waitingStep && !row.review?.pending && (
              <span className="text-xs text-slate-300">—</span>
            )}
          </div>
        </td>
        <td className="px-4 py-3 max-w-xs text-slate-600">
          <span className="italic">{truncate(row.trigger.quote)}</span>
          {renderDraft(row)}
        </td>
        <td className="px-4 py-3">
          {canAct && (row.state === 'proposed' || waitingStep) && (
            <>
              {!isActionOpen && (
                <button
                  onClick={() => setOpenAction({ id: row.intervention_id, kind: 'approve' })}
                  className="rounded-md bg-emerald-600 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-500"
                >
                  Approve
                </button>
              )}
              {isActionOpen && openAction?.kind === 'approve' && (
                <ApproveForm onSubmit={(note) => handleApprove(row.intervention_id, note)} onCancel={() => setOpenAction(null)} />
              )}
            </>
          )}
          {canAct && row.state !== 'closed' && (
            <button
              onClick={() => handleDraft(row.intervention_id)}
              disabled={drafting === row.intervention_id}
              className="mb-1 block rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              {drafting === row.intervention_id ? 'Drafting…' : row.agent_plan ? 'Redraft steps' : 'Draft steps'}
            </button>
          )}
          {canAct && row.review?.pending && (
            <button
              onClick={() => handleReview(row.intervention_id)}
              className="mb-1 block rounded-md border border-violet-300 px-2 py-1 text-xs font-medium text-violet-700 hover:bg-violet-50"
            >
              Mark reviewed
            </button>
          )}
          {canAct && row.state === 'sent' && (
            <>
              {!isActionOpen && (
                <button
                  onClick={() => setOpenAction({ id: row.intervention_id, kind: 'report' })}
                  className="rounded-md bg-slate-900 px-2 py-1 text-xs font-medium text-white hover:bg-slate-800"
                >
                  Report
                </button>
              )}
              {isActionOpen && openAction?.kind === 'report' && (
                <ReportForm onSubmit={(opts) => handleReport(row.intervention_id, opts)} onCancel={() => setOpenAction(null)} />
              )}
            </>
          )}
          {(!canAct || (row.state !== 'proposed' && row.state !== 'sent')) && <span className="text-xs text-slate-300">—</span>}
        </td>
      </tr>
    )
  }

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-slate-900">Interventions</h1>
        <div className="flex items-center gap-4">
          {canPickTenant && (
            <label className="flex items-center gap-2 text-sm text-slate-500">
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
          <label className="flex items-center gap-2 text-sm text-slate-500">
            Account ID
            <input
              type="number"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className="w-24 rounded-md border border-slate-300 px-2 py-1 text-sm"
              placeholder="all"
            />
          </label>
        </div>
      </div>

      <div className="mb-4 flex gap-2">
        {STATE_TABS.map((tab) => (
          <button
            key={tab.value}
            onClick={() => setStateFilter(tab.value)}
            className={`rounded-full px-3 py-1 text-sm font-medium ${
              stateFilter === tab.value ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {customerId == null && <p className="text-sm text-slate-500">Enter a customer ID to view interventions.</p>}

      {loading && <p className="text-sm text-slate-400">Loading…</p>}

      {error && <p className="text-sm text-red-600">{error}</p>}
      {draftError && <p className="text-sm text-red-600">{draftError}</p>}

      {data && (
        <>
          <SummaryStrip data={data} />

          {data.interventions.length === 0 ? (
            <p className="text-sm text-slate-500">No interventions for this filter.</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-2">Account</th>
                    <th className="px-4 py-2">Playbook</th>
                    <th className="px-4 py-2">State</th>
                    <th className="px-4 py-2">Urgency</th>
                    <th className="px-4 py-2 text-right">Exposure</th>
                    <th className="px-4 py-2">Flags</th>
                    <th className="px-4 py-2">Trigger</th>
                    <th className="px-4 py-2">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">{data.interventions.map(renderRow)}</tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}
