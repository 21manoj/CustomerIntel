// The business event log, shown for one intervention or one account: what happened, when, who did it
// and why. The Activity page shows the same events for the whole tenant.
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, getActivity } from '../api/client'
import type { BusinessEvent } from '../api/types'

export function eventTime(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

const ACTOR_KINDS: Record<string, string> = {
  user: 'Person',
  policy: 'Policy',
  agent: 'Agent',
  system: 'System',
  customer: 'Tenant key',
  server: 'Server key',
  local: 'Local',
}

export function actorText(e: BusinessEvent): string {
  const kind = e.actor_kind ? (ACTOR_KINDS[e.actor_kind] ?? e.actor_kind) : null
  if (!kind) return e.actor ?? '—'
  return e.actor && e.actor !== e.actor_kind ? `${kind} (${e.actor})` : kind
}

export function stepText(e: BusinessEvent): string | null {
  if (e.step_number == null) return null
  const of = e.steps_total ? ` of ${e.steps_total}` : ''
  return `Step ${e.step_number}${of}${e.action_class ? `: ${e.action_class}` : ''}`
}

// The one extra fact worth a line for this kind of event, beyond who and when.
export function eventNote(e: BusinessEvent): string | null {
  const d = e.detail ?? {}
  const parts: string[] = []
  if (e.reason) parts.push(e.reason)
  if (e.event_type === 'sent' && e.delivery_status) parts.push(`delivery: ${e.delivery_status}`)
  if (e.outcome_type) parts.push(`outcome: ${e.outcome_type}`)
  if (d.from != null && d.to != null) parts.push(`level ${String(d.from)} to ${String(d.to)}`)
  if (e.event_type === 'agent_drafted' && d.steps != null) parts.push(`${String(d.steps)} step(s) drafted`)
  return parts.length ? parts.join(' · ') : null
}

const TONE: Record<string, string> = {
  failed: 'bg-red-500',
  declined: 'bg-red-500',
  cancelled: 'bg-slate-400',
  done: 'bg-emerald-500',
  autonomy_lowered: 'bg-amber-500',
  stop_switch_on: 'bg-amber-500',
}

interface Props {
  customerId: number
  interventionId?: number
  accountId?: number
  limit?: number
  // Changing this refetches (for example after an approve or a report on the same page).
  refreshKey?: unknown
}

export default function EventTimeline({ customerId, interventionId, accountId, limit = 50, refreshKey }: Props) {
  const [events, setEvents] = useState<BusinessEvent[] | null>(null)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setError(null)
    getActivity(customerId, { interventionId, accountId, limit })
      .then((res) => {
        if (!live) return
        setEvents(res.events)
        setTotal(res.total)
      })
      .catch((err) => live && setError(err instanceof ApiError ? err.message : 'Failed to load the history.'))
    return () => {
      live = false
    }
  }, [customerId, interventionId, accountId, limit, refreshKey])

  if (error) return <p className="text-sm text-red-600">{error}</p>
  if (events == null) return <p className="text-sm text-slate-400">Loading…</p>
  if (events.length === 0) return <p className="text-sm text-slate-500">No events recorded yet.</p>

  const more = new URLSearchParams({ customer_id: String(customerId) })
  if (accountId != null) more.set('account_id', String(accountId))
  if (interventionId != null) more.set('intervention_id', String(interventionId))

  return (
    <div>
      <ol className="space-y-3 border-l border-slate-200 pl-4">
        {events.map((e) => {
          const step = stepText(e)
          const note = eventNote(e)
          return (
            <li key={e.id} className="relative">
              <span className={`absolute -left-[21px] top-1.5 h-2 w-2 rounded-full ${TONE[e.event_type] ?? 'bg-slate-300'}`} />
              <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
                <span className="font-medium text-slate-900">{e.event}</span>
                {step && <span className="text-slate-500">{step}</span>}
                {interventionId == null && e.playbook_id && <span className="text-slate-500">{e.playbook_id}</span>}
              </div>
              <div className="text-xs text-slate-500">
                {eventTime(e.at)} · {actorText(e)}
                {e.backfilled && <span title="Rebuilt from earlier history"> · rebuilt</span>}
              </div>
              {note && <div className="mt-0.5 text-xs text-slate-600">{note}</div>}
            </li>
          )
        })}
      </ol>
      {total > events.length && (
        <Link to={`/activity?${more.toString()}`} className="mt-3 inline-block text-xs font-medium text-slate-500 hover:text-slate-800">
          Showing the latest {events.length} of {total}. See all in Activity
        </Link>
      )}
    </div>
  )
}
