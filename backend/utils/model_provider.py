"""
The one place the platform talks to a model host.

Every model call site (ask_ai/answer.py, signal_engine/enrichment.py, signal_engine/external_research.py)
goes through this module, so the host is a config choice (config/model_provider.json, or env
MODEL_PROVIDER) and not something three files each decide. The default is Anthropic and the call it
makes is the same one those files made themselves: same client, same arguments, same response read.

What a call site may ask:
  available()            is the active provider usable (its key is set)? If not, call sites use their stub.
  supports(feature)      'tool_call' (one forced structured answer) or 'web_search' (host-run search).
  tool_call(...)         one forced tool call; returns ToolResult(payload, tokens_in, tokens_out).
  sdk_client()           the Anthropic SDK client, for the host-specific call external research makes.

Metering stays with the call sites (utils.llm_budget_controller.record_usage): each already has its own
rule for what to record on failure, and this change must not alter that.

Adding a host: add an entry under "providers" with a "kind" this module knows. Two kinds exist:
'anthropic' (the SDK) and 'openai_compatible' (plain HTTPS to <base_url>/chat/completions, which is what
most hosts of open-source models speak). A new kind is one adapter function here and nothing elsewhere.
"""
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'model_provider.json'
PROVIDER_ENV = 'MODEL_PROVIDER'
KINDS = ('anthropic', 'openai_compatible')
FEATURES = ('tool_call', 'web_search')


class ModelProviderError(RuntimeError):
    """The provider config is wrong, or the active provider cannot do what was asked."""


class NoToolCall(ValueError):
    """The host answered, and so the tokens were spent, but not through the tool. Carries the token
    counts so the call site can still meter the call before it raises."""

    def __init__(self, message: str, tokens_in: int = 0, tokens_out: int = 0):
        super().__init__(message)
        self.tokens_in, self.tokens_out = tokens_in, tokens_out


@dataclass
class ToolResult:
    payload: dict
    tokens_in: int
    tokens_out: int


@lru_cache(maxsize=1)
def load() -> dict:
    with open(CONFIG_PATH, encoding='utf-8') as f:
        cfg = json.load(f)
    providers = cfg.get('providers')
    if not isinstance(providers, dict) or not providers:
        raise ModelProviderError('model_provider.json: "providers" must list at least one provider')
    for name, p in providers.items():
        if p.get('kind') not in KINDS:
            raise ModelProviderError(f'model_provider.json: provider {name!r} has kind {p.get("kind")!r}; known kinds: {list(KINDS)}')
        if not p.get('api_key_env'):
            raise ModelProviderError(f'model_provider.json: provider {name!r} needs api_key_env (the NAME of the env var holding its key)')
        unknown = sorted(set(p.get('features') or []) - set(FEATURES))
        if unknown:
            raise ModelProviderError(f'model_provider.json: provider {name!r} lists unknown features {unknown}; known: {list(FEATURES)}')
        if p['kind'] == 'openai_compatible' and not p.get('base_url_env'):
            raise ModelProviderError(f'model_provider.json: provider {name!r} needs base_url_env (the NAME of the env var holding its API root)')
    if cfg.get('provider') not in providers:
        raise ModelProviderError(f'model_provider.json: provider {cfg.get("provider")!r} is not one of {sorted(providers)}')
    return cfg


def reload() -> dict:
    load.cache_clear()
    return load()


def active_name() -> str:
    cfg = load()
    name = os.environ.get(PROVIDER_ENV) or cfg['provider']
    if name not in cfg['providers']:
        raise ModelProviderError(f'{PROVIDER_ENV}={name!r} is not one of {sorted(cfg["providers"])}')
    return name


def active() -> dict:
    return load()['providers'][active_name()]


def _key(p: dict) -> Optional[str]:
    return os.environ.get(p['api_key_env']) or None


def _base_url(p: dict) -> Optional[str]:
    env = p.get('base_url_env')
    return (os.environ.get(env) or None) if env else None


def available() -> bool:
    """True when the active provider can be called. False sends call sites to their no-model path."""
    p = active()
    if not _key(p):
        return False
    return p['kind'] != 'openai_compatible' or bool(_base_url(p))


