import { useEffect, useMemo, useState } from 'react'
import { ApiError, getInvestmentPriorities, getMeasuredRoi, getPowerOfOne } from '../api/client'
import type {
  Money,
  MeasuredRoiResponse,
  PowerOfOneResponse,
  PrioritiesResponse,
} from '../api/types'
import { useAuth } from '../auth/AuthContext'

const ALLOWED_ROLES = ['cfo', 'cro', 'admin']

function fmtCurrency(value: number): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(value)
}

// Design doc §5: Po1 $/point (and every other dollar figure on this page) must show its basis
// chain visibly — never a bare number. This is the one place that renders a Money object, used
// throughout all three sections below.
function basisStyle(basis: string): string {
  switch (basis) {
    case 'measured':
      return 'bg-emerald-50 text-emerald-700'
    case 'derived':
      return 'bg-blue-50 text-blue-700'
    case 'assumed':
      return 'bg-amber-50 text-amber-700'
    default:
      return 'bg-slate-100 text-slate-600'
  }
}

function MoneyView({ money, align = 'right' }: { money: Money | null | undefined; align?: 'left' | 'right' }) {
  if (!money) return <span className="text-slate-400">—</span>
  const justify = align === 'right' ? 'justify-end' : 'justify-start'
  return (
    <span className={`inline-flex items-center gap-1.5 ${justify}`} title={money.basis_chain?.join(' → ')}>
      <span className={money.value == null ? 'text-slate-400' : 'text-slate-900'}>
        {money.value == null ? '—' : fmtCurrency(money.value)}
      </span>
      <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${basisStyle(money.basis)}`}>
        {money.basis}
      </span>
      {money.note && <span className="text-[11px] text-slate-400">({money.note})</span>}
    </span>
  )
}

function lensBadge(lens: string) {
  const cls = lens === 'protect' ? 'bg-rose-50 text-rose-700' : lens === 'grow' ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-600'
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>{lens}</span>
}

type Tab = 'priorities' | 'po1' | 'measured'

export default function Roi() {
  const { user } = useAuth()
  const [tab, setTab] = useState<Tab>('priorities')
  const [customerId, setCustomerId] = useState<number | null>(user?.customer_id ?? null)

  const [priorities, setPriorities] = useState<PrioritiesResponse | null>(null)
  const [po1, setPo1] = useState<PowerOfOneResponse | null>(null)
  const [measured, setMeasured] = useState<MeasuredRoiResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const canPickTenant = useMemo(() => user?.role === 'admin', [user])
  const permitted = !!user && ALLOWED_ROLES.includes(user.role)

  useEffect(() => {
    if (!permitted || customerId == null) return
    setLoading(true)
    setError(null)
    Promise.all([getInvestmentPriorities(customerId), getPowerOfOne(customerId), getMeasuredRoi(customerId)])
      .then(([p, po, m]) => {
        setPriorities(p)
        setPo1(po)
        setMeasured(m)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Failed to load ROI data.'))
      .finally(() => setLoading(false))
  }, [permitted, customerId])

  if (!permitted) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
        ROI &amp; Power-of-1 is not permitted for your role ({user?.role ?? 'unknown'}). This page is restricted to
        CFO, CRO, and admin roles.
      </div>
    )
  }

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-900">ROI &amp; Power-of-1</h1>
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

      {customerId == null && <p className="text-sm text-slate-500">Enter a customer ID to view its ROI data.</p>}
      {loading && <p className="text-sm text-slate-400">Loading…</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}

      {customerId != null && !loading && !error && (
        <>
          <div className="mb-6 flex gap-2 border-b border-slate-200">
            {(
              [
                ['priorities', 'Priorities'],
                ['po1', 'Power-of-1'],
                ['measured', 'Measured ROI'],
              ] as [Tab, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${
                  tab === key ? 'border-slate-900 text-slate-900' : 'border-transparent text-slate-500 hover:text-slate-700'
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {tab === 'priorities' && <PrioritiesTab data={priorities} />}
          {tab === 'po1' && <Po1Tab data={po1} />}
          {tab === 'measured' && <MeasuredTab data={measured} />}
        </>
      )}
    </div>
  )
}

function OriginBanner({ synthetic, disclosure }: { synthetic: boolean; disclosure: string }) {
  if (!synthetic) return null
  return (
    <div className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{disclosure}</div>
  )
}

// ── Priorities ──────────────────────────────────────────────────────

function PrioritiesTab({ data }: { data: PrioritiesResponse | null }) {
  if (!data) return null
  if (data.status !== 'ok') {
    return <p className="text-sm text-slate-500">{data.hint ?? `No priorities available (${data.status}).`}</p>
  }
  return (
    <div>
      <OriginBanner synthetic={data.synthetic} disclosure={data.disclosure} />
      <p className="mb-4 text-xs text-slate-500">{data.note}</p>

      {data.portfolio && (
        <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile label="Accounts listed" value={`${data.portfolio.listed} / ${data.portfolio.accounts}`} />
          <StatTile label="Revenue total" money={data.portfolio.revenue_total} />
          <StatTile label="Exposure-weighted (protect)" money={data.portfolio.exposure_weighted} />
          <StatTile label="Opportunity-weighted (grow)" money={data.portfolio.opportunity_weighted} />
          <StatTile label="Pending approvals" value={String(data.portfolio.pending_approvals)} />
          <StatTile label="Protect / Grow" value={`${data.portfolio.by_lens.protect ?? 0} / ${data.portfolio.by_lens.grow ?? 0}`} />
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2">Account</th>
              <th className="px-4 py-2">Lens</th>
              <th className="px-4 py-2 text-right">Revenue weighted</th>
              <th className="px-4 py-2 text-right">Risk factor</th>
              <th className="px-4 py-2 text-right">Opportunity factor</th>
              <th className="px-4 py-2 text-right">Pending approvals</th>
              <th className="px-4 py-2 text-right">Cited episodes</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.rows.map((row) => (
              <tr key={row.account_id} className="hover:bg-slate-50">
                <td className="px-4 py-3 font-medium text-slate-900">{row.account_name}</td>
                <td className="px-4 py-3">
                  {lensBadge(row.lens)}
                  {row.secondary_lens && <span className="ml-1 text-xs text-slate-400">(+{row.secondary_lens})</span>}
                </td>
                <td className="px-4 py-3 text-right">
                  <MoneyView money={row.revenue_weighted} />
                </td>
                <td className="px-4 py-3 text-right text-slate-600">{row.risk_factor.toFixed(2)}</td>
                <td className="px-4 py-3 text-right text-slate-600">{row.opportunity_factor.toFixed(2)}</td>
                <td className="px-4 py-3 text-right text-slate-600">{row.pending_approvals}</td>
                <td className="px-4 py-3 text-right text-slate-600">{row.cites.episode_ids.length}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Power-of-1 ──────────────────────────────────────────────────────

function Po1Tab({ data }: { data: PowerOfOneResponse | null }) {
  if (!data) return null
  if (data.status !== 'ok') {
    return <p className="text-sm text-slate-500">No accounts to size (status: {data.status}).</p>
  }
  return (
    <div>
      <OriginBanner synthetic={data.synthetic} disclosure={data.disclosure} />
      <p className="mb-4 text-xs text-slate-500">{data.note}</p>

      <div className="mb-6 rounded-lg border border-amber-200 bg-amber-50 p-4">
        <h2 className="mb-2 text-sm font-semibold text-amber-900">Economics assumptions ({data.economics.file})</h2>
        <p className="mb-2 text-xs text-amber-800">
          {data.economics.horizon_months}-month horizon. Every figure below inheriting this input is labelled{' '}
          <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-800">
            assumed
          </span>
          , not measured or derived.
        </p>
        <p className="mb-1 text-xs text-amber-900">
          Retention sensitivity: <strong>{(data.economics.retention_sensitivity_per_health_point.value * 100).toFixed(2)}%</strong> of
          revenue per health point — {data.economics.retention_sensitivity_per_health_point.basis}
        </p>
        <p className="text-xs text-amber-900">
          Revenue-at-risk share by band:{' '}
          {Object.entries(data.economics.revenue_at_risk_share_by_band)
            .filter(([k]) => k !== 'basis')
            .map(([band, share]) => `${band} ${(Number(share) * 100).toFixed(0)}%`)
            .join(', ')}
        </p>
      </div>

      {data.portfolio && (
        <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile label="Revenue base" money={data.portfolio.revenue_base} />
          <StatTile label="$ / health point (portfolio)" money={data.portfolio.revenue_per_health_point} />
          <StatTile label="Accounts scored" value={`${data.portfolio.accounts - data.portfolio.unscored_accounts} / ${data.portfolio.accounts}`} />
        </div>
      )}

      <h2 className="mb-2 text-sm font-semibold text-slate-900">By pillar (portfolio, revenue-weighted)</h2>
      <div className="mb-6 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2">Pillar</th>
              <th className="px-4 py-2 text-right">Score (rev-weighted)</th>
              <th className="px-4 py-2 text-right">$ / pillar point</th>
              <th className="px-4 py-2 text-right">$ / 1% move</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.portfolio?.pillars.map((p) => (
              <tr key={p.pillar}>
                <td className="px-4 py-3 font-medium text-slate-900">
                  {p.pillar} — {p.name ?? '—'}
                </td>
                <td className="px-4 py-3 text-right text-slate-600">{p.current_score_revenue_weighted ?? '—'}</td>
                <td className="px-4 py-3 text-right">
                  <MoneyView money={p.revenue_per_pillar_point} />
                </td>
                <td className="px-4 py-3 text-right">
                  <MoneyView money={p.revenue_per_one_pct_move} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="mb-2 text-sm font-semibold text-slate-900">Per account</h2>
      <div className="space-y-4">
        {data.accounts.map((a) => (
          <details key={a.account_id} className="rounded-lg border border-slate-200 bg-white p-4">
            <summary className="flex cursor-pointer items-center justify-between text-sm font-medium text-slate-900">
              <span>{a.account_name}</span>
              <span className="flex items-center gap-3 text-xs font-normal text-slate-500">
                health {a.health_now ?? '—'} · weight source {a.weight_source}
                <MoneyView money={a.revenue_per_health_point} />
              </span>
            </summary>
            <div className="mt-3">
              {a.band_view && (
                <p className="mb-3 text-xs text-slate-500">
                  Band: <strong>{a.band_view.band}</strong> · revenue at risk <MoneyView money={a.band_view.revenue_at_risk} align="left" />
                  {a.band_view.next_band && (
                    <>
                      {' '}
                      · {a.band_view.points_to_next_band} pts to {a.band_view.next_band} (protects{' '}
                      <MoneyView money={a.band_view.revenue_protected_if_next_band} align="left" />)
                    </>
                  )}
                </p>
              )}
              <table className="min-w-full divide-y divide-slate-200 text-xs">
                <thead className="text-left uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="py-1 pr-3">Pillar</th>
                    <th className="py-1 pr-3">Score</th>
                    <th className="py-1 pr-3 text-right">$ / pillar point</th>
                    <th className="py-1 pr-3 text-right">$ / 1% move</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {a.pillars.map((p) => (
                    <tr key={p.pillar}>
                      <td className="py-1.5 pr-3">
                        {p.pillar} — {p.name ?? '—'}
                      </td>
                      <td className="py-1.5 pr-3">{p.current_score ?? '—'}</td>
                      <td className="py-1.5 pr-3 text-right">
                        <MoneyView money={p.revenue_per_pillar_point} />
                      </td>
                      <td className="py-1.5 pr-3 text-right">
                        <MoneyView money={p.revenue_per_one_pct_move} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        ))}
      </div>
    </div>
  )
}

// ── Measured ROI ────────────────────────────────────────────────────

function MeasuredTab({ data }: { data: MeasuredRoiResponse | null }) {
  if (!data) return null
  return (
    <div>
      <OriginBanner synthetic={data.synthetic} disclosure={data.disclosure} />
      <p className="mb-4 text-xs text-slate-500">{data.note}</p>

      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatTile label="Revenue base" money={data.revenue_base} />
        <StatTile label="Interventions" value={String(data.interventions.count)} />
        <StatTile label="Stuck" value={String(data.interventions.stuck.length)} />
      </div>

      <h2 className="mb-2 text-sm font-semibold text-slate-900">By playbook</h2>
      <div className="mb-6 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2">Playbook</th>
              <th className="px-4 py-2 text-right">Closed done</th>
              <th className="px-4 py-2 text-right">Outcomes reported</th>
              <th className="px-4 py-2 text-right">Realized revenue</th>
              <th className="px-4 py-2 text-right">Exposure revenue</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.by_playbook.map((r) => (
              <tr key={r.playbook_id}>
                <td className="px-4 py-3 font-medium text-slate-900">{r.playbook_id}</td>
                <td className="px-4 py-3 text-right text-slate-600">{r.closed_done}</td>
                <td className="px-4 py-3 text-right text-slate-600">{r.outcomes_reported}</td>
                <td className="px-4 py-3 text-right">
                  <MoneyView money={r.realized_revenue} />
                </td>
                <td className="px-4 py-3 text-right">
                  <MoneyView money={r.exposure_revenue} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="border-t border-slate-100 px-4 py-2 text-[11px] text-slate-400">
          Realized and exposure revenue are two separate numbers, never summed.
        </p>
      </div>

      <h2 className="mb-2 text-sm font-semibold text-slate-900">By pillar</h2>
      <div className="mb-6 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2">Pillar</th>
              <th className="px-4 py-2">Roles</th>
              <th className="px-4 py-2 text-right">Interventions</th>
              <th className="px-4 py-2 text-right">Realized revenue</th>
              <th className="px-4 py-2 text-right">Exposure revenue</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.by_pillar.map((r) => (
              <tr key={r.pillar}>
                <td className="px-4 py-3 font-medium text-slate-900">
                  {r.pillar === 'unmapped' ? (
                    <span>
                      <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-slate-600">unmapped</span>{' '}
                      <span className="text-xs text-slate-500">{r.name}</span>
                    </span>
                  ) : (
                    `${r.pillar} — ${r.name}`
                  )}
                </td>
                <td className="px-4 py-3 text-xs text-slate-500">{r.roles.join(', ')}</td>
                <td className="px-4 py-3 text-right text-slate-600">{r.interventions}</td>
                <td className="px-4 py-3 text-right">
                  <MoneyView money={r.realized_revenue} />
                </td>
                <td className="px-4 py-3 text-right">
                  <MoneyView money={r.exposure_revenue} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="border-t border-slate-100 px-4 py-2 text-[11px] text-slate-400">
          An intervention citing roles in two pillars is counted under both; do not sum pillars.
        </p>
      </div>

      <h2 className="mb-2 text-sm font-semibold text-slate-900">Sensitivity ($ / health point, measured)</h2>
      <div className="mb-6 rounded-lg border border-slate-200 bg-white p-4 text-sm">
        <p className="mb-1">
          Measured: <MoneyView money={data.sensitivity.measured_revenue_per_health_point} align="left" /> (
          {data.sensitivity.qualifying_interventions} of {data.sensitivity.minimum_interventions} required interventions qualify)
        </p>
        <p className="text-xs text-slate-500">
          Assumed (economics file): {(data.sensitivity.assumed_revenue_share_per_health_point.value * 100).toFixed(2)}% of revenue per
          point — {data.sensitivity.assumed_revenue_share_per_health_point.basis}
        </p>
      </div>

      <h2 className="mb-2 text-sm font-semibold text-slate-900">Hindsight (Wizard B)</h2>
      <div className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
        {data.hindsight.status !== 'ok' ? (
          <p className="text-slate-500">{data.hindsight.hint ?? 'No Wizard B run yet.'}</p>
        ) : (
          <>
            <p className="mb-1 text-xs text-slate-500">
              Run {data.hindsight.run_id} · {data.hindsight.generated_at} · evidence: {data.hindsight.evidence_label}
            </p>
            {data.hindsight.interventions && (
              <p className="text-slate-700">
                {data.hindsight.interventions.n} interventions ·{' '}
                {(data.hindsight.interventions.with_health_lift_share * 100).toFixed(0)}% with a health lift · median lift{' '}
                {data.hindsight.interventions.median_lift_pts} pts
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// ── shared ──────────────────────────────────────────────────────────

function StatTile({ label, value, money }: { label: string; value?: string; money?: Money }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-1 text-sm font-medium text-slate-900">
        {money ? <MoneyView money={money} align="left" /> : value}
      </div>
    </div>
  )
}
