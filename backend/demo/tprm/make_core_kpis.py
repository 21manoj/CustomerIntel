"""Synthetic core-12 KPI history for the TPRM demo tenant (Acme Risk Team, 7 vendors).

SYNTHETIC: these numbers are invented to match each vendor's evidence timeline (the notes already in
the tenant); they are not a bank's data. Writes a kpi_measurements.csv — the same file a bank uploads —
covering May-Nov 2026 for the 12 core KPIs of config/tprm_v1_kpi_catalog.json. The other 21 KPIs are
deliberately absent: the scorer leaves an unmeasured KPI out and rescales the rest.

    python demo/tprm/make_core_kpis.py > demo/tprm/kpi_measurements.csv
"""
import csv
import sys

MONTHS = ['2026-05', '2026-06', '2026-07', '2026-08', '2026-09', '2026-10', '2026-11']
CORE = ['S-KPI1', 'S-KPI2', 'O-KPI1', 'O-KPI2', 'R-KPI1', 'R-KPI2', 'C-KPI1', 'C-KPI2', 'P-KPI1', 'P-KPI4', 'G-KPI2', 'G-KPI3']


def flat(v):
    return [v] * 7


def run(start, step):
    return [start + step * i for i in range(7)]


VENDORS = {
    # Payment gateway outage in July, corrective action plan closed in August, clean review in September.
    'Meridian Merchant Services': {
        'S-KPI1': [84, 84, 82, 82, 83, 84, 85], 'S-KPI2': [0, 0, 1, 1, 0, 0, 0],
        'O-KPI1': [96, 96, 80, 80, 92, 96, 96], 'O-KPI2': [0, 0, 2, 3, 1, 0, 0],
        'R-KPI1': flat(78), 'R-KPI2': [0, 0, 1, 2, 1, 0, 0],
        'C-KPI1': [200, 170, 140, 110, 80, 415, 385], 'C-KPI2': [0, 0, 0, 1, 1, 0, 0],
        'P-KPI1': [99.6, 99.5, 96.8, 95.2, 98.6, 99.4, 99.6], 'P-KPI4': [1, 2, 6, 9, 5, 2, 1],
        'G-KPI2': [100, 100, 100, 90, 90, 100, 100], 'G-KPI3': [150, 180, 210, 240, 30, 60, 90]},
    # Staffing ramp never caught up; questionnaire late; delivery slipping every month.
    'Crestpoint Workforce Solutions': {
        'S-KPI1': [78, 78, 77, 76, 75, 74, 74], 'S-KPI2': [0, 1, 1, 2, 2, 3, 3],
        'O-KPI1': [90, 90, 90, 85, 85, 80, 80], 'O-KPI2': [0, 0, 1, 1, 1, 2, 2],
        'R-KPI1': [66, 66, 64, 62, 60, 58, 58], 'R-KPI2': [0, 0, 1, 2, 2, 3, 3],
        'C-KPI1': run(150, -30), 'C-KPI2': [0, 0, 0, 1, 2, 2, 3],
        'P-KPI1': [99.0, 98.4, 97.5, 96.5, 95.8, 95.0, 94.6], 'P-KPI4': [2, 3, 5, 7, 9, 10, 11],
        'G-KPI2': [90, 90, 80, 75, 70, 65, 60], 'G-KPI3': run(300, 30)},
    # Going quiet: missed reviews, missing monthly reports.
    'Clearwater Screening Partners': {
        'S-KPI1': [81, 81, 80, 80, 79, 79, 78], 'S-KPI2': [0, 0, 0, 1, 1, 1, 2],
        'O-KPI1': [95, 95, 95, 90, 90, 90, 85], 'O-KPI2': [0, 0, 0, 0, 0, 1, 1],
        'R-KPI1': [72, 72, 71, 70, 68, 66, 65], 'R-KPI2': [0, 0, 1, 1, 2, 2, 2],
        'C-KPI1': run(300, -30), 'C-KPI2': [0, 0, 0, 0, 1, 1, 2],
        'P-KPI1': [99.3, 99.0, 98.5, 98.0, 97.4, 97.0, 96.8], 'P-KPI4': [1, 3, 4, 5, 6, 7, 7],
        'G-KPI2': [100, 90, 80, 60, 50, 45, 40], 'G-KPI3': run(200, 30)},
    # Account manager left in July, slow claims in August, new owner introduced in September.
    'Crestline Freight Systems': {
        'S-KPI1': flat(82), 'S-KPI2': flat(0),
        'O-KPI1': flat(95), 'O-KPI2': [0, 0, 0, 1, 0, 0, 0],
        'R-KPI1': flat(74), 'R-KPI2': [0, 0, 0, 1, 1, 0, 0],
        'C-KPI1': run(330, -30), 'C-KPI2': flat(0),
        'P-KPI1': [99.4, 99.3, 98.6, 97.2, 98.4, 99.0, 99.2], 'P-KPI4': [1, 1, 4, 7, 4, 2, 2],
        'G-KPI2': [100, 100, 85, 75, 90, 95, 100], 'G-KPI3': run(120, 30)},
    # Strong vendor, scope expanded in September.
    'Beacon Hill Cyber Defense': {
        'S-KPI1': [92, 92, 93, 93, 94, 94, 94], 'S-KPI2': flat(0),
        'O-KPI1': flat(100), 'O-KPI2': flat(0),
        'R-KPI1': flat(85), 'R-KPI2': flat(0),
        'C-KPI1': run(320, -30), 'C-KPI2': flat(0),
        'P-KPI1': flat(99.8), 'P-KPI4': [0, 1, 0, 0, 1, 0, 0],
        'G-KPI2': flat(100), 'G-KPI3': run(60, 30)},
    # Steady; annual risk audit in August refreshed the due diligence.
    'Kessler Voss LLP': {
        'S-KPI1': flat(86), 'S-KPI2': flat(0),
        'O-KPI1': flat(97), 'O-KPI2': flat(0),
        'R-KPI1': flat(88), 'R-KPI2': flat(0),
        'C-KPI1': run(290, -30), 'C-KPI2': flat(0),
        'P-KPI1': flat(99.5), 'P-KPI4': flat(1),
        'G-KPI2': flat(100), 'G-KPI3': [280, 310, 340, 5, 35, 65, 95]},
    # Ownership change in July, unanswered escalation in August, data breach in September.
    'Meridian Cloud Storage': {
        'S-KPI1': [80, 80, 78, 74, 52, 48, 50], 'S-KPI2': [0, 1, 2, 3, 6, 7, 5],
        'O-KPI1': [92, 92, 90, 85, 70, 70, 72], 'O-KPI2': [0, 0, 1, 1, 3, 2, 2],
        'R-KPI1': [70, 70, 55, 50, 42, 38, 38], 'R-KPI2': [0, 0, 1, 2, 4, 5, 4],
        'C-KPI1': run(120, -30), 'C-KPI2': [0, 0, 0, 1, 2, 3, 3],
        'P-KPI1': [99.2, 99.0, 98.2, 97.0, 94.5, 95.2, 95.6], 'P-KPI4': [2, 2, 4, 6, 12, 10, 9],
        'G-KPI2': [90, 90, 80, 70, 60, 60, 65], 'G-KPI3': run(250, 30)},
}


