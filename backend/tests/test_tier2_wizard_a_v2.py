"""
Tier 2A-5 checkpoint: Wizard A v2 (journeys/) against a real Postgres DB.

Five archetype accounts are inserted directly (health rows + observed
graph nodes) so each classifier rule, the phase detector, the
leading-vs-trailing series and the persistence contract can be asserted
exactly. The end-to-end path through process_data is covered by
test_tier2_process_data_parity.py on the customer-359 fixture.
"""
import os
import sys
import uuid
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from extensions import db


def _make_app():
    _app = Flask(__name__)
    _app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test'
    )
    db.init_app(_app)
    return _app


app = _make_app()

import mcp_server.common as _common
_common._flask_app = app

import utils.health_thresholds as ht
from models import Account, HealthScore, ContextNode, JourneyData


def _assert_isolated_test_db(uri: str) -> None:
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
    if 'test' not in db_name.lower():
        raise RuntimeError(
            f"test_tier2_wizard_a_v2.py refuses to run against database "
            f"{db_name!r} — its name doesn't contain 'test'."
        )


MONTHS = [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)]

ARCHETYPES = {
    # name: (health series Nov..Apr, signals [(date, subtype, sentiment)], extras)
    'Champion Lost': ([82, 80, 76, 68, 61, 55],
                      [(datetime(2026, 1, 10), 'champion_departure', -0.7),
                       (datetime(2026, 2, 5), 'engagement_decline', -0.5)],
                      {'renewal_date': '2026-06-15'}),
    'Silent Slide': ([78, 74, 69, 62, 54, 47],
                     [(datetime(2025, 12, 15), 'engagement_drop', -0.4),
                      (datetime(2026, 1, 20), 'login_decline', -0.5),
                      (datetime(2026, 2, 10), 'usage_decline', -0.6)],
                     {}),
    'Steady Eddie': ([84, 85, 84, 86, 85, 86],
                     [(datetime(2026, 2, 3), 'routine_review', 0.2),
                      (datetime(2026, 3, 9), 'advocacy', 0.8)],
                     {}),
    'Mystery Dip': ([75, 72, 66, 60, 58, 57],
                    [(datetime(2026, 1, 8), 'routine_review', 0.0),
                     (datetime(2026, 2, 14), 'weird_thing', -0.3)],
                    {}),
    'Infra Crisis': ([80, 78, 45, 40, 52, 60],
                     [(datetime(2026, 1, 5), 'reliability_sla_breach', -0.8),
                      (datetime(2026, 1, 12), 'support_escalation', -0.7),
                      (datetime(2026, 2, 1), 'csm_intervention', 0.3)],
                     {'decision': (datetime(2026, 1, 20), 'War room stood up'),
                      'outcome': (datetime(2026, 3, 15), 'churn_averted', 500000.0)}),
}


@pytest.fixture(scope='module')
def tenant():
    _assert_isolated_test_db(app.config['SQLALCHEMY_DATABASE_URI'])
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(data_origin='synthetic_test', name=f'WizardA {tag}', domain=f'wa-{tag}.test', vertical='datacenter_v1',
                              admin_email=f'wa_{tag}@t.test', admin_name='A')['customer_id']
        ids = {}
        for name, (series, signals, extras) in ARCHETYPES.items():
            pm = {'renewal_date': extras['renewal_date']} if 'renewal_date' in extras else {}
            a = Account(customer_id=cid, account_name=name, revenue=1_000_000, vertical='datacenter_v1',
                        profile_metadata=pm or None)
            db.session.add(a)
            db.session.flush()
            ids[name] = a.account_id
            for m, s in zip(MONTHS, series):
                db.session.add(HealthScore(account_id=a.account_id, measurement_month=m, health_score=s,
                                           kpi_only_score=s, health_status=ht.classify(s)))
            for i, (dt, sub, sent) in enumerate(signals):
                db.session.add(ContextNode(
                    customer_id=cid, account_id=a.account_id, node_type='SIGNAL', node_subtype=sub,
                    source='observed', title=f'{sub} ({name})', tier=2, occurred_at=dt,
                    properties={'sentiment_score': str(sent), 'signal_ref': f'{name}_{i}'},
                    source_platform='csv_import', source_event_id=f'{name}_{i}',
                ))
            if 'decision' in extras:
                dt, title = extras['decision']
                db.session.add(ContextNode(customer_id=cid, account_id=a.account_id, node_type='DECISION',
                                           node_subtype='escalation', source='observed', title=title, tier=1,
                                           occurred_at=dt, properties={}, source_platform='csv_import'))
            if 'outcome' in extras:
                dt, sub, rev = extras['outcome']
                db.session.add(ContextNode(customer_id=cid, account_id=a.account_id, node_type='OUTCOME',
                                           node_subtype=sub, source='observed', title=f'{sub} ({name})', tier=1,
                                           occurred_at=dt, revenue_impact=rev, revenue_impact_type=sub,
                                           properties={}, source_platform='csv_import'))
        db.session.commit()
        from journeys.wizard_a import run_wizard_a
        res = run_wizard_a(cid)
        yield cid, ids, res
        db.session.remove()
        db.drop_all()


