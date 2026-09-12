import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  ApiError,
  approveIntervention,
  evaluatePlaybooks,
  getAccount,
  getInterventions,
  reportIntervention,
} from '../api/client'
import type { Episode, EvidenceView, Intervention, Journey, LinkedEvidence, PlaybookEvaluation, ReportState } from '../api/types'
import { REPORT_STATES } from '../api/types'
import { useAuth } from '../auth/AuthContext'

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

// Resolve an episode_id (as narrative/arc sentences cite it, e.g. "sig:77") to the
// evidence quotes behind it: episode -> its evidence_node_ids -> journey.evidence[node_id].
function citedQuotes(journey: Journey, episodeId: string): EvidenceView[] {
  const ep = journey.episodes.find((e) => e.episode_id === episodeId)
  if (!ep) return []
  return ep.evidence_node_ids.map((id) => journey.evidence[String(id)]).filter((v): v is EvidenceView => Boolean(v))
}

const STATE_BADGE: Record<string, string> = {
  proposed: 'bg-amber-50 text-amber-700',
  approved: 'bg-blue-50 text-blue-700',
  sent: 'bg-blue-50 text-blue-700',
  closed: 'bg-slate-100 text-slate-600',
}

function interventionBadge(iv: Intervention) {
  const cls = STATE_BADGE[iv.state] || 'bg-slate-100 text-slate-600'
  const label = iv.state === 'closed' ? `closed (${iv.closed_state})` : iv.state
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>{label}</span>
}

// Why-panel A2 ("why is this outcome linked to this signal"): the outcome's own
// episode carries meta.linked_evidence (journeys/journey_builder.py), a list of
// {node_id, derivation, evidence_tier} -- the LED_TO edges into this outcome. Unlike
// CitationList (which resolves an episode id's OWN evidence), these are raw node ids
// naming a DIFFERENT node (the signal that justified the outcome), so they're read
// straight out of journey.evidence rather than through an episode lookup.
function linkedEvidenceFor(journey: Journey, outcomeNodeId: number): LinkedEvidence[] {
  const ep = journey.episodes.find((e) => e.episode_id === `out:${outcomeNodeId}`)
  const raw = (ep?.meta as { linked_evidence?: LinkedEvidence[] } | undefined)?.linked_evidence
  return raw ?? []
}

// evidence_tier is the one thing this list must never flatten away: 'observed' (a real
// person or workflow named this link) reads differently from 'unknown' (an upload
// claimed it and the platform can't yet say who) -- utils/provenance.py's vocabulary,
// carried through rather than shown as an equally-confident citation either way.
const TIER_BADGE: Record<string, string> = {
  observed: 'bg-emerald-50 text-emerald-700',
  inferred: 'bg-blue-50 text-blue-700',
  unknown: 'bg-amber-50 text-amber-700',
}

function LinkedEvidenceList({ journey, links }: { journey: Journey; links: LinkedEvidence[] }) {
  if (links.length === 0) {
    return <p className="mt-2 text-xs text-slate-400">No linked signal on record for this outcome.</p>
  }
  return (
    <ul className="mt-2 space-y-1.5 border-l-2 border-slate-200 pl-3">
      {links.map((link) => {
        const ev = journey.evidence[String(link.node_id)]
        const tier = link.evidence_tier ?? 'unknown'
        return (
          <li key={link.node_id} className="text-xs text-slate-500">
            <span className={`mr-1.5 rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase ${TIER_BADGE[tier] ?? TIER_BADGE.unknown}`}>
              {tier}
            </span>
            {ev ? (
              <>
                <span className="text-slate-700">&ldquo;{ev.quote}&rdquo;</span>
                {ev.person?.name && <span className="text-slate-400"> — {ev.person.name}{ev.person.title ? `, ${ev.person.title}` : ''}</span>}
                {ev.occurred_at && <span className="text-slate-400"> ({day(ev.occurred_at)})</span>}
              </>
            ) : (
              <span className="text-slate-400">node {link.node_id} (not resolvable from this journey)</span>
            )}
            {link.derivation && <span className="text-slate-300"> · {link.derivation}</span>}
          </li>
        )
      })}
    </ul>
  )
}

