"""
Playbook governance layer — the minimal version (docs/design/playbook-governance-layer.md).

    definitions   config/playbooks/<vertical>.json loaded + validated against the taxonomy; tenant overlay
    governance    evaluate → propose; approve → send + INTERVENTION node; report → close (+ outcome); list
    webhook       one signed JSON payload per approval, one retry, the delivery record
    http          /api/interventions*, /api/playbooks

The platform owns the record; the workflow runs elsewhere and calls back.
"""