def _journey(cid, aid):
    return JourneyData.query.filter_by(customer_id=cid, account_id=aid).first().journey_json


class TestRunSummary:
    def test_coverage_and_persistence(self, tenant):
        cid, ids, res = tenant
        assert res['status'] == 'completed'
        assert res['processed'] == 5
        assert res['coverage'] == {'classified': 3, 'steady': 1, 'unclassified': 1, 'classified_pct': 80.0}
        with app.app_context():
            rows = JourneyData.query.filter_by(customer_id=cid).all()
            assert len(rows) == 5
            from journeys.wizard_a import GENERATOR_VERSION
            assert all(r.generator_version == GENERATOR_VERSION and r.journey_json['version'] == '3.0'
                       and r.journey_json['generator_version'] == GENERATOR_VERSION for r in rows)

    def test_no_synthetic_nodes_or_edges_written(self, tenant):
        cid, ids, _ = tenant
        with app.app_context():
            assert ContextNode.query.filter(ContextNode.customer_id == cid,
                                            ContextNode.source != 'observed').count() == 0
            from models import ContextEdge
            assert ContextEdge.query.filter_by(customer_id=cid).count() == 0

    def test_rerun_is_idempotent(self, tenant):
        cid, ids, first = tenant
        with app.app_context():
            from journeys.wizard_a import run_wizard_a
            again = run_wizard_a(cid)
            assert again['coverage'] == first['coverage']
            assert again['leading_rows_written'] == 0        # nothing changed → nothing rewritten
            assert JourneyData.query.filter_by(customer_id=cid).count() == 5


class TestArcRules:
    def test_champion_loss_is_exec_sponsor_change_with_cited_episodes(self, tenant):
        cid, ids, _ = tenant
        with app.app_context():
            j = _journey(cid, ids['Champion Lost'])
            arc = j['arc']
            assert arc['state'] == 'classified' and arc['arc_type'] == 'exec_sponsor_change'
            assert arc['confidence_semantics'] == 'rule_match_constant'
            cited = {e['subtype'] for e in j['episodes'] if e['episode_id'] in arc['supporting_episode_ids']}
            assert cited == {'champion_departure'}
            assert 'champion_change' in arc['observed_roles']
            a = db.session.get(Account, ids['Champion Lost'])
            assert a.arc_type == 'exec_sponsor_change' and a.arc_confidence == 0.85
            assert j['expected_path']['arc_type'] == 'exec_sponsor_change'
            assert j['expected_path']['source'] == 'story_arc_template'
            assert j['features']['days_to_renewal_band'] == '31-90'   # 2026-06-15 from as_of 2026-04-30

    def test_silent_slide_is_silent_churn(self, tenant):
        cid, ids, _ = tenant
        with app.app_context():
            arc = _journey(cid, ids['Silent Slide'])['arc']
            assert arc['arc_type'] == 'silent_churn', arc
            assert set(arc['observed_roles']) == {'engagement_decline', 'usage_decline'}

    def test_healthy_quiet_account_is_steady_not_an_arc(self, tenant):
        cid, ids, _ = tenant
        with app.app_context():
            j = _journey(cid, ids['Steady Eddie'])
            assert j['state'] == 'steady' and j['arc']['arc_type'] is None
            assert j['pattern_type'] == 'steady'
            assert j['expected_path'] is None
            # advocacy alone is not expansion_champion — reported as the alternative it almost was
            alt = {a['arc_type']: a for a in j['arc']['alternatives']}
            assert 'expansion_champion' in alt and any('expansion_intent' in m for m in alt['expansion_champion']['missing'])
            assert db.session.get(Account, ids['Steady Eddie']).arc_type is None

    def test_decline_without_matching_evidence_is_unclassified_with_reason(self, tenant):
        cid, ids, _ = tenant
        with app.app_context():
            j = _journey(cid, ids['Mystery Dip'])
            assert j['state'] == 'unclassified'
            assert 'no rule satisfied' in j['arc']['reason']
            assert j['arc']['arc_type'] is None                       # no fallback arc, ever
            assert j['features']['unmapped_signals_90d'] == 1        # 'weird_thing' is visible as a gap
            assert db.session.get(Account, ids['Mystery Dip']).arc_type is None

    def test_infra_crisis_is_crisis_recovery_not_exec_sponsor_change(self, tenant):
        """The old classifier routed infra incidents with an escalation to
        exec_sponsor_change (escalation was in its champion-loss set)."""
        cid, ids, _ = tenant
        with app.app_context():
            j = _journey(cid, ids['Infra Crisis'])
            arc = j['arc']
            assert arc['arc_type'] == 'crisis_recovery', arc
            assert 'infra_incident' in arc['observed_roles'] and 'escalation' in arc['observed_roles']
            assert 'champion_change' not in arc['observed_roles']
            assert j['current_phase'] == 'resolution'


