import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { ApiError, getAccount } from '../api/client'
import type { Journey } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import JourneyCanvasSvg from '../components/canvas/JourneyCanvasSvg'

const ALLOWED_ROLES = ['cfo', 'cro', 'admin']

function money(value: number | null | undefined): string {
  if (value == null) return '—'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(value)
}

function pct(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${Math.round(value * 100)}%`
}

function day(value: string | null | undefined): string {
  if (!value) return '—'
  return value.slice(0, 10)
}

// The divergence label in one phrase (journey-canvas.md §3).
function divergenceMeaning(label: string | null | undefined): string | null {
  if (label === 'early_warning') return 'behavior ahead of the numbers'
  if (label === 'recovery_watch') return 'numbers ahead of behavior'
  if (label === 'aligned') return 'behavior and numbers agree'
  return null
}

function NowTile({ label, value, foot }: { label: string; value: React.ReactNode; foot?: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <dt className="text-xs uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="mt-1 text-lg font-semibold text-slate-900">{value}</dd>
      {foot && <p className="mt-1 text-xs text-slate-500">{foot}</p>}
    </div>
  )
}

export default function JourneyCanvas() {
  const { user } = useAuth()
  const { accountId } = useParams<{ accountId: string }>()
  const [searchParams] = useSearchParams()
  const customerId = useMemo(() => {
    const q = searchParams.get('customer_id')
    return q ? Number(q) : (user?.customer_id ?? null)
  }, [searchParams, user])

  const permitted = !!user && ALLOWED_ROLES.includes(user.role)

  const [journey, setJourney] = useState<Journey | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!permitted || customerId == null || !accountId) return
    setLoading(true)
    setError(null)
    getAccount(customerId, Number(accountId))
      .then(setJourney)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Failed to load journey.'))
      .finally(() => setLoading(false))
  }, [permitted, customerId, accountId])

  if (!permitted) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
        The Journey Canvas is not permitted for your role ({user?.role ?? 'unknown'}). This page is restricted to
        CFO, CRO, and admin roles.
      </div>
    )
  }
  if (customerId == null) {
    return <p className="text-sm text-slate-500">No customer context — open this account from the Portfolio page.</p>
  }
  if (loading && !journey) {
    return <p className="text-sm text-slate-400">Loading…</p>
  }
  if (error) {
    return <p className="text-sm text-red-600">{error}</p>
  }
  if (!journey) {
    return null
  }

  const latest = journey.leading_vs_trailing?.series?.slice(-1)[0]
  const arr = journey.forecast?.status === 'forecast' ? journey.forecast.revenue?.arr ?? null : null
  const roster: { label: string; present: boolean }[] = [
    { label: 'champion', present: !!journey.account.champion },
    { label: 'exec sponsor', present: !!journey.account.executive_sponsor },
    { label: 'CSM', present: !!journey.account.csm },
  ]

  return (
    <div className="space-y-6">
      <Link to={`/accounts/${accountId}${customerId ? `?customer_id=${customerId}` : ''}`} className="text-sm text-slate-500 hover:text-slate-800">
        ← {journey.account_name}
      </Link>

      {journey.synthetic && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{journey.disclosure}</div>
      )}

      <div>
        <h1 className="text-xl font-semibold text-slate-900">{journey.account_name} — Journey Canvas</h1>
        <p className="mt-1 text-sm text-slate-500">
          {journey.episodes.length} cited episodes · {journey.current_phase ?? 'phase unknown'} · updated {day(journey.generated_at)}
        </p>
      </div>

      {/* Band 2 — now */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <NowTile label="KPI health" value={latest?.kpi_only != null ? Math.round(latest.kpi_only) : '—'} />
        <NowTile
          label="Leading"
          value={latest?.qual != null ? Math.round(latest.qual) : '—'}
          foot={divergenceMeaning(latest?.early_warning)}
        />
        <NowTile
          label="Arc"
          value={journey.arc.arc_type ?? journey.arc.state}
          foot={journey.arc.confidence != null ? `${Math.round(journey.arc.confidence * 100)}% confidence` : undefined}
        />
        <NowTile label="ARR" value={money(arr)} foot={`renews ${day(journey.account.renewal_date)}`} />
        <NowTile
          label="People present"
          value={roster.filter((r) => r.present).map((r) => r.label).join(', ') || '—'}
        />
        <NowTile
          label="Forecast (Foresight)"
          value={journey.forecast?.status === 'forecast' ? `${pct(journey.forecast.retention?.p)} retain` : 'not run'}
          foot={
            journey.forecast?.status === 'forecast'
              ? `(${pct(journey.forecast.retention?.low)}–${pct(journey.forecast.retention?.high)})`
              : undefined
          }
        />
      </div>

      {/* Band 1 — the past */}
      <div className="rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">The past</h2>
        <p className="mt-1 text-xs text-slate-400">
          Trailing (KPI) in blue, leading (behavioral) in amber; the shaded gap is where they disagree by 10+ points.
          Dotted line is the arc template's typical path — a prior, not this account's data.
        </p>
        <div className="mt-4">
          <JourneyCanvasSvg journey={journey} />
        </div>
      </div>

      {/* Story, reusing the existing narrative generator verbatim */}
      {journey.narrative && journey.narrative.chapters.length > 0 && (
        <div className="rounded-lg border border-slate-200 bg-white p-5">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Narrative</h2>
          <div className="mt-3 space-y-4">
            {journey.narrative.chapters.map((ch, i) => (
              <div key={i}>
                <p className="text-xs font-medium uppercase tracking-wide text-slate-400">
                  {ch.phase} {ch.from && `· from ${day(ch.from)}`} {ch.to && `to ${day(ch.to)}`}
                </p>
                <div className="mt-1 space-y-1">
                  {ch.sentences.map((s, j) => (
                    <p key={j} className="text-sm text-slate-700">{s.text}</p>
                  ))}
                </div>
              </div>
            ))}
          </div>
          {journey.narrative.omitted.length > 0 && (
            <p className="mt-3 text-xs text-slate-400">{journey.narrative.omitted.length} sentence(s) omitted for lack of citation.</p>
          )}
        </div>
      )}
    </div>
  )
}
