import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import Graph3D from '../components/Graph3D'
import { esc, hasCycle, reaches, type G3Link, type G3Node } from '../components/graph3dUtil'
import { ApiError, getAccount, getAccountGraph } from '../api/client'
import type { AccountGraph, Episode, GraphNode, Journey, Phase } from '../api/types'
import { useAuth } from '../auth/AuthContext'

// Fixed colours, so a kind keeps its colour whatever else is on screen.
const PHASES: Record<string, { color: string; wash: string }> = {
  baseline: { color: '#7f8b87', wash: '#eef1ed' },
  deterioration: { color: '#d03b3b', wash: '#fbe9e9' },
  intervention: { color: '#eda100', wash: '#fdf3d6' },
  resolution: { color: '#0ca30c', wash: '#e4f5e4' },
}
const EPISODES: { kind: string; label: string; color: string }[] = [
  { kind: 'signal', label: 'Signal', color: '#2a78d6' },
  { kind: 'health_transition', label: 'Health change', color: '#8b5cc7' },
  { kind: 'intervention', label: 'Intervention', color: '#1f5f5b' },
  { kind: 'outcome', label: 'Outcome', color: '#101816' },
  { kind: 'renewal', label: 'Renewal date', color: '#c2408f' },
]
const NODE_KINDS: { type: string; label: string; color: string }[] = [
  { type: 'SIGNAL', label: 'Signal', color: '#2a78d6' },
  { type: 'INTERVENTION', label: 'Intervention', color: '#1f5f5b' },
  { type: 'DECISION', label: 'Decision', color: '#8b5cc7' },
  { type: 'OUTCOME', label: 'Outcome', color: '#101816' },
]
const INK = '#4d5955'
const FAINT = '#b9c2be'
const CAUSAL = '#c2570a'

const words = (s: string | null | undefined) => (s ?? '').replace(/_/g, ' ')
const day = (s: string | null | undefined) => (s ? s.slice(0, 10) : 'date not recorded')
const ms = (s: string) => new Date(s.length <= 10 ? `${s}T00:00:00` : s).getTime()
const monthYear = (s: string) => new Date(ms(s)).toLocaleDateString('en-US', { month: 'short', year: 'numeric' })
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
const phaseName = (p: Phase) => cap(words(p.label ?? p.name))
const episodeKind = (kind: string) => EPISODES.find((k) => k.kind === kind) ?? { kind, label: cap(words(kind)), color: '#7f8b87' }
const health = (p: Phase) =>
  p.health_start == null || p.health_end == null ? null : `health ${Math.round(p.health_start)} → ${Math.round(p.health_end)}`

type Tab = 'journey' | 'causal'
type Picked = { title: string; meta: string; body: string | null; foot?: string | null }

function Legend({ items }: { items: { label: string; color: string; line?: boolean }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
      {items.map((k) => (
        <li key={k.label} className="flex items-center gap-1.5">
          <span className={k.line ? 'inline-block h-0.5 w-5' : 'inline-block h-2.5 w-2.5 rounded-full'} style={{ background: k.color }} />
          {k.label}
        </li>
      ))}
    </ul>
  )
}

function Stage({ nodes, links, dag, picked, onSelect, onClose }: {
  nodes: G3Node[]; links: G3Link[]; dag: boolean; picked: Picked | null; onSelect: (id: string | null) => void; onClose: () => void
}) {
  return (
    <div className="relative">
      <Graph3D nodes={nodes} links={links} dag={dag} onSelect={onSelect} />
      {picked && (
        <aside className="absolute right-3 top-3 w-72 rounded-lg border border-slate-200 bg-white p-4 text-sm shadow-sm">
          <div className="flex items-start justify-between gap-2">
            <p className="font-semibold text-slate-900">{picked.title}</p>
            <button onClick={onClose} className="text-slate-400 hover:text-slate-700" aria-label="Close">×</button>
          </div>
          <p className="mt-0.5 text-xs text-slate-500">{picked.meta}</p>
          {picked.body && <p className="mt-2 text-slate-700">{picked.body}</p>}
          {picked.foot && <p className="mt-2 text-xs text-slate-500">{picked.foot}</p>}
        </aside>
      )}
    </div>
  )
}

// ── Journey: phases in order, the event that moved the account from one to the next, and what happened in each ──

// Which phase an episode falls in, by date. Anything before the first phase counts with the first.
function phaseIndex(phases: Phase[], date: string): number {
  const t = ms(date)
  let at = 0
  phases.forEach((p, i) => {
    if (t >= ms(p.entered_at)) at = i
  })
  return at
}

