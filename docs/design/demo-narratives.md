# Demo narratives — proving the early-warning mechanism before we have partner data

*2026-09-02. Companion to `wizard-a-assessment.md` §7. There is no design partner yet, so demos run on constructed tenants. The rule that keeps this credible: a demo proves the **mechanism** (behavioral → financial, with evidence, measured by the platform's own backtest) and says so; it never presents a constructed lead time as a measured one.*

---

## 1. What a demo must land — the outcomes

Six things the buyer should walk away having *seen*, not been told. Each scenario below hits all six.

| # | Outcome | What's on screen |
|---|---|---|
| O1 | **Lead time, measured by the platform** | The Hindsight backtest table for this tenant: *N past events, behavioral composite crossed a median D days before the financial decision in K of N, F false alarms / 100 account-months; trailing KPI health crossed D′ days before — the behavioral layer bought D − D′ days.* |
| O2 | **A live account in the same pattern, warned now** | Foresight: an account whose behavioral composite crossed the threshold this month, with the arc hypothesis, the phase, and days-to-renewal. |
| O3 | **Evidence, one click deep** | Every warning opens to the episodes that produced it — the signals, the stakeholder change, the KPI inflection — dated, with source platform. No black box. |
| O4 | **Dollars, CFO-framed** | ARR exposed on the warned accounts; Power-of-1 impact of moving the leading composite by one point; cost of inaction vs playbook cost. |
| O5 | **Action closes the loop** | Recommended playbook in the approval queue, CSM capacity check, and — for the historical case — what the intervention actually changed vs the arc's expected path. |
| O6 | **The platform tells the truth about itself** | Coverage stat (*"11 of 14 accounts classified with cited evidence; 3 unclassified"*), at least one visible false alarm with its resolution, cold-start labelling where calibration doesn't exist yet, and the banner: *synthetic demo tenant — lead times constructed to illustrate the mechanism; your measured numbers come from your own history in week 1 of onboarding.* |

O6 is not a disclaimer to hide in a footer. It is the differentiator against every "AI-powered" CS tool that shows a confident number and can't say where it came from.

## 2. Scenario design rules

Constructed tenants have to look like real portfolios, or the buyer's first question is "why does every account tell a story?"

- **12–15 accounts per tenant; 2–3 carry the featured story.** The rest are background: stable, mildly noisy, one genuinely `unclassified`, one false alarm (behavioral dip that recovered on its own, no financial event).
- **Behavioral onset precedes the financial event by 60–80 days *with noise*** — not a straight ramp. Signal cadence is irregular; KPIs have week-to-week jitter; one signal contradicts the trend.
- **Three timelines per featured account**, so O1's comparators exist in the data: behavioral composite crossing, trailing KPI health crossing, and the CRM's own risk flag (always last).
- **Financial events carry a decision date**, not just a contract end.
- **Every constructed signal is `data_origin`-tagged synthetic** at the tenant level; the load-driver's manifest mode must stamp it (open backlog item) before these tenants are built.
- **Vertical-native vocabulary.** Datacenter scenarios use the datacenter signal set (`reserved_cluster_idle`, `commitment_ramp_miss`, `multicloud_diversification`, `reliability_sla_breach`, `power_capacity_constraint`, `reservation_expansion_interest`, `silicon_refresh_interest`, `funding_raised`) — the very signals the old classifier couldn't read. That's a deliberate proof point for Wizard A v2's signal roles.

## 3. The three scenarios

### Scenario A — Silent displacement (datacenter_v1, H1 retention)

*"They didn't complain. They just started moving workloads."*

**Account:** Meridian AI, $4.1M ARR GPU-cloud commitment, renewal in 95 days at demo time.

| Day | Layer | What happens |
|---|---|---|
| T−78 | behavioral | `reserved_cluster_idle` rises 12% → 31% over three weeks; utilization KPI (P2) still inside target band |
| T−70 | behavioral | QBR postponed by the customer; champion (Head of ML Infra) login cadence halves |
| T−62 | behavioral | Support ticket mentions "validating a second provider for training jobs" → `multicloud_diversification` |
| T−55 | behavioral | `commitment_ramp_miss` — consumed 68% of committed capacity vs 85% plan; **behavioral composite crosses the warning threshold** |
| T−41 | trailing | Trailing KPI health crosses into at-risk (utilization + realized $/GPU-hr both below target) |
| T−14 | CRM | CSM marks renewal "at risk" after a pricing-pressure email |
| T | financial | Renewal signed at −$1.4M (contraction), decision date recorded |

**Platform shows:** Hindsight — this tenant's history has 9 similar events; behavioral led the decision by median 66 days, trailing by 38, CRM flag by 12. Foresight — a second account, Helix Compute, is at day −58 of the same pattern *today*: warning with the four episodes cited, arc hypothesis `silent_churn` @ evidence-cited confidence, $2.9M exposed, playbook "workload-retention review + reserved-pricing restructure" queued with cost and Po1 impact. **Honesty beat:** Quantum Labs shows the same idle-capacity spike at T−60 and recovered in three weeks after a batch-job migration — shown as a resolved false alarm, counted in F.

### Scenario B — Expansion intent the CRM couldn't see (datacenter_v1, H2 growth)

*"They were going to ask for more capacity in April. We knew in January."*

**Account:** Stellar Inference, $3.4M ARR, healthy, no open opportunity.

| Day | Layer | What happens |
|---|---|---|
| T−72 | behavioral | `funding_raised` signal (press/LinkedIn); utilization ramp from 71% → 88% over four weeks |
| T−60 | behavioral | `reservation_expansion_interest` — customer engineer asks about H100 reservation windows in a support thread |
| T−48 | behavioral | `silicon_refresh_interest`; exec engagement event (their CTO joins the monthly review) — **expansion composite crosses** |
| T−35 | trailing | Provisioning-velocity KPI (P6) hits ceiling; capacity KPI flags constraint |
| T−20 | CRM | Opportunity created by the AE after the customer emails asking for a quote |
| T | financial | +$900K expansion booked |

**Platform shows:** the expansion composite fired 52 days before the AE created the opportunity; Po1 on the expansion pillar; CSM capacity view showing who should own the conversation and when. Foresight — Zenith Training at day −50 of the same pattern, with a "capacity conversation" playbook, not a retention one. **Honesty beat:** Vector Dynamics has the utilization ramp but no intent signals — shown as *watch, not warned*, with the coverage stat explaining why.

### Scenario C — Champion departure, intervention, and the counterfactual (saas_premium, H1 with action)

*"The person who bought us left. Here's what we did, and what would have happened if we hadn't."*

**Account:** Northwind Analytics, $1.8M ARR, renewal in 120 days at story start.

| Day | Layer | What happens |
|---|---|---|
| T−82 | behavioral | STAKEHOLDER change: champion (VP Data) leaves — CRM contact update + LinkedIn signal |
| T−66 | behavioral | Engagement decline: weekly syncs lapse, NPS response from the replacement is a 5 |
| T−51 | behavioral | Ticket volume spike from a team that used to self-serve — **composite crosses; arc `exec_sponsor_change`, phase deterioration** |
| T−44 | action | Playbook "exec sponsor rebuild" approved and executed: exec-to-exec intro, value review with the new VP |
| T−30 | behavioral | New champion identified; engagement recovers; sentiment from signals turns positive |
| T−12 | trailing | Trailing KPI health bottoms and turns — *after* the intervention |
| T | financial | Renewal secured at −8% (minor contraction) vs the arc's expected path of full churn |

**Platform shows:** the journey with phases and the transition triggers; the **expected-path overlay** from the `exec_sponsor_change` template (typical: churn at renewal) against the actual path; the intervention's before/after window (counterfactual hook); playbook cost vs ARR protected. **Honesty beat:** the counterfactual is labelled *arc prior from N historical cases, not a prediction for this account*; a second champion-change account, Blue Harbor, recovered with no intervention — shown so the ROI number isn't overstated.

## 4. Why these three

- A and C are H1 across two verticals with different behavioral vocabularies (infra/usage vs stakeholder/engagement); B is H2 — the claim is *churn and expansion*, and most competitors only demo the defensive half.
- Together they exercise every part of Wizard A v2: signal roles (A, B), stakeholder episodes (C), phases with transition triggers (C), expected-path overlays (C), the leading/trailing series and first-dip dates (all), `unclassified` and false alarms (all).
- Each has a Hindsight half (the backtest) and a Foresight half (a live account mid-pattern) — the dual-horizon design made visible.

## 5. What each demo may and may not claim

| May say | May not say |
|---|---|
| "This is how the platform measures lead time — here's the backtest running on this tenant." | "The platform detects churn 60–80 days early." |
| "On this constructed tenant the behavioral layer led by a median 66 days; on your history you'll see your own number in week 1." | Any lead-time figure without the *synthetic* label. |
| "Here are the four signals that produced this warning." | A warning with no cited episodes. |
| "3 of 14 accounts are unclassified; here's why." | 100% coverage. |

## 6. What has to exist for these to run

1. Wizard A v2 (journey v3, evidence-cited classifier with datacenter signal roles, expected-path overlays) — Tier 2A-5.
2. `evals/lead_time_backtest.py` — the Hindsight table (§7.5 of the assessment); run live during the demo.
3. Load-driver manifests written *to the protocol*: per-account `behavioral_onset_day`, `trailing_cross_day`, `crm_flag_day`, `decision_day`, noise parameters, plus the background accounts; `data_origin` stamped. Three manifests: `demo_silent_displacement_dc.json`, `demo_expansion_intent_dc.json`, `demo_champion_departure_saas.json`.
4. The two-layer columns on `HealthScore` (`qual_score`, `divergence`, `early_warning`) written by the journey builder — today they have no writer.
