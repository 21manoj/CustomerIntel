"""
Power-of-1 / ROI — investment-allocation intelligence (docs/design/power-of-1-roi.md).

    roi.priorities.investment_priorities(customer_id, account_id=None)   where the next hour / dollar goes, cited
    roi.power_of_1.power_of_1(customer_id, account_id=None)              what a 1-point / 1% move is worth on the tenant's own base
    roi.measured.roi(customer_id)                                        realized $ vs exposure $, Wizard B's lift, the outcome ledger
    roi.investment.investment_cost(customer_id)                          what it costs to move a point, on the real playbook catalog
    roi.csm.csm_scorecard/team_capacity/csm_daily_actions/csm_ranking     CSM-book reporting, reusing priorities + list_interventions

Every dollar figure carries a basis (measured | derived | assumed) and its
chain; measured and assumed are never summed. Nothing here carries its own
number: config/power_of_1.json, config/economics/<vertical>.json and
config/investment/<vertical>.json.
"""
