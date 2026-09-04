"""
Narrative block: every sentence cites; uncited sentences are dropped into
`omitted`; duplicates collapse; rejected evidence and a passed renewal
without an outcome are named as things the story could not say.
Pure — no DB.
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from journeys.narrative import build_narrative, validate_narrative  # noqa: E402


def _ep(i, date, kind, subtype, role=None, title='', quote=None, who=None, who_role=None, rev=None, bucket=None):
    return {'episode_id': f'{"sig" if kind == "signal" else "out" if kind == "outcome" else "hs"}:{i}', 'date': date, 'kind': kind,
            'subtype': subtype, 'role': role, 'polarity': 0, 'source': 'observed', 'title': title, 'evidence_node_ids': [i],
            'meta': {'quote': quote, 'stakeholder': who, 'stakeholder_role': who_role, 'source_platform': 'meeting'},
            'revenue': rev, 'revenue_bucket': bucket}


def _journey():
    eps = [
        _ep(1, '2025-07-31T00:00:00', 'signal', 'kpi_decline', 'usage_decline', 'KPI metrics declining below threshold (Acme)'),
        _ep(2, '2025-10-15T00:00:00', 'signal', 'engagement_gap', 'engagement_decline', 'Champion declined last two QBR invitations.', who='Lisa Park'),
        _ep(3, '2025-10-15T00:00:00', 'signal', 'engagement_gap', 'engagement_decline', 'Champion declined last two QBR invitations.', who='Lisa Park'),   # duplicate
        {'episode_id': 'hs:4', 'date': '2025-10-01T00:00:00', 'kind': 'health_transition', 'subtype': 'healthy->at_risk', 'role': None,
         'polarity': -1, 'source': 'system', 'title': 'Health healthy → at_risk (68.7)', 'evidence_node_ids': [],
         'meta': {'health_score': 68.7, 'from': 'healthy', 'to': 'at_risk'}},
        _ep(5, '2025-11-28T00:00:00', 'outcome', 'renewal_uncertainty', title='Renewal at Risk — Acme', rev=-960000.0, bucket='at_risk'),
        _ep(6, '2026-03-08T00:00:00', 'signal', 'csm_intervention', 'intervention', 'New CSM assigned (Acme)', who='Jordan Blake'),
        _ep(7, '2026-03-28T00:00:00', 'outcome', 'churn_averted', title='Churn Risk Averted — Acme', rev=1920000.0, bucket='protected'),
        {'episode_id': 'renewal', 'date': '2026-08-01T00:00:00', 'kind': 'renewal', 'subtype': 'renewal_date', 'role': None, 'polarity': 0,
         'source': 'observed', 'title': 'Renewal date', 'evidence_node_ids': [], 'meta': {}},
        _ep(9, '2026-09-02T16:00:00', 'signal', 'champion_departure', 'champion_change', 'x', quote='replacing Lisa Park who left last month',
            who='Lisa Park', who_role='champion'),
    ]
    return {
        'account_name': 'Acme Data', 'as_of': '2026-09-02T16:00:00', 'last_scored_month': '2026-03-01',
        'live_months': ['2026-04-01', '2026-05-01', '2026-06-01', '2026-07-01', '2026-08-01', '2026-09-01'],
        'episodes': eps,
        'phases': [{'name': 'baseline', 'entered_at': '2025-07-01', 'exited_at': '2025-09-01', 'health_start': 77.5, 'health_end': 75.0, 'trigger_episode_id': 'sig:1'},
                   {'name': 'deterioration', 'entered_at': '2025-09-01', 'exited_at': '2026-03-01', 'health_start': 72.1, 'health_end': 56.7, 'trigger_episode_id': 'sig:2'},
                   {'name': 'baseline', 'entered_at': '2026-03-01', 'exited_at': None, 'health_start': 71.3, 'health_end': 71.3, 'trigger_episode_id': 'sig:6'}],
        'leading_vs_trailing': {'first_leading_warning_at': '2025-07-01', 'first_trailing_warning_at': None, 'lead_days': None,
                                'series': [{'month': '2025-07-01', 'contributing_episode_ids': ['sig:1']}]},
        'counterfactual_hooks': [{'episode_id': 'sig:6', 'date': '2026-03-08T00:00:00', 'title': 'New CSM assigned (Acme)',
                                  'health_before': {'n': 3, 'mean': 59.66, 'last': 56.73}, 'health_after': {'n': 1, 'mean': 71.25, 'last': 71.25},
                                  'outcomes_after': [{'episode_id': 'out:7', 'bucket': 'protected', 'revenue': 1920000.0}]}],
        'arc': {'arc_type': 'exec_sponsor_change', 'state': 'classified', 'confidence': 0.85, 'confidence_semantics': 'rule_match_constant',
                'supporting_episode_ids': ['sig:9']},
    }


def test_every_sentence_cites_and_ids_resolve():
    j = _journey()
    n = build_narrative(j, rejected=[{'node_id': 393, 'subtype': 'advocacy', 'by': 'manoj', 'at': '2026-09-04T04:34:15', 'note': 'wrong product line'}])
    ids = {e['episode_id'] for e in j['episodes']}
    assert n['validated'] and n['generator'] == 'template_v1' and n['sentence_count'] >= 6
    for ch in n['chapters']:
        for s in ch['sentences']:
            assert s['cites'] and set(s['cites']) <= ids, s
    assert [c['phase'] for c in n['chapters']] == ['baseline', 'deterioration', 'baseline', 'live']


def test_content_of_key_sentences():
    n = build_narrative(_journey())
    text = ' '.join(s['text'] for ch in n['chapters'] for s in ch['sentences'])
    assert 'From July 2025 Acme Data was in baseline on the numbers (77.5)' in text
    assert 'never crossed the at-risk line' in text
    assert 'champion declined last two qbr invitations (lisa park)' in text.lower()
    assert 'health moved from healthy to at_risk (68.7)' in text
    assert 'renewal at risk ($960,000 at_risk)' in text.lower()
    assert 'New CSM assigned on 8 March 2026; health averaged 59.66 in the 3 months before and stood at 71.25 after, with churn risk averted ($1,920,000 protected) within 90 days.' in text
    assert 'No KPI upload has arrived since March 2026' in text
    assert 'On 2 September 2026 meeting recorded replacing Lisa Park who left last month, raised by Lisa Park (champion).' in text
    assert 'The arc hypothesis is exec_sponsor_change (confidence 0.85, rule match constant), supported by 1 cited episode.' in text


def test_duplicates_collapse_into_one_sentence_citing_both():
    n = build_narrative(_journey())
    s = next(s for ch in n['chapters'] for s in ch['sentences'] if 'qbr' in s['text'].lower())
    assert set(s['cites']) >= {'sig:2', 'sig:3'} and s['text'].lower().count('qbr') == 1


def test_omitted_lists_what_could_not_be_said():
    n = build_narrative(_journey(), rejected=[{'node_id': 393, 'subtype': 'advocacy', 'by': 'manoj', 'at': '2026-09-04T04:34:15', 'note': 'wrong product line'}])
    reasons = {(o['reason'], o.get('template')) for o in n['omitted']}
    assert ('no_citation', 'renewal_outcome') in reasons                       # renewal passed, no outcome near it
    rej = next(o for o in n['omitted'] if o['reason'] == 'rejected_evidence')
    assert rej['cites'] == ['sig:393'] and 'manoj' in rej['note'] and 'wrong product line' in rej['note']


def test_validator_drops_uncited_and_unknown_citations():
    narr = {'chapters': [{'phase': 'x', 'from': None, 'to': None, 'sentences': [
        {'text': 'cited', 'cites': ['sig:1'], 'template': 't'},
        {'text': 'ghost citation', 'cites': ['sig:999'], 'template': 't'},
        {'text': 'no citation', 'cites': [], 'template': 't'}]}], 'omitted': []}
    out = validate_narrative(narr, {'sig:1'})
    assert [s['text'] for s in out['chapters'][0]['sentences']] == ['cited']
    assert len(out['omitted']) == 2 and out['sentence_count'] == 1 and out['cited_episode_ids'] == ['sig:1']


def test_steady_journey_without_evidence_says_nothing_and_says_why():
    j = {'account_name': 'Quiet Co', 'as_of': '2026-03-31T00:00:00', 'last_scored_month': '2026-03-01', 'live_months': [], 'episodes': [],
         'phases': [{'name': 'baseline', 'entered_at': '2026-01-01', 'exited_at': None, 'health_start': 80, 'health_end': 81, 'trigger_episode_id': None}],
         'leading_vs_trailing': {'series': []}, 'counterfactual_hooks': [],
         'arc': {'arc_type': None, 'state': 'steady', 'reason': 'no negative or positive evidence in 90 days'}}
    n = build_narrative(j)
    assert n['sentence_count'] == 0
    assert any(o['template'] == 'arc_statement' and 'steady' in (o.get('note') or '') for o in n['omitted'])
