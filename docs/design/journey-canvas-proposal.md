# Journey Canvas — proposal for team debate

*2026-09-02 · draft for discussion · nothing in §§3–5 is built. Companion documents: `journey-canvas.md` (the three-band design), `wizard-a-assessment.md` (what a journey is and why the old graph failed), `demo-narratives.md`. Live mock: `docs/design/mocks/journey-canvas-zenith.html`.*

---

## 1. What this document is for

The Journey Canvas mock now tells a credible past, present and next-six-months story for one account. Two questions came out of reviewing it that the canvas cannot answer yet:

1. **Who is accountable?** People appear by name but are tied to nothing — no signal, no action, no outcome.
2. **Where do I invest?** A VP of CS or a CFO looking at this account, or at the whole portfolio, cannot see *which pillar or product* to put money against, or *where effort is being wasted* on accounts that don't need it.

This document states the as-is honestly, proposes a model for each question, and lists the decisions the team has to make before anything is built. It is written to be argued with.

## 2. As-is

### 2.1 What the canvas is

One time axis, three bands, one JSON contract (journey v3 from the new build):

- **Now** — a state header: KPI health (the only number that feeds anything financial), the behavioral layer and its divergence, the arc hypothesis with the episodes that cite it, renewal countdown with the 90-day risk window, realized NRR beside narrative exposure (two numbers, never one), and the roster.
- **The past** — phases as chapters with the episode that triggered each; two health lines with the gap shaded; signals placed by role and colored by polarity with shape as a second channel; dated stakeholder changes; interventions with before/after; outcomes solid when ARR actually moved, hollow when narrative; the arc template's expected path as a dotted overlay; a story paragraph in which every sentence cites an episode.
- **The next 3–6 months** — a phase-outlook strip from the tenant's own transition matrix; the behavioral layer's hold-then-silence date; tripwires; recommended actions with the observed lift of past interventions; a likelihood fan that widens toward the renewal and labels itself an uncalibrated prior; a forecast ledger stating, for every forward mark, its basis and what would change it.
- **The now line is a scrubber** — drag it back and every band recomputes from what was known then.

### 2.2 What it is built from

| Layer | Source in the new build | Status |
|---|---|---|
| Episodes, phases, leading/trailing series, arc + evidence, expected path, intervention hooks, feature vector | `journeys/` (Wizard A v2), `JourneyData.journey_json` v3 | shipped, parity-checked on live tenants 359 and 415 |
| Transition matrix, realized NRR, derived early-warning rules, intervention stats | `wizards/wizard_b_hindsight.py` (Wizard B) | shipped |
| Lead-time backtest, evidence label | `evals/lead_time_backtest.py` | shipped |
| Forecast block (outlook, silence date, likelihood, tripwires, recommendations) | computed **in the mock's JavaScript** from the above | design only — not yet a platform output |
| Story paragraph | generated in the mock, citation-enforced | design only |
| Rendering | HTML + inline SVG; PNG/PDF via headless Chrome from the same file | mock; the Python renderer is retired |

### 2.3 The worked example — Zenith Data Corp (customer 415, real journey)

- 22 dated episodes; phases baseline (Jul–Aug 25) → deterioration (Sep 25–Feb 26, triggered by "usage quietly trending down") → baseline (Mar 26, triggered by the churn-averted outcome).
- Behavioral layer first warned **July 2025**; KPI health never crossed at-risk (lowest 56.7). The lead-time bracket is open; the Aug 1, 2026 renewal is the financial test.
- One intervention (new CSM, Mar 8): 56.7 → 71.3, followed by $2.16M of *narrative* protection. Realized NRR 100% — nothing lost, nothing booked.
- Arc `competitive_displacement` cited by two commercial-pressure episodes; alternative `silent_churn`.
- Two data-quality flags the canvas surfaces rather than hides: Sep/Oct signals carry positive sentiment against a negative role (the source of a false September "recovery watch"); three source rows are duplicates.
- The tenant has **zero loss events** in 90 account-months, so the likelihood fan is a prior (5–53%, median 29%) and says so.

### 2.4 What it deliberately does not do yet

- People are a roster. Signals carry a `stakeholder_name`, but the journey keeps it as text; decisions and interventions have no owner; outcomes have no accountable party.
- Health is shown as a rollup. `contributing_pillars` exist per month but are not surfaced, so nothing says *which pillar* is dragging.
- Nothing is attributed to a product. KPIs and signals carry no product tag.
- Effort (touches, hours, playbook cost) is not measured, so "over-invested" cannot be computed.
- Per-pillar dollar impact is not shown: it needs a calibrated health→retention relationship, and this tenant has no loss events to calibrate on.

## 3. Proposal A — accountability

### 3.1 The principle

An outcome that no one answers for is a number, not a result. The platform should be able to say, for any outcome, *which signals preceded it, which actions were taken, who owned them, and who was the sponsor of record* — from evidence, not from org charts.

### 3.2 Three relations, kept apart

