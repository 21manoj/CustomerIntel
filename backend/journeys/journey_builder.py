"""
Journey builder — episodes, phases, leading-vs-trailing series.

All windows are relative to `as_of` = the last scored month's end, not the
wall clock: a journey built from 2025 history must read the same in 2027,
and the old classifier's utcnow()-relative 30/60-day slope windows went
empty on any tenant whose data wasn't from this month (every fixture, and
every historical backtest).

Layer separation (Recency-Signal-DNA spec §1a, immutable): kpi_only_score
is never blended with anything here. qual_score is computed from signals
only, written to its own HealthScore columns, and compared — never
averaged — with kpi_only.
"""
from __future__ import annotations

import math
import statistics
from calendar import monthrange
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import utils.health_thresholds as ht


# ═══════════════════════════════════════════════════════════════════════
# Episodes
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class Episode:
    episode_id: str
    date: datetime
    kind: str                 # signal | stakeholder | decision | outcome | health_transition | renewal
    subtype: Optional[str]
    role: Optional[str]       # signal role from the taxonomy (signals only)
    polarity: int             # +1 / -1 / 0
    source: str               # observed | system
    title: str
    evidence_node_ids: List[int] = field(default_factory=list)
    sentiment: Optional[float] = None
    revenue: Optional[float] = None
    revenue_bucket: Optional[str] = None
    meta: Dict = field(default_factory=dict)

    def to_json(self) -> dict:
        d = asdict(self)
        d['date'] = self.date.isoformat()
        return d


