import { useMemo } from 'react'
import type { Episode, Journey } from '../../api/types'

// Palette ported from docs/design/mocks/journey-canvas-zenith.html (journey-canvas.md §6) —
// the validated data-ink tokens. Scoped under .jc-canvas so it never leaks into the rest of
// the app's Tailwind/slate chrome. Light-only for now: the rest of the app has no dark theme
// or toggle yet, so auto-switching this one component via prefers-color-scheme would read as
// a stray dark box rather than a real dark mode — revisit when the app itself supports theming.
const TOKENS = `
.jc-canvas {
  --jc-surface: #fcfdfb; --jc-surface-2: #eef1ed; --jc-ink: #101816; --jc-ink-2: #4d5955; --jc-ink-3: #7f8b87;
  --jc-hair: #dfe3df; --jc-hair-2: #cfd5d1; --jc-accent: #1f5f5b;
  --jc-trailing: #2a78d6; --jc-leading: #eda100; --jc-neg: #d03b3b; --jc-pos: #0ca30c; --jc-neutral: #7f8b87;
  --jc-wash-neg: rgba(208,59,59,.07); --jc-wash-pos: rgba(12,163,12,.07);
  --jc-wash-trailing: rgba(42,120,214,.08); --jc-wash-leading: rgba(237,161,0,.10);
  background: var(--jc-surface); border: 1px solid var(--jc-hair); color-scheme: light;
}
.jc-canvas text { font: 11px system-ui, sans-serif; fill: var(--jc-ink-3); }
.jc-canvas .jc-phase-label { font-weight: 600; letter-spacing: .04em; text-transform: uppercase; font-size: 10px; }
.jc-canvas .jc-lane-label { font-size: 10px; letter-spacing: .04em; text-transform: uppercase; }
`

const PHASE_FILL: Record<string, string> = {
  baseline: 'var(--jc-surface-2)',
  deterioration: 'var(--jc-wash-neg)',
  intervention: 'var(--jc-wash-trailing)',
  resolution: 'var(--jc-wash-pos)',
}

const MARGIN = { top: 20, right: 24, bottom: 26, left: 150 }
const HEALTH_H = 150
const LANE_GAP = 34
const PRESENCE_H = 20
const DAY_MS = 86_400_000

function parseDate(s: string | null | undefined): number | null {
  if (!s) return null
  const t = Date.parse(s)
  return Number.isNaN(t) ? null : t
}

function monthLabel(ms: number): string {
  return new Date(ms).toLocaleDateString('en-US', { month: 'short', year: '2-digit' })
}

