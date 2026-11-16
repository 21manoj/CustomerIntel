"""
Per-vertical display labels and Ask AI question sets — both are config a vertical ships,
read through platform functions that name no vertical.

  labels      utils.vertical_registry.labels(v): platform defaults + the catalog's "labels" block
  questions   ask_ai.settings.curated_questions(v): platform set, with each role the vertical's
              own config/ask_ai_questions/<v>.json lists replaced
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ask_ai import settings as ask_settings
from utils import vertical_registry as vr


@pytest.fixture(autouse=True)
def _fresh_caches():
    ask_settings.curated_questions.cache_clear()
    yield
    ask_settings.curated_questions.cache_clear()
    vr._labels_cache.clear()


# ── labels ──

def test_a_vertical_that_declares_nothing_gets_the_platform_labels():
    assert vr.labels('saas_premium') == vr.LABEL_DEFAULTS


def test_every_vertical_resolves_every_label_to_non_empty_text():
    for v in sorted(vr.SUPPORTED_VERTICALS):
        got = vr.labels(v)
        assert set(got) == set(vr.LABEL_DEFAULTS), v
        assert all(isinstance(t, str) and t.strip() for t in got.values()), v


def test_a_vertical_can_rename_its_accounts_and_money_words():
    got = vr.labels('tprm_v1')
    assert got['account'] == 'Vendor' and got['accounts'] == 'Vendors'
    assert 'revenue' not in got['revenue'].lower() and 'ARR' not in got['recurring_revenue']


@pytest.mark.parametrize('block, needle', [
    ({'acount': 'Vendor'}, 'unknown labels'),
    ({'account': ''}, 'non-empty text'),
    ({'account': 7}, 'non-empty text'),
    (['Vendor'], 'must be an object'),
])
def test_a_bad_labels_block_is_refused_not_ignored(tmp_path, monkeypatch, block, needle):
    path = tmp_path / 'x_kpi_catalog.json'
    path.write_text(json.dumps({'labels': block}))
    monkeypatch.setattr(vr, '_find_json_catalog_path', lambda v: str(path))
    vr._labels_cache.clear()
    with pytest.raises(ValueError, match=needle):
        vr.labels('made_up_vertical')


# ── questions ──

def test_no_vertical_and_a_vertical_without_its_own_file_get_the_platform_set():
    base = ask_settings.curated_questions()
    assert set(base) >= {'cfo', 'cro', 'admin'}
    assert ask_settings.curated_questions('saas_premium') == base


def test_a_vertical_file_replaces_only_the_roles_it_lists():
    base = ask_settings.curated_questions()
    own = ask_settings.curated_questions('tprm_v1')
    assert set(own) == set(base)
    assert own['admin'] == base['admin']                                  # not listed -> the platform's
    assert own['cfo'] != base['cfo'] and own['cro'] != base['cro']
    assert all(q['id'].startswith('tprm-') for q in own['cfo'] + own['cro'])


def test_an_exposure_vertical_question_set_never_asks_about_revenue():
    """Its accounts are not customers: a revenue / ARR question has nothing to ground on."""
    for v in sorted(vr.SUPPORTED_VERTICALS):
        if vr.capability(v, 'money_basis') != 'exposure':
            continue
        qs = ask_settings.curated_questions(v)
        for role in ('cfo', 'cro'):
            for q in qs[role]:
                text = q['text'].lower()
                assert 'revenue' not in text and ' arr' not in f' {text}', (v, q['id'])


def test_every_question_in_every_vertical_set_is_well_formed_with_unique_ids():
    for v in [None] + sorted(vr.SUPPORTED_VERTICALS):
        qs = ask_settings.curated_questions(v)
        ids = [q['id'] for role in qs.values() for q in role]
        assert len(ids) == len(set(ids)), v
        for role, items in qs.items():
            assert items, (v, role)
            assert all(q['scope'] in ('portfolio', 'account') and q['text'].endswith('?') for q in items), (v, role)


@pytest.mark.parametrize('content, needle', [
    ({'cfo': [{'id': 'a', 'scope': 'tenant', 'text': 'x?'}]}, 'needs id, text and scope'),
    ({'cfo': {'id': 'a'}}, 'must be a list'),
    ({'ceo': [{'id': 'a', 'scope': 'portfolio', 'text': 'x?'}]}, 'unknown roles'),
])
def test_a_malformed_vertical_question_file_is_refused(tmp_path, monkeypatch, content, needle):
    (tmp_path / 'made_up.json').write_text(json.dumps(content))
    monkeypatch.setattr(ask_settings, 'QUESTIONS_DIR', tmp_path)
    with pytest.raises(ValueError, match=needle):
        ask_settings.curated_questions('made_up')