function buildJourney(journey: Journey, graph: AccountGraph | null, withCausal: boolean) {
  const phases = journey.phases ?? []
  const episodes = journey.episodes ?? []
  const byEpisode = new Map(episodes.map((e) => [e.episode_id, e]))
  const triggers = new Map<string, number>() // episode id → index of the phase it started
  phases.forEach((p, i) => {
    if (i > 0 && p.trigger_episode_id && byEpisode.has(p.trigger_episode_id)) triggers.set(p.trigger_episode_id, i)
  })

  const nodes: G3Node[] = phases.map((p, i) => ({
    id: `phase:${i}`,
    color: PHASES[p.name]?.color ?? '#7f8b87',
    size: 9,
    text: [phaseName(p), `${monthYear(p.entered_at)} – ${p.exited_at ? monthYear(p.exited_at) : 'now'}`],
    tip: `<b>${esc(phaseName(p))}</b> · ${esc(day(p.entered_at))} to ${p.exited_at ? esc(day(p.exited_at)) : 'now'}${health(p) ? `<br>${esc(health(p) as string)}` : ''}`,
  }))
  episodes.forEach((e) => {
    const k = episodeKind(e.kind)
    nodes.push({
      id: e.episode_id,
      color: k.color,
      size: triggers.has(e.episode_id) ? 2.5 : 1,
      tip: `<b>${esc(k.label)}${e.subtype ? ` · ${esc(words(e.subtype))}` : ''}</b> · ${esc(day(e.date))}<br>${esc((e.title ?? '').slice(0, 180))}`,
    })
  })

  const links: G3Link[] = []
  phases.forEach((p, i) => {
    if (i === 0) return
    const trigger = p.trigger_episode_id && byEpisode.has(p.trigger_episode_id) ? p.trigger_episode_id : null
    const move = `${esc(phaseName(phases[i - 1]))} → ${esc(phaseName(p))} on ${esc(day(p.entered_at))}`
    if (trigger) {
      links.push({ source: `phase:${i - 1}`, target: trigger, color: INK, width: 1.4, tip: move })
      links.push({ source: trigger, target: `phase:${i}`, color: INK, width: 1.4, tip: `${move}<br>started by this event` })
    } else {
      links.push({ source: `phase:${i - 1}`, target: `phase:${i}`, color: INK, width: 1.4, tip: `${move}<br>no single starting event recorded` })
    }
  })
  if (phases.length > 0) {
    episodes.forEach((e) => {
      if (triggers.has(e.episode_id)) return
      const i = phaseIndex(phases, e.date)
      links.push({ source: `phase:${i}`, target: e.episode_id, color: FAINT, width: 0.3, tip: `happened during ${esc(phaseName(phases[i]))}` })
    })
  }

  // Recorded cause-and-effect between pieces of evidence, drawn on top. One that would run against
  // the order of the journey is left out (and counted), because the layout cannot hold a loop.
  let causalDrawn = 0
  let causalSkipped = 0
  if (withCausal && graph) {
    const episodeOf = new Map<number, string>()
    episodes.forEach((e) => (e.evidence_node_ids ?? []).forEach((id) => episodeOf.set(id, e.episode_id)))
    const seen = new Set<string>()
    graph.edges.forEach((edge) => {
      const a = episodeOf.get(edge.from_node_id)
      const b = episodeOf.get(edge.to_node_id)
      if (!a || !b || a === b || seen.has(`${a}>${b}`)) return
      seen.add(`${a}>${b}`)
      if (reaches(links, b, a)) {
        causalSkipped += 1
        return
      }
      links.push({ source: a, target: b, color: CAUSAL, width: 0.8, tip: `led to · ${edge.evidence_tier === 'observed' ? 'recorded link' : 'inferred link'}` })
      causalDrawn += 1
    })
  }
  return { nodes, links, causalDrawn, causalSkipped, triggers }
}

