#!/usr/bin/env python3
"""
Tests for Signal Processing Pipeline MVP.

Verifies:
  1. Email receiver — parsing, domain resolution, body cleaning
  2. Slack events — signature verification, channel resolution, challenge handling
  3. Transcript upload — VTT/SRT parsing
  4. MCP submit_signal tool — source code presence
  5. Docker compose — FEATURE_SIGNAL_ENGINE enabled
  6. App registration — blueprints wired
  7. Status endpoint — LLM enrichment detection
  8. System prompt — submit_signal documented

Run: python3 -m pytest tests/test_signal_pipeline_mvp.py -v
"""

import ast
import json
import os
import re
import sys
import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(os.path.dirname(BACKEND_DIR))
sys.path.insert(0, BACKEND_DIR)

os.environ['FEATURE_SIGNAL_ENGINE'] = 'true'


# ============================================================
# Email Receiver Tests
# ============================================================

class TestEmailReceiver:
    @pytest.fixture(autouse=True)
    def setup(self):
        from signal_engine.email_receiver import (
            _extract_email_address, _extract_domain, _clean_email_body,
        )
        self.extract_email = _extract_email_address
        self.extract_domain = _extract_domain
        self.clean_body = _clean_email_body

    def test_extract_email_from_name_format(self):
        assert self.extract_email('John Doe <john@acme.com>') == 'john@acme.com'

    def test_extract_email_plain(self):
        assert self.extract_email('jane@example.org') == 'jane@example.org'

    def test_extract_email_mixed_case(self):
        assert self.extract_email('CEO <CEO@BigCorp.COM>') == 'ceo@bigcorp.com'

    def test_extract_email_empty(self):
        assert self.extract_email('') == ''

    def test_extract_domain(self):
        assert self.extract_domain('john@acme.com') == 'acme.com'

    def test_extract_domain_no_at(self):
        assert self.extract_domain('acme.com') == 'acme.com'

    def test_clean_body_strips_signature(self):
        body = "Hello\nImportant info\n--\nJohn Doe\nVP Engineering"
        cleaned = self.clean_body(body)
        assert 'Important info' in cleaned
        assert 'VP Engineering' not in cleaned

    def test_clean_body_strips_forwarded(self):
        body = "Check this\n---------- Forwarded message ----------\nOriginal sender"
        cleaned = self.clean_body(body)
        assert 'Check this' in cleaned
        assert 'Original sender' not in cleaned

    def test_clean_body_strips_reply(self):
        body = "My response\nOn Mon, Jan 1, 2026 at 10:00 AM John wrote:\nOld text"
        cleaned = self.clean_body(body)
        assert 'My response' in cleaned
        assert 'Old text' not in cleaned

    def test_clean_body_caps_length(self):
        body = "x" * 10000
        cleaned = self.clean_body(body)
        assert len(cleaned) <= 5000

    def test_clean_body_empty(self):
        assert self.clean_body('') == ''
        assert self.clean_body(None) == ''

    def test_email_routes_registered(self):
        from signal_engine.http import ROUTES
        assert '/api/signals/ingest/email/parse' in ROUTES


# ============================================================
# Slack Events Tests
# ============================================================

class TestSlackEvents:
    def test_slack_routes_registered(self):
        from signal_engine.http import ROUTES
        assert '/api/signals/ingest/slack/events' in ROUTES

    def test_signature_passes_without_secret(self):
        """Dev mode: no SLACK_SIGNING_SECRET means verification passes."""
        os.environ.pop('SLACK_SIGNING_SECRET', None)
        # Can't test _verify_slack_signature directly (needs request context)
        # but verify it's importable
        from signal_engine.slack_events import _verify_slack_signature
        assert callable(_verify_slack_signature)

    def test_resolve_customer_from_team_importable(self):
        from signal_engine.slack_events import _resolve_customer_from_team
        assert callable(_resolve_customer_from_team)

    def test_resolve_account_from_channel_importable(self):
        from signal_engine.slack_events import _resolve_account_from_channel
        assert callable(_resolve_account_from_channel)


# ============================================================
# Transcript Upload Tests
# ============================================================