| Relation | Direction | Meaning | Zenith example | Source |
|---|---|---|---|---|
| **about** | signal → person | the customer-side person a signal concerns | "Lisa Park declined the last two QBRs" is *about* Lisa | `stakeholder_name` on the signal — already in the data |
| **owns** | action → person | the our-side person who took the action | the Mar 8 CSM assignment; a playbook execution | an `owner` on every decision / intervention — **missing today** |
| **accountable for** | outcome → person(s) | who answers for the outcome | "Churn risk averted" ← Jordan Blake (owned the actions in the prior 90 days) + Robert Diaz (sponsor of record) | **derived** from the chain outcome ← actions ← signals; always labelled *inferred* |

They come from different sources and carry different weight; collapsing them into one "involves" edge (the old repo's approach) is how accountability became decorative.

### 3.3 What changes on the canvas

- The people lane becomes an **accountability lane**. A customer-side row carries the signals *about* that person; an our-side row carries the actions they *own*. Outcomes get an *accountable* chip that opens the chain.
- **Silent sponsor** is a flag. Robert Diaz has zero episodes across nine months of an at-risk account. Today that is an empty bar; it should be a named leading indicator (`sponsor_silent`), because absence of engagement from the person who signs is exactly what the two-layer model exists to catch.
- **No owner** is a governance flag, not a quiet default — the same rule as "no arc without evidence."
- **Composition upward:** a CSM scorecard becomes *actions owned → outcomes that followed*, with observed lift, on evidence. That is what RBAC's "my view" should land on for a CSM; the VP view aggregates it; the exec view sees coverage (accounts with a silent sponsor, actions without owners).

### 3.4 Data implications

- Episodes gain `actor` (customer-side) and `owner` (our-side).
- The journey builder writes typed INVOLVES edges (`about` / `owns` / `accountable_for`); inherited accountability carries `derivation: inferred_from_window` and a confidence semantics label.
- CSV contract: `owner` on decisions; playbook executions carry their executor; stakeholders gain `since`/`until` dates so tenure is real, not the load timestamp.
- Honesty rules: an inferred accountability is displayed as such; a person with no dated episode is shown as *present, no evidence*, never as engaged.

## 4. Proposal B — the investment lens

### 4.1 The principle

The canvas says *how* the account is doing. Executives need *what to do and what it costs*: which pillar, which product, which budget (engineering, CS capacity, commercial), and where effort is being spent without need. Every recommendation must carry the same honesty as the forecast: basis, confidence semantics, tripwire.

### 4.2 Where to add — pillar drag and product attribution

**Pillar drag.** Health is a weighted rollup and `contributing_pillars` are stored per month. Each pillar's *shortfall from healthy × its weight* is its share of the gap. On Zenith the canvas can already compute "P3 is responsible for 60% of the distance to 70" — it just doesn't show it. A small stacked bar in the state header, and a trend of it across the timeline, make the pillar the first thing a VP sees after the number.

**The qualitative layer points at pillars too.** Signal roles map to pillars through the catalogs' `pillar_roles`: usage decline → adoption, infra incident → reliability, commercial pressure → revenue/expansion. When the KPI layer and the behavioral layer point at the same pillar, that is the strongest "invest here" the platform can produce; when they disagree, that disagreement is itself the finding.

**Product attribution — the missing piece.** Zenith's "storage utilization dropped from 78% to 55%; DR tests skipped" is plainly about Disaster Recovery and High-Performance Storage — $4.0M of the $4.8M ARR — but nothing tags it. The tag turns "P1 is dragging" into "product A is causing it; take it to engineering." Three possible sources, in order of trust: a product tag on KPIs in the catalog; an explicit product column on signals; inference from signal text (fast, must be labelled *inferred*).

**The lever follows from the pillar's role.** This is the CFO's actual question — *which budget*:

| Pillar role (from the catalog) | Deficit means | Lever | Budget |
|---|---|---|---|
| adoption, reliability, capacity | the product is not delivering | fix / enable | engineering, support, enablement |
| engagement, champion coverage | the relationship is thinning | rebuild | CS capacity |
| revenue, expansion, commercial pressure | the deal is under pressure | reprice / repackage | commercial |

**Portfolio: the investment map.** Pillar × product (or vertical) with ARR-weighted deficit in the cells. "$18M of at-risk ARR shares a reliability deficit on product X" is one engineering decision; "$6M shares a champion-coverage gap" is one hiring decision. Concentration is the finding; that is what an exec can act on.

### 4.3 Where to pull back — effort vs need

Two axes per account:

- **Need** — what the platform already knows: leading state, phase, renewal proximity, ARR at stake.
- **Effort** — what it does not yet measure: touches, CSM hours, playbook cost.

Plotted as a quadrant: under-served-and-at-risk (invest), over-served-and-steady (reshuffle), and the two honest corners. The platform's own evidence keeps this from being a slogan: on Zenith the one intervention was followed by +14.5 points; on a steady account with heavy touch and no measured lift, the effort is *unproven* — which is the argument for reducing it, stated as a hypothesis with a tripwire ("move to monthly cadence; re-engage if leading < 60"). Rolled up: hours freed from over-served accounts against hours the under-served need, with the arc-transition priors giving the expected value of the move.

### 4.4 Dollars or priority

Power-of-1 is the right frame — "one point of P3 on this account is worth $X" — but a per-pillar dollar figure needs a calibrated health→retention relationship, and Zenith's tenant has zero loss events. The honest output until calibration exists is **exposure-weighted priority** (ARR × deficit × weight): the same ranking, no invented ROI. Dollars appear per tenant once its backtest has events — the same rule the forecast fan already follows.

### 4.5 What changes on the canvas

- **Band 2:** a pillar-drag bar (each pillar's share of the gap, product tags where known) beside the health number.
- **Band 3:** an *invest here* card — pillar → product → lever → owner → expected effect (observed where the tenant has it) → tripwire.
- **Portfolio (new surface):** the investment map; the effort-vs-need quadrant; a reallocation table.

## 5. What each proposal needs

| Need | Proposal | Who supplies it | Cost |
|---|---|---|---|
| `owner` on decisions / interventions; executor on playbook runs | A | our side (UI, playbook engine) | small |
| stakeholder `since` / `until` | A | customer CSV or CRM connector | small |
| typed INVOLVES edges from the journey builder | A | platform | small |
| pillar drag from `contributing_pillars` | B | platform | small — data exists |
| product tags on KPIs (catalog) | B | platform, per vertical | medium |
| product column on signals | B | customer CSV / connectors | medium — customer effort |
| effort logging (touches, hours, or playbook cost) | B | our side; CRM activity or playbook engine | **largest** — it does not exist |
| health→retention calibration for $ per pillar | B | time + loss events per tenant | cannot be bought; accrues |

## 6. Decisions to debate

| # | Question | Options | Recommendation |
|---|---|---|---|
| 1 | Who is accountable for an outcome? | (a) the CSM only · (b) the account team: CSM + sponsor of record · (c) whoever owned actions in the window, whoever they are | **(b), displayed with (c)'s chain.** Scorecards then measure the team, and a silent sponsor is visibly part of the result. |
| 2 | Where does product attribution come from? | (a) product tags on KPIs in the catalog · (b) a product column on signals · (c) text inference | **(a) + (b) as truth; (c) only as a flagged suggestion.** Never let an inference reach a budget decision unlabelled. |
| 3 | What is the unit of effort? | (a) touches · (b) CSM hours · (c) playbook cost | **Log (a) now — every action already has a date — and price it with (c) where a playbook ran.** Hours are the most honest and the least likely to be recorded. |
| 4 | Dollars or priority per pillar? | (a) $ ROI now, from the arc prior · (b) exposure-weighted priority now, $ once the tenant is calibrated | **(b).** The same credibility rule as the forecast fan; a ranking with no invented ROI is more persuasive to a CFO than a dollar figure that cannot be defended. |

Two secondary questions worth settling in the same conversation: whether a *silent sponsor* should count against the account's leading score or only be flagged (recommendation: flag first, score only after the backtest shows it predicts anything); and whether the over-served quadrant should ever auto-recommend reducing touch, or only surface it (recommendation: surface, with the tripwire; a human decides).

## 7. Suggested sequence

1. Accountability edges and owner fields — small, unlocks the lane, the scorecard and the silent-sponsor flag.
2. Pillar drag in Band 2 — data exists; immediately answers "which pillar."
3. Product tags on KPIs for the datacenter and SaaS catalogs; product column added to the signals contract.
4. The portfolio surface: investment map first (it needs only 1–3), effort quadrant once effort is logged.
5. Dollars per pillar, per tenant, when its backtest has events.

## 8. Appendix — Zenith numbers the proposals would use

- ARR $4.8M: High-Performance Storage $2.5M, Disaster Recovery $1.5M, Monitoring & Observability $0.8M; adoption 72.
- Latest KPI health 71.3; behavioral 89.4 (+18.2, recovery watch); lowest KPI 56.7 (Feb 26).
- Signals by role (whole journey): usage decline 4 (+2 duplicate rows), engagement decline 1 (+1 duplicate), commercial pressure 1 (+1 duplicate), recovery 6, CSM intervention 1.
- Intervention: new CSM Mar 8 — 56.7 → 71.3 (+14.5); outcomes within 90 days: revenue protected $240K, churn averted $1.92M (both narrative).
- Tenant transition prior from baseline: → deterioration 53% (8 of 15 segments), ~1.4 months, most often triggered by usage decline.
- Likelihood fan to Aug 1: 5–53%, median 29%; expected loss at the median ≈ $1.39M — prior only, 0 loss events on the tenant.
- People: Lisa Park (champion, Director of Infrastructure, engagement 83) — declined last two QBRs Oct 15; Robert Diaz (exec sponsor) — no dated episode; Jordan Blake (CSM since Mar 8); Sam Rivera (CS manager).
