"""
Keep credentials out of the logs.

With MCP_ALLOW_QUERY_KEY=true a key may arrive as ?api_key=… (single-URL
connectors such as claude.ai). uvicorn's access line prints the full path
with its query string, so the key was written to the container log on
every request. RedactSecretsFilter rewrites log records before any handler
formats them:

  * the value of a credential-bearing query parameter (api_key, token, key,
    access_token, setup_token, password, secret) -> REDACTED
  * anything shaped like a customer key (csp_read_/csp_write_/csp_admin_…),
    wherever it appears -> its scope prefix + REDACTED
  * the server key (MCP_SERVER_API_KEY), verbatim, wherever it appears

It is attached to handlers, not loggers: a logger-level filter does not see
records propagated from child loggers.
"""
from __future__ import annotations

import copy
import logging
import os
import re

REDACTED = 'REDACTED'
_PARAMS = r'api_key|apikey|token|access_token|setup_token|key|password|secret'
_QUERY_RE = re.compile(r'(?i)(?<![A-Za-z0-9_])(' + _PARAMS + r')=([^&\s"\'#]+)')
_CUSTOMER_KEY_RE = re.compile(r'\b(csp_(?:read|write|admin)_)[A-Za-z0-9_\-]{6,}')
_BEARER_RE = re.compile(r'(?i)\b(Bearer\s+)[A-Za-z0-9_\-\.=]{8,}')


def redact(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    out = _QUERY_RE.sub(lambda m: f'{m.group(1)}={REDACTED}', text)
    out = _CUSTOMER_KEY_RE.sub(lambda m: m.group(1) + REDACTED, out)
    out = _BEARER_RE.sub(lambda m: m.group(1) + REDACTED, out)
    server_key = os.environ.get('MCP_SERVER_API_KEY') or ''
    if len(server_key) >= 8 and server_key in out:
        out = out.replace(server_key, REDACTED)
    return out


class RedactSecretsFilter(logging.Filter):
    """Never drops a record; never raises."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            args = record.args
            if not args:
                if isinstance(record.msg, str):
                    record.msg = redact(record.msg)
                return True
            # Redact each argument in place first: uvicorn's access formatter reads the
            # argument tuple itself, so its shape has to survive.
            if isinstance(args, tuple):
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in args)
            elif isinstance(args, dict):
                record.args = {k: (redact(v) if isinstance(v, str) else v) for k, v in args.items()}
            # A secret can also straddle the template and an argument ('token=%s'). If the
            # rendered line still changes under redaction, log the redacted rendering.
            rendered = record.getMessage()
            clean = redact(rendered)
            if clean != rendered:
                record.msg, record.args = clean, None
        except Exception:  # pragma: no cover — logging must not fail
            pass
        return True


def install_on_root() -> None:
    """Attach the filter to every handler on the root logger (idempotent)."""
    for h in logging.getLogger().handlers:
        if not any(isinstance(f, RedactSecretsFilter) for f in h.filters):
            h.addFilter(RedactSecretsFilter())


def uvicorn_log_config() -> dict:
    """uvicorn's default logging config with the filter on each of its handlers.
    uvicorn installs its handlers itself (dictConfig) when the server starts, so a
    filter added beforehand would be discarded; it has to be part of the config."""
    from uvicorn.config import LOGGING_CONFIG
    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault('filters', {})['redact_secrets'] = {'()': 'utils.log_redaction.RedactSecretsFilter'}
    for handler in cfg.get('handlers', {}).values():
        handler.setdefault('filters', [])
        if 'redact_secrets' not in handler['filters']:
            handler['filters'].append('redact_secrets')
    return cfg
