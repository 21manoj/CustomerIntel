"""
External-world research — the Claygent-inspired counterpart to enrichment.py.

enrichment.py extracts signals from communications the platform is handed;
this module goes and finds them. Given an account, it researches the real
company behind it via Claude's web_search server tool (funding, leadership
changes, layoffs, hiring shifts, competitor adoption), then feeds findings
through the SAME extraction pipeline internal signals use
(signal_engine.pipeline.ingest, source_type='external' — that value has
been defined in SOURCE_TYPES since this schema was written, with zero
producers until now).

Deliberately on-demand only, never scheduled — an explicit call, not a
background job, so cost stays under the caller's control. Uses
utils.llm_budget_controller.can_call_and_run() rather than a new rate
limiter: that function already existed with zero production callers
anywhere in the codebase before this module: this is its first one.

Honesty is achieved by prompting, not by a new confidence-scoring path:
external findings are structurally weaker evidence than a customer's own
communication, but rather than hard-capping confidence downstream (which
would touch enrichment.py's shared, already-tested confidence logic used
by every other source type), the research prompt is instructed to hedge
unconfirmed claims explicitly in the returned text. enrichment.py's own
existing rule already sets requires_review when confidence reads low from
that hedging language ("Confidence reflects explicitness... below
confidence_threshold, set requires_review"). No new pipeline logic.

Without ANTHROPIC_API_KEY, raises — there is no meaningful stub for
"go research the real world."
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from signal_engine import settings

logger = logging.getLogger(__name__)

MODULE_NAME = 'signal_engine_external_research'

RESEARCH_SYSTEM_PROMPT = """You research one company's recent public activity for a B2B Customer Success platform. Use web search to find SPECIFIC, DATED findings only in these categories: funding events, leadership or executive changes, layoffs or workforce reductions, hiring shifts in a relevant function, and adoption of a competing product or vendor.

Rules:
1. Report only what you actually found via search. Never invent a finding to fill a category.
2. State your confidence in your own language: a fact confirmed by an official source (a press release, an SEC filing, the company's own announcement) is "confirmed"; a claim from a single secondary source (a review site, an unverified report) is "unconfirmed" or "reported but not verified" — say so explicitly in the sentence itself, not as a separate caveat.
3. If a category has nothing found, say so plainly ("no recent leadership changes found") rather than omitting it silently.
4. Cite where each finding came from (the publication or site name) in the sentence.
5. Do not speculate about what a finding means for this company's relationship with any vendor — report only the external fact itself.

Write your findings as plain narrative sentences, one finding per sentence, most relevant first. This is not a customer communication — do not write as if you are the company or its contact."""

USER_PROMPT_TEMPLATE = "Research {company_name}{domain_hint}'s public activity over roughly the last 6 months: funding, leadership changes, layoffs, hiring shifts, competitor adoption."


def research_company(company_name: str, domain: Optional[str] = None, customer_id: Optional[int] = None) -> dict:
    """One web-search-backed LLM call, gated by the customer's LLM budget.
    Returns {status:'ok', findings_text, model, searched_at} or {status:'error', error}.
    Raises PermissionError if the budget circuit breaker is tripped, RuntimeError if no API key."""
    import os
    if not os.environ.get('ANTHROPIC_API_KEY'):
        raise RuntimeError('external research needs a real model — no stub exists for "go research the real world"')

    import anthropic
    from utils.llm_budget_controller import can_call_and_run

    cfg = settings.get('external_research')
    domain_hint = f' ({domain})' if domain else ''
    user = USER_PROMPT_TEMPLATE.format(company_name=company_name, domain_hint=domain_hint)
    client = anthropic.Anthropic()
    model = cfg['model']

    response = can_call_and_run(
        client, int(customer_id) if customer_id is not None else 0, MODULE_NAME, model,
        estimated_tokens=cfg['max_tokens'],
        max_tokens=cfg['max_tokens'], system=RESEARCH_SYSTEM_PROMPT,
        tools=[{'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': cfg['max_web_searches']}],
        messages=[{'role': 'user', 'content': user}],
    )
    text = '\n'.join(b.text for b in response.content if getattr(b, 'type', None) == 'text').strip()
    if not text:
        return {'status': 'error', 'error': 'model returned no findings text'}
    return {'status': 'ok', 'findings_text': text, 'model': model, 'searched_at': datetime.utcnow().isoformat()}


def research_and_ingest(customer_id: int, account_id: int, process_now: bool = True) -> dict:
    """research_company() on the account's own company, then ingest() the findings
    the same way any other external signal enters the graph. process_now mirrors
    submit_signal's own default: force extraction now rather than waiting for the
    background worker, so the caller sees the actual typed signal, not just 'queued'."""
    from models import Account
    from signal_engine.pipeline import ingest, process_pending

    acct = Account.query.filter_by(account_id=int(account_id), customer_id=int(customer_id)).first()
    if not acct:
        raise ValueError(f'account {account_id} does not belong to customer {customer_id}')
    if not acct.account_name:
        raise ValueError(f'account {account_id} has no account_name to research')
    domain = acct.external_account_id if acct.external_account_id and '.' in (acct.external_account_id or '') else None

    research = research_company(acct.account_name, domain=domain, customer_id=customer_id)
    if research['status'] != 'ok':
        return {'status': research['status'], 'error': research.get('error'), 'ingest': None}

    source_ref = f"external_research:{acct.account_id}:{research['searched_at'][:10]}"
    result = ingest(int(customer_id), int(account_id), source_type='external', raw_text=research['findings_text'],
                    source_ref=source_ref, occurred_at=datetime.utcnow())
    if result['status'] == 'queued' and process_now:
        out = process_pending(customer_id=int(customer_id), limit=50)
        mine = next((x for x in out['signals'] if x['signal_id'] == result['signal_id']), None)
        result.update({'processed': True, 'evidence': mine, 'journeys_rebuilt': out['journeys_rebuilt']})
    return {'status': 'ok', 'research': research, 'ingest': result}
