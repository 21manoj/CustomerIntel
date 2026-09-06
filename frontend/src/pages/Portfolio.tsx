import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, getPortfolio } from '../api/client'
import type { PortfolioResponse } from '../api/types'
import { useAuth } from '../auth/AuthContext'

function money(value: number | null): string {
  if (value == null) return '—'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(value)
}

// journey_json.state is the arc-classification outcome (classified/steady/unclassified),
// not a health status — there's no single health-status string on the portfolio row today
// (latest.kpi_only/qual are the actual scores). Color accordingly, not by health thresholds.
function stateBadge(state: string | null) {
  const styles: Record<string, string> = {
    classified: 'bg-blue-50 text-blue-700',
    steady: 'bg-emerald-50 text-emerald-700',
    unclassified: 'bg-amber-50 text-amber-700',
  }
  const cls = (state && styles[state]) || 'bg-slate-100 text-slate-600'
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>{state ?? 'unknown'}</span>
}

export default function Portfolio() {
  const { user } = useAuth()
  const [customerId, setCustomerId] = useState<number | null>(user?.customer_id ?? null)
  const [data, setData] = useState<PortfolioResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const canPickTenant = useMemo(() => user?.role === 'admin', [user])

  useEffect(() => {
    if (customerId == null) return
    setLoading(true)
    setError(null)
    getPortfolio(customerId)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Failed to load portfolio.'))
      .finally(() => setLoading(false))
  }, [customerId])

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-900">Portfolio</h1>
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

      {customerId == null && <p className="text-sm text-slate-500">Enter a customer ID to view its portfolio.</p>}

      {loading && <p className="text-sm text-slate-400">Loading…</p>}

      {error && <p className="text-sm text-red-600">{error}</p>}

      {data && (
        <>
          {data.synthetic && (
            <div className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              {data.disclosure}
            </div>
          )}
          {data.accounts.length === 0 ? (
            <p className="text-sm text-slate-500">No accounts with a journey yet for this customer.</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-2">Account</th>
                    <th className="px-4 py-2">State</th>
                    <th className="px-4 py-2">Arc</th>
                    <th className="px-4 py-2">Forecast</th>
                    <th className="px-4 py-2">Priority</th>
                    <th className="px-4 py-2">Open interventions</th>
                    <th className="px-4 py-2 text-right">Revenue</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.accounts.map((row) => (
                    <tr key={row.account_id} className="hover:bg-slate-50">
                      <td className="px-4 py-3 font-medium text-slate-900">
                        <Link to={`/accounts/${row.account_id}?customer_id=${customerId}`} className="hover:underline">
                          {row.account_name}
                        </Link>
                      </td>
                      <td className="px-4 py-3">{stateBadge(row.state)}</td>
                      <td className="px-4 py-3 text-slate-600">{row.arc_type ?? '—'}</td>
                      <td className="px-4 py-3 text-slate-600">
                        {row.forecast?.status === 'forecast'
                          ? `${Math.round((row.forecast.p_retain ?? 0) * 100)}% retain (${row.forecast.basis})`
                          : '—'}
                      </td>
                      <td className="px-4 py-3 text-slate-600">{row.priority?.lens ?? '—'}</td>
                      <td className="px-4 py-3 text-slate-600">{row.open_review_count}</td>
                      <td className="px-4 py-3 text-right text-slate-900">{money(row.revenue)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}