function dayLabel(ms: number): string {
  return new Date(ms).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

const NEG_BUCKETS = new Set(['lost', 'at_risk'])
const POS_BUCKETS = new Set(['expansion', 'protected'])
const REALIZED_BUCKETS = new Set(['lost', 'expansion'])

type RosterRow = { label: string; name: string | null }

export default function JourneyCanvasSvg({ journey }: { journey: Journey }) {
  const width = 1100
  const roster: RosterRow[] = [
    { label: 'Champion', name: journey.account.champion },
    { label: 'Executive sponsor', name: journey.account.executive_sponsor },
    { label: 'CSM', name: journey.account.csm },
  ].filter((r) => r.name)

  const model = useMemo(() => {
    const phases = journey.phases ?? []
    const series = journey.leading_vs_trailing?.series ?? []
    const episodes = journey.episodes ?? []

    const dates: number[] = []
    for (const p of phases) {
      const a = parseDate(p.entered_at)
      if (a != null) dates.push(a)
      const b = parseDate(p.exited_at)
      if (b != null) dates.push(b)
    }
    for (const s of series) {
      const d = parseDate(s.month)
      if (d != null) dates.push(d)
    }
    for (const e of episodes) {
      const d = parseDate(e.date)
      if (d != null) dates.push(d)
    }
    const asOf = parseDate(journey.as_of)
    if (asOf != null) dates.push(asOf)

    if (!dates.length) return null

    let domainStart = Math.min(...dates)
    let domainEnd = Math.max(...dates)
    if (domainEnd - domainStart < DAY_MS * 14) {
      const mid = (domainStart + domainEnd) / 2
      domainStart = mid - DAY_MS * 15
      domainEnd = mid + DAY_MS * 15
    }
    const pad = (domainEnd - domainStart) * 0.03
    domainStart -= pad
    domainEnd += pad

    return { phases, series, episodes, domainStart, domainEnd }
  }, [journey])

  if (!model) {
    return <p className="text-sm text-slate-400">Not enough dated history to render a canvas yet.</p>
  }
  const { phases, series, episodes, domainStart, domainEnd } = model

  const plotLeft = MARGIN.left
  const plotWidth = width - MARGIN.left - MARGIN.right
  const x = (ms: number) => plotLeft + ((ms - domainStart) / (domainEnd - domainStart)) * plotWidth

  const healthTop = MARGIN.top
  const y = (v: number) => healthTop + (1 - Math.max(0, Math.min(100, v)) / 100) * HEALTH_H

  const laneSignalY = healthTop + HEALTH_H + 30
  const laneDecisionY = laneSignalY + LANE_GAP
  const laneOutcomeY = laneDecisionY + LANE_GAP
  const presenceTop = laneOutcomeY + LANE_GAP
  const plotBottom = presenceTop + roster.length * PRESENCE_H
  const height = plotBottom + MARGIN.bottom

  // Phase backdrop bands, full plot height, painted first.
  const phaseBands = phases.map((p, i) => {
    const startMs = parseDate(p.entered_at) ?? domainStart
    const endMs = parseDate(p.exited_at) ?? domainEnd
    return { key: `phase-${i}`, x1: x(startMs), x2: x(endMs), name: p.name, midMs: (startMs + endMs) / 2 }
  })

  // Trailing (kpi_only) step line + leading (qual) line + the shaded divergence gap.
  const trailingPts = series.filter((s) => s.kpi_only != null).map((s) => ({ ms: parseDate(s.month)!, v: s.kpi_only as number }))
  const leadingPts = series.filter((s) => s.qual != null).map((s) => ({ ms: parseDate(s.month)!, v: s.qual as number }))
  const warnPts = journey.leading_vs_trailing?.divergence_warning_pts ?? 10

  let trailingPath = ''
  trailingPts.forEach((p, i) => {
    if (i === 0) trailingPath += `M ${x(p.ms)} ${y(p.v)}`
    else {
      trailingPath += ` L ${x(p.ms)} ${y(trailingPts[i - 1].v)}`
      trailingPath += ` L ${x(p.ms)} ${y(p.v)}`
    }
  })
  const leadingPath = leadingPts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(p.ms)} ${y(p.v)}`).join(' ')

  // Contiguous runs where both layers are known and |divergence| >= warning threshold.
  const gapRuns: { path: string; kind: 'early_warning' | 'recovery_watch' }[] = []
  let run: typeof series = []
  let runKind: 'early_warning' | 'recovery_watch' | null = null
  const flushRun = () => {
    if (run.length >= 2 && runKind) {
      const fwd = run.map((s, i) => `${i === 0 ? 'M' : 'L'} ${x(parseDate(s.month)!)} ${y(s.kpi_only as number)}`).join(' ')
      const back = [...run].reverse().map((s) => `L ${x(parseDate(s.month)!)} ${y(s.qual as number)}`).join(' ')
      gapRuns.push({ path: `${fwd} ${back} Z`, kind: runKind })
    }
    run = []
    runKind = null
  }
  for (const s of series) {
    const div = s.divergence
    const kind = div != null && div <= -warnPts ? 'early_warning' : div != null && div >= warnPts ? 'recovery_watch' : null
    if (kind && s.kpi_only != null && s.qual != null && (runKind === null || runKind === kind)) {
      runKind = kind
      run.push(s)
    } else {
      flushRun()
      if (kind && s.kpi_only != null && s.qual != null) {
        runKind = kind
        run.push(s)
      }
    }
  }
  flushRun()

  // Expected path overlay: the arc template's typical trajectory, aligned phase-by-phase
  // (by order, not by name) onto this account's actual phase boundaries — a prior, drawn
  // dotted, never confused with observed data (journey-canvas.md §2).
  const expectedSegments: { x1: number; x2: number; y1: number; y2: number }[] = []
  const ep = journey.expected_path
  if (ep?.phases?.length) {
    for (let i = 0; i < Math.min(ep.phases.length, phases.length); i++) {
      const actual = phases[i]
      const tpl = ep.phases[i]
      if (tpl.health_start == null || tpl.health_end == null) continue
      const startMs = parseDate(actual.entered_at) ?? domainStart
      const endMs = parseDate(actual.exited_at) ?? domainEnd
      expectedSegments.push({ x1: x(startMs), x2: x(endMs), y1: y(tpl.health_start), y2: y(tpl.health_end) })
    }
  }

  // Episode markers by lane (signal / decision / outcome), offsetting same-day clusters
  // vertically so they don't sit exactly on top of one another.
  const laneY: Record<string, number> = { signal: laneSignalY, decision: laneDecisionY, outcome: laneOutcomeY }
  const groups = new Map<string, Episode[]>()
  for (const e of episodes) {
    if (!laneY[e.kind]) continue
    const key = `${e.kind}:${e.date.slice(0, 10)}`
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(e)
  }
  const markers = episodes
    .filter((e) => laneY[e.kind] != null && parseDate(e.date) != null)
    .map((e) => {
      const group = groups.get(`${e.kind}:${e.date.slice(0, 10)}`)!
      const idx = group.indexOf(e)
      const offset = (idx - (group.length - 1) / 2) * 10
      const cx = x(parseDate(e.date)!)
      const cy = laneY[e.kind] + offset
      let fill = 'var(--jc-neutral)'
      let stroke: string | null = null
      let hollow = false
      if (e.kind === 'signal') {
        fill = e.polarity > 0 ? 'var(--jc-pos)' : e.polarity < 0 ? 'var(--jc-neg)' : 'var(--jc-neutral)'
      } else if (e.kind === 'decision') {
        fill = 'var(--jc-accent)'
      } else if (e.kind === 'outcome') {
        const neg = NEG_BUCKETS.has(e.revenue_bucket ?? '')
        const pos = POS_BUCKETS.has(e.revenue_bucket ?? '')
        hollow = !REALIZED_BUCKETS.has(e.revenue_bucket ?? '')
        const base = neg ? 'var(--jc-neg)' : pos ? 'var(--jc-pos)' : 'var(--jc-neutral)'
        if (hollow) {
          stroke = base
          fill = 'var(--jc-surface)'
        } else {
          fill = base
        }
      }
      const r = e.kind === 'outcome' && e.revenue ? Math.min(14, 5 + Math.abs(e.revenue) / 300_000) : 5
      return { key: `ep-${e.episode_id}`, cx, cy, r, fill, stroke, hollow, title: `${e.date.slice(0, 10)} — ${e.title}` }
    })

  // Stakeholder presence: dated evidence citing this person, against a fixed roster
  // (champion / executive sponsor / CSM). journey-canvas.md §2 "people on the timeline";
  // a row with zero ticks is flagged, not hidden — same honesty rule as everywhere else.
  const presenceRows = roster.map((r, i) => {
    const rowY = presenceTop + i * PRESENCE_H + PRESENCE_H / 2
    const ticks = Object.values(journey.evidence)
      .filter((ev) => ev.person?.name === r.name && ev.occurred_at)
      .map((ev) => ({ ms: parseDate(ev.occurred_at)!, id: ev.node_id }))
      .filter((t) => t.ms != null)
    return { ...r, rowY, ticks }
  })

  // Month tick marks along the bottom, thinned if the range spans many months.
  const months: number[] = []
  {
    const d = new Date(domainStart)
    d.setDate(1)
    while (d.getTime() <= domainEnd) {
      months.push(d.getTime())
      d.setMonth(d.getMonth() + 1)
    }
  }
  const tickStride = Math.max(1, Math.ceil(months.length / 14))
  const ticks = months.filter((_, i) => i % tickStride === 0)

  return (
    <div className="jc-canvas overflow-x-auto rounded-lg">
      <style>{TOKENS}</style>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" style={{ display: 'block', minWidth: 700 }}>
        {phaseBands.map((b) => (
          <g key={b.key}>
            <rect x={b.x1} y={healthTop} width={Math.max(0, b.x2 - b.x1)} height={plotBottom - healthTop} fill={PHASE_FILL[b.name] ?? PHASE_FILL.baseline} />
            <text x={(b.x1 + b.x2) / 2} y={healthTop - 6} textAnchor="middle" className="jc-phase-label">{b.name}</text>
          </g>
        ))}

        {/* lane separators + labels */}
        {(['Signals', 'Decisions', 'Outcomes'] as const).map((label, i) => {
          const ly = [laneSignalY, laneDecisionY, laneOutcomeY][i]
          return (
            <g key={label}>
              <line x1={plotLeft} x2={width - MARGIN.right} y1={ly + LANE_GAP / 2 - 1} y2={ly + LANE_GAP / 2 - 1} stroke="var(--jc-hair)" />
              <text x={plotLeft - 8} y={ly + 3} textAnchor="end" className="jc-lane-label">{label}</text>
            </g>
          )
        })}

        {/* divergence gap shading */}
        {gapRuns.map((g, i) => (
          <path key={`gap-${i}`} d={g.path} fill={g.kind === 'early_warning' ? 'var(--jc-wash-leading)' : 'var(--jc-wash-pos)'} />
        ))}

        {/* expected path overlay (prior, dotted) */}
        {expectedSegments.map((s, i) => (
          <line key={`exp-${i}`} x1={s.x1} x2={s.x2} y1={s.y1} y2={s.y2} stroke="var(--jc-ink-3)" strokeWidth={1.5} strokeDasharray="1 4" strokeLinecap="round" />
        ))}

        {/* trailing (kpi) + leading (behavioral) lines */}
        <path d={trailingPath} fill="none" stroke="var(--jc-trailing)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        <path d={leadingPath} fill="none" stroke="var(--jc-leading)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />

        {/* episode markers */}
        {markers.map((m) => (
          <circle key={m.key} cx={m.cx} cy={m.cy} r={m.r} fill={m.fill} stroke={m.hollow ? m.stroke ?? undefined : 'var(--jc-surface)'} strokeWidth={m.hollow ? 2 : 1.5}>
            <title>{m.title}</title>
          </circle>
        ))}

        {/* stakeholder presence rows */}
        {presenceRows.map((r) => (
          <g key={r.label}>
            <text x={plotLeft - 8} y={r.rowY + 3} textAnchor="end" className="jc-lane-label">{r.label}</text>
            <line x1={plotLeft} x2={width - MARGIN.right} y1={r.rowY} y2={r.rowY} stroke="var(--jc-hair-2)" strokeWidth={2} strokeLinecap="round" />
            {r.ticks.map((t, i) => (
              <circle key={i} cx={x(t.ms)} cy={r.rowY} r={4} fill="var(--jc-accent)">
                <title>{r.name} — {dayLabel(t.ms)}</title>
              </circle>
            ))}
            {r.ticks.length === 0 && (
              <text x={plotLeft + 4} y={r.rowY - 6} className="jc-lane-label" fill="var(--jc-neg)">no dated evidence</text>
            )}
          </g>
        ))}

        {/* x axis */}
        <line x1={plotLeft} x2={width - MARGIN.right} y1={plotBottom} y2={plotBottom} stroke="var(--jc-hair)" />
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={healthTop} y2={plotBottom} stroke="var(--jc-hair)" strokeDasharray="1 3" opacity={0.6} />
            <text x={x(t)} y={plotBottom + 16} textAnchor="middle">{monthLabel(t)}</text>
          </g>
        ))}
      </svg>
    </div>
  )
}
