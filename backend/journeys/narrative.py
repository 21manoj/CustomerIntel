"""
Narrative block — the journey told as prose, every sentence citing the
episode ids it was built from. A sentence that cannot cite is not written;
it is listed under `omitted` with the reason, so the reader sees what the
story could NOT say as well as what it did.

    build_narrative(journey, rejected=[...]) -> dict   (pure; called last in build_journey)

Template first (generator 'template_v1'). LLM phrasing, when it comes, is
a second pass over these sentences under the same validator.

Shape:
    {generator, citation_rule, validated, chapters: [{phase, from, to, sentences: [{text, cites, template}]}],
     omitted: [{reason, template?, cites?, note}], sentence_count, cited_episode_ids}
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

GENERATOR = 'template_v1'
CITATION_RULE = 'every sentence cites >=1 episode_id present in this journey; uncited sentences are dropped'
RENEWAL_OUTCOME_WINDOW_DAYS = 60

_SOURCE_NAME = {'meeting': 'a meeting note', 'email': 'an email', 'slack': 'a Slack message', 'transcript': 'a call transcript',
                'ticket': 'a ticket', 'crm_activity': 'a CRM activity', 'manual': 'a CSM note', 'external': 'an external source',
                'csv_import': 'the CSV upload', 'load_driver': 'the replay'}

_ROLE_VERB = {
    'champion_change': 'a champion change', 'engagement_decline': 'engagement falling off',
    'usage_decline': 'usage declining', 'escalation': 'an escalation', 'infra_incident': 'an incident',
    'capacity_pressure': 'capacity pressure', 'delivery_stall': 'a delivery stall',
    'commercial_pressure': 'commercial pressure', 'announcement': 'a customer announcement',
    'crm_flag': "the CSM's own risk flag", 'expansion_intent': 'expansion interest',
    'expansion_realized': 'a realized expansion', 'advocacy': 'advocacy', 'recovery': 'recovery',
    'intervention': 'an intervention', 'routine': 'routine activity', 'product_friction': 'product friction',
}


# ── helpers ─────────────────────────────────────────────────────────────

def _d(iso: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(iso[:19]) if iso else None


def _month(iso: Optional[str]) -> str:
    dt = _d(iso)
    return dt.strftime('%B %Y') if dt else '?'


def _day(iso: Optional[str]) -> str:
    dt = _d(iso)
    return dt.strftime('%-d %B %Y') if dt else '?'


def _is_acronym(w: str) -> bool:
    return len(w) > 1 and w.isupper()


def _lc(s: str) -> str:
    """Make a title read inside a sentence: lower the leading capital, and a
    Title-Cased phrase ('Churn Risk Averted') entirely — acronyms (KPI, DR,
    QBR) and names are left alone."""
    if not s:
        return s
    words = s.split(' ')
    caps = [w for w in words if w and w[0].isalpha() and w[:1].isupper()]
    small = {'at', 'of', 'to', 'in', 'on', 'for', 'and', 'or', 'the', 'a', 'an', 'with'}
    if len(words) >= 2 and len(caps) >= 2 and all(w[:1].isupper() or w.lower() in small for w in words if w and w[0].isalpha()):
        return ' '.join(w if _is_acronym(w) else w.lower() for w in words)
    # a sentence that opens with a person's name ("Elena Rossi accepted…") keeps its capital
    if len(words) >= 3 and words[0][:1].isupper() and words[1][:1].isupper() and not words[2][:1].isupper():
        return s
    return s if _is_acronym(words[0]) else s[:1].lower() + s[1:]


def _strip_suffix(s: str) -> str:
    # demo/replay titles carry " (Account Name)" / " — Account Name"; the sentence names the account already
    for sep in (' (', ' — '):
        if sep in s and s.endswith(')') if sep == ' (' else sep in s:
            s = s.split(sep)[0]
    return s.strip().rstrip('.')


def _person(meta: dict, text: str = '') -> str:
    who = meta.get('stakeholder')
    if not who or who.lower() in (text or '').lower():
        return ''
    role = meta.get('stakeholder_role')
    if role:
        return f", raised by {who} ({role.replace('_', ' ')})"
    return f" ({who})"


def _phrase(ep: dict) -> str:
    """The evidence in words: the quote when there is one, else the title."""
    meta = ep.get('meta') or {}
    if ep['kind'] == 'signal':
        text = _strip_suffix(meta.get('quote') or ep.get('title') or ep.get('subtype') or 'a signal')
        return _lc(text) + _person(meta, text)
    if ep['kind'] == 'outcome':
        rev = ep.get('revenue')
        amt = f" (${abs(rev):,.0f} {ep.get('revenue_bucket') or ''})".rstrip() if rev is not None else ''
        return _lc(_strip_suffix(ep.get('title') or 'an outcome')) + amt
    if ep['kind'] == 'health_transition':
        return f"health moved from {meta.get('from')} to {meta.get('to')} ({meta.get('health_score')})"
    if ep['kind'] == 'decision':
        return 'decision: ' + _lc(_strip_suffix(ep.get('title') or ''))
    if ep['kind'] == 'intervention':
        return _intervention_words(ep)
    if ep['kind'] == 'stakeholder':
        return _lc(_strip_suffix(ep.get('title') or 'a stakeholder change'))
    return _lc(ep.get('title') or ep['kind'])


def _dup_key(e: dict) -> tuple:
    # the same words on the same day are one piece of evidence, whatever subtypes were read into them
    return (e['date'][:10], e['kind'], ((e.get('meta') or {}).get('quote') or e.get('title') or '').strip().lower())


def _collapse(eps: List[dict]) -> List[List[dict]]:
    """Identical evidence recorded twice (same day, kind, subtype, text) is
    said once and cites every id."""
    groups: 'OrderedDict[tuple, List[dict]]' = OrderedDict()
    for e in eps:
        groups.setdefault(_dup_key(e), []).append(e)
    return list(groups.values())


def _twins(ep: dict, episodes: List[dict]) -> List[str]:
    """Ids of every episode identical to `ep` (itself included)."""
    k = _dup_key(ep)
    return [e['episode_id'] for e in episodes if _dup_key(e) == k]


def _join(parts: List[str]) -> str:
    if len(parts) <= 1:
        return ''.join(parts)
    return ', '.join(parts[:-1]) + ', and ' + parts[-1]


# ── templates: each returns (text, cites) or None ───────────────────────

def _t_phase_open(phase: dict, by_id: Dict[str, dict], account: str, first: bool, episodes: List[dict]) -> Optional[Tuple[str, List[str]]]:
    trig = by_id.get(phase.get('trigger_episode_id') or '')
    if not trig:
        return None
    name = phase['name'].replace('_', ' ')
    when = _month(phase['entered_at'])
    if first and phase.get('health_start') is None:
        text = (f"From {when} {account} is read from evidence alone (no KPI layer); the first thing it showed was "
                f"{_ROLE_VERB.get(trig.get('role'), 'activity')}: {_phrase(trig)}.")
    elif first:
        text = (f"From {when} {account} was in {name} on the numbers ({phase.get('health_start')}) "
                f"while the evidence already showed {_ROLE_VERB.get(trig.get('role'), 'activity')}: {_phrase(trig)}.")
    else:
        text = f"{name.capitalize()} began in {when} with {_phrase(trig)}."
    return text, _twins(trig, episodes)


def _t_first_warning(lvt: dict, by_id: Dict[str, dict]) -> Optional[Tuple[str, List[str]]]:
    first = lvt.get('first_leading_warning_at')
    if not first:
        return None
    entry = next((s for s in lvt.get('series', []) if s.get('month') == first[:10]), None)
    cites = [c for c in (entry or {}).get('contributing_episode_ids', []) if c in by_id]
    if not cites:
        return None
    trailing = lvt.get('first_trailing_warning_at')
    if trailing and lvt.get('lead_days') is not None:
        tail = f"the KPI score crossed at-risk in {_month(trailing)}, {lvt['lead_days']} days later"
    else:
        tail = "the KPI score never fell into the critical band, which is the trailing layer's own warning line"
    return f"The leading layer first flagged early_warning in {_month(first)}; {tail}.", cites


def _t_month_events(month_key: str, groups: List[List[dict]]) -> Optional[Tuple[str, List[str]]]:
    parts, cites = [], []
    for g in groups:
        parts.append(_phrase(g[0]))
        cites.extend(e['episode_id'] for e in g)
    if not parts:
        return None
    return f"In {month_key}, {_join(parts)}.", cites


def _intervention_words(ep: dict) -> str:
    """A governed playbook intervention in words: which playbook, who approved, whether it reached the workflow,
    what the workflow reported. The delivery problem is said, never hidden (design §6)."""
    meta = ep.get('meta') or {}
    d = meta.get('delivery_status')
    tail = ', but the workflow endpoint was not configured' if d == 'not_configured' else \
           ', but delivery to the workflow failed' if d == 'failed' else ' and sent to the workflow' if d == 'delivered' else ''
    closed = meta.get('closed_state')
    if closed:
        tail += f'; the workflow reported it {closed}'
    ac = f" ({meta['action_class']})" if meta.get('action_class') else ''
    return f"playbook '{_lc(_strip_suffix(ep.get('title') or 'an intervention'))}'{ac} was approved by {meta.get('approved_by') or 'a person'}{tail}"


def _t_intervention(hook: dict, by_id: Dict[str, dict]) -> Optional[Tuple[str, List[str]]]:
    ep = by_id.get(hook.get('episode_id'))
    if not ep:
        return None
    b, a = hook.get('health_before') or {}, hook.get('health_after') or {}
    cites = [ep['episode_id']]
    if ep.get('kind') == 'intervention':
        words = _intervention_words(ep)
        text = f"{words[:1].upper()}{words[1:]} on {_day(hook.get('date'))}"
    else:
        text = f"{_strip_suffix(ep.get('title') or 'An intervention')} on {_day(hook.get('date'))}"
    if b.get('mean') is not None and a.get('last') is not None:
        text += f"; health averaged {b['mean']} in the {b.get('n')} months before and stood at {a['last']} after"
    outs = [o for o in hook.get('outcomes_after', []) if o.get('episode_id') in by_id]
    if outs:
        cites.extend(o['episode_id'] for o in outs)
        def _amt(o):      # an outcome without a revenue figure is said without one, never as $0
            return f" (${abs(o['revenue']):,.0f} {o.get('bucket', '')})".rstrip() if o.get('revenue') is not None else (f" ({o['bucket']})" if o.get('bucket') else '')
        text += ", with " + _join([f"{_lc(_strip_suffix(by_id[o['episode_id']].get('title') or o.get('bucket', 'an outcome')))}{_amt(o)}"
                                   for o in outs]) + " within 90 days"
    return text + '.', cites


def _t_live_notice(journey: dict, live_eps: List[dict]) -> Optional[Tuple[str, List[str]]]:
    if not journey.get('live_months') or not live_eps:
        return None
    cov = journey.get('data_coverage') or {}
    layer = cov.get('kpi_layer')
    if layer == 'none':
        text = (f"This account has no KPI layer ({cov.get('basis')}); everything here is read from evidence — "
                f"{cov.get('evidence_count')} signals over {cov.get('evidence_span_days')} days.")
    elif layer == 'not_yet':
        text = f"No KPI upload yet ({cov.get('basis')}); what follows is evidence only."
    else:
        text = f"No KPI upload has arrived since {_month(journey.get('last_scored_month'))}; what follows is live evidence only."
    return text, [live_eps[0]['episode_id']]


def _t_arc(arc: dict, by_id: Dict[str, dict]) -> Optional[Tuple[str, List[str]]]:
    cites = [c for c in arc.get('supporting_episode_ids', []) if c in by_id]
    if arc.get('arc_type') and cites:
        sem = (arc.get('confidence_semantics') or '').replace('_', ' ')
        return (f"The arc hypothesis is {arc['arc_type']} (confidence {arc.get('confidence')}, {sem}), "
                f"supported by {len(cites)} cited episode{'s' if len(cites) != 1 else ''}."), cites
    return None


# ── assembly ────────────────────────────────────────────────────────────

def _chapters_for(journey: dict, episodes: List[dict]) -> List[dict]:
    phases = journey.get('phases') or []
    if not phases:
        return [{'phase': 'evidence', 'from': episodes[0]['date'][:10] if episodes else None, 'to': None, 'episodes': list(episodes)}]
    out = []
    for i, ph in enumerate(phases):
        start, end = _d(ph['entered_at']), _d(ph.get('exited_at'))
        eps = [e for e in episodes if _d(e['date']) >= start and (end is None or _d(e['date']) < end or i == len(phases) - 1)]
        out.append({'phase': ph['name'], 'from': ph['entered_at'][:10], 'to': (ph.get('exited_at') or '')[:10] or None,
                    'episodes': eps, '_phase': ph})
    # evidence before the first scored phase belongs to the opening chapter
    first_start = _d(phases[0]['entered_at'])
    early = [e for e in episodes if _d(e['date']) < first_start]
    if early:
        out[0]['episodes'] = early + out[0]['episodes']
    return out


def build_narrative(journey: dict, rejected: Optional[List[dict]] = None) -> dict:
    episodes = [e for e in journey.get('episodes', []) if e.get('kind') != 'renewal']
    by_id = {e['episode_id']: e for e in journey.get('episodes', [])}
    account = journey.get('account_name') or 'The account'
    lvt = journey.get('leading_vs_trailing') or {}
    hooks = journey.get('counterfactual_hooks') or []
    hook_ids = {h.get('episode_id') for h in hooks}
    hook_outcome_ids = {o.get('episode_id') for h in hooks for o in h.get('outcomes_after', [])}
    live_from = _d(journey['live_months'][0]) if journey.get('live_months') and journey.get('phases_basis') != 'evidence' else None
    live_eps = [e for e in episodes if live_from and _d(e['date']) >= live_from]      # every kind: signals, outcomes, decisions

    chapters, omitted = [], []
    for ci, ch in enumerate(_chapters_for(journey, episodes)):
        sentences: List[dict] = []

        def add(res, template, why=None):
            if res is None:
                omitted.append({'reason': 'no_citation', 'template': template, 'note': why or 'nothing in the journey to cite'})
                return
            text, cites = res
            sentences.append({'text': text, 'cites': list(dict.fromkeys(cites)), 'template': template})

        if ci == 0 and journey.get('phases_basis') == 'evidence':
            first_sig = next((e for e in episodes if e['kind'] == 'signal'), None)
            if first_sig:
                cov = journey.get('data_coverage') or {}
                sentences.append({'text': (f"This account has no KPI layer ({cov.get('basis')}); everything here is read from evidence — "
                                           f"{cov.get('evidence_count')} signals over {cov.get('evidence_span_days')} days."),
                                  'cites': [first_sig['episode_id']], 'template': 'coverage_notice'})
        if '_phase' in ch:
            trig_id = ch['_phase'].get('trigger_episode_id')
            if trig_id in hook_ids and ci > 0:
                # a phase opened by an intervention is told with its before/after, not a plain opener
                add(_t_intervention(next(h for h in hooks if h.get('episode_id') == trig_id), by_id), 'intervention_before_after')
            else:
                add(_t_phase_open(ch['_phase'], by_id, account, first=(ci == 0), episodes=episodes), 'phase_open_with_trigger',
                    why=None if trig_id else f"phase '{ch['_phase']['name']}' from {ch['_phase']['entered_at'][:10]} has no trigger episode (no evidence before it)")
        if ci == 0:
            add(_t_first_warning(lvt, by_id), 'first_warning_gap')

        # the month-by-month record, interventions told with their before/after
        used = set(s for sent in sentences for s in sent['cites'])
        by_month: 'OrderedDict[str, List[dict]]' = OrderedDict()
        for e in ch['episodes']:
            if e['episode_id'] in used or e['episode_id'] in hook_outcome_ids:
                continue
            if e['episode_id'] in hook_ids:
                add(_t_intervention(next(h for h in hooks if h.get('episode_id') == e['episode_id']), by_id), 'intervention_before_after')
                continue
            if live_from and _d(e['date']) >= live_from:
                continue   # told below, under the live notice
            by_month.setdefault(_month(e['date']), []).append(e)
        for mk, eps in by_month.items():
            add(_t_month_events(mk, _collapse(eps)), 'month_events')

        chapters.append({'phase': ch['phase'], 'from': ch['from'], 'to': ch['to'], 'sentences': sentences})

    # evidence after the last scored month — its own closing chapter
    if live_eps:
        sentences = []
        res = _t_live_notice(journey, live_eps)
        if res:
            sentences.append({'text': res[0], 'cites': res[1], 'template': 'live_months_notice'})
        by_day: 'OrderedDict[str, List[dict]]' = OrderedDict()
        for e in live_eps:
            by_day.setdefault(e['date'][:10], []).append(e)
        for day, eps in by_day.items():
            groups = _collapse(eps)
            parts = [_phrase(g[0]) for g in groups]
            cites = [e['episode_id'] for g in groups for e in g]
            if all(e['kind'] == 'signal' for e in eps):
                src = _SOURCE_NAME.get((eps[0].get('meta') or {}).get('source_platform') or '', 'a note')
                text = f"On {_day(day)} {src} recorded {_join(parts)}."
            else:
                text = f"On {_day(day)}, {_join(parts)}."
            sentences.append({'text': text, 'cites': cites, 'template': 'live_evidence_cluster'})
        chapters.append({'phase': 'live', 'from': journey['live_months'][0], 'to': None, 'sentences': sentences})

    # the arc, last, so it reads as the conclusion of the evidence above
    res = _t_arc(journey.get('arc') or {}, by_id)
    if res:
        chapters[-1]['sentences'].append({'text': res[0], 'cites': res[1], 'template': 'arc_statement'})
    elif (journey.get('arc') or {}).get('state') in ('steady', 'unclassified'):
        omitted.append({'reason': 'no_citation', 'template': 'arc_statement',
                        'note': f"state {journey['arc']['state']}: {journey['arc'].get('reason') or 'no rule satisfied'}"})

    # things the story could not say
    renewal = next((e for e in journey.get('episodes', []) if e.get('kind') == 'renewal'), None)
    as_of = _d(journey.get('as_of'))
    if renewal and as_of and _d(renewal['date']) < as_of:
        rd = _d(renewal['date'])
        near = [e for e in episodes if e['kind'] == 'outcome' and abs((_d(e['date']) - rd).days) <= RENEWAL_OUTCOME_WINDOW_DAYS]
        if not near:
            omitted.append({'reason': 'no_citation', 'template': 'renewal_outcome',
                            'note': f"renewal date {rd.date().isoformat()} has passed but no outcome episode exists within "
                                    f"{RENEWAL_OUTCOME_WINDOW_DAYS} days of it; sentence not written"})
    for r in (rejected or []):
        omitted.append({'reason': 'rejected_evidence', 'cites': [f"sig:{r['node_id']}"],
                        'note': f"{r.get('subtype')} rejected by {r.get('by') or 'a reviewer'} on {(r.get('at') or '')[:10]}"
                                + (f": {r['note']}" if r.get('note') else '')})

    narrative = {'generator': GENERATOR, 'citation_rule': CITATION_RULE, 'chapters': chapters, 'omitted': omitted}
    return validate_narrative(narrative, set(by_id))


def validate_narrative(narrative: dict, episode_ids: set) -> dict:
    """Enforce the rule: drop any sentence without a citation that resolves."""
    kept_total, cited = 0, set()
    for ch in narrative['chapters']:
        kept = []
        for s in ch['sentences']:
            cites = [c for c in s.get('cites', []) if c in episode_ids]
            if not cites:
                narrative['omitted'].append({'reason': 'no_citation', 'template': s.get('template'), 'note': s.get('text', '')[:120]})
                continue
            s['cites'] = cites
            kept.append(s)
            cited.update(cites)
        ch['sentences'] = kept
        kept_total += len(kept)
    narrative['validated'] = True
    narrative['sentence_count'] = kept_total
    narrative['cited_episode_ids'] = sorted(cited)
    return narrative
