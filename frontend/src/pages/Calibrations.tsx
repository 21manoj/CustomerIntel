import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError, approveCalibration, getCalibrations, proposeCalibration, rejectCalibration } from '../api/client'
import type { CalibrationEffect, CalibrationProposal, CalibrationProposalSummary, CalibrationResponse } from '../api/types'
import { useAuth } from '../auth/AuthContext'

const ORIGIN_LABEL: Record<string, string> = {
  vertical_default: 'Vertical default',
  customer_config: 'Set by a person',
  wizard_c: 'Approved calibration (Wizard C)',
  catalog: 'Catalog default (no override on file)',
}

const STATE_STYLES: Record<string, string> = {
  proposed: 'bg-blue-50 text-blue-700',
  approved: 'bg-emerald-50 text-emerald-700',
  rejected: 'bg-red-50 text-red-700',
  superseded: 'bg-slate-100 text-slate-500',
}

const CONFIDENCE_STYLES: Record<string, string> = {
  high: 'bg-emerald-50 text-emerald-700',
  medium: 'bg-blue-50 text-blue-700',
  low: 'bg-amber-50 text-amber-700',
  none: 'bg-slate-100 text-slate-500',
}

function stateBadge(state: string) {
  const cls = STATE_STYLES[state] || 'bg-slate-100 text-slate-600'
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>{state}</span>
}

function confidenceBadge(tier: string) {
  const cls = CONFIDENCE_STYLES[tier] || 'bg-slate-100 text-slate-600'
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>{tier}</span>
}