def supports(feature: str) -> bool:
    return feature in (active().get('features') or [])


def model_id(requested: str) -> str:
    """The host's name for the model a call site asked for (model_map), else the id unchanged."""
    return (active().get('model_map') or {}).get(requested, requested)


def describe() -> dict:
    """For status pages and logs. Never includes a key."""
    p = active()
    return {'provider': active_name(), 'kind': p['kind'], 'available': available(),
            'features': list(p.get('features') or []), 'key_env': p['api_key_env'], 'key_set': bool(_key(p))}


# ── anthropic ───────────────────────────────────────────────────────────

def sdk_client():
    """The Anthropic SDK client. Only for the 'anthropic' kind; a call site that needs a host-specific
    call (web search) checks supports() first and says plainly when the provider cannot do it."""
    p = active()
    if p['kind'] != 'anthropic':
        raise ModelProviderError(f'provider {active_name()!r} has no Anthropic SDK client')
    import anthropic
    base = _base_url(p)
    if base:
        return anthropic.Anthropic(api_key=_key(p), base_url=base)
    return anthropic.Anthropic(api_key=_key(p))


def _anthropic_tool_call(model, max_tokens, system, user, tool) -> ToolResult:
    response = sdk_client().messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{'role': 'user', 'content': user}],
        tools=[tool], tool_choice={'type': 'tool', 'name': tool['name']})
    payload = next((b.input for b in response.content if getattr(b, 'type', '') == 'tool_use'), None)
    if payload is None:
        raise NoToolCall('model returned no tool_use block', response.usage.input_tokens, response.usage.output_tokens)
    return ToolResult(payload, response.usage.input_tokens, response.usage.output_tokens)


# ── openai-compatible hosts ─────────────────────────────────────────────

def _http_post_json(url: str, headers: dict, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), method='POST',
                                 headers={'Content-Type': 'application/json', **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 — the URL is operator config
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:300]
        raise RuntimeError(f'model host returned HTTP {e.code}: {detail}') from None


def _openai_compatible_tool_call(model, max_tokens, system, user, tool) -> ToolResult:
    p = active()
    body = {
        'model': model, 'max_tokens': max_tokens,
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'tools': [{'type': 'function', 'function': {'name': tool['name'], 'description': tool.get('description', ''),
                                                    'parameters': tool['input_schema']}}],
        'tool_choice': {'type': 'function', 'function': {'name': tool['name']}},
    }
    data = _http_post_json(_base_url(p).rstrip('/') + '/chat/completions',
                           {'Authorization': f'Bearer {_key(p)}'}, body, float(p.get('timeout_seconds') or 120))
    usage = data.get('usage') or {}
    t_in, t_out = int(usage.get('prompt_tokens') or 0), int(usage.get('completion_tokens') or 0)
    calls = ((data.get('choices') or [{}])[0].get('message') or {}).get('tool_calls') or []
    call = next((c for c in calls if (c.get('function') or {}).get('name') == tool['name']), None)
    if call is None:
        raise NoToolCall('model returned no tool call', t_in, t_out)
    args = call['function'].get('arguments')
    try:
        payload = json.loads(args) if isinstance(args, str) else args
    except json.JSONDecodeError:
        raise NoToolCall('model returned tool arguments that are not valid JSON', t_in, t_out) from None
    if not isinstance(payload, dict):
        raise NoToolCall('model returned tool arguments that are not an object', t_in, t_out)
    return ToolResult(payload, t_in, t_out)


_ADAPTERS = {'anthropic': _anthropic_tool_call, 'openai_compatible': _openai_compatible_tool_call}


def tool_call(*, model: str, max_tokens: int, system: str, user: str, tool: dict) -> ToolResult:
    """One forced call of `tool` (Anthropic tool shape: name, description, input_schema). The model
    must answer through the tool; the tool's input comes back as ToolResult.payload."""
    if not supports('tool_call'):
        raise ModelProviderError(f'provider {active_name()!r} cannot make structured tool calls')
    return _ADAPTERS[active()['kind']](model_id(model), max_tokens, system, user, tool)