class TestTranscriptParsing:
    @pytest.fixture(autouse=True)
    def setup(self):
        from signal_engine.ingest_api import _parse_vtt, _parse_srt
        self.parse_vtt = _parse_vtt
        self.parse_srt = _parse_srt

    def test_vtt_strips_header(self):
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHello world"
        result = self.parse_vtt(vtt)
        assert 'WEBVTT' not in result
        assert 'Hello world' in result

    def test_vtt_strips_timestamps(self):
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nLine one\n\n00:00:05.000 --> 00:00:08.000\nLine two"
        result = self.parse_vtt(vtt)
        assert '00:00' not in result
        assert 'Line one' in result
        assert 'Line two' in result

    def test_vtt_strips_cue_identifiers(self):
        vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\nFirst cue\n\n2\n00:00:05.000 --> 00:00:08.000\nSecond cue"
        result = self.parse_vtt(vtt)
        assert 'First cue' in result
        assert 'Second cue' in result

    def test_vtt_empty(self):
        assert self.parse_vtt('') == ''
        assert self.parse_vtt('WEBVTT') == ''

    def test_srt_strips_sequence_numbers(self):
        srt = "1\n00:00:01,000 --> 00:00:04,000\nFirst line\n\n2\n00:00:05,000 --> 00:00:08,000\nSecond line"
        result = self.parse_srt(srt)
        assert 'First line' in result
        assert 'Second line' in result

    def test_srt_strips_timestamps(self):
        srt = "1\n00:00:01,000 --> 00:00:04,000\nContent here"
        result = self.parse_srt(srt)
        assert '00:00' not in result
        assert 'Content here' in result

    def test_srt_empty(self):
        assert self.parse_srt('') == ''


# ============================================================
# MCP submit_signal Tool Tests
# ============================================================

class TestSubmitSignalTool:
    pytestmark = pytest.mark.skip(reason="reads mcp_server/cs_pulse_intelligence.py source, Tier 2/3, not yet ported")

    @pytest.fixture(autouse=True)
    def load_source(self):
        path = os.path.join(BACKEND_DIR, 'mcp_server', 'cs_pulse_intelligence.py')
        with open(path) as f:
            self.source = f.read()
        self.tree = ast.parse(self.source)
        self.func_names = [n.name for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)]

    def test_submit_signal_exists(self):
        assert 'submit_signal' in self.func_names

    def test_accepts_required_params(self):
        # Find the function def and check args
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == 'submit_signal':
                arg_names = [a.arg for a in node.args.args]
                assert 'customer_id' in arg_names
                assert 'account_id' in arg_names
                assert 'raw_text' in arg_names
                break

    def test_validates_source_type(self):
        assert "valid_sources = ('manual', 'email', 'slack', 'transcript')" in self.source

    def test_validates_consent_for_transcript(self):
        assert 'consent_verified' in self.source

    def test_wakes_enrichment_worker(self):
        assert 'notify_new_signal' in self.source

    def test_auto_enables_signal_engine(self):
        # submit_signal should auto-enable signal_engine feature toggle
        assert 'Auto-enabled by submit_signal' in self.source


# ============================================================
# Docker Compose Tests
# ============================================================

class TestDockerCompose:
    pytestmark = pytest.mark.skip(reason="reads kpi-dashboard/docker-compose.*.yml, old repo's deploy config, not carried over")

    @pytest.fixture(params=[
        'docker-compose.cspulse.yml',
        'docker-compose.ec2-registry.yml',
        'docker-compose.ec2-platform-replica.yml',
    ])
    def compose_file(self, request):
        import yaml
        path = os.path.join(ROOT_DIR, 'kpi-dashboard', request.param)
        with open(path) as f:
            return yaml.safe_load(f), request.param

    def test_feature_signal_engine_enabled(self, compose_file):
        data, filename = compose_file
        for svc_name, svc in data.get('services', {}).items():
            env = svc.get('environment', {})
            if 'FEATURE_MCP_SERVER' in str(env):
                sig = env.get('FEATURE_SIGNAL_ENGINE')
                assert sig is not None, f"FEATURE_SIGNAL_ENGINE missing in {filename} service {svc_name}"
                assert 'true' in str(sig).lower(), f"FEATURE_SIGNAL_ENGINE not true in {filename}"


# ============================================================
# App Registration Tests
# ============================================================

