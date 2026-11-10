"""
Signals-only journeys: an intervention sent after the account's last signal
must still land inside the journey's live months, so it becomes a phase.

Found on the TPRM pilot replay (2026-11-09): seven vendors whose last signal
was 10 Sep had their interventions sent on 9 Nov. The live-month window ended
at the last *signal*, so November was outside every month, no 'intervention'
phase was detected, the journey kept showing the pre-intervention phase as
current, and the tprm_v1 phase-transition chain never reached the
INTERVENTION nodes (11 LED_TO edges instead of the local demo's 17).

Pure functions only here; the end-to-end case through run_wizard_a lives in
tests/test_phase_transition_edges.py.
"""
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journeys.journey_builder import Episode, _live_months, detect_phases_from_evidence  # noqa: E402


def _sig(eid, dt, role, polarity):
    return Episode(episode_id=eid, date=dt, kind='signal', subtype=role, role=role,
                   polarity=polarity, source='observed', title=eid, evidence_node_ids=[1])


def _int(eid, dt):
    return Episode(episode_id=eid, date=dt, kind='intervention', subtype='incident_corrective_action',
                   role='intervention', polarity=0, source='observed', title=eid, evidence_node_ids=[2])


SIGNALS = [
    _sig('sig:1', datetime(2026, 7, 3), 'infra_incident', -1),
    _sig('sig:2', datetime(2026, 8, 12), 'escalation', -1),
    _sig('sig:3', datetime(2026, 9, 10), 'escalation', -1),
]
LATE_INTERVENTION = _int('int:9', datetime(2026, 11, 9, 2, 51))


def test_signals_only_window_runs_to_a_late_intervention():
    months = _live_months([], SIGNALS + [LATE_INTERVENTION])
    assert months == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)]


def test_late_intervention_becomes_the_current_phase_with_its_own_trigger():
    eps = SIGNALS + [LATE_INTERVENTION]
    phases = detect_phases_from_evidence(eps, _live_months([], eps))
    assert [p['name'] for p in phases] == ['deterioration', 'intervention'], phases
    assert phases[0]['trigger_episode_id'] == 'sig:1'
    # the intervention's own episode triggers the phase, so the phase-transition
    # chain gets a sig:1 -> int:9 pair to turn into a LED_TO edge
    assert phases[-1]['trigger_episode_id'] == 'int:9'
    assert phases[-1]['entered_at'] == '2026-11-01' and phases[-1]['exited_at'] is None


def test_without_interventions_the_window_still_ends_at_the_last_signal():
    assert _live_months([], SIGNALS)[-1] == date(2026, 9, 1)


def test_kpi_scored_journeys_keep_the_signal_bound():
    """Phases on a KPI-scored journey come from health, not from this window,
    and its live chapter is told from these months; leave them unchanged."""
    points = [(date(2026, 6, 1), 72.0)]
    months = _live_months(points, SIGNALS + [LATE_INTERVENTION])
    assert months == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]


def test_a_future_dated_intervention_is_capped_at_the_current_month():
    future = _int('int:10', datetime.utcnow() + timedelta(days=75))
    months = _live_months([], SIGNALS + [future])
    assert months[-1] == datetime.utcnow().date().replace(day=1)


def test_interventions_alone_do_not_open_a_window():
    """No signals means no evidence journey; an intervention by itself (which
    the governance layer only writes after a signal-triggered proposal) must
    not start one."""
    assert _live_months([], [LATE_INTERVENTION]) == []
