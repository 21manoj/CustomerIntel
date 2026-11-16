"""
The model provider layer (utils/model_provider.py, config/model_provider.json). No network, no key spend.
  * the default provider is Anthropic and the call it makes is the one the call sites used to make
  * no key -> not available -> call sites take their no-model path
  * the three call sites go through this module and none builds a client itself
  * switching provider is config/env only: an OpenAI-style host gets the same tool as a function call,
    and its answer comes back in the same shape; model ids are renamed by model_map
  * a host that cannot search the web makes external research refuse in plain words
  * a host that answers without the tool still has its tokens reported for metering
  * a bad config is refused with the reason; a key never appears in describe()
"""
import ast
import json
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from utils import model_provider as mp                     # noqa: E402

TOOL = {'name': 'record_answer', 'description': 'd', 'input_schema': {'type': 'object', 'properties': {'x': {'type': 'string'}}}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (mp.PROVIDER_ENV, 'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'MODEL_API_KEY', 'MODEL_BASE_URL'):
        monkeypatch.delenv(k, raising=False)
    mp.reload()
    yield
    mp.reload()


def _fake_anthropic(monkeypatch, content):
    seen = {}

    class _Usage:
        input_tokens, output_tokens = 11, 7

    class _Resp:
        usage = _Usage()

    _Resp.content = content

    class _Messages:
        def create(self, **kw):
            seen['create'] = kw
            return _Resp()

    class _Client:
        def __init__(self, **kw):
            seen['init'] = kw
            self.messages = _Messages()

    import anthropic
    monkeypatch.setattr(anthropic, 'Anthropic', _Client)
    return seen


def test_default_is_anthropic_and_needs_its_key(monkeypatch):
    assert mp.active_name() == 'anthropic' and mp.active()['kind'] == 'anthropic'
    assert mp.available() is False
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k-secret-value')
    assert mp.available() is True
    assert mp.supports('tool_call') and mp.supports('web_search')
    d = mp.describe()
    assert d['key_set'] is True and 'k-secret-value' not in json.dumps(d)


def test_anthropic_tool_call_is_the_same_call_as_before(monkeypatch):
    class _Block:
        type, input = 'tool_use', {'x': 'y'}

    seen = _fake_anthropic(monkeypatch, [_Block()])
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')
    out = mp.tool_call(model='claude-test', max_tokens=99, system='S', user='U', tool=TOOL)
    assert seen['init'] == {'api_key': 'k'}
    assert seen['create'] == {'model': 'claude-test', 'max_tokens': 99, 'system': 'S',
                              'messages': [{'role': 'user', 'content': 'U'}], 'tools': [TOOL],
                              'tool_choice': {'type': 'tool', 'name': 'record_answer'}}
    assert (out.payload, out.tokens_in, out.tokens_out) == ({'x': 'y'}, 11, 7)


def test_an_answer_without_the_tool_still_reports_its_tokens(monkeypatch):
    _fake_anthropic(monkeypatch, [])
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')
    with pytest.raises(mp.NoToolCall) as e:
        mp.tool_call(model='m', max_tokens=1, system='S', user='U', tool=TOOL)
    assert (e.value.tokens_in, e.value.tokens_out) == (11, 7) and isinstance(e.value, ValueError)


def test_switching_to_an_openai_style_host_is_env_only(monkeypatch):
    monkeypatch.setenv(mp.PROVIDER_ENV, 'hosted_open_model')
    assert mp.available() is False                                   # no key, no base url
    monkeypatch.setenv('MODEL_API_KEY', 'hk')
    assert mp.available() is False                                   # still no base url
    monkeypatch.setenv('MODEL_BASE_URL', 'https://host.example/v1/')
    assert mp.available() is True and mp.supports('tool_call') and not mp.supports('web_search')
    sent = {}

    def fake_post(url, headers, body, timeout):
        sent.update(url=url, headers=headers, body=body, timeout=timeout)
        return {'choices': [{'message': {'tool_calls': [{'function': {'name': 'record_answer', 'arguments': '{"x": "z"}'}}]}}],
                'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}

    monkeypatch.setattr(mp, '_http_post_json', fake_post)
    monkeypatch.setitem(mp.active()['model_map'], 'claude-test', 'open-model-70b')
    out = mp.tool_call(model='claude-test', max_tokens=50, system='S', user='U', tool=TOOL)
    assert sent['url'] == 'https://host.example/v1/chat/completions'
    assert sent['headers'] == {'Authorization': 'Bearer hk'}
    assert sent['body']['model'] == 'open-model-70b' and sent['body']['max_tokens'] == 50
    assert sent['body']['messages'] == [{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'U'}]
    assert sent['body']['tools'] == [{'type': 'function', 'function': {'name': 'record_answer', 'description': 'd',
                                                                       'parameters': TOOL['input_schema']}}]
    assert sent['body']['tool_choice'] == {'type': 'function', 'function': {'name': 'record_answer'}}
    assert (out.payload, out.tokens_in, out.tokens_out) == ({'x': 'z'}, 5, 3)

    monkeypatch.setattr(mp, '_http_post_json', lambda *a: {'choices': [{'message': {'content': 'prose'}}],
                                                           'usage': {'prompt_tokens': 4, 'completion_tokens': 2}})
    with pytest.raises(mp.NoToolCall) as e:
        mp.tool_call(model='m', max_tokens=1, system='S', user='U', tool=TOOL)
    assert (e.value.tokens_in, e.value.tokens_out) == (4, 2)
    with pytest.raises(mp.ModelProviderError):
        mp.sdk_client()


def test_external_research_refuses_plainly_on_a_host_without_web_search(monkeypatch):
    monkeypatch.setenv(mp.PROVIDER_ENV, 'hosted_open_model')
    monkeypatch.setenv('MODEL_API_KEY', 'hk')
    monkeypatch.setenv('MODEL_BASE_URL', 'https://host.example/v1')
    from signal_engine.external_research import research_company
    with pytest.raises(RuntimeError, match='search the web'):
        research_company('Some Company', customer_id=1)


def test_unknown_provider_and_bad_config_are_refused(monkeypatch, tmp_path):
    monkeypatch.setenv(mp.PROVIDER_ENV, 'nope')
    with pytest.raises(mp.ModelProviderError, match='nope'):
        mp.active()
    monkeypatch.delenv(mp.PROVIDER_ENV)
    good = json.loads(mp.CONFIG_PATH.read_text())
    for mutate, needle in [
        (lambda c: c.__setitem__('provider', 'missing'), 'is not one of'),
        (lambda c: c['providers']['anthropic'].__setitem__('kind', 'magic'), 'known kinds'),
        (lambda c: c['providers']['anthropic'].pop('api_key_env'), 'api_key_env'),
        (lambda c: c['providers']['anthropic'].__setitem__('features', ['telepathy']), 'unknown features'),
        (lambda c: c['providers']['hosted_open_model'].pop('base_url_env'), 'base_url_env'),
    ]:
        cfg = json.loads(json.dumps(good))
        mutate(cfg)
        path = tmp_path / 'model_provider.json'
        path.write_text(json.dumps(cfg))
        monkeypatch.setattr(mp, 'CONFIG_PATH', path)
        with pytest.raises(mp.ModelProviderError, match=needle):
            mp.reload()
    monkeypatch.undo()


def test_no_key_is_stored_in_the_config_file():
    text = mp.CONFIG_PATH.read_text()
    assert 'sk-' not in text
    for p in json.loads(text)['providers'].values():
        assert set(p) <= {'_doc', 'kind', 'api_key_env', 'base_url_env', 'timeout_seconds', 'features', 'model_map'}


def test_no_call_site_builds_a_model_client_itself():
    """Platform code outside the provider module must not construct an SDK client or read a model key."""
    skip = {'tests', 'scripts', 'evals', 'migrations', 'config', '.venv', 'venv', 'node_modules', 'demo'}
    offenders = []
    for path in BACKEND.rglob('*.py'):
        rel = path.relative_to(BACKEND)
        # feature_toggles.py: an old feature flag's metadata names the env var it needs; it calls nothing.
        if rel.parts[0] in skip or rel in (Path('utils/model_provider.py'), Path('feature_toggles.py')):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                if any(n.split('.')[0] in ('anthropic', 'openai') for n in names):
                    offenders.append(f'{rel}:{node.lineno} imports a model SDK')
            if isinstance(node, ast.Constant) and node.value == 'ANTHROPIC_API_KEY':
                offenders.append(f'{rel}:{node.lineno} reads the model key by name')
    assert not offenders, '\n'.join(offenders)