function CitationList({ journey, episodeIds }: { journey: Journey; episodeIds: string[] }) {
  const quotes = episodeIds.flatMap((id) => citedQuotes(journey, id))
  if (quotes.length === 0) return null
  return (
    <ul className="mt-2 space-y-1.5 border-l-2 border-slate-200 pl-3">
      {quotes.map((q) => (
        <li key={q.node_id} className="text-xs text-slate-500">
          <span className="text-slate-700">&ldquo;{q.quote}&rdquo;</span>
          {q.person?.name && <span className="text-slate-400"> — {q.person.name}{q.person.title ? `, ${q.person.title}` : ''}</span>}
          {q.occurred_at && <span className="text-slate-400"> ({day(q.occurred_at)})</span>}
        </li>
      ))}
    </ul>
  )
}

function ReportForm({ iv, customerId, onDone }: { iv: Intervention; customerId: number; onDone: () => void }) {
  const [state, setState] = useState<ReportState>('done')
  const [note, setNote] = useState('')
  const [outcomeType, setOutcomeType] = useState('')
  const [outcomeDate, setOutcomeDate] = useState('')
  const [revenue, setRevenue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await reportIntervention(iv.intervention_id, customerId, {
        state,
        note: note || undefined,
        outcomeType: outcomeType || undefined,
        outcomeDate: outcomeDate || undefined,
        revenue: revenue ? Number(revenue) : undefined,
      })
      onDone()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to report outcome.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="mt-3 flex flex-wrap items-end gap-2 rounded-md bg-slate-50 p-3 text-xs">
      <label className="flex flex-col gap-1">
        State
        <select
          value={state}
          onChange={(e) => setState(e.target.value as ReportState)}
          className="rounded border border-slate-300 px-2 py-1"
        >
          {REPORT_STATES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1">
        Outcome type
        <input value={outcomeType} onChange={(e) => setOutcomeType(e.target.value)} placeholder="optional" className="w-32 rounded border border-slate-300 px-2 py-1" />
      </label>
      <label className="flex flex-col gap-1">
        Outcome date
        <input type="date" value={outcomeDate} onChange={(e) => setOutcomeDate(e.target.value)} className="rounded border border-slate-300 px-2 py-1" />
      </label>
      <label className="flex flex-col gap-1">
        Revenue
        <input type="number" value={revenue} onChange={(e) => setRevenue(e.target.value)} placeholder="optional" className="w-28 rounded border border-slate-300 px-2 py-1" />
      </label>
      <label className="flex flex-1 flex-col gap-1" style={{ minWidth: 160 }}>
        Note
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="optional" className="rounded border border-slate-300 px-2 py-1" />
      </label>
      <button type="submit" disabled={submitting} className="rounded-md bg-slate-900 px-3 py-1.5 font-medium text-white hover:bg-slate-800 disabled:opacity-50">
        {submitting ? 'Reporting…' : 'Report'}
      </button>
      {error && <p className="w-full text-red-600">{error}</p>}
    </form>
  )
}

function InterventionRow({
  iv, journey, customerId, canAct, onChanged,
}: { iv: Intervention; journey: Journey; customerId: number; canAct: boolean; onChanged: () => void }) {
  const [reporting, setReporting] = useState(false)
  const [approving, setApproving] = useState(false)
  const [whyOpen, setWhyOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleApprove() {
    setError(null)
    setApproving(true)
    try {
      await approveIntervention(iv.intervention_id, customerId)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to approve.')
    } finally {
      setApproving(false)
    }
  }

  return (
    <li className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-medium text-slate-900">{iv.playbook_id}</span>
            {interventionBadge(iv)}
            {iv.delivery_problem && <span className="rounded-full bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">delivery problem</span>}
            {iv.stuck && <span className="rounded-full bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">stuck {iv.stuck_days}d</span>}
          </div>
          <p className="mt-1 text-sm text-slate-600">{iv.trigger.quote}</p>
          <p className="mt-1 text-xs text-slate-400">
            {iv.action_class} · urgency {iv.urgency ?? '—'} · exposure {money(iv.exposure_revenue)}
            {iv.proposed_at && ` · proposed ${day(iv.proposed_at)}`}
            {iv.sent_at && ` · sent ${day(iv.sent_at)}`}
            {iv.closed_at && ` · closed ${day(iv.closed_at)} (${iv.closed_state})`}
          </p>
          {iv.outcome && (
            <p className="mt-1 text-xs text-slate-500">
              outcome: {iv.outcome.outcome_type ?? '—'} {iv.outcome.revenue != null && `(${money(iv.outcome.revenue)})`}
              {iv.outcome.in_window != null && (iv.outcome.in_window ? ' · in window' : ' · outside window')}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button onClick={() => setWhyOpen((v) => !v)} className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50">
            {whyOpen ? 'Hide why' : 'Why?'}
          </button>
          {canAct && iv.state === 'proposed' && (
            <button onClick={handleApprove} disabled={approving} className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:opacity-50">
              {approving ? 'Approving…' : 'Approve'}
            </button>
          )}
          {canAct && iv.state === 'sent' && (
            <button onClick={() => setReporting((v) => !v)} className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50">
              {reporting ? 'Cancel' : 'Report outcome'}
            </button>
          )}
        </div>
      </div>
      {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
      {reporting && <ReportForm iv={iv} customerId={customerId} onDone={() => { setReporting(false); onChanged() }} />}
      {whyOpen && (
        <div className="mt-3 space-y-3 rounded-md bg-slate-50 p-3 text-xs">
          <div>
            <p className="font-medium text-slate-500">Why proposed (roles: {iv.trigger.roles?.join(', ') || '—'}, as of {day(iv.trigger.evaluated_as_of)})</p>
            {iv.trigger.episode_ids && iv.trigger.episode_ids.length > 0 ? (
              <CitationList journey={journey} episodeIds={iv.trigger.episode_ids} />
            ) : (
              <p className="mt-1 text-slate-400">No cited episode ids on record.</p>
            )}
          </div>
          <div>
            <p className="font-medium text-slate-500">Approval &amp; delivery</p>
            <p className="mt-1 text-slate-600">
              {iv.approved_by ? `approved by ${iv.approved_by}${iv.approved_at ? ` on ${day(iv.approved_at)}` : ''}` : 'not yet approved'}
            </p>
            {iv.delivery?.status && (
              <p className="mt-1 text-slate-600">
                delivery: {iv.delivery.status}
                {iv.delivery.url_host && ` → ${iv.delivery.url_host}`}
                {iv.delivery.attempts != null && ` (${iv.delivery.attempts} attempt${iv.delivery.attempts === 1 ? '' : 's'})`}
                {iv.delivery.error && <span className="text-red-600"> — {iv.delivery.error}</span>}
              </p>
            )}
          </div>
          {iv.outcome && (
            <div>
              <p className="font-medium text-slate-500">
                Why this outcome is {iv.outcome.bucket ?? 'unclassified'} revenue ({iv.outcome.outcome_type ?? 'type unknown'})
              </p>
              <LinkedEvidenceList journey={journey} links={linkedEvidenceFor(journey, iv.outcome.node_id)} />
            </div>
          )}
        </div>
      )}
    </li>
  )
}

// Why-panel B3 ("why didn't playbook X fire for this account"): playbooks.governance's
// own skip reasons (roles_missing, urgency_below_floor, renewal_beyond_window, exists,
// open, suppressed_recent_close), already computed on every process_data run but never
// surfaced anywhere -- previously only reachable by hand-calling the MCP tool or the
// Bearer-keyed /api/interventions/evaluate route. On demand, not eager: evaluate()
// re-checks every playbook against the account's latest evidence, a real (if cheap)
// query, not a free read of something already cached.
function PlaybookEvaluationSection({ customerId, accountId }: { customerId: number; accountId: number }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<PlaybookEvaluation | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || data || loading) return
    setLoading(true)
    setError(null)
    evaluatePlaybooks(customerId, accountId)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Failed to evaluate playbooks.'))
      .finally(() => setLoading(false))
  }, [open, data, loading, customerId, accountId])

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-5">
      <button onClick={() => setOpen((v) => !v)} className="text-sm font-semibold uppercase tracking-wide text-slate-500 hover:text-slate-700">
        Playbook evaluation {open ? '▾' : '▸'}
      </button>
      {open && (
        <div className="mt-3">
          {loading && <p className="text-sm text-slate-400">Evaluating…</p>}
          {error && <p className="text-sm text-red-600">{error}</p>}
          {data && (
            <div className="space-y-3">
              {data.status !== 'evaluated' ? (
                <p className="text-sm text-slate-500">{data.note ?? data.status}</p>
              ) : (
                <>
                  <p className="text-xs text-slate-400">considered: {data.playbooks_considered.join(', ') || '—'}</p>
                  {data.skipped.length === 0 ? (
                    <p className="text-sm text-slate-500">Nothing was skipped this pass.</p>
                  ) : (
                    <ul className="space-y-1.5">
                      {data.skipped.map((s, i) => (
                        <li key={i} className="text-xs text-slate-600">
                          <span className="font-medium text-slate-700">{s.playbook_id}</span>
                          <span className="text-slate-400"> — {s.reason}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// Why-panel A2/A4 for an outcome with no Intervention behind it -- same "Why?" toggle
// as InterventionRow, minus the approve/report actions that only make sense for a
// governed intervention. journey.evidence already resolves the linked signal(s); no
// separate fetch needed.
function StandaloneOutcomeRow({ episode, journey }: { episode: Episode; journey: Journey }) {
  const [whyOpen, setWhyOpen] = useState(false)
  const links = (episode.meta as { linked_evidence?: LinkedEvidence[] } | undefined)?.linked_evidence ?? []

  return (
    <li className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="font-medium text-slate-900">{episode.title}</p>
          <p className="mt-1 text-xs text-slate-400">
            {episode.subtype ?? 'outcome'} · {episode.revenue_bucket ?? 'unclassified'} revenue
            {episode.revenue != null && ` · ${money(episode.revenue)}`} · {day(episode.date)}
          </p>
        </div>
        <button onClick={() => setWhyOpen((v) => !v)} className="shrink-0 rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50">
          {whyOpen ? 'Hide why' : 'Why?'}
        </button>
      </div>
      {whyOpen && (
        <div className="mt-3 rounded-md bg-slate-50 p-3 text-xs">
          <p className="font-medium text-slate-500">
            Why this is {episode.revenue_bucket ?? 'unclassified'} revenue ({episode.subtype ?? 'type unknown'})
          </p>
          <LinkedEvidenceList journey={journey} links={links} />
        </div>
      )}
    </li>
  )
}

export default function AccountDetail() {
  const { user } = useAuth()
  const { accountId } = useParams<{ accountId: string }>()
  const [searchParams] = useSearchParams()
  const customerId = useMemo(() => {
    const q = searchParams.get('customer_id')
    return q ? Number(q) : (user?.customer_id ?? null)
  }, [searchParams, user])

  const [journey, setJourney] = useState<Journey | null>(null)
  const [interventions, setInterventions] = useState<Intervention[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  const canAct = user?.role === 'admin' || user?.role === 'csm'
  const canSeeCanvas = user?.role === 'admin' || user?.role === 'cfo' || user?.role === 'cro'

  useEffect(() => {
    if (customerId == null || !accountId) return
    setLoading(true)
    setError(null)
    Promise.all([
      getAccount(customerId, Number(accountId)),
      getInterventions(customerId, { accountId: Number(accountId) }),
    ])
      .then(([j, iv]) => {
        setJourney(j)
        setInterventions(iv.interventions)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Failed to load account.'))
      .finally(() => setLoading(false))
  }, [customerId, accountId, reloadKey])

  function refresh() {
    setReloadKey((k) => k + 1)
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

  const arc = journey.arc
  const forecast = journey.forecast
  const acct = journey.account
  const open = interventions.filter((iv) => iv.state !== 'closed')
  const closed = interventions.filter((iv) => iv.state === 'closed')
  const revenue = forecast?.status === 'forecast' ? forecast.revenue?.arr ?? null : null

  // Why-panel A2/A4, standalone case (found 2026-09-13 trying the panel on Lumen
  // Workflows): an OUTCOME episode already carries meta.linked_evidence regardless of
  // whether an Intervention ever pointed at it -- journey_builder.py enriches every
  // outcome node in the account, not just the ones report_intervention() closed. A
  // manifest-authored event (or any outcomes.csv row) with no intervention behind it
  // had real, queryable evidence and no way to inspect it. covered = outcome node ids
  // already shown inline on an Intervention row, so this list only adds what isn't
  // already visible there.
  const coveredOutcomeIds = new Set(interventions.map((iv) => iv.outcome?.node_id).filter((id): id is number => id != null))
  const standaloneOutcomes = journey.episodes.filter(
    (e) => e.kind === 'outcome' && !coveredOutcomeIds.has(e.evidence_node_ids[0]),
  )

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <Link to={`/portfolio${customerId ? `?customer_id=${customerId}` : ''}`} className="text-sm text-slate-500 hover:text-slate-800">
          ← Portfolio
        </Link>
        {canSeeCanvas && (
          <Link
            to={`/accounts/${accountId}/canvas${customerId ? `?customer_id=${customerId}` : ''}`}
            className="text-sm font-medium text-slate-700 hover:text-slate-900"
          >
            View Journey Canvas →
          </Link>
        )}
      </div>

      {journey.synthetic && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{journey.disclosure}</div>
      )}

      {/* Header */}
      <div className="rounded-lg border border-slate-200 bg-white p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold text-slate-900">{journey.account_name}</h1>
            <p className="mt-1 text-sm text-slate-500">
              {acct.contract_type ?? 'contract type unknown'} · renews {day(acct.renewal_date)}
            </p>
          </div>
          <div className="text-right">
            <p className="text-lg font-semibold text-slate-900">{money(revenue)}</p>
            <p className="text-xs text-slate-400">
              {revenue != null ? 'ARR, from the latest forecast run' : 'revenue not carried on the journey'}
            </p>
          </div>
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-400">Champion</dt>
            <dd className="text-slate-700">{acct.champion ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-400">Executive sponsor</dt>
            <dd className="text-slate-700">{acct.executive_sponsor ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-400">CSM</dt>
            <dd className="text-slate-700">{acct.csm ?? '—'}</dd>
          </div>
        </dl>
        {acct.use_cases.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {acct.use_cases.map((uc, i) => (
              <span key={`${uc.name}-${i}`} className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
                {uc.name}
                {uc.product ? <span className="text-slate-400"> ({uc.product})</span> : null}
                {uc.status ? <span className="text-slate-400"> · {uc.status}</span> : null}
                {uc.target_date ? <span className="text-slate-400"> · target {day(uc.target_date)}</span> : null}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Arc */}
      <div className="rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Arc</h2>
        <div className="mt-2 flex items-center gap-3">
          <span className="text-lg font-semibold text-slate-900">{arc.arc_type ?? arc.state}</span>
          {arc.confidence != null && <span className="text-sm text-slate-500">confidence {Math.round(arc.confidence * 100)}%</span>}
        </div>
        {arc.reason && <p className="mt-1 text-sm text-slate-500">{arc.reason}</p>}
        {arc.contradicting_evidence.length > 0 && (
          <p className="mt-1 text-xs text-amber-700">contradicting: {arc.contradicting_evidence.join('; ')}</p>
        )}
        {arc.alternatives.length > 0 && (
          <div className="mt-2">
            <p className="text-xs font-medium text-slate-500">Why not one of these instead?</p>
            <ul className="mt-1 space-y-1">
              {arc.alternatives.map((alt) => (
                <li key={alt.arc_type} className="text-xs text-slate-500">
                  <span className="font-medium text-slate-600">{alt.arc_type}</span> — present: {alt.present.join(', ') || '—'};
                  {' '}ruled out by: {alt.missing.join(', ') || '—'}
                </li>
              ))}
            </ul>
          </div>
        )}
        {arc.supporting_episode_ids.length > 0 && (
          <>
            <p className="mt-3 text-xs font-medium text-slate-500">Cited evidence</p>
            <CitationList journey={journey} episodeIds={arc.supporting_episode_ids} />
          </>
        )}
      </div>

      {/* Narrative */}
      {journey.narrative && journey.narrative.chapters.length > 0 && (
        <div className="rounded-lg border border-slate-200 bg-white p-5">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Narrative</h2>
          <div className="mt-3 space-y-4">
            {journey.narrative.chapters.map((ch, i) => (
              <div key={i}>
                <p className="text-xs font-medium uppercase tracking-wide text-slate-400">
                  {ch.phase} {ch.from && `· from ${day(ch.from)}`} {ch.to && `to ${day(ch.to)}`}
                </p>
                <div className="mt-1 space-y-2">
                  {ch.sentences.map((s, j) => (
                    <div key={j}>
                      <p className="text-sm text-slate-700">{s.text}</p>
                      <CitationList journey={journey} episodeIds={s.cites} />
                    </div>
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

      {/* Forecast */}
      {forecast?.status === 'forecast' && (
        <div className="rounded-lg border border-slate-200 bg-white p-5">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Forecast (Foresight)</h2>
            {forecast.stale && <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700">stale</span>}
          </div>
          <p className="mt-1 text-xs text-slate-400">{forecast.basis_note ?? forecast.basis}</p>
          <div className="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
            <div>
              <dt className="text-xs uppercase tracking-wide text-slate-400">Retention</dt>
              <dd className="text-sm text-slate-800">{pct(forecast.retention?.p)} <span className="text-slate-400">({pct(forecast.retention?.low)}–{pct(forecast.retention?.high)})</span></dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-slate-400">Expansion</dt>
              <dd className="text-sm text-slate-800">{pct(forecast.expansion?.p)} <span className="text-slate-400">({pct(forecast.expansion?.low)}–{pct(forecast.expansion?.high)})</span></dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-slate-400">Expected ARR</dt>
              <dd className="text-sm text-slate-800">{money(forecast.revenue?.expected_arr_end)} <span className="text-slate-400">({money(forecast.revenue?.low)}–{money(forecast.revenue?.high)})</span></dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-slate-400">Decision point</dt>
              <dd className="text-sm text-slate-800">{forecast.decision_point?.kind ?? '—'} {day(forecast.decision_point?.at)}</dd>
            </div>
          </div>
        </div>
      )}

      {/* Interventions */}
      <div className="rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Interventions</h2>
        {!canAct && (
          <p className="mt-1 text-xs text-slate-400">Read-only for your role — approvals and reports are limited to csm/admin.</p>
        )}
        <div className="mt-3">
          <p className="mb-2 text-xs font-medium text-slate-500">Open ({open.length})</p>
          {open.length === 0 ? (
            <p className="text-sm text-slate-400">No open interventions.</p>
          ) : (
            <ul className="space-y-2">
              {open.map((iv) => (
                <InterventionRow key={iv.intervention_id} iv={iv} journey={journey} customerId={customerId} canAct={canAct} onChanged={refresh} />
              ))}
            </ul>
          )}
        </div>
        {closed.length > 0 && (
          <div className="mt-5">
            <p className="mb-2 text-xs font-medium text-slate-500">Closed ({closed.length})</p>
            <ul className="space-y-2">
              {closed.map((iv) => (
                <InterventionRow key={iv.intervention_id} iv={iv} journey={journey} customerId={customerId} canAct={canAct} onChanged={refresh} />
              ))}
            </ul>
          </div>
        )}
      </div>

      {standaloneOutcomes.length > 0 && (
        <div className="rounded-lg border border-slate-200 bg-white p-5">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Other outcomes</h2>
          <p className="mt-1 text-xs text-slate-400">Logged directly (not from a governed intervention) — same evidence, same disclosure.</p>
          <ul className="mt-3 space-y-2">
            {standaloneOutcomes.map((ep) => (
              <StandaloneOutcomeRow key={ep.episode_id} episode={ep} journey={journey} />
            ))}
          </ul>
        </div>
      )}

      <PlaybookEvaluationSection customerId={customerId} accountId={Number(accountId)} />
    </div>
  )
}
