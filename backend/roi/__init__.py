"""
Power-of-1 / ROI — investment-allocation intelligence (docs/design/power-of-1-roi.md).

    roi.priorities.investment_priorities(customer_id, account_id=None)   where the next hour / dollar goes, cited
    roi.power_of_1.power_of_1(customer_id, account_id=None)              what a 1-point / 1% move is worth on the tenant's own base
    roi.measured.roi(customer_id)                                        realized $ vs exposure $, Wizard B's lift, the outcome ledger

Every dollar figure carries a basis (measured | derived | assumed) and its
chain; measured and assumed are never summed. Nothing here carries its own
number: config/power_of_1.json and config/economics/<vertical>.json.
"""