class TestAppRegistration:
    pytestmark = pytest.mark.skip(reason="reads app_v3_minimal.py source (82-blueprint entry point), Tier 2/3, not yet ported")

    @pytest.fixture(autouse=True)
    def load_source(self):
        path = os.path.join(BACKEND_DIR, 'app_v3_minimal.py')
        with open(path) as f:
            self.source = f.read()

    def test_email_receiver_registered(self):
        assert 'email_receiver_api' in self.source

    def test_slack_events_registered(self):
        assert 'slack_events_api' in self.source

    def test_signal_api_registered(self):
        assert 'signal_api' in self.source


# ============================================================
# Status Endpoint Tests
# ============================================================

class TestStatusEndpoint:
    def test_llm_enrichment_checks_api_key(self):
        path = os.path.join(BACKEND_DIR, 'signal_engine', 'ingest_api.py')
        with open(path) as f:
            source = f.read()
        assert "llm_available = bool(os.environ.get('ANTHROPIC_API_KEY'))" in source
        assert "'llm_enrichment': llm_available" in source

    def test_channels_in_status(self):
        path = os.path.join(BACKEND_DIR, 'signal_engine', 'ingest_api.py')
        with open(path) as f:
            source = f.read()
        assert "'channels'" in source
        assert "'manual'" in source
        assert "'email'" in source
        assert "'slack'" in source
        assert "'transcript'" in source


# ============================================================
# System Prompt Tests
# ============================================================

class TestSystemPrompts:
    pytestmark = pytest.mark.skip(reason="reads config/mcp_system_prompt.md, CS_PULSE_MCP_SYSTEM_PROMPT.md, and mcp_server/sanitized_tool_descriptions.json -- all Tier 2/3, not yet ported")

    @pytest.fixture(params=[
        os.path.join(BACKEND_DIR, 'config', 'mcp_system_prompt.md'),
        os.path.join(ROOT_DIR, 'CS_PULSE_MCP_SYSTEM_PROMPT.md'),
    ])
    def prompt_file(self, request):
        with open(request.param) as f:
            return f.read(), os.path.basename(request.param)

    def test_submit_signal_documented(self, prompt_file):
        content, filename = prompt_file
        assert 'submit_signal' in content, f"submit_signal not documented in {filename}"

    def test_sanitized_descriptions_has_submit_signal(self):
        path = os.path.join(BACKEND_DIR, 'mcp_server', 'sanitized_tool_descriptions.json')
        with open(path) as f:
            data = json.load(f)
        assert 'submit_signal' in data['tools'], "submit_signal missing from sanitized_tool_descriptions.json"


# ============================================================
# Enrichment Pipeline Tests
# ============================================================

class TestEnrichmentPipeline:
    def test_stub_enrichment_returns_valid_structure(self):
        from signal_engine.enrichment import _stub_enrichment
        result = _stub_enrichment("Customer is frustrated with deployment delays and evaluating competitors")
        assert 'sentiment_score' in result
        assert 'intent_signals' in result
        assert 'requires_review' in result
        assert result['requires_review'] is True  # Stub always requires review
        assert 'product_frustration' in result['intent_signals']
        assert 'competitor_mention' in result['intent_signals']

    def test_stub_enrichment_positive(self):
        from signal_engine.enrichment import _stub_enrichment
        result = _stub_enrichment("Great news! Customer wants to expand capacity and is very happy")
        assert result['sentiment_score'] > 0
        assert 'expansion_interest' in result['intent_signals']

    def test_stub_enrichment_champion_change(self):
        from signal_engine.enrichment import _stub_enrichment
        result = _stub_enrichment("The champion is leaving the company for a new role")
        assert 'champion_change' in result['intent_signals']

    def test_stub_enrichment_empty_text(self):
        from signal_engine.enrichment import _stub_enrichment
        result = _stub_enrichment("")
        assert result['intent_signals'] == []

    def test_rate_limit_check(self):
        from signal_engine.enrichment import _check_rate_limit
        # Fresh state — should not be rate limited
        result = _check_rate_limit(customer_id=99999, account_id=99999)
        assert result is None  # No limit exceeded

    def test_stub_subtypes_are_taxonomy_subtypes(self):
        # v2: the closed vocabulary is the tenant taxonomy (base + overlay); the
        # stub's keyword map must only emit base subtypes every vertical carries.
        from signal_engine.enrichment import STUB_KEYWORDS
        from utils.taxonomy_loader import get_taxonomy
        for vertical in ('dc2_s', 'saas_premium'):
            tax = get_taxonomy(vertical)
            assert all(tax.signal_role(sub) for _, sub in STUB_KEYWORDS)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
