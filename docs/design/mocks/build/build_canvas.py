"""Build the static Journey Canvas mock for Zenith Data Corp from the real
journey v3 JSON (zenith_journey.json, exported from the new build) + the
customer-415 fixture (for outcome→signal links). Writes the HTML with the
data embedded, one directory up.

    python build_canvas.py "#eda100" "#c98500"     # leading-line hex, light / dark
"""
import csv, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE.parents[3] / 'backend' / 'tests' / 'fixtures' / 'customer415_dc2_s'
LEADING_LIGHT = sys.argv[1] if len(sys.argv) > 1 else '#eda100'
LEADING_DARK = sys.argv[2] if len(sys.argv) > 2 else '#c98500'
VARIANT = sys.argv[3] if len(sys.argv) > 3 else 'health'   # 'health' (score as spine) | 'signals' (evidence as spine)
TEMPLATE = {'health': 'canvas_template.html', 'signals': 'canvas_signals_first_template.html'}[VARIANT]
OUT = HERE.parent / ('journey-canvas-zenith.html' if VARIANT == 'health' else 'journey-canvas-zenith-signals-first.html')

j = json.load(open(HERE / 'zenith_journey.json'))

# outcome → linked signal (the fixture keeps the live account id 3734; match on title)
sig_rows = {r['signal_ref']: r for r in csv.DictReader(open(FIX / 'enhanced_qualitative_signals.csv'))
            if r['source_account_id'] == '3734'}
out_rows = [r for r in csv.DictReader(open(FIX / 'outcomes.csv')) if r['source_account_id'] == '3734']
links = []
for o in out_rows:
    s = sig_rows.get(o['linked_signal_id'])
    if s:
        links.append({'outcome_title': o['title'], 'outcome_date': o['outcome_date'],
                      'signal_date': s['signal_date'], 'signal_content': s['content'][:60]})

payload = {
    'account': j['_account'], 'as_of': j['as_of'], 'arc': j['arc'], 'state': j['state'],
    'current_phase': j['current_phase'], 'phases': j['phases'], 'episodes': j['episodes'],
    'series': j['leading_vs_trailing']['series'],
    'lvt': {k: v for k, v in j['leading_vs_trailing'].items() if k != 'series'},
    'hooks': j['counterfactual_hooks'], 'expected_path': j['expected_path'], 'features': j['features'],
    'summary': j['summary'], 'stakeholders': j['_stakeholders'], 'wizard_b': j['_wizard_b'], 'links': links,
}
html = (HERE / TEMPLATE).read_text()
html = (html.replace('__DATA__', json.dumps(payload, default=str))
            .replace('__LEADING_LIGHT__', LEADING_LIGHT).replace('__LEADING_DARK__', LEADING_DARK))
OUT.write_text(html)
print(f'wrote {OUT} ({len(html)//1024} KB), links={len(links)}, episodes={len(payload["episodes"])}')
