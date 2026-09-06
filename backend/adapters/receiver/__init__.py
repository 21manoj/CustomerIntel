"""
The reference webhook receiver — the engine a partner with no n8n can run today.

    python -m adapters.receiver --port 8210 --secret … --platform-url … --key …   (env: CI_RECEIVER_* / CI_PLATFORM_*)

    from adapters.receiver import ReceiverConfig, Receiver, create_app
    app = create_app(ReceiverConfig(secret=…, platform_url=…, platform_key=…))

Verifies X-CI-Signature over '<X-CI-Timestamp>.<body>' inside the configured tolerance, is
idempotent per intervention_id (a replay is acknowledged, not re-processed), logs every event to
a JSONL file, and calls the platform back: `started` right after acknowledging, `done` after the
configured delay (policy auto_done) or never (policy manual).
"""
from adapters.receiver.app import ReceiverConfig, Receiver, create_app   # noqa: F401