class TestPhasesAndEpisodes:
    def test_phase_segments_with_triggers(self, tenant):
        cid, ids, _ = tenant
        with app.app_context():
            j = _journey(cid, ids['Infra Crisis'])
            names = [p['name'] for p in j['phases']]
            assert names == ['baseline', 'intervention', 'resolution'], names
            interv = j['phases'][1]
            assert interv['entered_at'] == '2026-01-01' and interv['months'] == 2
            assert interv['trigger_episode_id'] is not None
            trigger = next(e for e in j['episodes'] if e['episode_id'] == interv['trigger_episode_id'])
            # the most recent thing that happened TO the account before the
            # phase — the escalation, not the war-room decision taken in response
            assert trigger['kind'] == 'signal' and trigger['subtype'] == 'support_escalation'

    def test_episode_kinds_and_evidence(self, tenant):
        cid, ids, _ = tenant
        with app.app_context():
            j = _journey(cid, ids['Infra Crisis'])
            kinds = j['summary']['episodes_by_kind']
            assert kinds['signal'] == 3 and kinds['decision'] == 1 and kinds['outcome'] == 1
            assert kinds['health_transition'] >= 2          # healthy→critical, critical→at_risk, at_risk→healthy
            out = next(e for e in j['episodes'] if e['kind'] == 'outcome')
            assert out['revenue_bucket'] == 'protected' and out['revenue'] == 500000.0
            assert all(e['evidence_node_ids'] for e in j['episodes'] if e['kind'] in ('signal', 'decision', 'outcome'))
            sig = next(e for e in j['episodes'] if e['subtype'] == 'reliability_sla_breach')
            assert sig['role'] == 'infra_incident' and sig['polarity'] == -1 and sig['sentiment'] == -0.8

    def test_counterfactual_hooks_around_interventions(self, tenant):
        cid, ids, _ = tenant
        with app.app_context():
            hooks = _journey(cid, ids['Infra Crisis'])['counterfactual_hooks']
            assert len(hooks) == 2                           # observed decision + csm_intervention
            war_room = next(h for h in hooks if 'War room' in h['title'])
            # decision on 2026-01-20: before = Nov, Dec; after = Jan, Feb, Mar (month ends within 90d)
            assert war_room['health_before'] == {'n': 2, 'mean': 79.0, 'last': 78.0}
            assert war_room['health_after']['n'] == 3 and war_room['health_after']['last'] == 52.0
            assert any(o['bucket'] == 'protected' and o['revenue'] == 500000.0 for o in war_room['outcomes_after'])