function pct(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${Math.round(v * 100)}%`
}

function fmt(v: number | null | undefined, digits = 1): string {
  return v == null ? '—' : v.toFixed(digits)
}

function dt(v: string | null): string {
  if (!v) return '—'
  return new Date(v).toLocaleString()
}

// Pillar/KPI weight comparison table — current vs proposed, with the evidence (n/effect/confidence)
// that drove the change when one exists. Rows without a confident effect keep their current weight
// (wizard_c_calibration._propose_weights) and show as unchanged, not hidden — a human should see
// what wasn't touched too.
function WeightTable({
  title,
  currentWeights,
  proposedWeights,
  evidence,
}: {
  title: string
  currentWeights: Record<string, number>
  proposedWeights: Record<string, number> | null
  evidence: Record<string, CalibrationEffect>
}) {
  const codes = useMemo(() => Object.keys(currentWeights).sort(), [currentWeights])
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-3 py-2">{title}</th>
            <th className="px-3 py-2 text-right">Current</th>
            {proposedWeights && <th className="px-3 py-2 text-right">Proposed</th>}
            <th className="px-3 py-2 text-right">Effect (pts)</th>
            <th className="px-3 py-2 text-right">n+/n-</th>
            <th className="px-3 py-2">Confidence</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {codes.map((code) => {
            const ev = evidence[code]
            const cur = currentWeights[code]
            const prop = proposedWeights?.[code]
            const changed = proposedWeights != null && prop != null && Math.abs(prop - cur) > 1e-6
            return (
              <tr key={code} className={changed ? 'bg-amber-50/40' : undefined}>
                <td className="px-3 py-2 font-medium text-slate-900">
                  {code}
                  {ev?.name && <span className="ml-1 font-normal text-slate-500">{ev.name}</span>}
                </td>
                <td className="px-3 py-2 text-right text-slate-600">{pct(cur)}</td>
                {proposedWeights && (
                  <td className={`px-3 py-2 text-right ${changed ? 'font-semibold text-slate-900' : 'text-slate-600'}`}>
                    {prop != null ? pct(prop) : '—'}
                  </td>
                )}
                <td className="px-3 py-2 text-right text-slate-600">{ev ? fmt(ev.effect_pts) : '—'}</td>
                <td className="px-3 py-2 text-right text-slate-600">{ev ? `${ev.n_pos}/${ev.n_neg}` : '—'}</td>
                <td className="px-3 py-2">{ev ? confidenceBadge(ev.confidence) : '—'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function ProposalDetail({
  proposal,
  onDecided,
}: {
  proposal: CalibrationProposal
  onDecided: (updated: CalibrationProposal) => void
}) {
  const { user } = useAuth()
  const [note, setNote] = useState('')
  const [deciding, setDeciding] = useState<'approve' | 'reject' | null>(null)
  const [decisionError, setDecisionError] = useState<string | null>(null)

  async function decide(kind: 'approve' | 'reject') {
    setDeciding(kind)
    setDecisionError(null)
    try {
      const fn = kind === 'approve' ? approveCalibration : rejectCalibration
      const updated = await fn(proposal.proposal_id, proposal.customer_id, note.trim() || undefined)
      onDecided(updated)
      setNote('')
    } catch (err) {
      setDecisionError(err instanceof ApiError ? err.message : `Failed to ${kind} the proposal.`)
    } finally {
      setDeciding(null)
    }
  }

  const oc = proposal.outcome_counts
  const canDecide = proposal.state === 'proposed'

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-slate-900">Proposal #{proposal.proposal_id}</h3>
          <p className="text-xs text-slate-500">
            Method {proposal.method_version} · Catalog {proposal.catalog_version ?? '—'} · Proposed by{' '}
            {proposal.proposed_by ?? 'unknown'} at {dt(proposal.proposed_at)}
          </p>
        </div>
        {stateBadge(proposal.state)}
      </div>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <div className="text-xs uppercase tracking-wide text-slate-500">Outcomes</div>
          <div className="text-lg font-semibold text-slate-900">{oc.total}</div>
          <div className="text-xs text-slate-500">
            +{oc.positive} / -{oc.negative} across {oc.accounts} accounts
          </div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <div className="text-xs uppercase tracking-wide text-slate-500">Unfeatured</div>
          <div className="text-lg font-semibold text-slate-900">{oc.unfeatured ?? 0}</div>
          <div className="text-xs text-slate-500">no KPI row in the feature window</div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <div className="text-xs uppercase tracking-wide text-slate-500">Accounts scored</div>
          <div className="text-lg font-semibold text-slate-900">{proposal.impact.summary.accounts_scored}</div>
          <div className="text-xs text-slate-500">{proposal.impact.summary.accounts_unscored} unscored</div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <div className="text-xs uppercase tracking-wide text-slate-500">Health impact</div>
          <div className="text-lg font-semibold text-slate-900">{fmt(proposal.impact.summary.mean_delta, 2)} avg</div>
          <div className="text-xs text-slate-500">
            max |Δ| {fmt(proposal.impact.summary.max_abs_delta, 2)} · {proposal.impact.summary.band_changes} band changes
          </div>
        </div>
      </div>

      <div>
        <h4 className="mb-2 text-sm font-semibold text-slate-700">Pillar weights</h4>
        <WeightTable
          title="Pillar"
          currentWeights={proposal.current.pillar_weights}
          proposedWeights={proposal.proposed.pillar_weights}
          evidence={proposal.evidence.pillars}
        />
      </div>

      <div>
        <h4 className="mb-2 text-sm font-semibold text-slate-700">KPI weights</h4>
        <div className="space-y-4">
          {Object.keys(proposal.current.kpi_weights)
            .sort()
            .map((p) => (
              <div key={p}>
                <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">{p}</div>
                <WeightTable
                  title="KPI"
                  currentWeights={proposal.current.kpi_weights[p]}
                  proposedWeights={proposal.proposed.kpi_weights[p] ?? null}
                  evidence={proposal.evidence.kpis}
                />
              </div>
            ))}
        </div>
      </div>

      {proposal.notes.length > 0 && (
        <div>
          <h4 className="mb-2 text-sm font-semibold text-slate-700">History</h4>
          <ul className="space-y-1 text-xs text-slate-500">
            {proposal.notes.map((n, i) => (
              <li key={i}>
                {dt(n.at)} — {n.transition} by {n.by}
                {n.note ? `: ${n.note}` : ''}
              </li>
            ))}
          </ul>
        </div>
      )}

      {proposal.decision_note && (
        <p className="text-sm text-slate-600">
          Decision note ({proposal.decided_by ?? 'unknown'} at {dt(proposal.decided_at)}): {proposal.decision_note}
        </p>
      )}

      {canDecide && user?.role === 'admin' && (
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <label className="mb-1 block text-sm font-medium text-slate-700" htmlFor="decision-note">
            Note (optional)
          </label>
          <textarea
            id="decision-note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            className="mb-3 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
            placeholder="Why you're approving or rejecting this…"
          />
          {decisionError && <p className="mb-3 text-sm text-red-600">{decisionError}</p>}
          <div className="flex gap-3">
            <button
              onClick={() => decide('approve')}
              disabled={deciding !== null}
              className="rounded-md bg-emerald-600 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
            >
              {deciding === 'approve' ? 'Approving…' : 'Approve'}
            </button>
            <button
              onClick={() => decide('reject')}
              disabled={deciding !== null}
              className="rounded-md border border-red-300 px-3 py-2 text-sm font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
            >
              {deciding === 'reject' ? 'Rejecting…' : 'Reject'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export default function Calibrations() {
  const { user } = useAuth()
  const [customerId, setCustomerId] = useState<number | null>(user?.customer_id ?? null)
  const [data, setData] = useState<CalibrationResponse | null>(null)
  const [detail, setDetail] = useState<CalibrationProposal | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [proposing, setProposing] = useState(false)
  const [proposeMessage, setProposeMessage] = useState<{ kind: 'success' | 'gate' | 'no_effect' | 'error'; text: string } | null>(
    null,
  )

  const load = useCallback(
    async (cid: number) => {
      setLoading(true)
      setError(null)
      try {
        const res = await getCalibrations(cid)
        setData(res)
        setSelectedId(res.proposal?.proposal_id ?? null)
        setDetail(res.proposal)
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Failed to load calibrations.')
      } finally {
        setLoading(false)
      }
    },
    [],
  )

  useEffect(() => {
    if (customerId != null) load(customerId)
  }, [customerId, load])

  async function selectProposal(id: number) {
    if (customerId == null) return
    setSelectedId(id)
    if (data?.proposal?.proposal_id === id) {
      setDetail(data.proposal)
      return
    }
    setDetailLoading(true)
    try {
      const res = await getCalibrations(customerId, id)
      setDetail(res.proposal)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load proposal detail.')
    } finally {
      setDetailLoading(false)
    }
  }

  async function handlePropose() {
    if (customerId == null) return
    setProposing(true)
    setProposeMessage(null)
    try {
      const result = await proposeCalibration(customerId)
      if (result.status === 'proposed') {
        setProposeMessage({
          kind: 'success',
          text: `Proposal #${result.proposal_id} created from ${result.outcome_counts.total} outcomes — ${result.adjusted} weight${
            result.adjusted === 1 ? '' : 's'
          } adjusted.`,
        })
      } else if (result.status === 'insufficient_outcomes') {
        const g = result.gate
        const oc = result.outcome_counts
        setProposeMessage({
          kind: 'gate',
          text:
            `Not enough outcome data yet: needs ≥${g.min_outcomes_total} outcomes (≥${g.min_outcomes_per_class} per class) ` +
            `across ≥${g.min_accounts_with_outcomes} accounts — this tenant has ${oc.total} (${oc.positive} positive / ` +
            `${oc.negative} negative) across ${oc.accounts} accounts.`,
        })
      } else {
        setProposeMessage({
          kind: 'no_effect',
          text: result.note ?? 'Outcomes were logged, but no KPI or pillar reached the confidence threshold needed to adjust weights.',
        })
      }
      await load(customerId)
    } catch (err) {
      setProposeMessage({ kind: 'error', text: err instanceof ApiError ? err.message : 'Failed to propose a calibration.' })
    } finally {
      setProposing(false)
    }
  }

  function handleDecided(updated: CalibrationProposal) {
    setDetail(updated)
    if (customerId != null) load(customerId)
  }

  if (user && user.role !== 'admin') {
    return (
      <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
        Calibrations is an admin-only page.
      </div>
    )
  }

  const canPickTenant = user?.role === 'admin'

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Calibrations</h1>
          <p className="text-sm text-slate-500">
            Wizard C proposes KPI/pillar weight changes from logged outcomes — never from HealthScore itself — and nothing changes
            until you approve it here.
          </p>
        </div>
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
      </div>

      {customerId == null && <p className="text-sm text-slate-500">Enter a customer ID to view its calibrations.</p>}
      {loading && <p className="text-sm text-slate-400">Loading…</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}

      {data && (
        <>
          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-700">Weights in force</h2>
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
                {ORIGIN_LABEL[data.in_force.origin] ?? data.in_force.origin}
                {data.in_force.config_version ? ` · v${data.in_force.config_version}` : ''}
              </span>
            </div>
            {data.in_force.warnings.map((w, i) => (
              <p key={i} className="mb-2 text-xs text-amber-700">
                {w}
              </p>
            ))}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-6">
              {Object.entries(data.in_force.pillar_weights)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([p, w]) => (
                  <div key={p} className="rounded-md bg-slate-50 px-3 py-2 text-center">
                    <div className="text-xs font-medium uppercase tracking-wide text-slate-400">{p}</div>
                    <div className="text-sm font-semibold text-slate-900">{pct(w)}</div>
                  </div>
                ))}
            </div>
          </section>

          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-700">Propose a new calibration</h2>
              <button
                onClick={handlePropose}
                disabled={proposing}
                className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
              >
                {proposing ? 'Proposing…' : 'Propose calibration'}
              </button>
            </div>
            {proposeMessage && (
              <p
                className={`mt-3 text-sm ${
                  proposeMessage.kind === 'success'
                    ? 'text-emerald-700'
                    : proposeMessage.kind === 'error'
                      ? 'text-red-600'
                      : 'text-amber-700'
                }`}
              >
                {proposeMessage.text}
              </p>
            )}
          </section>

          <section>
            <h2 className="mb-2 text-sm font-semibold text-slate-700">Proposals ({data.count})</h2>
            {data.proposals.length === 0 ? (
              <p className="text-sm text-slate-500">No calibration has been proposed for this tenant yet.</p>
            ) : (
              <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
                <table className="min-w-full divide-y divide-slate-200 text-sm">
                  <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="px-4 py-2">#</th>
                      <th className="px-4 py-2">State</th>
                      <th className="px-4 py-2">Proposed</th>
                      <th className="px-4 py-2">By</th>
                      <th className="px-4 py-2">Decided</th>
                      <th className="px-4 py-2 text-right">Outcomes</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {data.proposals.map((p: CalibrationProposalSummary) => (
                      <tr
                        key={p.proposal_id}
                        onClick={() => selectProposal(p.proposal_id)}
                        className={`cursor-pointer hover:bg-slate-50 ${selectedId === p.proposal_id ? 'bg-slate-50' : ''}`}
                      >
                        <td className="px-4 py-3 font-medium text-slate-900">#{p.proposal_id}</td>
                        <td className="px-4 py-3">{stateBadge(p.state)}</td>
                        <td className="px-4 py-3 text-slate-600">{dt(p.proposed_at)}</td>
                        <td className="px-4 py-3 text-slate-600">{p.proposed_by ?? '—'}</td>
                        <td className="px-4 py-3 text-slate-600">{p.decided_by ? `${p.decided_by} · ${dt(p.decided_at)}` : '—'}</td>
                        <td className="px-4 py-3 text-right text-slate-600">{p.outcomes ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {detailLoading && <p className="text-sm text-slate-400">Loading proposal…</p>}
          {detail && !detailLoading && (
            <section className="border-t border-slate-200 pt-6">
              <ProposalDetail proposal={detail} onDecided={handleDecided} />
            </section>
          )}
        </>
      )}
    </div>
  )
}
