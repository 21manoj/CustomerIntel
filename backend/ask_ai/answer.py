"""
Ask AI over the journey contract (P10) — the answer engine.

    ask(customer_id, question, account_id=None, as_of=None, history=None) -> dict

Flow
  1. scope    one account (account_id given, or an account named in the
              question, or — for a follow-up that names neither an account
              nor the portfolio — the account the previous turn was about)
              or the portfolio (portfolio phrasing, or no match).
  2. gather   ONLY from journeys.read: get_journey (journey + evidence index
              + narrative) for an account, list_journeys rows for the
              portfolio, get_evidence for a taxonomy role the question names.
              Also roi.priorities/roi.power_of_1/roi.measured/roi.investment,
              always for one account, keyword-gated
              (settings.scope.investment_phrases) for the portfolio — see
              'priority'/'po1'/'roi'/'investment_cost' below. A missing
              economics/investment file or unresolved vertical is caught and
              listed under evidence_gaps, never a crash.
              A char budget (config) decides what the model is shown; only
              ids actually shown are citable.
  3. model    one forced tool call, `answer_with_citations`, metered through
              utils.llm_budget_controller.record_usage. Without
              ANTHROPIC_API_KEY a deterministic stub answers from the
              narrative block (model 'stub_narrative_v1').
  4. validate every sentence must cite ids that resolve to the context it
              was given (episode ids, evidence node ids, 'row:<account_id>',
              'journey:<account_id>' for the arc/phases/series/forecast block,
              'priority:<account_id>'/'priority:portfolio' for investment
              priority (protect/grow, risk/opportunity factors), 'po1:<...>'
              for Power-of-1 ($ per point/percent, revenue-at-risk by band),
              'roi:portfolio' for realized vs. exposure $ and Wizard B lift,
              'investment_cost:<account_id>'/'investment_cost:portfolio' for
              what it costs to run a playbook and the $ per health point that
              buys); a sentence with no citation, or with one that does not
              resolve, is dropped and listed under `unsupported` — the same
              rule journeys/narrative.validate_narrative applies to the
              story block. Numbers in a kept sentence that do not appear in
              its cited blocks are flagged (`unverified_numbers`), never
              silently accepted.
  5. return   {answer, sentences, citations, unsupported, evidence_gaps,
              scope, model, generator}

Time travel: `as_of` applies the scrubber — episodes, series months,
hooks and evidence after that instant are removed before the model sees
anything, and the narrative is re-validated against what remains.

Conversation: `history` is the prior turns of the SAME chat, held by the
client and sent back on each question — in-session only, never persisted
(that is the separate cross-session memory feature, and this is not it).
It does two bounded things. (a) Scope: a follow-up that names no account
and no portfolio phrase inherits the previous turn's account instead of
falling back to the portfolio. (b) Reference: a capped recap of the last
few turns is shown under CONVERSATION SO FAR — deliberately OUTSIDE the
context blocks, so it never enters Context.citable and can never be cited.
It exists so "what about their champion?" can resolve `their`; every fact
in the answer must still come from, and cite, the evidence blocks.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ask_ai import settings

logger = logging.getLogger(__name__)

GENERATOR = 'ask_v1'
STUB_MODEL = 'stub_narrative_v1'
TOOL_NAME = 'answer_with_citations'
CITATION_RULE = ('every sentence cites >=1 id shown to the model (episode id, evidence node id, row:<account_id>, '
                 'journey:<account_id> for the arc/phases/series/forecast block, priority:<account_id>/priority:portfolio '
                 'for investment priority, po1:<account_id>/po1:portfolio for Power-of-1, roi:portfolio for realized '
                 'vs. exposure $, or investment_cost:<account_id>/investment_cost:portfolio for playbook cost and $ per '
                 'health point); a sentence with no citation or an unresolved citation is dropped and listed under unsupported')

SYSTEM_PROMPT = """You are Ask AI for a B2B Customer Success platform. You answer questions about one account's journey, or a portfolio of accounts, using ONLY the context blocks in the user message. The blocks come from the platform's evidence read layer: a cited narrative, a journey (arc, phases, leading-vs-trailing series), its episodes, the evidence index behind them, and — for portfolio questions — one row per account.