function Timeline({ journey }: { journey: Journey }) {
  const phases = journey.phases
  const start = ms(phases[0].entered_at)
  const end = Math.max(ms(journey.as_of), ms(phases[phases.length - 1].entered_at) + 30 * 86400000)
  const span = end - start
  const pct = (t: number) => `${Math.min(100, Math.max(0, ((t - start) / span) * 100))}%`
  const inRange = journey.episodes.filter((e) => ms(e.date) >= start && ms(e.date) <= end)
  const later = journey.episodes.filter((e) => ms(e.date) > end)
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Timeline</p>
      <div className="mt-3 overflow-x-auto">
        <div className="min-w-[560px]">
          <div className="flex h-12 overflow-hidden rounded-md border border-slate-200">
            {phases.map((p, i) => {
              const to = p.exited_at ? ms(p.exited_at) : end
              return (
                <div key={i} className="min-w-0 border-l border-white px-2 py-1 first:border-l-0"
                  style={{ flexGrow: Math.max(to - ms(p.entered_at), span * 0.04), flexBasis: 0, background: PHASES[p.name]?.wash ?? '#eef1ed', borderTop: `3px solid ${PHASES[p.name]?.color ?? '#7f8b87'}` }}>
                  <p className="truncate text-xs font-semibold text-slate-800">{phaseName(p)}</p>
                  <p className="truncate text-[11px] text-slate-500">{health(p) ?? ''}</p>
                </div>
              )
            })}
          </div>
          <div className="relative mt-1 h-4">
            {inRange.map((e) => (
              <span key={e.episode_id} title={`${episodeKind(e.kind).label} · ${day(e.date)} · ${e.title ?? ''}`}
                className="absolute top-1 h-2 w-2 -translate-x-1/2 rounded-full" style={{ left: pct(ms(e.date)), background: episodeKind(e.kind).color }} />
            ))}
          </div>
          <div className="relative h-5 text-[11px] text-slate-500">
            {phases.map((p, i) => (
              <span key={i} className="absolute whitespace-nowrap" style={{ left: pct(ms(p.entered_at)) }}>{day(p.entered_at)}</span>
            ))}
            <span className="absolute right-0 whitespace-nowrap">as of {day(journey.as_of)}</span>
          </div>
        </div>
      </div>
      {later.length > 0 && (
        <p className="mt-2 text-xs text-slate-500">
          After the as-of date: {later.map((e) => `${episodeKind(e.kind).label.toLowerCase()} on ${day(e.date)}`).join(', ')}.
        </p>
      )}
    </div>
  )
}

function JourneyView({ journey, graph }: { journey: Journey; graph: AccountGraph | null }) {
  const [withCausal, setWithCausal] = useState(false)
  const [pickedId, setPickedId] = useState<string | null>(null)
  const built = useMemo(() => buildJourney(journey, graph, withCausal), [journey, graph, withCausal])
  const phases = useMemo(() => journey.phases ?? [], [journey])
  const hasCausal = (graph?.edges.length ?? 0) > 0

  const picked = useMemo<Picked | null>(() => {
    if (!pickedId) return null
    if (pickedId.startsWith('phase:')) {
      const p = phases[Number(pickedId.slice(6))]
      if (!p) return null
      const n = journey.episodes.filter((e) => phaseIndex(phases, e.date) === phases.indexOf(p)).length
      return { title: phaseName(p), meta: `${day(p.entered_at)} to ${p.exited_at ? day(p.exited_at) : 'now'}`, body: health(p) ? cap(health(p) as string) : null, foot: `${n} recorded events in this phase` }
    }
    const e = journey.episodes.find((x) => x.episode_id === pickedId) as Episode | undefined
    if (!e) return null
    const started = built.triggers.get(e.episode_id)
    return {
      title: `${episodeKind(e.kind).label}${e.subtype ? ` · ${words(e.subtype)}` : ''}`,
      meta: day(e.date),
      body: e.title,
      foot: started != null ? `This event started the ${phaseName(phases[started]).toLowerCase()} phase.` : null,
    }
  }, [pickedId, journey, phases, built])

  if (phases.length === 0) {
    return <p className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">No journey phases are recorded for this account yet.</p>
  }
  const kinds = EPISODES.filter((k) => journey.episodes.some((e) => e.kind === k.kind))
  return (
    <>
      <p className="text-sm text-slate-500">
        {phases.length} {phases.length === 1 ? 'phase' : 'phases'} · {phases.length - 1} {phases.length === 2 ? 'transition' : 'transitions'} · {journey.episodes.length} recorded events
        {phases.length === 1 && ' · this account has stayed in one phase, so there is no transition to draw'}
      </p>
      <Timeline journey={journey} />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Legend items={[
          ...phases.filter((p, i) => phases.findIndex((q) => q.name === p.name) === i).map((p) => ({ label: `${phaseName(p)} phase`, color: PHASES[p.name]?.color ?? '#7f8b87' })),
          ...kinds.map((k) => ({ label: k.label, color: k.color })),
          { label: 'Transition', color: INK, line: true },
          ...(withCausal && built.causalDrawn > 0 ? [{ label: 'Led to', color: CAUSAL, line: true }] : []),
        ]} />
        {hasCausal && (
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input type="checkbox" checked={withCausal} onChange={(e) => setWithCausal(e.target.checked)} />
            Show causal links
          </label>
        )}
      </div>
      {withCausal && built.causalSkipped > 0 && (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          {built.causalSkipped} causal {built.causalSkipped === 1 ? 'link runs' : 'links run'} against the order of the journey and {built.causalSkipped === 1 ? 'is' : 'are'} not drawn here. The causal links tab shows all of them.
        </p>
      )}
      <Stage nodes={built.nodes} links={built.links} dag picked={picked} onSelect={setPickedId} onClose={() => setPickedId(null)} />
      <details className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
        <summary className="cursor-pointer font-medium text-slate-700">The same journey as a list</summary>
        <ol className="mt-3 space-y-3 text-slate-600">
          {phases.map((p, i) => {
            const trigger = p.trigger_episode_id ? journey.episodes.find((e) => e.episode_id === p.trigger_episode_id) : null
            return (
              <li key={i}>
                <span className="font-medium text-slate-800">{phaseName(p)}</span> · {day(p.entered_at)} to {p.exited_at ? day(p.exited_at) : 'now'}
                {health(p) && ` · ${health(p)}`}
                {i > 0 && <span className="block text-slate-500">Started by: {trigger ? `${trigger.title} (${day(trigger.date)})` : 'no single event recorded'}</span>}
              </li>
            )
          })}
        </ol>
      </details>
    </>
  )
}