class TestLeadingVsTrailing:
    def test_leading_warns_before_trailing(self, tenant):
        cid, ids, _ = tenant
        with app.app_context():
            lvt = _journey(cid, ids['Silent Slide'])['leading_vs_trailing']
            assert lvt['first_leading_warning_at'] == '2025-12-01'
            assert lvt['first_trailing_warning_at'] == '2026-04-01'
            assert lvt['lead_days'] == 121
            dec = next(s for s in lvt['series'] if s['month'] == '2025-12-01')
            assert dec['qual'] == 30.0                       # (-0.4 + 1) * 50, single signal in window
            assert dec['divergence'] == round(30.0 - 74, 2)
            assert dec['early_warning'] == 'early_warning'
            nov = next(s for s in lvt['series'] if s['month'] == '2025-11-01')
            assert nov['qual'] is None and nov['early_warning'] is None   # no signals yet → no claim

    def test_qual_never_blended_into_kpi_only(self, tenant):
        cid, ids, _ = tenant
        with app.app_context():
            for hs in HealthScore.query.filter_by(account_id=ids['Silent Slide']).all():
                assert hs.kpi_only_score == hs.health_score
            hs_dec = HealthScore.query.filter_by(account_id=ids['Silent Slide'], measurement_month=date(2025, 12, 1)).first()
            assert float(hs_dec.qual_score) == 30.0
            assert float(hs_dec.divergence) == -44.0
            assert hs_dec.early_warning == 'early_warning'
            hs_nov = HealthScore.query.filter_by(account_id=ids['Silent Slide'], measurement_month=date(2025, 11, 1)).first()
            assert hs_nov.qual_score is None

    def test_intervention_lifts_leading_before_trailing_moves(self, tenant):
        """Infra Crisis: the +0.3 csm_intervention on Feb 1 pulls qual up
        from Jan's incident-dominated value while kpi_only is still 40 —
        the leading layer moves first; the label stays early_warning until
        the gap closes."""
        cid, ids, _ = tenant
        with app.app_context():
            lvt = _journey(cid, ids['Infra Crisis'])['leading_vs_trailing']
            jan = next(s for s in lvt['series'] if s['month'] == '2026-01-01')
            feb = next(s for s in lvt['series'] if s['month'] == '2026-02-01')
            assert jan['qual'] < 20 and jan['early_warning'] == 'early_warning'
            assert feb['qual'] > jan['qual'] and feb['signal_count'] == 3
            assert lvt['first_leading_warning_at'] == '2026-01-01' and lvt['lead_days'] == 0

    def test_recovery_watch_label_pure(self):
        """Leading well above trailing → recovery_watch (pure function)."""
        from journeys.journey_builder import leading_series, Episode
        pts = [(date(2026, 1, 1), 40.0), (date(2026, 2, 1), 42.0)]
        eps = [Episode('sig:1', datetime(2026, 2, 10), 'signal', 'executive_engagement', 'advocacy', 1,
                       'observed', 'exec joined review', [1], sentiment=0.8)]
        lvt = leading_series(pts, {m: s for m, s in pts}, eps)
        feb = lvt['series'][1]
        assert feb['qual'] == 90.0 and feb['divergence'] == 48.0 and feb['early_warning'] == 'recovery_watch'
        assert lvt['series'][0]['qual'] is None
        assert lvt['first_leading_warning_at'] is None and lvt['first_trailing_warning_at'] == '2026-01-01'


class TestSignalRoles:
    def test_roles_resolve_base_and_overlay(self):
        from utils.taxonomy_loader import get_taxonomy, reset_cache
        reset_cache()
        t = get_taxonomy('datacenter_v1')
        assert t.signal_role('reliability_sla_breach') == 'infra_incident'    # overlay
        assert t.signal_role('champion_departure') == 'champion_change'       # base
        assert t.signal_role('support_escalation') == 'escalation'
        assert t.signal_role('not_a_subtype') is None
        assert t.role_polarity('infra_incident') == -1 and t.role_polarity('advocacy') == 1
        assert t.role_polarity('intervention') == 0
        assert get_taxonomy('saas_premium').signal_role('reliability_sla_breach') is None

    def test_every_datacenter_fixture_subtype_has_a_role(self):
        """The signals the old classifier could not read."""
        import csv
        from pathlib import Path
        from utils.taxonomy_loader import get_taxonomy
        t = get_taxonomy('datacenter_v1')
        f = Path(__file__).parent / 'fixtures' / 'customer359_datacenter_v1' / 'enhanced_qualitative_signals.csv'
        subtypes = {r['signal_type'] for r in csv.DictReader(open(f))}
        unmapped = {s for s in subtypes if t.signal_role(s) is None}
        assert not unmapped, unmapped

    def test_validation_rejects_subtype_in_two_roles(self):
        from utils.taxonomy_loader import _validate_structural, TaxonomyValidationError
        with pytest.raises(TaxonomyValidationError, match='exactly one role'):
            _validate_structural({'version': '0.1', 'signal_roles': {'a': ['x'], 'b': ['x']}}, 'f.json', False)

    def test_validation_rejects_overlay_moving_base_subtype(self):
        from utils.taxonomy_loader import _validate_overlay_vs_base, _load_base, TaxonomyValidationError
        overlay = {'version': '0.1', 'extends': 'base', 'vertical': 'x',
                   'signal_roles': {'advocacy': ['champion_departure']}}
        with pytest.raises(TaxonomyValidationError, match='cannot move'):
            _validate_overlay_vs_base(overlay, _load_base(), 'taxonomy_x.json')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
