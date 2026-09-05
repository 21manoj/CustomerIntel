"""
Adapters around the playbook contract (docs/design/adapters.md):

    receiver/        the reference webhook receiver (python -m adapters.receiver)
    n8n/             an importable n8n workflow doing the same job
    slack_notify.py  the optional Slack post for notify-class interventions
    sources/         inbound source adapters → the communications lane (Gainsight Timeline first)
    settings.py      config/adapters.json — every number the adapters use
"""