# The source ids the vendors were registered under (accounts.external_account_id on the demo tenant).
SOURCE_IDS = {
    'Meridian Merchant Services': 'meridian-merchant-services-a80d3d',
    'Crestpoint Workforce Solutions': 'crestpoint-workforce-solutions-df971d',
    'Clearwater Screening Partners': 'clearwater-screening-partners-39e23e',
    'Crestline Freight Systems': 'crestline-freight-systems-d6995e',
    'Beacon Hill Cyber Defense': 'beacon-hill-cyber-defense-2d5508',
    'Kessler Voss LLP': 'kessler-voss-llp-e36d11',
    'Meridian Cloud Storage': 'meridian-cloud-storage-8f47f9',
}

def rows():
    for vendor, series in VENDORS.items():
        assert sorted(series) == sorted(CORE), vendor
        for code in CORE:
            assert len(series[code]) == len(MONTHS), (vendor, code)
            for month, value in zip(MONTHS, series[code]):
                yield {'source_account_id': SOURCE_IDS[vendor], 'kpi_code': code, 'measured_at': f'{month}-05', 'value': value}


if __name__ == '__main__':
    w = csv.DictWriter(sys.stdout, fieldnames=['source_account_id', 'kpi_code', 'measured_at', 'value'])
    w.writeheader()
    w.writerows(rows())