def _sentiment_from_props(props: dict, label_default: dict, polarity: int) -> Optional[float]:
    raw = props.get('sentiment_score')
    try:
        if raw not in (None, '', 'nan', 'None'):
            return max(-1.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        pass
    label = str(props.get('sentiment') or '').strip().lower()
    if label in ('positive', 'negative', 'neutral'):
        return label_default[label]
    if polarity:
        return label_default['positive' if polarity > 0 else 'negative']
    return None


def collect_episodes(account, taxonomy, health_rows) -> List[Episode]:
    """Episodes from the graph (observed SIGNAL / DECISION / OUTCOME nodes),
    health-status transitions, and the renewal milestone."""
    from models import ContextNode
    li = ht.leading_indicator_config()
    label_default = li['default_sentiment_by_polarity']

    eps: List[Episode] = []
    nodes = ContextNode.query.filter(
        ContextNode.account_id == account.account_id,
        ContextNode.node_type.in_(['SIGNAL', 'DECISION', 'OUTCOME']),
        ContextNode.source == 'observed',
    ).order_by(ContextNode.occurred_at).all()

    for n in nodes:
        props = n.properties or {}
        sub = (n.node_subtype or '').strip().lower() or None
        review = (props.get('review') or {}).get('status')
        if review == 'rejected':
            continue  # a human said this is not evidence; the node stays for audit, the journey ignores it
        if n.node_type == 'SIGNAL':
            if sub == 'arc_detection':
                continue  # legacy system node, never observed evidence
            role = taxonomy.signal_role(sub)
            pol = taxonomy.role_polarity(role)
            raw_sent = _sentiment_from_props(props, label_default, pol)
            # Polarity reconciliation: the role is the evidence class; a
            # sentiment that contradicts it (a "positive" usage-decline) is
            # a data error on the row, not a recovery. The role wins and the
            # conflict is carried on the episode — found on live tenant 415,
            # where two such rows produced a false September recovery_watch.
            sent, conflict = raw_sent, False
            if pol != 0 and raw_sent is not None and ((raw_sent > 0) if pol < 0 else (raw_sent < 0)):
                sent, conflict = label_default['negative' if pol < 0 else 'positive'], True
            eps.append(Episode(
                episode_id=f'sig:{n.node_id}', date=n.occurred_at, kind='signal',
                subtype=sub, role=role, polarity=pol, source='observed',
                title=(n.title or sub or 'signal')[:200],
                evidence_node_ids=[n.node_id],
                sentiment=sent,
                meta={'source_platform': n.source_platform, 'stakeholder': props.get('stakeholder_name'),
                      'stakeholder_role': props.get('stakeholder_role'),
                      'person_unresolved': props.get('person_unresolved'),
                      'polarity_conflict': conflict, 'raw_sentiment': raw_sent if conflict else None,
                      'quote': props.get('quote'), 'confidence': props.get('confidence'),
                      'requires_review': bool(props.get('requires_review')), 'review': review},
            ))
        elif n.node_type == 'DECISION':
            eps.append(Episode(
                episode_id=f'dec:{n.node_id}', date=n.occurred_at, kind='decision',
                subtype=sub, role=None, polarity=0, source='observed',
                title=(n.title or 'decision')[:200], evidence_node_ids=[n.node_id],
                meta={'chosen_option': props.get('chosen_option')},
            ))
        elif n.node_type == 'OUTCOME':
            bucket = taxonomy.revenue_bucket(n.revenue_impact_type or sub)
            rev = float(n.revenue_impact) if n.revenue_impact is not None else None
            pol = 1 if bucket in ('expansion', 'protected', 'pipeline') else -1 if bucket in ('lost', 'at_risk') else 0
            eps.append(Episode(
                episode_id=f'out:{n.node_id}', date=n.occurred_at, kind='outcome',
                subtype=sub, role=None, polarity=pol, source='observed',
                title=(n.title or sub or 'outcome')[:200], evidence_node_ids=[n.node_id],
                revenue=rev, revenue_bucket=bucket,
                meta={'evidence_clamped': props.get('evidence_clamped', False)},
            ))

    prev_status = None
    for hs in health_rows:
        status = hs.health_status
        if prev_status is not None and status != prev_status:
            m = hs.measurement_month
            eps.append(Episode(
                episode_id=f'hs:{hs.health_score_id}', date=datetime(m.year, m.month, m.day),
                kind='health_transition', subtype=f'{prev_status}->{status}', role=None,
                polarity=1 if _band_rank(status) > _band_rank(prev_status) else -1,
                source='system', title=f'Health {prev_status} → {status} ({float(hs.health_score):.1f})',
                meta={'health_score': float(hs.health_score), 'from': prev_status, 'to': status},
            ))
        prev_status = status

    renewal = (account.profile_metadata or {}).get('renewal_date') or (account.profile_metadata or {}).get('contract_end')
    if renewal:
        try:
            rd = datetime.fromisoformat(str(renewal)[:10])
            eps.append(Episode(
                episode_id='renewal', date=rd, kind='renewal', subtype='renewal_date', role=None,
                polarity=0, source='observed', title='Renewal date', meta={'renewal_date': rd.date().isoformat()},
            ))
        except ValueError:
            pass

    eps.sort(key=lambda e: e.date)
    return eps


def _band_rank(status: Optional[str]) -> int:
    return {'critical': 0, 'at_risk': 1, 'healthy': 2}.get(status or '', 1)


# ═══════════════════════════════════════════════════════════════════════
# Health series helpers
# ═══════════════════════════════════════════════════════════════════════

def month_end(m: date) -> datetime:
    return datetime(m.year, m.month, monthrange(m.year, m.month)[1], 23, 59, 59)


def slope_pts_per_month(points: List[tuple], months: int) -> float:
    """(month, score) points, chronological. Slope over the last `months`+1 points."""
    pts = points[-(months + 1):]
    if len(pts) < 2:
        return 0.0
    (m0, s0), (m1, s1) = pts[0], pts[-1]
    span = (m1.year - m0.year) * 12 + (m1.month - m0.month) or 1
    return (s1 - s0) / span


def trajectory_label(scores: List[float]) -> tuple:
    """The old repo's health-shape classifier, kept as a FEATURE (not an arc)
    for continuity with existing consumers. Same math as
    wizard_a_journey_db._classify_trajectory_with_confidence."""
    if not scores:
        return 'unknown', 0.0
    n = len(scores)
    if n < 2:
        return ('crisis', 0.7) if scores[0] < ht.at_risk_min() else ('stable', 0.6)
    mid = max(1, n // 2)
    first, last = statistics.mean(scores[:mid]), statistics.mean(scores[mid:])
    delta_conf = min(abs(last - first) / 20.0, 0.35)
    c = lambda base: min(base + delta_conf, 1.0)  # noqa: E731
    has_crisis = any(s < ht.at_risk_min() for s in scores)
    if has_crisis:
        return ('recovery', c(0.55)) if last > first + 5 else ('crisis', c(0.65))
    if last > first + 5:
        return 'improving', c(0.55)
    if last < first - 5:
        return 'declining', c(0.60)
    return 'stable', 0.55


# ═══════════════════════════════════════════════════════════════════════
# Phases
# ═══════════════════════════════════════════════════════════════════════

def detect_phases(points: List[tuple], episodes: List[Episode]) -> List[dict]:
    """Phase per scored month → contiguous segments with entry/exit and the
    episode that triggered each transition (nearest preceding episode within
    the lookback, else the health transition itself)."""
    if not points:
        return []
    rules = ht.phase_rules()
    at_risk, healthy = ht.at_risk_min(), ht.healthy_min()
    lookback = timedelta(days=rules['phase_trigger_lookback_days'])

    labels = []
    dipped = False
    for i, (m, s) in enumerate(points):
        slope = slope_pts_per_month(points[:i + 1], 1)
        if s < at_risk:
            dipped = True
        if dipped and s >= at_risk and slope > rules['resolution_slope_pts']:
            ph = 'resolution'
        elif s < at_risk:
            ph = 'intervention'
        elif slope < rules['deterioration_slope_pts'] and s < healthy + rules['deterioration_health_ceiling_offset']:
            ph = 'deterioration'
        else:
            ph = 'baseline'
        labels.append((m, ph, s))

    segments: List[dict] = []
    for m, ph, s in labels:
        if segments and segments[-1]['name'] == ph:
            segments[-1]['exited_at'] = None
            segments[-1]['months'] += 1
            segments[-1]['health_end'] = s
            continue
        if segments:
            segments[-1]['exited_at'] = m.isoformat()
        entered = datetime(m.year, m.month, 1)
        trigger = None
        for e in reversed(episodes):
            # A trigger is something that happened TO the account — a signal,
            # an outcome, the health move itself. Decisions and CSM
            # interventions are responses to a phase, never its trigger.
            if e.kind in ('renewal', 'decision') or (e.kind == 'signal' and e.role == 'intervention'):
                continue
            if entered - lookback <= e.date <= month_end(m):
                trigger = e.episode_id
                break
        segments.append({
            'name': ph, 'entered_at': m.isoformat(), 'exited_at': None, 'months': 1,
            'health_start': s, 'health_end': s, 'trigger_episode_id': trigger,
        })
    return segments


def detect_phases_from_evidence(episodes: List[Episode], months: List[date]) -> List[dict]:
    """Phases when there is no KPI layer: per month, from the roles that
    happened (evidence_phase_rules in health_thresholds.json). Same segment
    shape as detect_phases, with health_start/end None and basis='evidence'."""
    if not months:
        return []
    rules = ht.evidence_phase_rules()
    by_month: Dict[str, List[Episode]] = {}
    for e in episodes:
        if e.kind == 'signal' and e.role:
            by_month.setdefault(e.date.strftime('%Y-%m'), []).append(e)

    labels = []
    prev, quiet_run = 'baseline', 0
    for m in months:
        eps = by_month.get(m.strftime('%Y-%m'), [])
        neg = [e for e in eps if e.polarity < 0]
        pos = [e for e in eps if e.polarity > 0]
        interventions = [e for e in eps if e.role == 'intervention']
        if interventions:
            ph, trigger = 'intervention', interventions[0]
        elif neg and len(neg) > len(pos):
            ph, trigger = 'deterioration', neg[0]
        elif pos and prev in ('deterioration', 'intervention', 'resolution'):
            ph, trigger = 'resolution', pos[0]
        elif pos:
            ph, trigger = 'baseline', pos[0]
        elif eps:
            ph, trigger = prev if prev != 'resolution' else 'baseline', eps[0]
        else:
            quiet_run += 1
            ph, trigger = (prev if quiet_run < rules['quiet_months_to_baseline'] else 'baseline'), None
        if eps:
            quiet_run = 0
        labels.append((m, ph, trigger, len(neg), len(pos)))
        prev = ph

    segments: List[dict] = []
    for m, ph, trigger, n_neg, n_pos in labels:
        if segments and segments[-1]['name'] == ph:
            segments[-1]['months'] += 1
            segments[-1]['negative_signals'] += n_neg
            segments[-1]['positive_signals'] += n_pos
            continue
        if segments:
            segments[-1]['exited_at'] = m.isoformat()
        segments.append({
            'name': ph, 'entered_at': m.isoformat(), 'exited_at': None, 'months': 1,
            'health_start': None, 'health_end': None, 'basis': 'evidence',
            'trigger_episode_id': trigger.episode_id if trigger else None,
            'negative_signals': n_neg, 'positive_signals': n_pos,
        })
    return segments


# ═══════════════════════════════════════════════════════════════════════
# Leading vs trailing
# ═══════════════════════════════════════════════════════════════════════

def leading_series(points: List[tuple], kpi_only: Dict[date, float], episodes: List[Episode]) -> dict:
    """Per month: kpi_only (trailing), qual (leading — recency-weighted
    behavioral composite of the signals in the trailing window), their
    divergence, and the early-warning label. Plus the first-warning dates
    for each layer, which is what the lead-time backtest measures.

    `points` may end with LIVE months — (month, None) — for evidence that
    arrived after the last scored KPI month (signals always run ahead of a
    monthly KPI feed). Those months carry qual/roles, no trailing, label
    'leading_only'. Without them a live signal would be invisible on the
    journey until the next KPI upload — the opposite of a leading indicator."""
    from utils.taxonomy_loader import LEADING_EXCLUDED_ROLES
    li = ht.leading_indicator_config()
    window = timedelta(days=li['signal_window_days'])
    lam = li['decay_lambda_per_day']
    warn = li['divergence_warning_pts']
    unreviewed_w = li['unreviewed_low_confidence_weight']
    at_risk = ht.at_risk_min()

    behavioral = [
        e for e in episodes
        if e.kind == 'signal' and e.sentiment is not None and e.role and e.role not in LEADING_EXCLUDED_ROLES
    ]   # e.role: an unclassified signal (no evidence class) is visible on the journey but never scored
    series = []
    first_leading = first_trailing = None
    for m, score in points:
        end = month_end(m)
        num = den = 0.0
        n = 0
        contributors = []
        roles: Dict[str, int] = {}
        unreviewed = 0
        for e in behavioral:
            if end - window < e.date <= end:
                w = math.exp(-lam * (end - e.date).days)
                if e.meta.get('requires_review') and not e.meta.get('review'):
                    w *= unreviewed_w        # low-confidence, not yet verified: counts, but less
                    unreviewed += 1
                h = (e.sentiment + 1.0) * 50.0
                num += w * h
                den += w
                n += 1
                contributors.append(e.episode_id)
                if e.role:
                    roles[e.role] = roles.get(e.role, 0) + 1
        qual = round(num / den, 2) if den else None
        trailing = kpi_only.get(m, score)          # None on a live month: no KPI row yet
        div = round(qual - trailing, 2) if (qual is not None and trailing is not None) else None
        if trailing is None:
            label = 'leading_only' if qual is not None else None
        elif div is None:
            label = None
        elif div <= -warn:
            label = 'early_warning'
        elif div >= warn:
            label = 'recovery_watch'
        else:
            label = 'aligned'
        if first_leading is None and qual is not None and (label == 'early_warning' or qual < at_risk):
            first_leading = m
        if first_trailing is None and trailing is not None and trailing < at_risk:
            first_trailing = m
        series.append({
            'month': m.isoformat(), 'kpi_only': round(trailing, 2) if trailing is not None else None, 'qual': qual,
            'divergence': div, 'early_warning': label, 'signal_count': n, 'live': trailing is None,
            'unreviewed_count': unreviewed, 'contributing_episode_ids': contributors, 'roles': roles,
        })
    lead_days = (first_trailing - first_leading).days if first_leading and first_trailing else None
    return {
        'series': series,
        'first_leading_warning_at': first_leading.isoformat() if first_leading else None,
        'first_trailing_warning_at': first_trailing.isoformat() if first_trailing else None,
        'lead_days': lead_days,
        'window_days': li['signal_window_days'],
        'divergence_warning_pts': warn,
        'note': 'qual is computed from signals only and is never blended into kpi_only (absolute-separation invariant).',
    }


# ═══════════════════════════════════════════════════════════════════════
# Counterfactual hooks
# ═══════════════════════════════════════════════════════════════════════

def counterfactual_hooks(episodes: List[Episode], points: List[tuple]) -> List[dict]:
    """For each observed decision / intervention: health and outcomes in
    the 90 days before and after — the raw material for Wizard B's
    'what did the intervention change' analysis."""
    hooks = []
    win = timedelta(days=90)
    for e in episodes:
        if not (e.kind == 'decision' or (e.kind == 'signal' and e.role == 'intervention')):
            continue
        before = [s for m, s in points if e.date - win <= month_end(m) < e.date]
        after = [s for m, s in points if e.date <= month_end(m) < e.date + win]
        outcomes_after = [
            {'episode_id': o.episode_id, 'bucket': o.revenue_bucket, 'revenue': o.revenue}
            for o in episodes if o.kind == 'outcome' and e.date <= o.date < e.date + win
        ]
        hooks.append({
            'episode_id': e.episode_id, 'date': e.date.isoformat(), 'title': e.title,
            'health_before': {'n': len(before), 'mean': round(statistics.mean(before), 2) if before else None,
                              'last': before[-1] if before else None},
            'health_after': {'n': len(after), 'mean': round(statistics.mean(after), 2) if after else None,
                             'last': after[-1] if after else None},
            'outcomes_after': outcomes_after,
        })
    return hooks


# ═══════════════════════════════════════════════════════════════════════
# Assemble
# ═══════════════════════════════════════════════════════════════════════

def _next_month(m: date) -> date:
    return date(m.year + (m.month == 12), (m.month % 12) + 1, 1)


def _live_months(points: List[tuple], episodes: List[Episode]) -> List[date]:
    """Months after the last scored month (or from the first signal, when
    nothing is scored yet — the signals-only tier) up to the latest observed
    signal, capped at the current month."""
    dates = [e.date for e in episodes if e.kind == 'signal']
    if not dates:
        return []
    latest = min(max(dates), datetime.utcnow()).date().replace(day=1)
    m = _next_month(points[-1][0]) if points else min(dates).date().replace(day=1)
    out = []
    while m <= latest:
        out.append(m)
        m = _next_month(m)
    return out


def build_journey(account, vertical: str) -> dict:
    """Journey schema v3 for one account. Pure read; persistence is wizard_a.run_wizard_a."""
    from models import HealthScore
    from utils.taxonomy_loader import get_taxonomy
    from journeys import features as feat
    from journeys import arc_classifier
    from utils.story_arc_loader import expected_path

    taxonomy = get_taxonomy(vertical)
    health_rows = HealthScore.query.filter_by(account_id=account.account_id).order_by(
        HealthScore.measurement_month).all()
    points = [(hs.measurement_month, float(hs.health_score)) for hs in health_rows if hs.health_score is not None]
    kpi_only = {
        hs.measurement_month: float(hs.kpi_only_score if hs.kpi_only_score is not None else hs.health_score)
        for hs in health_rows if hs.health_score is not None
    }
    scores = [s for _, s in points]
    episodes = collect_episodes(account, taxonomy, health_rows)
    live_months = _live_months(points, episodes)
    last_evidence = max((e.date for e in episodes if e.kind == 'signal'), default=None)
    if points:
        as_of = month_end(points[-1][0])
        if last_evidence and last_evidence > as_of:
            as_of = min(last_evidence, datetime.utcnow())     # evidence after the last scored month moves 'now' forward
    else:
        # no KPI layer: 'now' is the latest evidence (capped at today) — a replayed or historical signals-only
        # tenant is read as of its last signal, not as of the wall clock, or every 90-day window would be empty
        as_of = min(last_evidence, datetime.utcnow()) if last_evidence else datetime.utcnow()

    from journeys.coverage import data_coverage
    coverage = data_coverage(account, points, episodes, as_of)
    if points:
        phases = detect_phases(points, episodes)
        phases_basis = 'health'
    else:
        phases = detect_phases_from_evidence(episodes, live_months)
        phases_basis = 'evidence'
    lvt = leading_series(points + [(m, None) for m in live_months], kpi_only, episodes)
    fv = feat.compute(account, vertical, taxonomy, points, episodes, phases, lvt, as_of)
    arc = arc_classifier.classify(fv, episodes, taxonomy, as_of)
    hooks = counterfactual_hooks(episodes, points)
    traj, traj_conf = trajectory_label(scores)

    by_kind: Dict[str, int] = {}
    for e in episodes:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1

    journey = {
        'version': '3.0',
        'account_id': account.account_id,
        'account_name': account.account_name,
        'vertical': vertical,
        'as_of': as_of.isoformat(),
        'last_scored_month': points[-1][0].isoformat() if points else None,
        'live_months': [m.isoformat() for m in live_months],
        'last_evidence_at': last_evidence.isoformat() if last_evidence else None,
        'arc': arc,
        'state': arc['state'],
        'current_phase': phases[-1]['name'] if phases else None,
        'phases': phases,
        'phases_basis': phases_basis,
        'data_coverage': coverage,
        'episodes': [e.to_json() for e in episodes],
        'leading_vs_trailing': lvt,
        'counterfactual_hooks': hooks,
        'expected_path': expected_path(arc['arc_type']) if arc.get('arc_type') else None,
        'features': fv,
        'summary': {
            'starting_health': scores[0] if scores else None,
            'ending_health': scores[-1] if scores else None,
            'lowest_health': min(scores) if scores else None,
            'highest_health': max(scores) if scores else None,
            'months_scored': len(points),
            'episodes_by_kind': by_kind,
            'trajectory': traj,
            'trajectory_confidence': round(traj_conf, 2),
        },
        # Compatibility keys for consumers that read the v2 shape.
        'pattern_type': arc.get('arc_type') or arc['state'],
        'starting_health': scores[0] if scores else None,
        'ending_health': scores[-1] if scores else None,
        'total_months': len(points),
        'total_weeks': len(points) * 4,
    }
    from journeys.narrative import build_narrative
    journey['narrative'] = build_narrative(journey, rejected=_rejected_evidence(account))
    return journey


def _rejected_evidence(account) -> List[dict]:
    """Evidence a reviewer rejected — kept out of the journey, named in the narrative's omitted list."""
    from models import ContextNode
    out = []
    for n in ContextNode.query.filter_by(account_id=account.account_id, node_type='SIGNAL', source='observed').all():
        rv = (n.properties or {}).get('review') or {}
        if rv.get('status') == 'rejected':
            out.append({'node_id': n.node_id, 'subtype': n.node_subtype, 'by': rv.get('by'), 'at': rv.get('at'), 'note': rv.get('note')})
    return out