// ── Causal links: which piece of evidence led to which ──

function CausalView({ graph }: { graph: AccountGraph }) {
  const [linkedOnly, setLinkedOnly] = useState(true)
  const [picked, setPicked] = useState<GraphNode | null>(null)
  const data = useMemo(() => {
    const ids = new Set(graph.nodes.map((n) => n.node_id))
    // The same signal can be linked to the same outcome more than once; one line per pair is enough.
    const seen = new Set<string>()
    const edges = graph.edges.filter((e) => {
      const key = `${e.from_node_id}>${e.to_node_id}`
      if (e.from_node_id === e.to_node_id || !ids.has(e.from_node_id) || !ids.has(e.to_node_id) || seen.has(key)) return false
      seen.add(key)
      return true
    })
    const touched = new Set(edges.flatMap((e) => [e.from_node_id, e.to_node_id]))
    const links: G3Link[] = edges.map((e) => ({
      source: String(e.from_node_id),
      target: String(e.to_node_id),
      color: e.evidence_tier === 'observed' ? INK : FAINT,
      tip: `led to · ${e.evidence_tier === 'observed' ? 'recorded link' : 'inferred link'}${e.derivation ? ` (${esc(words(e.derivation))})` : ''}`,
    }))
    return { edges, links, touched, unlinked: graph.nodes.filter((n) => !touched.has(n.node_id)).length, cyclic: hasCycle(links) }
  }, [graph])
  const nodes = useMemo<G3Node[]>(() => {
    const hide = linkedOnly && data.links.length > 0
    return graph.nodes.filter((n) => !hide || data.touched.has(n.node_id)).map((n) => ({
      id: String(n.node_id),
      color: NODE_KINDS.find((k) => k.type === n.node_type)?.color ?? '#7f8b87',
      size: 1,
      tip: `<b>${esc(words(n.subtype) || n.node_type.toLowerCase())}</b> · ${esc(day(n.occurred_at))}<br>${esc((n.quote ?? n.title ?? '').slice(0, 180))}`,
    }))
  }, [graph, data, linkedOnly])

  if (graph.nodes.length === 0) {
    return <p className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">No evidence is recorded for this account yet.</p>
  }
  const byId = new Map(graph.nodes.map((n) => [n.node_id, n]))
  const name = (id: number) => {
    const n = byId.get(id)
    return n ? `${words(n.subtype) || n.node_type.toLowerCase()} (${day(n.occurred_at)})` : `#${id}`
  }
  return (
    <>
      <p className="text-sm text-slate-500">
        {graph.nodes.length} pieces of evidence · {data.edges.length} causal links
        {data.unlinked > 0 && ` · ${data.unlinked} not linked to anything yet`}
      </p>
      {data.edges.length === 0 && (
        <p className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-600">
          No causal links are recorded for this account, so the evidence appears as separate points. Links are
          added when an intervention cites a signal or an outcome is logged against one.
        </p>
      )}
      {data.cyclic && (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          These links form a loop, so they cannot be laid out left to right. They are shown in a free layout instead.
        </p>
      )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Legend items={[
          ...NODE_KINDS.filter((k) => graph.nodes.some((n) => n.node_type === k.type)),
          { label: 'Recorded link', color: INK, line: true },
          ...(data.edges.some((e) => e.evidence_tier !== 'observed') ? [{ label: 'Inferred link', color: FAINT, line: true }] : []),
        ]} />
        {data.edges.length > 0 && data.unlinked > 0 && (
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input type="checkbox" checked={!linkedOnly} onChange={(e) => setLinkedOnly(!e.target.checked)} />
            Show the {data.unlinked} unlinked
          </label>
        )}
      </div>
      <Stage nodes={nodes} links={data.links} dag={!data.cyclic}
        picked={picked && {
          title: words(picked.subtype) || picked.node_type.toLowerCase(),
          meta: `${NODE_KINDS.find((k) => k.type === picked.node_type)?.label ?? picked.node_type} · ${day(picked.occurred_at)}`,
          body: picked.quote ?? picked.title,
          foot: picked.person?.name ? `${picked.person.name}${picked.person.title ? `, ${picked.person.title}` : ''}` : null,
        }}
        onSelect={(id) => setPicked(id ? (byId.get(Number(id)) ?? null) : null)} onClose={() => setPicked(null)} />
      {data.edges.length > 0 && (
        <details className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
          <summary className="cursor-pointer font-medium text-slate-700">The same links as a list ({data.edges.length})</summary>
          <ul className="mt-3 space-y-1.5 text-slate-600">
            {data.edges.map((e) => (
              <li key={e.edge_id}>
                {name(e.from_node_id)} <span className="text-slate-400">led to</span> {name(e.to_node_id)}
                {e.evidence_tier !== 'observed' && <span className="text-slate-400"> · inferred</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </>
  )
}

export default function AccountGraphPage() {
  const { user } = useAuth()
  const { accountId } = useParams<{ accountId: string }>()
  const [searchParams] = useSearchParams()
  const customerId = useMemo(() => {
    const q = searchParams.get('customer_id')
    return q ? Number(q) : (user?.customer_id ?? null)
  }, [searchParams, user])

  const [journey, setJourney] = useState<Journey | null>(null)
  const [graph, setGraph] = useState<AccountGraph | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('journey')

  useEffect(() => {
    if (customerId == null || !accountId) return
    let live = true
    setLoaded(false)
    setError(null)
    setJourney(null)
    setGraph(null)
    Promise.allSettled([getAccount(customerId, Number(accountId)), getAccountGraph(customerId, Number(accountId))]).then(([j, g]) => {
      if (!live) return
      if (j.status === 'fulfilled') setJourney(j.value)
      if (g.status === 'fulfilled') setGraph(g.value)
      if (g.status === 'rejected') setError(g.reason instanceof ApiError ? g.reason.message : 'The graph could not be loaded.')
      else if (j.status === 'rejected') setTab('causal')
      setLoaded(true)
    })
    return () => {
      live = false
    }
  }, [customerId, accountId])

  const back = `/accounts/${accountId}${customerId ? `?customer_id=${customerId}` : ''}`
  if (customerId == null) {
    return <p className="text-sm text-slate-500">No customer context — open this account from the Portfolio page.</p>
  }
  if (error) return <p className="text-sm text-red-600">{error}</p>
  if (!loaded || !graph) return <p className="text-sm text-slate-400">Loading the graph…</p>

  const tabs: { id: Tab; label: string }[] = [
    { id: 'journey', label: 'Journey' },
    { id: 'causal', label: 'Causal links' },
  ]
  return (
    <div className="space-y-5">
      <Link to={back} className="text-sm text-slate-500 hover:text-slate-800">← {graph.account_name}</Link>
      {graph.synthetic && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{graph.disclosure}</div>
      )}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold text-slate-900">{graph.account_name} — journey graph</h1>
        <div className="flex rounded-md border border-slate-200 bg-white p-0.5 text-sm" role="tablist">
          {tabs.map((t) => (
            <button key={t.id} role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}
              className={`rounded px-3 py-1 ${tab === t.id ? 'bg-slate-900 text-white' : 'text-slate-600 hover:text-slate-900'}`}>
              {t.label}
            </button>
          ))}
        </div>
      </div>
      {tab === 'journey' ? (
        journey ? <JourneyView journey={journey} graph={graph} /> : (
          <p className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">No journey has been built for this account yet.</p>
        )
      ) : (
        <CausalView graph={graph} />
      )}
    </div>
  )
}
