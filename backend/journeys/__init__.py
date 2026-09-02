"""
Journeys — Wizard A v2 (Tier 2A-5, 2026-09-02).

Turns an account's rows (HealthScore, KPIMeasurement, the context graph)
into an evidence-bearing journey: episodes, phases with transition
triggers, an evidence-cited arc hypothesis, the leading-vs-trailing
divergence series that is the early-warning primitive, counterfactual
hooks, an expected-path overlay, and the feature vector Wizards B and D
share. Design and rationale: docs/design/wizard-a-assessment.md.

    journey_builder.build_journey(account)  -> journey dict (schema v3)
    arc_classifier.classify(journey_ctx)    -> arc hypothesis with evidence
    features.compute(...)                   -> shared feature vector
    wizard_a.run_wizard_a(customer_id)      -> persists JourneyData, Account
                                               arc columns, HealthScore
                                               leading columns
"""