RULES (the product contract; the validator enforces them after you answer)
1. Every sentence cites. Each answer sentence lists the ids it was built from: episode ids (sig:N, out:N, dec:N, hs:N, renewal), evidence node ids (the bare node_id), row:<account_id>, journey:<account_id> (arc, phases, leading/trailing series, forecast, state — cite for arc confidence, lead_days, qual/kpi_only values), priority:<account_id> or priority:portfolio (lens, risk_factor, opportunity_factor, revenue_weighted — cite for who should get the next dollar/hour and why), po1:<account_id> or po1:portfolio (revenue per pillar/KPI point, revenue at risk by band — cite for what a 1-point or 1% move is worth), roi:portfolio (realized vs. exposure $ by playbook/pillar, the outcome ledger, Wizard B's lift — cite for what past interventions actually returned), or investment_cost:<account_id> / investment_cost:portfolio (estimated CSM hours and $ per playbook execution, $ per health point that buys — cite for what an intervention would cost, and whether that cost is estimated or backed by a measured lift). Cite only ids that appear in the blocks. A sentence that cannot cite is not written.
2. Numbers are read, never computed. Quote scores, dates, counts, revenue and lead days exactly as they appear in the cited blocks. Do not add, average, subtract or estimate. If the answer needs a number the blocks do not contain, say that it is not in the evidence.
3. "Why" states its evidence tier. When you explain a cause, say what kind of evidence backs it: observed evidence (a quote from a named source, its evidence_tier, confidence, whether it still requires review) versus system-derived facts (health transitions, arc hypotheses with their confidence semantics). Rejected or unreviewed evidence is said to be so.
4. Absence of evidence is an answer. If the blocks do not support a claim, say so plainly and list what is missing under evidence_gaps (e.g. no outcome recorded after the renewal date, no evidence since a month, an arc left unclassified). Never fill a gap with general knowledge or a plausible story.
5. Never invent: no people, quotes, dates, causes, playbooks or outcomes beyond the blocks. Do not speculate about what the customer "probably" feels.
6. Time travel. If the context is marked as_of, answer as of that instant only — nothing later exists.
7. Portfolio questions aggregate the same objects: every account you mention cites its row:<account_id>; rank or compare only on values present in the rows. priority:portfolio, po1:portfolio, roi:portfolio and investment_cost:portfolio (when shown) already are portfolio totals — cite those directly rather than summing per-account rows yourself.
8. Money has a basis, and every cost figure here is an informal estimate. Every dollar figure in a priority/po1/roi/investment_cost block carries a basis: measured (cited to a real outcome), derived (computed from the tenant's own data), or assumed (a configured economics or investment-cost estimate) — state which when it matters, the way you already state an evidence tier for a cause. risk_factor, opportunity_factor and revenue_weighted are a prioritization ranking, not a computed ROI — never call them "ROI" or "return". investment_cost figures (estimated CSM hours, $ per playbook execution, $ per health point) are basis assumed — informal CS-leadership estimates, not audited or measured costs, even on the rare figure where the $ per health point uses a measured lift (the cost side is still never measured, so the ratio stays assumed) — say so plainly if asked what something would cost, and never present an investment_cost figure as a precise or audited number.

9. The conversation recap is not evidence. If a CONVERSATION SO FAR section is present, it is a record of what this user already asked and what you already answered, nothing more. Use it ONLY to work out what the new question means — who "they" are, which account "it" is, what "that drop" refers to. It carries no ids and you may not cite it. A fact you stated in an earlier turn is not usable here unless the context blocks below say it again; if this turn's blocks do not support it, say so rather than repeating yourself on the strength of having said it before.

Write plain, specific sentences. Prefer the narrative's own wording when it already says the thing. Keep to at most {max_sentences} sentences. confidence (0-1) is your confidence that the kept sentences answer the question from the cited evidence — low when the evidence is thin or unreviewed."""

USER_PROMPT = """{history_block}QUESTION: {question}

SCOPE: {scope_line}

CONTEXT BLOCKS (the only evidence you may use; cite the ids exactly as written):
{context}"""

HISTORY_HEADER = ("CONVERSATION SO FAR (earlier turns of this chat, oldest first — read them to resolve what the new "
                  "question refers to; they are not evidence and carry no citable ids):\n")

_STOPWORDS = {'what', 'when', 'where', 'which', 'this', 'that', 'with', 'from', 'have', 'does', 'did', 'the', 'and', 'for',
              'why', 'how', 'was', 'were', 'has', 'about', 'account', 'accounts', 'tell', 'show', 'their', 'there', 'they',
              'happened', 'happen', 'give', 'please', 'would', 'could', 'should', 'into', 'over', 'been', 'being', 'some',
              'more', 'most', 'many', 'much', 'here', 'also', 'just', 'than', 'then', 'them', 'these', 'those', 'your'}

_NUMBER_RE = re.compile(r'\d[\d,]*(?:\.\d+)?')


# ── helpers ─────────────────────────────────────────────────────────────

def _parse_as_of(as_of) -> Optional[datetime]:
    if not as_of:
        return None
    if isinstance(as_of, datetime):
        return as_of
    s = str(as_of).strip()
    try:
        return datetime.fromisoformat(s[:19]) if len(s) > 10 else datetime.fromisoformat(s[:10]).replace(hour=23, minute=59, second=59)
    except ValueError:
        raise ValueError(f'as_of must be an ISO date or datetime, got {as_of!r}')


def _d(iso: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(str(iso)[:19]) if iso else None


def _keywords(question: str) -> List[str]:
    n = settings.get('answer', 'stub_min_keyword_chars')
    words = re.findall(r'[a-z0-9_]+', (question or '').lower())
    return [w for w in words if len(w) >= n and w not in _STOPWORDS]


# ── conversation (in-session, client-held) ──────────────────────────────

def normalize_history(history, strip_prefix: Optional[str] = None) -> List[dict]:
    """The last `conversation.max_turns` usable turns, oldest first, as
    {question, answer, account_id}. A turn is usable when it has both a
    question and an answer — a turn still in flight, or one the citation
    rule emptied, teaches the model nothing and is dropped rather than shown
    as a blank exchange. Anything else in the client's turn objects (their
    sentences, citations, gaps) is ignored on purpose: the recap is a memory
    aid, not a second evidence channel.

    `strip_prefix`, when given, is the current tenant's disclosure prefix
    (e.g. "[Synthetic demo data] "). A prior turn's answer carries it because
    the model's own output does (below) — replaying it back unstripped taught
    the model to imitate the prefix as part of ITS answer text, which then got
    the same disclosure prepended a second time, mechanically, on top."""
    if not history:
        return []
    if isinstance(history, dict):
        history = [history]
    out = []
    for t in history:
        if not isinstance(t, dict):
            continue
        q = str(t.get('question') or '').strip()
        a = str(t.get('answer') or '').strip()
        if not q or not a:
            continue
        if strip_prefix and a.startswith(strip_prefix):
            a = a[len(strip_prefix):].strip()
        aid = t.get('account_id')
        try:
            aid = int(aid) if aid is not None and str(aid).strip() != '' else None
        except (TypeError, ValueError):
            aid = None
        out.append({'question': q, 'answer': a, 'account_id': aid})
    return out[-settings.get('conversation', 'max_turns'):]


def history_block(history: List[dict]) -> str:
    """The recap the model is shown, or '' — capped twice: per turn
    (question_chars / answer_chars) and overall (max_chars, oldest turns
    dropped first). It sits outside the context blocks, so unlike every
    other thing the model reads it is never added to Context.citable."""
    if not history:
        return ''
    qn, an, cap = (settings.get('conversation', 'question_chars'), settings.get('conversation', 'answer_chars'),
                   settings.get('conversation', 'max_chars'))
    lines = [f'{i}. asked: {t["question"][:qn]}\n   answered: {t["answer"][:an]}' for i, t in enumerate(history, 1)]
    while lines and len(HISTORY_HEADER) + sum(len(x) + 1 for x in lines) > cap:
        lines.pop(0)                                  # oldest first: the turn nearest the new question matters most
    return (HISTORY_HEADER + '\n'.join(lines) + '\n\n') if lines else ''


def _history_account(history: List[dict], rows: List[dict]) -> Optional[int]:
    """The account the conversation is already about: the most recent turn
    that resolved to one, and only if it is still in this customer's
    portfolio (a stale or forged id from the client never selects an
    account — the portfolio rows are the authority, as they are for a name
    matched out of the question text)."""
    known = {int(r['account_id']) for r in rows}
    for t in reversed(history or []):
        if t.get('account_id') is not None and int(t['account_id']) in known:
            return int(t['account_id'])
    return None


class Context:
    """The blocks the model sees, under a character budget. `citable` maps
    every id shown to the object the answer may cite for it."""

    def __init__(self, budget: int):
        self.budget = int(budget)
        self.parts: List[str] = []
        self.used = 0
        self.citable: Dict[str, dict] = {}
        self.truncated: List[str] = []

    def add(self, label: str, obj, cite_id: Optional[str] = None, citation: Optional[dict] = None) -> bool:
        if cite_id is not None and isinstance(obj, dict):
            obj = {'cite': str(cite_id), **obj}          # the exact string the answer must cite for this block
        s = f'[{label}] ' + json.dumps(obj, default=str, separators=(',', ':'), sort_keys=True)
        if self.used + len(s) + 1 > self.budget:
            if label not in self.truncated:
                self.truncated.append(label)
            return False
        self.parts.append(s)
        self.used += len(s) + 1
        if cite_id is not None:
            self.citable[str(cite_id)] = citation if citation is not None else obj
        return True

    def text(self) -> str:
        return '\n'.join(self.parts)


# ── scope ───────────────────────────────────────────────────────────────

def decide_scope(customer_id: int, question: str, account_id: Optional[int], rows: List[dict],
                 history: Optional[List[dict]] = None) -> Tuple[str, Optional[int]]:
    """('account', id) or ('portfolio', None). An explicit account_id wins;
    then portfolio phrasing; then an account name from the portfolio found
    in the question; then — for a follow-up in an ongoing conversation —
    the account the previous turn resolved to; otherwise portfolio.

    The history step goes LAST, after everything this turn's own words say,
    so a follow-up can still change the subject: "and the portfolio?" reads
    as portfolio, "how is Contoso?" moves to Contoso. It only replaces the
    final fallback, which was never a decision about this question — it was
    a default."""
    if account_id:
        return 'account', int(account_id)
    q = (question or '').lower()
    if any(p in q for p in settings.get('scope', 'portfolio_phrases')):
        return 'portfolio', None
    for r in sorted(rows, key=lambda r: -len(r.get('account_name') or '')):     # longest name first: 'Acme Cloud' before 'Acme'
        name = (r.get('account_name') or '').strip().lower()
        if name and name in q:
            return 'account', int(r['account_id'])
    if history and settings.get('conversation', 'follow_up_inherits_account'):
        carried = _history_account(history, rows)
        if carried is not None:
            return 'account', carried
    return 'portfolio', None


def detect_role(question: str, vertical: Optional[str]) -> Optional[str]:
    """A taxonomy signal role the question names ('champion change' → champion_change)."""
    if not vertical:
        return None
    try:
        from utils.taxonomy_loader import get_taxonomy
        roles = list(get_taxonomy(vertical).signal_roles)
    except Exception:
        return None
    q = (question or '').lower()
    for role in sorted(roles, key=len, reverse=True):
        if role in q or role.replace('_', ' ') in q:
            return role
    return None


# ── time travel (scrubber semantics) ────────────────────────────────────

def apply_as_of(journey: dict, as_of: Optional[datetime]) -> Tuple[dict, List[str]]:
    """Return the journey as it was known at `as_of`: later episodes, series
    months, hooks and evidence removed; the narrative re-validated against
    the surviving episode ids (journeys.narrative.validate_narrative)."""
    if not as_of:
        return journey, []
    from journeys.narrative import validate_narrative
    j = copy.deepcopy(journey)
    gaps = []
    before = len(j.get('episodes') or [])
    j['episodes'] = [e for e in (j.get('episodes') or []) if _d(e.get('date')) and _d(e['date']) <= as_of]
    kept_ids = {e['episode_id'] for e in j['episodes']}
    kept_nodes = {str(nid) for e in j['episodes'] for nid in (e.get('evidence_node_ids') or [])}
    if before != len(j['episodes']):
        gaps.append(f"as_of {as_of.isoformat()}: {before - len(j['episodes'])} later episode(s) hidden by the scrubber")
    j['evidence'] = {k: v for k, v in (j.get('evidence') or {}).items() if k in kept_nodes}
    lvt = j.get('leading_vs_trailing') or {}
    if lvt.get('series'):
        lvt['series'] = [s for s in lvt['series'] if _d(s.get('month')) and _d(s['month']) <= as_of]
    j['live_months'] = [m for m in (j.get('live_months') or []) if _d(m) and _d(m) <= as_of]
    j['counterfactual_hooks'] = [h for h in (j.get('counterfactual_hooks') or []) if _d(h.get('date')) and _d(h['date']) <= as_of]
    j['phases'] = [p for p in (j.get('phases') or []) if _d(p.get('entered_at')) and _d(p['entered_at']) <= as_of]
    arc = j.get('arc') or {}
    sup = arc.get('supporting_episode_ids') or []
    if sup and any(s not in kept_ids for s in sup):
        gaps.append('the arc hypothesis was classified with evidence after as_of; it is shown with those citations removed')
        arc['supporting_episode_ids'] = [s for s in sup if s in kept_ids]
    if j.get('narrative'):
        j['narrative'] = validate_narrative(copy.deepcopy(j['narrative']), kept_ids)
    j['as_of'] = as_of.isoformat()
    return j, gaps


# ── gather ──────────────────────────────────────────────────────────────

def _episode_compact(e: dict, quote_chars: int) -> dict:
    meta = e.get('meta') or {}
    return {
        'id': e['episode_id'], 'date': str(e.get('date') or '')[:10], 'kind': e.get('kind'), 'subtype': e.get('subtype'),
        'role': e.get('role'), 'title': e.get('title'), 'quote': (meta.get('quote') or '')[:quote_chars] or None,
        'evidence_node_ids': e.get('evidence_node_ids') or [], 'tier': e.get('source'),
        'sentiment': e.get('sentiment'), 'revenue': e.get('revenue'), 'revenue_bucket': e.get('revenue_bucket'),
        'person': meta.get('stakeholder'), 'person_role': meta.get('stakeholder_role'), 'person_unresolved': meta.get('person_unresolved'),
        'confidence': meta.get('confidence'), 'requires_review': meta.get('requires_review'), 'review': meta.get('review'),
        'health_score': meta.get('health_score'), 'from': meta.get('from'), 'to': meta.get('to'),
    }


def _evidence_compact(v: dict, quote_chars: int) -> dict:
    prov = v.get('provenance') or {}
    return {
        'node_id': v['node_id'], 'account_id': v.get('account_id'), 'role': v.get('role'), 'subtype': v.get('subtype'),
        'occurred_at': str(v.get('occurred_at') or '')[:10], 'quote': (v.get('quote') or '')[:quote_chars],
        'person': v.get('person'), 'sentiment': v.get('sentiment'), 'effective_urgency': v.get('effective_urgency'),
        'evidence_tier': prov.get('evidence_tier') or prov.get('tier'), 'source_platform': prov.get('source_platform'),
        'classification_basis': prov.get('classification_basis'), 'confidence': v.get('confidence'),
        'requires_review': v.get('requires_review'), 'review': v.get('review'),
    }


def _row_compact(r: dict) -> dict:
    return {k: r.get(k) for k in ('account_id', 'account_name', 'revenue', 'arc_type', 'state', 'arc_confidence', 'current_phase',
                                  'last_scored_month', 'live_months', 'last_evidence_at', 'latest', 'first_leading_warning_at',
                                  'lead_days', 'episodes', 'open_review_count', 'forecast',
                                  'priority')}          # list_journeys already attaches this (roi.priorities.compact); it was
                                                         # dropped here silently — nothing upstream of this whitelist filters it


def _trim_money(m: Optional[dict]) -> Optional[dict]:
    """A roi.basis.money() dict, minus its basis_chain (the derivation sentence) — the model needs
    the value and the basis tier (measured/derived/assumed), not the chain of config keys behind it."""
    if not isinstance(m, dict):
        return m
    return {k: v for k, v in m.items() if k in ('value', 'basis', 'note')}


def _headroom_compact(hr: dict) -> dict:
    """The peer-headroom block trimmed for context: the ranking inputs plus the heaviest few KPI rows
    (a catalog can benchmark dozens; the model needs the ones that actually carry the factor)."""
    hr = hr or {}
    kpis = sorted(hr.get('kpis') or [], key=lambda k: -k.get('weight', 0))[:5]      # same cap as the Po1 portfolio block
    return {'status': hr.get('status'), 'factor': hr.get('factor'), 'multiplier': hr.get('multiplier'),
            'covered_kpis': hr.get('covered_kpis'), 'sources': hr.get('sources'), 'basis': hr.get('basis'),
            'kpis': [{k: v for k, v in row.items() if k in ('kpi', 'name', 'value', 'position', 'headroom', 'weight',
                                                            'percentiles', 'benchmark_source', 'benchmark_node_id')}
                     for row in kpis]}


def _priority_account_compact(row: dict) -> dict:
    """One account's full roi.priorities.score_account() row, trimmed for context (drop the static
    weights config — it's the same for every account and adds nothing per-account)."""
    factors = {k: v for k, v in (row.get('factors') or {}).items() if k != 'weights'}
    return {'lens': row.get('lens'), 'secondary_lens': row.get('secondary_lens'), 'risk_factor': row.get('risk_factor'),
            'opportunity_factor': row.get('opportunity_factor'), 'priority_factor': row.get('priority_factor'),
            'revenue': _trim_money(row.get('revenue')), 'revenue_weighted': _trim_money(row.get('revenue_weighted')),
            'addressable_weighted': _trim_money(row.get('addressable_weighted')),
            'benchmark_headroom': _headroom_compact(row.get('benchmark_headroom')),
            'factors': factors, 'opportunity': row.get('opportunity'),
            'open_interventions': row.get('open_interventions'), 'pending_approvals': row.get('pending_approvals'),
            'cites': row.get('cites')}


def _priority_portfolio_compact(portfolio: dict) -> dict:
    return {k: (_trim_money(v) if k in ('revenue_total', 'revenue_in_protect_lens', 'revenue_in_grow_lens',
                                        'exposure_weighted', 'opportunity_weighted') else v)
            for k, v in (portfolio or {}).items()}


def _po1_pillar_compact(p: dict) -> dict:
    return {'pillar': p.get('pillar'), 'name': p.get('name'), 'weight': p.get('weight'), 'weight_source': p.get('weight_source'),
            'current_score': p.get('current_score'), 'revenue_per_pillar_point': _trim_money(p.get('revenue_per_pillar_point')),
            'revenue_per_one_pct_move': _trim_money(p.get('revenue_per_one_pct_move'))}


def _po1_account_compact(acct: dict) -> dict:
    """One account's power_of_1.account_power_of_1() row: pillars in full (a handful), KPIs trimmed to
    the top-10-by-revenue-impact of the ones with a real measurement. A catalog can carry 40+ KPIs
    (measured live on a real datacenter_v1 account: 38 kpis, all measured, 18KB serialized — 75% of the
    whole 24000-char context budget for one block) — an unmeasured KPI's $/point is config arithmetic
    and the rest are long-tail next to the top movers, so both are worth cutting before the model sees it."""
    scored = [{'kpi': k['kpi'], 'name': k.get('name'), 'pillar': k.get('pillar'), 'unit': k.get('unit'),
              'revenue_per_kpi_score_point': _trim_money(k.get('revenue_per_kpi_score_point')),
              'one_pct_value_move': {**k['one_pct_value_move'], 'revenue_delta': _trim_money(k['one_pct_value_move'].get('revenue_delta'))}}
             for k in (acct.get('kpis') or []) if k.get('one_pct_value_move')]
    kpis = sorted(scored, key=lambda k: -abs((k['one_pct_value_move'].get('revenue_delta') or {}).get('value') or 0))[:10]
    return {'revenue': _trim_money(acct.get('revenue')), 'health_now': acct.get('health_now'), 'weight_source': acct.get('weight_source'),
            'revenue_per_health_point': _trim_money(acct.get('revenue_per_health_point')),
            'pillars': [_po1_pillar_compact(p) for p in (acct.get('pillars') or [])],
            'kpis': kpis, 'kpis_measured_total': len(scored), 'band_view': acct.get('band_view')}


def _po1_portfolio_compact(portfolio: dict) -> dict:
    """Portfolio-level power_of_1() aggregate — kpis capped to the top 5 movers by revenue impact
    (not 10: this block sits alongside priority_portfolio/roi_portfolio, all three competing for
    budget against the per-account rows a portfolio question also needs — see portfolio_context)."""
    kpis = sorted((portfolio.get('kpis') or []), key=lambda k: -((k.get('one_pct_value_move_revenue') or {}).get('value') or 0))[:5]
    return {'accounts': portfolio.get('accounts'), 'unscored_accounts': portfolio.get('unscored_accounts'),
            'revenue_base': _trim_money(portfolio.get('revenue_base')), 'revenue_per_health_point': _trim_money(portfolio.get('revenue_per_health_point')),
            'pillars': [{**{k: v for k, v in p.items() if k not in ('revenue_per_pillar_point', 'revenue_per_one_pct_move')},
                        'revenue_per_pillar_point': _trim_money(p.get('revenue_per_pillar_point')),
                        'revenue_per_one_pct_move': _trim_money(p.get('revenue_per_one_pct_move'))} for p in (portfolio.get('pillars') or [])],
            'kpis_measured_total': len(portfolio.get('kpis') or []),
            'kpis': [{**{k: v for k, v in k_.items() if k not in ('revenue_per_kpi_score_point', 'one_pct_value_move_revenue')},
                     'revenue_per_kpi_score_point': _trim_money(k_.get('revenue_per_kpi_score_point')),
                     'one_pct_value_move_revenue': _trim_money(k_.get('one_pct_value_move_revenue'))} for k_ in kpis],
            'bands': [{**b, 'revenue': _trim_money(b.get('revenue')), 'revenue_at_risk': _trim_money(b.get('revenue_at_risk'))}
                     for b in (portfolio.get('bands') or [])],
            'scenarios': [{k: v for k, v in s.items() if k != 'basis'} for s in (portfolio.get('scenarios') or [])]}


def _roi_compact(r: dict) -> dict:
    by_pillar = [{**{k: v for k, v in row.items() if k not in ('realized_revenue', 'exposure_revenue')},
                 'realized_revenue': _trim_money(row.get('realized_revenue')), 'exposure_revenue': _trim_money(row.get('exposure_revenue'))}
                for row in (r.get('by_pillar') or [])]
    ledger = r.get('ledger') or {}
    by_bucket = [{**{k: v for k, v in row.items() if k not in ('revenue', 'linked_revenue')},
                 'revenue': _trim_money(row.get('revenue')), 'linked_revenue': _trim_money(row.get('linked_revenue'))}
                for row in (ledger.get('by_bucket') or [])]
    hindsight = r.get('hindsight') or {}
    sensitivity = r.get('sensitivity') or {}
    return {'revenue_base': _trim_money(r.get('revenue_base')), 'interventions': r.get('interventions'), 'by_pillar': by_pillar,
            'ledger': {'by_bucket': by_bucket, 'outside_buckets': ledger.get('outside_buckets')},
            'hindsight': {'status': hindsight.get('status'), 'run_id': hindsight.get('run_id'), 'evidence_label': hindsight.get('evidence_label'),
                         'interventions': hindsight.get('interventions'), 'realized_nrr': hindsight.get('realized_nrr'),
                         'realized_nrr_basis': hindsight.get('realized_nrr_basis'), 'hint': hindsight.get('hint')},
            'sensitivity': {k: v for k, v in sensitivity.items() if k != 'pairs'}}


def _investment_playbook_compact(p: dict) -> dict:
    """One roi.investment.playbook_investment() row, trimmed: the lift's measured/estimated detail is
    reduced to source + qualifying count (the full pairs list isn't useful to the model, same call
    _roi_compact makes for sensitivity's 'pairs')."""
    lift = p.get('lift') or {}
    measured = lift.get('measured') or {}
    return {'playbook_id': p.get('playbook_id'), 'targets_pillars': p.get('targets_pillars'), 'targets_kpis': p.get('targets_kpis'),
            'estimated_csm_hours': _trim_money(p.get('estimated_csm_hours')), 'estimated_cost_per_execution': _trim_money(p.get('estimated_cost_per_execution')),
            'lift_source': lift.get('source'), 'lift_value': lift.get('value'), 'lift_qualifying_interventions': measured.get('qualifying_interventions'),
            'cost_per_health_point': _trim_money(p.get('cost_per_health_point'))}


def _investment_rollup_compact(rows: List[dict], key: str) -> List[dict]:
    """pillars or kpis rollup from roi.investment.investment_cost() — the covering playbooks' cost/point
    figures only (not the full playbook detail again), trimmed."""
    return [{key: r.get(key), 'name': r.get('name'), 'status': r.get('status'),
            'playbooks': [{'playbook_id': pb['playbook_id'], 'estimated_cost_per_execution': _trim_money(pb.get('estimated_cost_per_execution')),
                          'cost_per_health_point': _trim_money(pb.get('cost_per_health_point'))} for pb in (r.get('playbooks') or [])],
            'note': r.get('note')} for r in rows]


def _investment_cost_compact(ic: dict) -> dict:
    """roi.investment.investment_cost() is vertical-level, not account-scaled (no ARR multiplier the
    way po1 has) — the same content is shown whether the question is account- or portfolio-scoped;
    only the cite_id (investment_cost:<account_id> vs investment_cost:portfolio) differs, matching the
    other blocks' naming so the model has one consistent citation rule to learn."""
    return {'vertical': ic.get('vertical'), 'basis': ic.get('basis'), 'csm_hourly_cost': _trim_money(ic.get('csm_hourly_cost')),
            'playbooks': [_investment_playbook_compact(p) for p in (ic.get('playbooks') or [])],
            'pillars': _investment_rollup_compact(ic.get('pillars') or [], 'pillar'),
            'kpis': _investment_rollup_compact(ic.get('kpis') or [], 'kpi'),
            'note': ic.get('note')}


def detect_investment_intent(question: str) -> bool:
    """Whether a question is shaped like it needs priority/po1/roi/investment_cost context —
    keyword-gated (same pattern as decide_scope's portfolio_phrases) so the common question
    does not pay for extra reads and a bigger context block it will never cite. Both scopes
    gate on this: portfolio for all four aggregates, account for investment_cost."""
    q = (question or '').lower()
    return any(p in q for p in settings.get('scope', 'investment_phrases'))


def _narrative_for_context(narrative: dict, citable: Dict[str, dict]) -> Tuple[List[dict], int]:
    """The narrative's chapters with every sentence whose citations the model could not
    legally reuse removed → (chapters, sentences dropped).

    The narrative is prose ABOUT the episodes; its `cites` are episode ids. Whatever it
    names, the model will cite — and validate_answer keeps a sentence only when every
    citation resolves. So a narrative sentence citing an episode that did not fit the
    budget is not merely useless, it is a trap: it produces a fluent answer that is then
    dropped whole. Call this AFTER the episode blocks have been added."""
    chapters, dropped = [], 0
    for ch in narrative.get('chapters') or []:
        kept = []
        for s in ch.get('sentences') or []:
            cites = [c for c in (s.get('cites') or []) if str(c) in citable]
            if not cites:
                dropped += 1
                continue
            kept.append({'text': s['text'], 'cites': cites})
        if kept:
            chapters.append({'phase': ch.get('phase'), 'from': ch.get('from'), 'to': ch.get('to'), 'sentences': kept})
    return chapters, dropped


def _narrative_gaps(narrative: dict) -> List[str]:
    out = []
    for o in (narrative or {}).get('omitted') or []:
        note = o.get('note') or o.get('template') or ''
        if o.get('reason') == 'rejected_evidence':
            out.append(f"rejected evidence excluded: {note}")
        elif note and o.get('template') in ('renewal_outcome', 'arc_statement'):
            out.append(f"the narrative could not say ({o.get('template')}): {note}")
    return out


def account_context(customer_id: int, account_id: int, question: str, as_of: Optional[datetime],
                    row: Optional[dict]) -> Tuple[Context, List[str], dict, dict]:
    """The blocks for one account → (context, gaps, meta, narrative as shown).
    Raises LookupError when no journey exists."""
    from journeys.read import get_journey, get_evidence
    j = get_journey(int(customer_id), int(account_id), compact=False)
    if j is None:
        raise LookupError(f'no journey for account {account_id} — run process_data or trigger_wizard(customer_id, "a")')
    j, gaps = apply_as_of(j, as_of)
    quote_chars = settings.get('context', 'quote_chars')
    ctx = Context(settings.get('context', 'max_chars'))

    lvt = j.get('leading_vs_trailing') or {}
    series = (lvt.get('series') or [])[-settings.get('context', 'series_months'):]
    journey_block = {
        'account_id': j.get('account_id'), 'account_name': j.get('account_name'), 'vertical': j.get('vertical'),
        'as_of': j.get('as_of'), 'last_scored_month': j.get('last_scored_month'), 'live_months': j.get('live_months'),
        'last_evidence_at': j.get('last_evidence_at'), 'state': j.get('state'), 'current_phase': j.get('current_phase'),
        'arc': j.get('arc'), 'summary': j.get('summary'), 'open_review_count': j.get('open_review_count'),
        'phases': j.get('phases'), 'counterfactual_hooks': j.get('counterfactual_hooks'),
        'leading_vs_trailing': {k: v for k, v in lvt.items() if k != 'series'} | {'series': series},
        'forecast': j.get('forecast'),          # Foresight block: basis, label counts, probabilities with ranges, expected ARR
    }
    # Cited like `row` below (journey:<id>) — without this, every true number that lives only here
    # (arc confidence, lead_days, the leading/trailing series, phase state) can never legally be
    # cited, so an honest sentence stating one is always flagged as unverified. Found via a live
    # Ask AI eval (scripts/eval_ask_ai_questions.py) against a real model, not assumed.
    ctx.add('journey', journey_block, cite_id=f"journey:{j.get('account_id')}", citation=journey_block)
    if row:
        ctx.add('row', _row_compact(row), cite_id=f"row:{row['account_id']}", citation=row)

    try:
        from roi.priorities import investment_priorities
        pr = investment_priorities(int(customer_id), int(account_id))
        if pr.get('status') == 'ok' and pr.get('rows'):
            prow = pr['rows'][0]
            ctx.add('priority', _priority_account_compact(prow), cite_id=f"priority:{account_id}", citation=prow)
    except ValueError as e:
        gaps.append(f'investment priority not available: {e}')
    try:
        from roi.power_of_1 import power_of_1
        p1 = power_of_1(int(customer_id), int(account_id))
        if p1.get('status') == 'ok' and p1.get('accounts'):
            arow = p1['accounts'][0]
            ctx.add('po1', _po1_account_compact(arow), cite_id=f"po1:{account_id}", citation=arow)
    except ValueError as e:
        gaps.append(f'power_of_1 not available: {e}')
    # Gated on the question, as the portfolio path gates its own investment blocks and for the same
    # budget reason: this is by far the largest optional block (every playbook plus the pillar and KPI
    # rollups), and on a NON-investment question it bought nothing while pushing episodes out of the
    # budget entirely — which silently emptied the whole answer (see the narrative filter below).
    if detect_investment_intent(question):
        try:
            from roi.investment import investment_cost
            ic = investment_cost(int(customer_id))
            ctx.add('investment_cost', _investment_cost_compact(ic), cite_id=f"investment_cost:{account_id}", citation=ic)
        except ValueError as e:
            gaps.append(f'investment cost not available: {e}')

    # Episodes and evidence FIRST, narrative second. The narrative's own sentences carry episode ids,
    # so it teaches the model the citation vocabulary; added before the episodes it names, it can teach
    # ids that never made the budget. The model then cites them faithfully, validate_answer drops every
    # such sentence as unresolved_citation, and the caller gets a confident, empty answer.
    episodes = sorted(j.get('episodes') or [], key=lambda e: str(e.get('date') or ''), reverse=True)
    for e in episodes:
        if not ctx.add('episode', _episode_compact(e, quote_chars), cite_id=e['episode_id'], citation=e):
            break
    for nid, v in (j.get('evidence') or {}).items():
        if not ctx.add('evidence', _evidence_compact(v, quote_chars), cite_id=str(nid), citation=v):
            break

    narrative = j.get('narrative') or {}
    chapters, dropped = _narrative_for_context(narrative, ctx.citable)
    ctx.add('narrative', {'citation_rule': narrative.get('citation_rule'), 'chapters': chapters, 'omitted': narrative.get('omitted')})
    gaps.extend(_narrative_gaps(narrative))
    if dropped:
        gaps.append(f'{dropped} narrative sentence(s) were not shown to the model: the episodes they cite did not '
                    f'fit the context budget, and a sentence the model cannot legally cite must not be put in front of it')

    role = detect_role(question, j.get('vertical'))
    if role:
        until = as_of.isoformat() if as_of else None
        rows = get_evidence(int(customer_id), int(account_id), role=role, until=until, limit=settings.get('context', 'role_evidence_limit'))
        if not rows:
            gaps.append(f'no observed evidence with role {role} for this account' + (' as of the scrubber date' if as_of else ''))
        for v in rows:
            if str(v['node_id']) in ctx.citable:
                continue
            if not ctx.add('role_evidence', _evidence_compact(v, quote_chars), cite_id=str(v['node_id']), citation=v):
                break

    if not (j.get('episodes') or []):
        gaps.append('no episodes: nothing observed and no health transitions for this account')
    if ctx.truncated:
        gaps.append(f"context budget reached; not every block was shown to the model ({', '.join(ctx.truncated)})")
    meta = {'account_id': int(account_id), 'account_name': j.get('account_name'), 'as_of': j.get('as_of'), 'role_filter': role}
    return ctx, gaps, meta, narrative


def portfolio_context(customer_id: int, question: str, rows: List[dict], as_of: Optional[datetime]) -> Tuple[Context, List[str], dict]:
    ctx = Context(settings.get('context', 'max_chars'))
    gaps = []
    cap = settings.get('context', 'portfolio_max_rows')
    ctx.add('portfolio', {'accounts': len(rows), 'shown': min(len(rows), cap),
                          'note': 'one row per account, computed from cited evidence; latest = last month of the leading-vs-trailing series'})
    if not rows:
        gaps.append('no journeys for this customer yet — run process_data')
    if as_of:
        gaps.append('as_of applies to one account\'s journey; portfolio rows are as of their last build')

    # Investment blocks are added BEFORE the rows loop, on purpose: measured live on a real 12-account
    # tenant, the original three portfolio aggregates (priority/po1/roi) alone ran ~11000 of the
    # 24000-char budget, and rows are capped by count (portfolio_max_rows) but not by size — a tenant
    # with more accounts would let rows exhaust the budget first and silently starve the very blocks an
    # investment question is asking for. investment_cost_portfolio (added 2026-09-07) follows the same
    # rule for the same reason: measured live on a real 2-account tenant, all four investment blocks
    # (priority/po1/roi/investment_cost) plus every row used 15959 of 24000 chars — see
    # test_investment_cost.py::test_portfolio_investment_blocks_fit_budget_together, which asserts the
    # general property (not just this one measurement) so a future regression on a larger tenant shows
    # up as a test failure, not a silently-starved block. Added first, the CFO's actual answer survives
    # even on a large portfolio; rows fill what's left.
    if rows and detect_investment_intent(question):
        try:
            from roi.priorities import investment_priorities
            pr = investment_priorities(int(customer_id))
            if pr.get('status') == 'ok' and pr.get('portfolio'):
                ctx.add('priority_portfolio', _priority_portfolio_compact(pr['portfolio']), cite_id='priority:portfolio', citation=pr['portfolio'])
        except ValueError as e:
            gaps.append(f'investment priority not available: {e}')
        try:
            from roi.power_of_1 import power_of_1
            p1 = power_of_1(int(customer_id))
            if p1.get('status') == 'ok' and p1.get('portfolio'):
                ctx.add('po1_portfolio', _po1_portfolio_compact(p1['portfolio']), cite_id='po1:portfolio', citation=p1['portfolio'])
        except ValueError as e:
            gaps.append(f'power_of_1 not available: {e}')
        try:
            from roi.measured import roi as measured_roi
            rr = measured_roi(int(customer_id))
            ctx.add('roi_portfolio', _roi_compact(rr), cite_id='roi:portfolio', citation=rr)
        except ValueError as e:
            gaps.append(f'roi not available: {e}')
        try:
            from roi.investment import investment_cost
            ic = investment_cost(int(customer_id))
            ctx.add('investment_cost_portfolio', _investment_cost_compact(ic), cite_id='investment_cost:portfolio', citation=ic)
        except ValueError as e:
            gaps.append(f'investment cost not available: {e}')

    for r in rows[:cap]:
        if not ctx.add('row', _row_compact(r), cite_id=f"row:{r['account_id']}", citation=r):
            break
    if len(rows) > cap:
        gaps.append(f'portfolio has {len(rows)} accounts; only the first {cap} rows (by name) were shown')

    if ctx.truncated:
        gaps.append(f"context budget reached; not every block was shown to the model ({', '.join(ctx.truncated)})")
    return ctx, gaps, {'accounts': len(rows), 'shown': min(len(rows), cap)}


# ── model ───────────────────────────────────────────────────────────────

def answer_tool() -> dict:
    return {
        'name': TOOL_NAME,
        'description': 'Answer the question in sentences that each cite the ids of the context blocks they were built from.',
        'input_schema': {
            'type': 'object',
            'required': ['answer_sentences', 'evidence_gaps', 'confidence'],
            'properties': {
                'answer_sentences': {'type': 'array', 'items': {
                    'type': 'object', 'required': ['text', 'cites'],
                    'properties': {
                        'text': {'type': 'string', 'description': 'one sentence, numbers copied verbatim from the cited blocks'},
                        'cites': {'type': 'array', 'items': {'type': 'string'},
                                  'description': 'ids from the context: episode ids (sig:N, out:N, dec:N, hs:N, renewal), evidence node ids, row:<account_id>, '
                                                  'journey:<account_id> for the arc/phases/series/forecast block, priority:<account_id>/priority:portfolio, '
                                                  'po1:<account_id>/po1:portfolio, roi:portfolio, or investment_cost:<account_id>/investment_cost:portfolio'},
                    }}},
                'evidence_gaps': {'type': 'array', 'items': {'type': 'string'},
                                  'description': 'what the evidence could not say about this question'},
                'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
            },
        },
    }


def _coerce_payload(data) -> dict:
    """Models sometimes return the payload (or a field) as a JSON string."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {'answer_sentences': [], 'evidence_gaps': ['model returned unparseable output'], 'confidence': 0}
    if not isinstance(data, dict):
        return {'answer_sentences': [], 'evidence_gaps': ['model returned no payload'], 'confidence': 0}
    sents = data.get('answer_sentences')
    if isinstance(sents, str):
        try:
            parsed = json.loads(sents)
        except ValueError:
            parsed = []
        data = {**data, 'answer_sentences': parsed if isinstance(parsed, list) else []}
    return data


def _call_model(customer_id: int, system: str, user: str) -> Tuple[dict, str]:
    """One forced tool call; metered through record_usage on success AND failure."""
    import anthropic
    from utils.llm_budget_controller import record_usage
    model = settings.llm_model()
    module = settings.get('llm', 'module')
    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    try:
        response = client.messages.create(
            model=model, max_tokens=settings.get('llm', 'max_tokens'), system=system,
            messages=[{'role': 'user', 'content': user}],
            tools=[answer_tool()], tool_choice={'type': 'tool', 'name': TOOL_NAME})
    except Exception as e:
        try:
            record_usage(customer_id=int(customer_id), module=module, model=model, success=False, error_message=str(e)[:200])
        except Exception as cost_err:  # pragma: no cover — metering must never mask the real error
            logger.debug('%s: cost tracking failed: %s', module, cost_err)
        raise
    try:
        record_usage(customer_id=int(customer_id), module=module, tokens_in=response.usage.input_tokens,
                     tokens_out=response.usage.output_tokens, model=model, success=True)
    except Exception as cost_err:  # pragma: no cover
        logger.debug('%s: cost tracking failed: %s', module, cost_err)
    payload = next((b.input for b in response.content if getattr(b, 'type', '') == 'tool_use'), None)
    if payload is None:
        raise ValueError('model returned no tool_use block')
    return _coerce_payload(payload), model


# ── stub (no API key) ───────────────────────────────────────────────────

def _stub_keyword_weights(question: str, history: Optional[List[dict]]) -> Dict[str, int]:
    """Keywords to rank the stub's sentences by. This turn's words weigh
    double the previous turn's, so a follow-up still ranks on what it
    actually asked ("what about their champion?" → champion), while one
    carrying almost no words of its own ("and after that?") can still land
    on the subject the conversation was already about instead of on the
    first sentence in the narrative."""
    weights = {k: 2 for k in _keywords(question)}
    for k in _keywords((history or [{}])[-1].get('question') or ''):
        weights.setdefault(k, 1)
    return weights


def _stub_answer(question: str, scope: str, ctx: Context, narrative: Optional[dict], rows: List[dict],
                 history: Optional[List[dict]] = None) -> dict:
    """Deterministic: the narrative sentences (or portfolio rows) that share
    the most keywords with the question, already carrying their citations."""
    n = settings.get('answer', 'stub_max_sentences')
    weights = _stub_keyword_weights(question, history)
    sentences: List[dict] = []
    if scope == 'account':
        pool = [{'text': s['text'], 'cites': list(s['cites'])}
                for ch in (narrative or {}).get('chapters') or [] for s in ch.get('sentences') or []]
        scored = [(sum(w for k, w in weights.items() if k in s['text'].lower()), i, s) for i, s in enumerate(pool)]
        hits = sorted([t for t in scored if t[0] > 0], key=lambda t: (-t[0], t[1]))
        chosen = [t[2] for t in hits[:n]] or [s for s in pool[:n]]
        sentences = sorted(chosen, key=lambda s: pool.index(s))
        gaps = ['stub: keyword match over the narrative block, no model reading'] if pool else ['the narrative block has no sentences to answer from']
    else:
        def rank(r):
            latest = r.get('latest') or {}
            blob = json.dumps(_row_compact(r), default=str).lower()
            hit = sum(w for k, w in weights.items() if k in blob)
            return (-hit, 0 if latest.get('early_warning') else 1, latest.get('kpi_only') if latest.get('kpi_only') is not None else 10**9)
        for r in sorted(rows, key=rank)[:n]:
            rid = f"row:{r['account_id']}"
            if rid not in ctx.citable:
                continue
            latest = r.get('latest') or {}
            sentences.append({'text': (f"{r.get('account_name')}: arc {r.get('arc_type') or 'unclassified'} (state {r.get('state')}), "
                                       f"latest month {latest.get('month')} kpi_only {latest.get('kpi_only')} qual {latest.get('qual')} "
                                       f"early_warning {latest.get('early_warning')}, {r.get('episodes')} episodes, "
                                       f"{r.get('open_review_count')} open reviews."), 'cites': [rid]})
        gaps = ['stub: keyword match over portfolio rows, no model reading'] if rows else []
    return {'answer_sentences': sentences, 'evidence_gaps': gaps, 'confidence': settings.get('answer', 'stub_confidence')}


# ── validate ────────────────────────────────────────────────────────────

def _unverified_numbers(text: str, cited: List[dict]) -> List[str]:
    """Numbers in the sentence that do not occur in the blocks it cites."""
    hay = json.dumps(cited, default=str).replace(',', '')
    out = []
    for tok in _NUMBER_RE.findall(text):
        norm = tok.replace(',', '')
        candidates = {norm, norm.rstrip('0').rstrip('.') if '.' in norm else norm}
        if not any(c and c in hay for c in candidates):
            out.append(tok)
    return out


def validate_answer(payload: dict, citable: Dict[str, dict], max_sentences: int) -> Tuple[List[dict], List[dict]]:
    """Enforce the rule: keep a sentence only when every citation resolves to
    something the model was shown. Returns (kept, unsupported)."""
    kept, unsupported = [], []
    for s in (payload.get('answer_sentences') or []):
        if not isinstance(s, dict):
            continue
        text = (s.get('text') or '').strip()
        if not text:
            continue
        raw = s.get('cites') or []
        if isinstance(raw, str):
            raw = [raw]
        cites = list(dict.fromkeys(str(c).strip() for c in raw if str(c).strip()))
        if not cites:
            unsupported.append({'text': text, 'cites': [], 'reason': 'no_citation'})
            continue
        ghosts = [c for c in cites if c not in citable]
        if ghosts:
            unsupported.append({'text': text, 'cites': cites, 'reason': 'unresolved_citation', 'unresolved': ghosts})
            continue
        if len(kept) >= max_sentences:
            unsupported.append({'text': text, 'cites': cites, 'reason': 'over_max_sentences'})
            continue
        item = {'text': text, 'cites': cites}
        nums = _unverified_numbers(text, [citable[c] for c in cites])
        if nums:
            item['unverified_numbers'] = nums
        kept.append(item)
    return kept, unsupported


# ── entry point ─────────────────────────────────────────────────────────

def ask(customer_id: int, question: str, account_id: Optional[int] = None, as_of=None, history=None) -> dict:
    question = (question or '').strip()
    if not question:
        raise ValueError('question is required')
    when = _parse_as_of(as_of)
    from journeys.read import list_journeys, origin_block
    origin = origin_block(customer_id)
    turns = normalize_history(history, f"[{origin['label']}] " if origin['synthetic'] else None)
    rows = list_journeys(int(customer_id))
    scope, aid = decide_scope(int(customer_id), question, account_id, rows, turns)
    # Whether the subject was inherited rather than stated is part of the answer, not a hidden
    # convenience: a follow-up silently re-scoped to the previous account is exactly the kind of
    # thing a reader must be able to see (and the UI shows it).
    carried = bool(turns and not account_id and scope == 'account'
                   and aid is not None and aid == _history_account(turns, rows)
                   and not any((r.get('account_name') or '').strip().lower() in question.lower()
                               for r in rows if (r.get('account_name') or '').strip()))

    narrative = None
    if scope == 'account':
        row = next((r for r in rows if int(r['account_id']) == aid), None)
        ctx, gaps, meta, narrative = account_context(customer_id, aid, question, when, row)
        scope_line = f"one account — {meta.get('account_name')} (account_id {aid})" + (f", as of {meta['as_of']}" if when else '')
        if carried:
            scope_line += ' — carried over from the previous turn of this conversation, which the new question did not re-name'
    else:
        ctx, gaps, meta = portfolio_context(customer_id, question, rows, when)
        scope_line = f"portfolio — {meta['shown']} of {meta['accounts']} accounts shown"
    meta = {**meta, 'history_turns': len(turns), 'scope_carried_from_history': carried}

    max_sentences = settings.get('answer', 'max_sentences')
    system = SYSTEM_PROMPT.format(max_sentences=max_sentences)
    user = USER_PROMPT.format(history_block=history_block(turns), question=question, scope_line=scope_line, context=ctx.text())

    if os.environ.get('ANTHROPIC_API_KEY'):
        payload, model = _call_model(customer_id, system, user)
    else:
        payload, model = _stub_answer(question, scope, ctx, narrative, rows, turns), STUB_MODEL

    sentences, unsupported = validate_answer(payload, ctx.citable, max_sentences)
    cited_ids = list(dict.fromkeys(c for s in sentences for c in s['cites']))
    model_gaps = [str(g) for g in (payload.get('evidence_gaps') or []) if str(g).strip()]
    try:
        confidence = max(0.0, min(1.0, float(payload.get('confidence'))))
    except (TypeError, ValueError):
        confidence = None
    if not sentences and unsupported:
        # Never hand back a blank string with a confident-looking score: say which rule emptied it.
        # (The reasons are in `unsupported`, but nothing above the fold said the answer was dropped.)
        reasons = sorted({u['reason'] for u in unsupported})
        gaps.append(f'the model answered but every sentence was dropped by the citation rule ({", ".join(reasons)}); '
                    f'see "unsupported" for the text and the ids that did not resolve')
    answer = ' '.join(s['text'] for s in sentences)
    if origin['synthetic']:
        prefix = f"[{origin['label']}] "
        if answer.startswith(prefix):                        # model imitated the prefix from history_block; don't double it
            answer = answer[len(prefix):]
        answer = prefix + answer                              # disclosure travels with the answer, not beside it
    answer = answer.strip()
    return {
        'question': question, 'scope': scope, 'scope_detail': meta, **origin,
        'answer': answer,
        'sentences': sentences,
        'citations': {c: ctx.citable[c] for c in cited_ids},
        'unsupported': unsupported,
        'evidence_gaps': list(dict.fromkeys(gaps + model_gaps)),
        'confidence': confidence,
        'citation_rule': CITATION_RULE,
        'context_chars': ctx.used,
        'history_turns': len(turns),          # counted separately from context_chars: the recap is not a context block
        'model': model, 'generator': GENERATOR,
    }
