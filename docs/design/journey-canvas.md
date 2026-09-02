# Journey Canvas — design

*2026-09-02. Design only; nothing here is built yet beyond the static mock (`docs/design/mocks/journey-canvas-zenith.html`, rendered from Zenith Data Corp's real journey v3 out of the new build). Companion to `wizard-a-assessment.md` (what a journey is) and `demo-narratives.md` (what a demo must show).*

---

## 0. Why the old graph fails

The old repo's causal-graph PNG (customer 415, account 3734) has the right spine — real calendar time on x, lanes by node type, numbered markers with an index — and the wrong content. Every defect in it is a data defect the new build already removes:

| What the PNG shows | Cause | New build |
|---|---|---|
| One signal in July "led to" outcomes in Nov, Dec and *March*, incl. "Churn risk averted" | arc-template edges bound by ordinal (`signal:1 → outcome:N`) | no template edges; only `linked_signal_id` / observed edges |
| "Urgent alert", "Arc detected" dated 2026-08-28 — five months after the story | `occurred_at = utcnow()` on system nodes | system nodes not written to the graph; all windows data-relative |
| "Usage quietly trending down" colored positive | source polarity error, rendered verbatim | role says `usage_decline` (negative); **conflict surfaced, not hidden** |
| "At risk $1.44M" and "net protected $2.16M" summed as if realized | narrative outcomes counted as ARR movement | realized NRR (lost/expansion) and narrative exposure reported as two numbers |
| Stakeholders on a static rail — the champion's disengagement has no *when* | STAKEHOLDER nodes undated | stakeholder changes are dated episodes |
| "Insufficient data — PB-DC-04" ×2 as outcomes | playbook plumbing in the outcome lane | not in the journey |

The design below is what the journey should *render as*, and the two things that belong above and beyond it.

## 1. The object: one time axis, three bands

```
┌ NOW — state header ─────────────────────────────────────────────────────────────┐
│ health (KPI) · leading · divergence label · arc + phase + evidence count ·        │
│ ARR · renewal countdown · people present · realized NRR · open warnings           │
├ THE PAST — the story ───────────────────────────── ┃ now ┃ ── THE NEXT 3–6 MONTHS ┤
│ phases (backdrop, trigger at each boundary)        ┃      │ expected next phase (prior)
│ health: KPI step line + leading curve, gap shaded  ┃      │ leading decay / silence
│ ⚑ first leading warning   ⚑ first KPI crossing     ┃      │ renewal + risk window
│ signals by role (clusters), people, actions,       ┃      │ likelihood as a range, labelled
│ outcomes (realized solid / narrative hollow)       ┃      │ tripwires · recommended action
│ story paragraph — every sentence cites an episode  ┃      │ with observed lift
└ honesty strip: evidence tiers · coverage · data origin · what the model is ──────┘
```

The bands share one x axis so "then", "now" and "next" are literally continuous. The **now line** is a control (a scrubber): drag it back and the canvas shows only what was known then. That is the backtest as a picture, and it is what makes the forecast band believable.

## 2. Band 1 — the past

**Phases are the chapters.** Baseline → deterioration → intervention → resolution as background bands from journey v3's `phases[]`, each boundary annotated with `trigger_episode_id` (the escalation, the outcome, the health move — never the decision taken in response). On Zenith: baseline Jul–Aug 25 → deterioration Sep 25–Feb 26, triggered by the "usage quietly trending down" signal → baseline again Mar 26, triggered by the churn-averted outcome.

**Two health lines, one gap.** `kpi_only` (trailing) as a step line; `qual` (leading) as a curve that decays between signals; the band between them shaded when |divergence| ≥ 10 with the month's label (`early_warning` / `recovery_watch`). Flags at `first_leading_warning_at` and `first_trailing_warning_at`, joined by a bracket labelled with `lead_days`. On Zenith the leading flag is **July 2025** and the KPI line **never crossed** — the bracket is open-ended, which is the honest reading: six months of behavioral warning, no financial event (yet).

**People on the timeline.** A stakeholder lane with presence bars; changes (champion disengaged, new CSM) are dated episodes with evidence. Roster from `STAKEHOLDER` nodes, changes from `champion_change` / `intervention` roles.

**Signals as evidence, not confetti.** Dots colored by *polarity* (status red/green, shape ▼/▲ as the second channel), lane position by *role*. Same-role signals inside one window cluster into a single mark with a count ("usage decline ×3, Oct–Nov"). A **polarity-conflict badge** where the source sentiment disagrees with the role (Zenith's Sep/Oct signals: role negative, sentiment +0.69). Duplicated source rows (same content, different `signal_ref`) render once with "×2".

**Actions and outcomes.** Decisions and CSM interventions in one lane, each with its before/after window from `counterfactual_hooks` (shaded 90 days either side; lift in points). Outcomes: **solid** bars for ARR that actually moved (`lost` / `expansion`), **hollow** markers for narrative annotations (`at_risk` / `protected`). Zenith: two hollow at-risk markers (−$960K, −$480K), two hollow protected (+$240K, +$1.92M), zero solid — nothing was lost or booked.

**Expected path.** The arc template's typical health trajectory, dotted, aligned at phase entry (`expected_path.phases`). Zenith's `competitive_displacement` template expects 62 at the trough and 80 at the end; actual: 56.7 and 71.3.

**Edges.** Only `linked_signal_id` and observed edges, drawn as light connectors from a signal to the outcome it fed; never template edges. Tooltip shows `evidence_tier`.

**The story paragraph.** Generated from episodes in order, chapter per phase, **every sentence citing an episode id**; a sentence that can't cite is not written. Template first; LLM phrasing later, under the same citation rule.

## 3. Band 2 — now (the header is a decision surface)

Left to right: **KPI health** (the only number that feeds anything financial) · **leading** with the divergence label and its meaning in one phrase ("behavior ahead of the numbers" / "numbers ahead of behavior") · **arc + phase + "6 episodes cite this"** with the alternative it almost was · ARR and products · **renewal countdown with the 90-day risk window** (from the days-to-renewal band) · people present (champion ✓ / exec sponsor ✓ / CSM ✓) · **realized NRR** (lost/expansion only) beside **narrative exposure** — two numbers, never one · open warnings (stories not yet judged). Account details (`profile_metadata`) live in a drawer, not the header.

## 4. Band 3 — the next 3–6 months

Design rule: **every forward statement carries its basis, its confidence semantics, and what would change it.** Four elements, in order of how much they can be trusted:

1. **Leading trajectory (mechanical).** The behavioral composite holds while its signals are inside the 60-day window and then goes *silent* — the canvas says "no claim after May 31 unless new signals arrive," not a made-up curve.
2. **Expected next phase (prior).** From the arc template and this tenant's own transition matrix (Wizard B): "baseline → deterioration happened 8 of 15 times on this tenant, after ~1.4 months, most often triggered by usage decline." Labelled *prior*.
3. **Likelihood of a financial event by 90 / 180 days, as a range.** Sources in trust order, each labelled: Hindsight base rates on this tenant → Wizard D's calibrated predictor (when ported) → the arc prior. Zenith's tenant has **zero** loss events in its history, so the honest card reads *"not calibrated on this tenant — 0 events; arc prior only"* with a wide band, and the CFO line (probability × ARR) inherits that band. A demo tenant with real loss events shows a narrower fan.
4. **Tripwires and the recommended action.** Explicit watch conditions ("leading < 50", "a `commercial_pressure` or `champion_change` signal", "no signal for 30 days", "renewal window opens May 3") and the arc's typical next decision with the **observed** lift of interventions on this tenant (Zenith: new CSM, +14.5 pts, followed by $2.16M protected — narrative).

The scrubber makes Band 3 auditable: drag now back to Nov 2025 and the forecast band shows what would have been said then.

## 5. Exec vs operator views

The account canvas is the operator's drill-down. The C-level surface is the **portfolio**: risk-adjusted NRR forecast (a range), top-10 at risk *with lead time and evidence count*, expansion opportunities, and the platform's own honesty stats (coverage, false-alarm rate, open warnings, data origin). Same components, aggregated; the account canvas opens from any row.

## 6. Identity (the mock's tokens)

| Role | Light | Dark | Why |
|---|---|---|---|
| ground / surface | `#f5f6f3` / `#fcfdfb` | `#0e1312` / `#141a19` | cool-green-grey "ledger paper" — a chosen neutral, not cream or pure grey |
| ink / secondary / muted | `#101816` / `#4d5955` / `#7f8b87` | `#eef1ee` / `#b7c2be` / `#8a9793` | |
| trailing (KPI) line | `#2a78d6` | `#3987e5` | series slot 1 |
| leading (behavioral) line | `#eda100` | `#c98500` | slot 4 — orange sat 10.8 ΔE from critical red (negative dots land on this line) and violet collapsed against blue in dark (1.9 ΔE); yellow clears both: ≥24/17 ΔE from red, 31/27 from blue |
| negative / positive signal | `#d03b3b` / `#0ca30c` | same | status palette, with ▼/▲ shape as the second channel |
| UI accent (labels, controls) | `#1f5f5b` | `#7fc2bb` | teal, never on data |
| type | IBM Plex Serif (names, band titles) · IBM Plex Sans (UI, body) · IBM Plex Mono (dates, ids, axis) | | an engineered, evidentiary voice; not the default sans |

Validated with the dataviz palette checker: series pair passes all six checks in both modes; status colors carry icon/shape + label, never hue alone.

## 7. What it implies for the build

- **One JSON contract → three renderers.** Journey v3 + a `forecast` block + a `narrative` block feeds the HTML canvas, the Python/PNG renderer for decks, and later the React UI. The Python script stops carrying its own data.
- **`forecast` block** on the journey from a Foresight step: projected leading series with a silence date, next-phase prior (template × tenant transitions), event likelihood with `source` and `confidence_semantics`, tripwires, recommended action with observed lift. Wizard D replaces the likelihood source when it lands; the block's shape doesn't change.
- **Future milestones as episodes** (`kind: milestone`: renewal, contract end, planned QBR) so the right side of the axis is never empty.
- **Narrative generator**, citation-enforced.
- **Polarity reconciliation** in the leading composite: when a signal's sentiment contradicts its role's polarity, use the role default and flag the row (Zenith's false September `recovery_watch` is this bug).
- **Dedup by content** for same-day identical signals with distinct refs (render-time "×2"; ingest-time is a data-quality question for the load-driver).
- **Portfolio aggregation** endpoints for the exec view.

## 8. Open questions

1. Exec-readable account canvas by default, or operator detail with an exec toggle?
2. Fan chart + "prior / not calibrated" labels on stage, or a simplified exec mode with the interval one click away?
3. Rendering path: HTML-first with PNG export, or keep the Python renderer as a peer?
