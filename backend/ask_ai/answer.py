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
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ask_ai import settings

logger = logging.getLogger(__name__)

GENERATOR = 'ask_v1'
STUB_MODEL = 'stub_narrative_v1'
STUB_LABEL = 'No model: answered from the data by fixed rules'
TOOL_NAME = 'answer_with_citations'
CITATION_RULE = ('every sentence cites >=1 id shown to the model (episode id, evidence node id, row:<account_id>, '
                 'portfolio for the whole-book counts, '
                 'journey:<account_id> for the arc/phases/series/forecast block, priority:<account_id>/priority:portfolio '
                 'for investment priority, po1:<account_id>/po1:portfolio for Power-of-1, roi:portfolio for realized '
                 'vs. exposure $, or investment_cost:<account_id>/investment_cost:portfolio for playbook cost and $ per '
                 'health point); a sentence with no citation or an unresolved citation is dropped and listed under unsupported')

SYSTEM_PROMPT = """You are Ask AI for a B2B Customer Success platform. You answer questions about one account's journey, or a portfolio of accounts, using ONLY the context blocks in the user message. The blocks come from the platform's evidence read layer: a cited narrative, a journey (arc, phases, leading-vs-trailing series), its episodes, the evidence index behind them, and — for portfolio questions — one row per account.

RULES (the product contract; the validator enforces them after you answer)
1. Every sentence cites. Each answer sentence lists the ids it was built from: episode ids (sig:N, out:N, dec:N, hs:N, renewal), evidence node ids (the bare node_id), row:<account_id>, portfolio (the whole-book counts block on a portfolio question: how many accounts are in each state, arc type, phase and early-warning label, how many have open reviews, total revenue), journey:<account_id> (arc, phases, leading/trailing series, forecast, state — cite for arc confidence, lead_days, qual/kpi_only values), priority:<account_id> or priority:portfolio (lens, risk_factor, opportunity_factor, revenue_weighted — cite for who should get the next dollar/hour and why), po1:<account_id> or po1:portfolio (revenue per pillar/KPI point, revenue at risk by band — cite for what a 1-point or 1% move is worth), roi:portfolio (realized vs. exposure $ by playbook/pillar, the outcome ledger, Wizard B's lift — cite for what past interventions actually returned), exposure:<account_id> or exposure:portfolio (shown when the tenant's money is measured in exposure, not revenue: expected annual exposure per account and for the book, the split by tier, what reaching the next likelihood band would remove, and each playbook's cost beside the exposure it avoided — cite for any question about exposure or what a failure would cost), or investment_cost:<account_id> / investment_cost:portfolio (estimated CSM hours and $ per playbook execution, $ per health point that buys — cite for what an intervention would cost, and whether that cost is estimated or backed by a measured lift). Cite only ids that appear in the blocks. A sentence that cannot cite is not written.
2. Numbers are read, never computed. Quote scores, dates, counts, revenue and lead days exactly as they appear in the cited blocks. Do not add, average, subtract or estimate. If the answer needs a number the blocks do not contain, say that it is not in the evidence.
3. "Why" states its evidence tier. When you explain a cause, say what kind of evidence backs it: observed evidence (a quote from a named source, its evidence_tier, confidence, whether it still requires review) versus system-derived facts (health transitions, arc hypotheses with their confidence semantics). Rejected or unreviewed evidence is said to be so.
4. Absence of evidence is an answer. If the blocks do not support a claim, say so plainly and list what is missing under evidence_gaps (e.g. no outcome recorded after the renewal date, no evidence since a month, an arc left unclassified). Never fill a gap with general knowledge or a plausible story.
5. Never invent: no people, quotes, dates, causes, playbooks or outcomes beyond the blocks. Do not speculate about what the customer "probably" feels.
6. Time travel. If the context is marked as_of, answer as of that instant only — nothing later exists.
7. Portfolio questions aggregate the same objects: every account you mention cites its row:<account_id>; rank or compare only on values present in the rows. A count or statement about the whole book ("2 of the 20 accounts…", "no account is…") cites portfolio and takes its number from that block's counts; if the count you need is not one of them, name the accounts and cite their rows instead of tallying. priority:portfolio, po1:portfolio, roi:portfolio and investment_cost:portfolio (when shown) already are portfolio totals — cite those directly rather than summing per-account rows yourself.
8. Money has a basis, and every cost figure here is an informal estimate. Every dollar figure in a priority/po1/roi/investment_cost block carries a basis: measured (cited to a real outcome), derived (computed from the tenant's own data), or assumed (a configured economics or investment-cost estimate) — state which when it matters, the way you already state an evidence tier for a cause. risk_factor, opportunity_factor and revenue_weighted are a prioritization ranking, not a computed ROI — never call them "ROI" or "return". investment_cost figures (estimated CSM hours, $ per playbook execution, $ per health point) are basis assumed — informal CS-leadership estimates, not audited or measured costs, even on the rare figure where the $ per health point uses a measured lift (the cost side is still never measured, so the ratio stays assumed) — say so plainly if asked what something would cost, and never present an investment_cost figure as a precise or audited number.

8a. Exposure is assumed, and is never divided by cost. Every figure in an exposure block is basis assumed (published averages and stated scale-downs applied to the tenant's own tiers and health scores) — say so. Report exposure avoided and playbook cost side by side; never divide one by the other or call the result a return or ROI. An account the block lists as not priced has no exposure figure: say it could not be priced and why, do not estimate one.

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
        # Plain statements about what the DATA cannot answer, said before the answer itself (never a
        # claim from evidence, so they sit outside the citation rule): "cost figures are not set up".
        self.notices: List[str] = []

    @staticmethod
    def _render(label: str, obj, cite_id: Optional[str] = None) -> str:
        if cite_id is not None and isinstance(obj, dict):
            obj = {'cite': str(cite_id), **obj}          # the exact string the answer must cite for this block
        return f'[{label}] ' + json.dumps(obj, default=str, separators=(',', ':'), sort_keys=True)

    def size(self, label: str, obj, cite_id: Optional[str] = None) -> int:
        """What add() would charge for this block — lets a caller choose a level of detail that fits
        BEFORE adding, instead of finding out by losing the tail of a list."""
        return len(self._render(label, obj, cite_id)) + 1

    @property
    def room(self) -> int:
        return self.budget - self.used

    def add(self, label: str, obj, cite_id: Optional[str] = None, citation: Optional[dict] = None) -> bool:
        s = self._render(label, obj, cite_id)
        if self.used + len(s) + 1 > self.budget:
            if label not in self.truncated:
                self.truncated.append(label)
            return False
        self.parts.append(s)
        self.used += len(s) + 1
        if cite_id is not None:
            self.citable[str(cite_id)] = citation if citation is not None else ({'cite': str(cite_id), **obj} if isinstance(obj, dict) else obj)
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

def _prune(obj):
    """Drop keys whose value is None / '' / [] / {} (recursively). A signal episode carries ~14 such
    keys (health fields on a signal, person fields on a health change…): measured on the persona-eval
    tenants they were over a third of every episode block, and that padding is what pushed the later
    episodes, the evidence index and the narrative out of the budget. False and 0 are kept — they say
    something; an absent key says exactly what a null did."""
    if isinstance(obj, dict):
        out = {k: _prune(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v is not None and v != '' and v != [] and v != {}}
    if isinstance(obj, list):
        return [_prune(v) for v in obj]
    return obj


def _episode_compact(e: dict, quote_chars: int) -> dict:
    meta = e.get('meta') or {}
    return _prune({
        'id': e['episode_id'], 'date': str(e.get('date') or '')[:10], 'kind': e.get('kind'), 'subtype': e.get('subtype'),
        'role': e.get('role'), 'title': e.get('title'), 'quote': (meta.get('quote') or '')[:quote_chars] or None,
        'evidence_node_ids': e.get('evidence_node_ids') or [], 'tier': e.get('source'),
        'sentiment': e.get('sentiment'), 'revenue': e.get('revenue'), 'revenue_bucket': e.get('revenue_bucket'),
        'person': meta.get('stakeholder'), 'person_role': meta.get('stakeholder_role'), 'person_unresolved': meta.get('person_unresolved'),
        'confidence': meta.get('confidence'), 'requires_review': meta.get('requires_review'), 'review': meta.get('review'),
        'health_score': meta.get('health_score'), 'from': meta.get('from'), 'to': meta.get('to'),
    })


def _evidence_compact(v: dict, quote_chars: int, quoted_in: Optional[str] = None) -> dict:
    """quoted_in: the id of an episode ALREADY shown that carries this node's quote. The evidence
    block then points at it instead of repeating the quote — the same sentence twice bought nothing
    and cost the budget the evidence blocks further down the list."""
    prov = v.get('provenance') or {}
    return _prune({
        'node_id': v['node_id'], 'account_id': v.get('account_id'), 'role': v.get('role'), 'subtype': v.get('subtype'),
        'occurred_at': str(v.get('occurred_at') or '')[:10],
        'quote': None if quoted_in else (v.get('quote') or '')[:quote_chars], 'quote_in_episode': quoted_in,
        'person': v.get('person'), 'sentiment': v.get('sentiment'), 'effective_urgency': v.get('effective_urgency'),
        'evidence_tier': prov.get('evidence_tier') or prov.get('tier'), 'source_platform': prov.get('source_platform'),
        'classification_basis': prov.get('classification_basis'), 'confidence': v.get('confidence'),
        'requires_review': v.get('requires_review'), 'review': v.get('review'),
    })


def _row_compact(r: dict) -> dict:
    return {k: r.get(k) for k in ('account_id', 'account_name', 'revenue', 'arc_type', 'state', 'arc_confidence', 'current_phase',
                                  'last_scored_month', 'live_months', 'last_evidence_at', 'latest', 'first_leading_warning_at',
                                  'lead_days', 'episodes', 'open_review_count', 'forecast',
                                  'priority')}          # list_journeys already attaches this (roi.priorities.compact); it was
                                                         # dropped here silently — nothing upstream of this whitelist filters it


def _row_slim(r: dict) -> dict:
    """The same account row at a lower level of detail, for a portfolio too large to show every full
    row. It keeps what a portfolio question ranks and counts by; the forecast ranges, the role counts
    and the priority factors stay on the full row. Cited as row:<id> exactly like the full one."""
    f, p, lt = r.get('forecast') or {}, r.get('priority') or {}, r.get('latest') or {}
    return _prune({
        'account_id': r.get('account_id'), 'account_name': r.get('account_name'), 'revenue': r.get('revenue'),
        'arc_type': r.get('arc_type'), 'state': r.get('state'), 'current_phase': r.get('current_phase'),
        'last_evidence_at': str(r.get('last_evidence_at') or '')[:10], 'lead_days': r.get('lead_days'),
        'episodes': r.get('episodes'), 'open_review_count': r.get('open_review_count'),
        'latest': {k: lt.get(k) for k in ('month', 'early_warning', 'qual')},
        'forecast': {k: f.get(k) for k in ('status', 'basis', 'decision_point', 'p_retain', 'p_expand', 'expected_arr_end')},
        'priority': {k: p.get(k) for k in ('lens', 'revenue_weighted', 'risk_factor', 'opportunity_factor', 'pending_approvals')},
        'detail': 'short',
    })


def _tally(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        key = 'none' if v in (None, '') else str(v)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _portfolio_summary(rows: List[dict]) -> dict:
    """Whole-book counts over EVERY account row (not only the rows that fit the budget), so a
    statement about the portfolio as a whole has one block to cite and its numbers are read from
    it. Counts only — nothing here is a judgement, and no dollar figure is summed except total
    revenue, which is the plain sum of the rows' own `revenue`."""
    with_reviews = [r for r in rows if (r.get('open_review_count') or 0) > 0]
    roles: Dict[str, int] = {}
    for r in rows:
        for role in ((r.get('latest') or {}).get('roles') or {}):
            roles[role] = roles.get(role, 0) + 1
    revenues = [r.get('revenue') for r in rows if isinstance(r.get('revenue'), (int, float))]
    return {
        'accounts': len(rows),
        'revenue_total': round(sum(revenues), 2) if revenues else None, 'accounts_with_revenue': len(revenues),
        'accounts_by_state': _tally(r.get('state') for r in rows),
        'accounts_by_arc_type': _tally(r.get('arc_type') for r in rows),
        'accounts_by_current_phase': _tally(r.get('current_phase') for r in rows),
        'accounts_by_latest_early_warning': _tally((r.get('latest') or {}).get('early_warning') for r in rows),
        'accounts_by_role_in_latest_month': dict(sorted(roles.items(), key=lambda kv: (-kv[1], kv[0]))),
        'accounts_with_open_reviews': len(with_reviews),
        'open_reviews_total': sum(r.get('open_review_count') or 0 for r in rows),
        'note': 'counts over every account in the portfolio, one row per account below; "none" = the field is empty for that '
                'account; latest = last month of the leading-vs-trailing series. Cite `portfolio` for these counts and for '
                'statements about the whole book; cite row:<account_id> for each account you name',
    }


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
    whole context budget of the time, 24000 chars, for one block) — an unmeasured KPI's $/point is config arithmetic
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


def _po1_top_at_risk(accounts: List[dict], n: int = 5) -> List[dict]:
    """The accounts carrying the most revenue at risk, largest first — so "which accounts carry most of it"
    can be answered from the same block as the band totals. Figures are power_of_1's own (band_view)."""
    rows = []
    for a in accounts:
        bv = a.get('band_view') or {}
        at_risk = _mv(bv.get('revenue_at_risk'))
        if at_risk:
            rows.append({'account_id': a.get('account_id'), 'account_name': a.get('account_name'), 'band': bv.get('band'),
                         'health_now': bv.get('health_now'), 'revenue': _mv(a.get('revenue')), 'revenue_at_risk': at_risk})
    return sorted(rows, key=lambda r: -r['revenue_at_risk'])[:n]


def _roi_compact(r: dict) -> dict:
    # `note` is the same sentence on every pillar row (do not sum pillars): shown once, as
    # by_pillar_note. An unmapped row's `reason` keeps which roles have no pillar and drops
    # catalog_pillar_roles_notes — paragraphs about how the catalog was authored, not about this tenant.
    notes = list(dict.fromkeys(row.get('note') for row in (r.get('by_pillar') or []) if row.get('note')))
    by_pillar = [{**{k: v for k, v in row.items() if k not in ('realized_revenue', 'exposure_revenue', 'note', 'reason')},
                 'realized_revenue': _trim_money(row.get('realized_revenue')), 'exposure_revenue': _trim_money(row.get('exposure_revenue')),
                 **({'reason': {k: v for k, v in row['reason'].items() if k != 'catalog_pillar_roles_notes'}}
                    if isinstance(row.get('reason'), dict) else ({'reason': row['reason']} if row.get('reason') else {}))}
                for row in (r.get('by_pillar') or [])]
    ledger = r.get('ledger') or {}
    by_bucket = [{**{k: v for k, v in row.items() if k not in ('revenue', 'linked_revenue')},
                 'revenue': _trim_money(row.get('revenue')), 'linked_revenue': _trim_money(row.get('linked_revenue'))}
                for row in (ledger.get('by_bucket') or [])]
    hindsight = r.get('hindsight') or {}
    sensitivity = r.get('sensitivity') or {}
    return {'revenue_base': _trim_money(r.get('revenue_base')), 'interventions': r.get('interventions'), 'by_pillar': by_pillar,
            'by_pillar_note': notes[0] if len(notes) == 1 else (notes or None),
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
    # Each covering playbook is already shown in full (basis, lift, the note explaining a null) in
    # the block's own `playbooks` list, so the rollup repeats only the two figures, as plain numbers
    # — the block-level `basis` covers them. A row's config-authoring note is kept only where the row
    # is NOT covered (there it is the reason); on a covered row it is commentary for whoever edits
    # the config. Measured: the two rollups were 4900 of this block's 8300 chars, mostly those repeats.
    return [_prune({key: r.get(key), 'name': r.get('name'), 'status': r.get('status'),
                    'playbooks': [{'playbook_id': pb['playbook_id'],
                                   'estimated_cost_per_execution': (pb.get('estimated_cost_per_execution') or {}).get('value'),
                                   'cost_per_health_point': (pb.get('cost_per_health_point') or {}).get('value')}
                                  for pb in (r.get('playbooks') or [])],
                    'note': None if r.get('status') == 'covered' else r.get('note')}) for r in rows]


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


def _money_basis(customer_id: int) -> str:
    """What the tenant's vertical measures money in (catalog capability money_basis): 'revenue' or
    'exposure'. The context builders ask this, never which vertical it is."""
    try:
        from utils.vertical_registry import get_vertical_for_customer, capability
        return capability(get_vertical_for_customer(int(customer_id)), 'money_basis')
    except Exception:
        return 'revenue'


def _account_words(customer_id: int) -> Tuple[str, str]:
    """(singular, plural) the tenant's vertical uses for an account, lower-cased."""
    try:
        from utils.vertical_registry import get_vertical_for_customer, labels
        lab = labels(get_vertical_for_customer(int(customer_id)))
        return lab['account'].lower(), lab['accounts'].lower()
    except Exception:
        return 'account', 'accounts'


def _money_unavailable(ctx: 'Context', gaps: List[str], what: str, err: Exception, asked: bool) -> None:
    """A money block could not be built. The reason goes to the log in full; the reader gets plain
    words with no file names or module paths. When the question itself asked for money, the same
    sentence also leads the answer (ctx.notices) instead of sitting in a list under it."""
    logger.info('ask_ai: %s not available: %s', what, err)
    line = f'{what} are not set up for this tenant, so questions about them cannot be answered from data yet'
    if line not in gaps:
        gaps.append(line)
    if asked and line not in ctx.notices:
        ctx.notices.append(line[0].upper() + line[1:] + '.')


def _mv(m) -> Optional[float]:
    return m.get('value') if isinstance(m, dict) else m


def _exposure_account_compact(r: dict) -> dict:
    nb = r.get('next_band') or {}
    return _prune({'account_id': r.get('account_id'), 'account_name': r.get('account_name'), 'tier': r.get('tier'),
                   'regulated_flag': r.get('regulated_flag') or None, 'health_now': r.get('health_now'),
                   'failure_cost': _mv(r.get('failure_cost')), 'failure_cost_source': r.get('failure_cost_source'),
                   'annual_failure_likelihood': r.get('annual_failure_likelihood'),
                   'expected_annual_exposure': _mv(r.get('expected_annual_exposure')),
                   'health_points_to_next_band': nb.get('health_points_needed'),
                   'avoidable_exposure_next_band': _mv(nb.get('avoidable_exposure')),
                   'not_priced': r.get('not_priced') or None, 'regulated_note': r.get('regulated_note')})


def _exposure_playbooks_compact(pb: Optional[dict]) -> Optional[dict]:
    if not pb or pb.get('status') != 'ok':
        return None
    return {'hourly_cost': _mv(pb.get('hourly_cost')), 'estimated_cost_to_date': _mv(pb.get('estimated_cost_to_date')), 'note': pb.get('note'),
            'rows': [_prune({'playbook_id': r['playbook_id'], 'interventions': r['interventions'], 'runs': r.get('runs'), 'closed_done': r['closed_done'],
                             'estimated_hours': r['estimated_hours'], 'estimated_cost_per_execution': _mv(r['estimated_cost_per_execution']),
                             'estimated_cost_to_date': _mv(r['estimated_cost_to_date']), 'exposure_avoided': _mv(r['exposure_avoided']),
                             'exposure_avoided_note': (r['exposure_avoided'] or {}).get('note')}) for r in pb.get('rows') or []]}


def _exposure_compact(ex: dict) -> dict:
    """roi.exposure.exposure(), trimmed for the model: every account's exposure figures as plain
    numbers (largest first), the book totals and tier split, and playbook cost beside exposure avoided.
    The block says once that every figure is assumed and that cost and exposure are never divided."""
    pf = ex.get('portfolio') or {}
    econ = ex.get('economics') or {}
    accounts = sorted((_exposure_account_compact(r) for r in ex.get('accounts') or []),
                      key=lambda r: -(r.get('expected_annual_exposure') or 0))
    return _prune({
        'money_basis': ex.get('money_basis'), 'basis': 'assumed', 'note': ex.get('note'), 'horizon_months': econ.get('horizon_months'),
        'assumptions': {'failure_events': {k: {'label': v.get('label'), 'cost_by_tier': v.get('cost_by_tier'), 'basis': v.get('basis')}
                                           for k, v in (econ.get('failure_events') or {}).items()},
                        'annual_failure_likelihood_by_health': {k: v for k, v in (econ.get('annual_failure_likelihood_by_health') or {}).items() if k != 'sources'}},
        'portfolio': {'accounts': pf.get('accounts'), 'priced_accounts': pf.get('priced_accounts'),
                      'not_priced_accounts': (pf.get('accounts') or 0) - (pf.get('priced_accounts') or 0), 'not_priced': pf.get('not_priced'),
                      'regulated_flag_accounts': pf.get('regulated_flag_accounts'), 'contract_value': _mv(pf.get('contract_value')),
                      'expected_annual_exposure': _mv(pf.get('expected_annual_exposure')),
                      'avoidable_exposure_next_band': _mv(pf.get('avoidable_exposure_next_band')),
                      'by_tier': [{'tier': t['tier'], 'accounts': t['accounts'], 'priced_accounts': t['priced_accounts'],
                                   'contract_value': _mv(t['contract_value']), 'expected_annual_exposure': _mv(t['expected_annual_exposure'])}
                                  for t in pf.get('by_tier') or []]} if pf else None,
        'playbooks': _exposure_playbooks_compact(ex.get('playbooks')),
        'accounts': accounts,
    })


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

    asked_money = detect_investment_intent(question)
    exposure_basis = _money_basis(customer_id) == 'exposure'
    try:
        from roi.priorities import investment_priorities
        pr = investment_priorities(int(customer_id), int(account_id))
        if pr.get('status') == 'ok' and pr.get('rows'):
            prow = pr['rows'][0]
            ctx.add('priority', _priority_account_compact(prow), cite_id=f"priority:{account_id}", citation=prow)
    except Exception as e:  # noqa: BLE001 — a money block that cannot be built must not take the answer down
        _money_unavailable(ctx, gaps, 'investment priorities', e, asked_money)
    if exposure_basis:
        # The vertical prices exposure, not revenue (capability money_basis): the revenue blocks do not
        # apply, and asking for them only produced "not available". One account is cheap, so its exposure
        # block is always shown — as po1 is for a revenue vertical.
        try:
            from roi.exposure import exposure
            ex = exposure(int(customer_id), int(account_id))
            if ex.get('status') == 'ok':
                block = _exposure_compact(ex)
                ctx.add('exposure', block, cite_id=f"exposure:{account_id}", citation=block)
        except Exception as e:  # noqa: BLE001 — a money block that cannot be built must not take the answer down
            _money_unavailable(ctx, gaps, 'exposure figures', e, asked_money)
    else:
        try:
            from roi.power_of_1 import power_of_1
            p1 = power_of_1(int(customer_id), int(account_id))
            if p1.get('status') == 'ok' and p1.get('accounts'):
                arow = p1['accounts'][0]
                ctx.add('po1', _po1_account_compact(arow), cite_id=f"po1:{account_id}", citation=arow)
        except Exception as e:  # noqa: BLE001 — a money block that cannot be built must not take the answer down
            _money_unavailable(ctx, gaps, 'revenue-per-health-point figures', e, asked_money)
    # Gated on the question, as the portfolio path gates its own investment blocks and for the same
    # budget reason: this is by far the largest optional block (every playbook plus the pillar and KPI
    # rollups), and on a NON-investment question it bought nothing while pushing episodes out of the
    # budget entirely — which silently emptied the whole answer (see the narrative filter below).
    if asked_money:
        try:
            from roi.investment import investment_cost
            ic = investment_cost(int(customer_id))
            ctx.add('investment_cost', _investment_cost_compact(ic), cite_id=f"investment_cost:{account_id}", citation=ic)
        except Exception as e:  # noqa: BLE001 — a money block that cannot be built must not take the answer down
            _money_unavailable(ctx, gaps, 'playbook cost figures', e, True)

    # Episodes and evidence FIRST, narrative second. The narrative's own sentences carry episode ids,
    # so it teaches the model the citation vocabulary; added before the episodes it names, it can teach
    # ids that never made the budget. The model then cites them faithfully, validate_answer drops every
    # such sentence as unresolved_citation, and the caller gets a confident, empty answer.
    episodes = sorted(j.get('episodes') or [], key=lambda e: str(e.get('date') or ''), reverse=True)
    quoted_in: Dict[str, Tuple[str, str]] = {}          # evidence node id → (shown episode id, the quote it carries)
    for e in episodes:
        if not ctx.add('episode', _episode_compact(e, quote_chars), cite_id=e['episode_id'], citation=e):
            break
        q = ((e.get('meta') or {}).get('quote') or '')[:quote_chars]
        for nid in (e.get('evidence_node_ids') or []) if q else []:
            quoted_in.setdefault(str(nid), (e['episode_id'], q))
    for nid, v in (j.get('evidence') or {}).items():
        eid, q = quoted_in.get(str(nid), (None, None))
        same = eid if q == (v.get('quote') or '')[:quote_chars] else None          # only when it IS the same text
        if not ctx.add('evidence', _evidence_compact(v, quote_chars, quoted_in=same), cite_id=str(nid), citation=v):
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
    # Citable, as `portfolio`. It was not: the model cited it anyway for every whole-book statement
    # ("2 of the 20 accounts have an open review") and validate_answer dropped that sentence as an
    # unresolved citation — in the persona eval, 11 of the 12 dropped portfolio sentences, and usually
    # the headline one. It also now carries the counts those sentences state, so rule 2 (numbers are
    # read, never computed) is satisfiable for them instead of quietly broken by the model's own tally.
    summary = _portfolio_summary(rows)
    ctx.add('portfolio', summary, cite_id='portfolio', citation=summary)
    if not rows:
        gaps.append('no journeys for this customer yet — run process_data')
    if as_of:
        gaps.append('as_of applies to one account\'s journey; portfolio rows are as of their last build')

    # Investment blocks are added BEFORE the rows loop, on purpose: measured live on a real 12-account
    # tenant, the original three portfolio aggregates (priority/po1/roi) alone ran ~11000 of the
    # then-24000-char budget (48000 since 2026-11), and rows are capped by count (portfolio_max_rows) but not by size — a tenant
    # with more accounts would let rows exhaust the budget first and silently starve the very blocks an
    # investment question is asking for. investment_cost_portfolio (added 2026-09-07) follows the same
    # rule for the same reason: measured live on a real 2-account tenant, all four investment blocks
    # (priority/po1/roi/investment_cost) plus every row used 15959 of the then 24000 chars — see
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
        except Exception as e:  # noqa: BLE001 — a money block that cannot be built must not take the answer down
            _money_unavailable(ctx, gaps, 'investment priorities', e, True)
        if _money_basis(customer_id) == 'exposure':
            # Exposure verticals: one block carries the book totals, every account's exposure and the
            # playbooks' cost beside exposure avoided. The revenue blocks (po1, measured ROI) do not apply.
            try:
                from roi.exposure import exposure
                ex = exposure(int(customer_id))
                if ex.get('status') == 'ok':
                    block = _exposure_compact(ex)
                    ctx.add('exposure_portfolio', block, cite_id='exposure:portfolio', citation=block)
            except Exception as e:  # noqa: BLE001 — a money block that cannot be built must not take the answer down
                _money_unavailable(ctx, gaps, 'exposure figures', e, True)
        else:
            try:
                from roi.power_of_1 import power_of_1
                p1 = power_of_1(int(customer_id))
                if p1.get('status') == 'ok' and p1.get('portfolio'):
                    top = _po1_top_at_risk(p1.get('accounts') or [])
                    ctx.add('po1_portfolio', {**_po1_portfolio_compact(p1['portfolio']), 'top_revenue_at_risk': top},
                            cite_id='po1:portfolio', citation={**p1['portfolio'], 'top_revenue_at_risk': top})
            except Exception as e:  # noqa: BLE001 — a money block that cannot be built must not take the answer down
                _money_unavailable(ctx, gaps, 'revenue-per-health-point figures', e, True)
            try:
                from roi.measured import roi as measured_roi
                rr = measured_roi(int(customer_id))
                ctx.add('roi_portfolio', _roi_compact(rr), cite_id='roi:portfolio', citation=rr)
            except Exception as e:  # noqa: BLE001 — a money block that cannot be built must not take the answer down
                _money_unavailable(ctx, gaps, 'measured return figures', e, True)
        try:
            from roi.investment import investment_cost
            ic = investment_cost(int(customer_id))
            ctx.add('investment_cost_portfolio', _investment_cost_compact(ic), cite_id='investment_cost:portfolio', citation=ic)
        except Exception as e:  # noqa: BLE001 — a money block that cannot be built must not take the answer down
            _money_unavailable(ctx, gaps, 'playbook cost figures', e, True)

    # Every account before any account's detail. The rows used to be added in full, in name order,
    # until one did not fit — so on an investment question over a 20-account tenant the model saw the
    # aggregates and the first 5 accounts alphabetically, and answered "which accounts…" from those 5
    # (persona eval, 2026-11: all 12 such questions). Now the level of detail is chosen up front: full
    # rows when they all fit, otherwise the short row for every account; and if even the short rows do
    # not fit, the largest accounts by revenue are the ones shown and the gap says how many were not.
    listed = rows[:cap]
    def _fits(compact):
        return sum(ctx.size('row', compact(r), f"row:{r['account_id']}") for r in listed) <= ctx.room
    compact = _row_compact
    if listed and not _fits(_row_compact):
        compact = _row_slim
        gaps.append('account rows are shown in short form so that every account fits the context budget '
                    '(forecast ranges, role counts and priority factors per account were left out)')
        if not _fits(_row_slim):
            listed = sorted(listed, key=lambda r: -(r.get('revenue') or 0))
    shown = 0
    for r in listed:
        if not ctx.add('row', compact(r), cite_id=f"row:{r['account_id']}", citation=r):
            break
        shown += 1
    if len(rows) > cap:
        gaps.append(f'portfolio has {len(rows)} accounts; only the first {cap} rows (by name) were considered')
    if shown < len(listed):
        gaps.append(f'context budget reached; {shown} of {len(listed)} account rows were shown to the model '
                    f'(largest by revenue first) — the other {len(listed) - shown} are missing from this answer')
    other = [t for t in ctx.truncated if t != 'row']
    if other:
        gaps.append(f"context budget reached; not every block was shown to the model ({', '.join(other)})")
    return ctx, gaps, {'accounts': len(rows), 'shown': shown}


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
    from utils import model_provider
    from utils.llm_budget_controller import record_usage
    model = settings.llm_model()
    module = settings.get('llm', 'module')
    try:
        result = model_provider.tool_call(model=model, max_tokens=settings.get('llm', 'max_tokens'),
                                          system=system, user=user, tool=answer_tool())
    except model_provider.NoToolCall as e:      # the host answered (tokens spent) but not through the tool
        try:
            record_usage(customer_id=int(customer_id), module=module, tokens_in=e.tokens_in,
                         tokens_out=e.tokens_out, model=model, success=True)
        except Exception as cost_err:  # pragma: no cover
            logger.debug('%s: cost tracking failed: %s', module, cost_err)
        raise
    except Exception as e:
        try:
            record_usage(customer_id=int(customer_id), module=module, model=model, success=False, error_message=str(e)[:200])
        except Exception as cost_err:  # pragma: no cover — metering must never mask the real error
            logger.debug('%s: cost tracking failed: %s', module, cost_err)
        raise
    try:
        record_usage(customer_id=int(customer_id), module=module, tokens_in=result.tokens_in,
                     tokens_out=result.tokens_out, model=model, success=True)
    except Exception as cost_err:  # pragma: no cover
        logger.debug('%s: cost tracking failed: %s', module, cost_err)
    return _coerce_payload(result.payload), model


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


def _fmt_money(v) -> str:
    """Dollars as the block holds them (whole when whole, else cents), so the figure in a sentence is
    literally the figure in the cited block and the number check passes without rounding."""
    f = float(v)
    return f'${int(f):,}' if f == int(f) else f'${f:,.2f}'


_NOT_PRICED_WORDS = {'no_tier': 'no tier is recorded', 'unknown_tier': 'the recorded tier is not one the cost figures cover',
                     'no_health_score': 'there is no health score yet', 'no_failure_event_applies': 'no failure event in the cost figures applies'}


def _not_priced_words(code) -> str:
    return _NOT_PRICED_WORDS.get(str(code), str(code).replace('_', ' '))


def _stub_money_answer(question: str, ctx: Context, words: Tuple[str, str]) -> Optional[List[dict]]:
    """Without a model, a money question is answered from the money blocks by fixed rules — not by
    keyword-matching the story, which recited a timeline at someone who asked what playbooks cost.
    None when the context holds no money block (the caller then leads with why)."""
    q = (question or '').lower()
    one, many = words
    ex_id = next((c for c in ctx.citable if c.startswith('exposure:')), None)
    ic_id = next((c for c in ctx.citable if c.startswith('investment_cost:')), None)
    out: List[dict] = []
    if ex_id:
        ex = ctx.citable[ex_id]
        pf, accts = ex.get('portfolio') or {}, ex.get('accounts') or []
        priced = [a for a in accts if a.get('expected_annual_exposure') is not None]
        pb = ex.get('playbooks') or {}
        wants_cost = any(k in q for k in ('playbook', 'cost', 'spend', 'spent', 'budget'))
        if ex_id == 'exposure:portfolio':
            if pf.get('expected_annual_exposure') is not None:
                out.append({'text': f"Expected annual exposure across {pf.get('priced_accounts')} priced {many} is "
                                    f"{_fmt_money(pf['expected_annual_exposure'])}, on assumed figures; "
                                    f"{pf.get('not_priced_accounts')} of {pf.get('accounts')} {many} could not be priced.", 'cites': [ex_id]})
            elif pf.get('accounts'):
                why = '; '.join(f"{len(ids)} because {_not_priced_words(code)}" for code, ids in (pf.get('not_priced') or {}).items())
                out.append({'text': f"None of the {pf.get('accounts')} {many} could be priced, so there is no exposure figure yet: {why}.", 'cites': [ex_id]})
            if 'tier' in q or 'split' in q:
                parts = [f"{t['tier']} {_fmt_money(t['expected_annual_exposure'])} ({t['accounts']} {many})"
                         for t in pf.get('by_tier') or [] if t.get('expected_annual_exposure') is not None]
                if parts:
                    out.append({'text': 'By tier: ' + '; '.join(parts) + '.', 'cites': [ex_id]})
            if priced and not wants_cost:
                top = ', '.join(f"{a['account_name']} ({_fmt_money(a['expected_annual_exposure'])})" for a in priced[:3])
                out.append({'text': f'The largest exposures are {top}.', 'cites': [ex_id]})
            if pf.get('avoidable_exposure_next_band') is not None and not wants_cost:
                out.append({'text': f"Moving every {one} below the best band up one band would remove "
                                    f"{_fmt_money(pf['avoidable_exposure_next_band'])} of that exposure.", 'cites': [ex_id]})
        elif priced:
            a = priced[0]
            out.append({'text': f"{a['account_name']} ({a.get('tier')} tier) carries an expected annual exposure of "
                                f"{_fmt_money(a['expected_annual_exposure'])}, on assumed figures: a failure cost of "
                                f"{_fmt_money(a['failure_cost'])} at an annual likelihood of {a['annual_failure_likelihood']}.", 'cites': [ex_id]})
            if a.get('avoidable_exposure_next_band') is not None:
                out.append({'text': f"Reaching the next band ({a['health_points_to_next_band']} health points away) would remove "
                                    f"{_fmt_money(a['avoidable_exposure_next_band'])} of it.", 'cites': [ex_id]})
        elif accts:
            out.append({'text': f"{accts[0]['account_name']} could not be priced, so it has no exposure figure: "
                                f"{_not_priced_words(accts[0].get('not_priced'))}.", 'cites': [ex_id]})
        rows = pb.get('rows') or []
        if rows and wants_cost:
            by_cost = sorted(rows, key=lambda r: -(r.get('estimated_cost_per_execution') or 0))
            listed = '; '.join(f"{r['playbook_id']} {_fmt_money(r['estimated_cost_per_execution'])}" for r in by_cost)
            out.append({'text': f"Each playbook's estimated cost per run, in analyst time at {_fmt_money(pb['hourly_cost'])} an hour (assumed): {listed}.",
                        'cites': [ex_id]})
            used = [r for r in rows if r.get('runs', r.get('interventions'))]       # approved runs: a proposal nobody approved costs nothing
            if used:
                parts = '; '.join(f"{r['playbook_id']} {_fmt_money(r['estimated_cost_to_date'])} over {r.get('runs', r['interventions'])} approved run(s)"
                                  + (f", exposure avoided {_fmt_money(r['exposure_avoided'])}" if r.get('exposure_avoided') is not None
                                     else ', exposure avoided not yet measurable') for r in used)
                out.append({'text': f'Cost to date beside exposure avoided (the two are not divided into a return): {parts}.', 'cites': [ex_id]})
            else:
                out.append({'text': 'No playbook has been run yet, so there is no cost to date and no exposure avoided to report.', 'cites': [ex_id]})
        return out or None
    p1_id = next((c for c in ctx.citable if c.startswith('po1:')), None)
    wants_cost = any(k in q for k in ('playbook', 'cost', 'spend', 'spent', 'budget', 'per dollar', 'lift'))
    wants_risk = bool(re.search(r'\b(at[ -]risk|revenue|arr|exposure|churn|lose|losing)\b', q))
    if p1_id and (wants_risk or not wants_cost):
        p1 = ctx.citable[p1_id]
        if p1_id == 'po1:portfolio':
            bands = [b for b in p1.get('bands') or [] if _mv(b.get('revenue_at_risk')) is not None]
            if bands:
                total = sum(_mv(b['revenue_at_risk']) for b in bands)
                parts = '; '.join(f"{b['band']} {_fmt_money(_mv(b['revenue_at_risk']))} ({b['accounts']} {many})" for b in bands)
                base = _mv(p1.get('revenue_base'))
                out.append({'text': f"Revenue at risk across {p1.get('accounts')} {many} is {_fmt_money(round(total, 2))}"
                                    + (f" of {_fmt_money(base)} in revenue" if base is not None else '')
                                    + f", on assumed loss shares by health band: {parts}.", 'cites': [p1_id]})
            top = p1.get('top_revenue_at_risk') or []
            if top:
                listed = ', '.join(f"{a['account_name']} ({_fmt_money(a['revenue_at_risk'])}, {a['band']})" for a in top[:3])
                out.append({'text': f'The {many} carrying the most of it are {listed}.', 'cites': [p1_id]})
            per_pt = _mv(p1.get('revenue_per_health_point'))
            if per_pt is not None and not wants_cost:        # a question that also asks for costs needs the room
                out.append({'text': f"One health point across the portfolio is worth {_fmt_money(per_pt)} a year (assumed).", 'cites': [p1_id]})
        else:
            bv = p1.get('band_view') or {}
            at_risk, rev, per_pt = _mv(bv.get('revenue_at_risk')), _mv(p1.get('revenue')), _mv(p1.get('revenue_per_health_point'))
            if at_risk is not None:
                out.append({'text': f"This {one} is in the {bv.get('band')} band at a health of {bv.get('health_now')}, which puts "
                                    f"{_fmt_money(at_risk)}" + (f" of its {_fmt_money(rev)} revenue" if rev is not None else '')
                                    + ' at risk, on an assumed loss share for that band.', 'cites': [p1_id]})
            if per_pt is not None:
                out.append({'text': f"One health point on this {one} is worth {_fmt_money(per_pt)} a year (assumed).", 'cites': [p1_id]})
    if ic_id and (wants_cost or not out):
        ic = ctx.citable[ic_id]
        pbs = [_investment_playbook_compact(p) for p in ic.get('playbooks') or []]
        def cv(p, k):
            return _mv(p.get(k))
        costed = sorted([p for p in pbs if cv(p, 'cost_per_health_point') is not None], key=lambda p: cv(p, 'cost_per_health_point'))
        uncosted = [p for p in pbs if cv(p, 'cost_per_health_point') is None]
        if costed:
            best = costed[0]
            out.append({'text': f"{best['playbook_id']} gives the most health-point lift per dollar: {_fmt_money(cv(best, 'cost_per_health_point'))} "
                                f"per health point ({best.get('lift_source')} lift of {best.get('lift_value')} points).", 'cites': [ic_id]})
        priced = [p for p in costed + uncosted if cv(p, 'estimated_cost_per_execution') is not None]
        if priced:
            listed = '; '.join(f"{p['playbook_id']} {_fmt_money(cv(p, 'estimated_cost_per_execution'))} per run"
                               + (f" and {_fmt_money(cv(p, 'cost_per_health_point'))} per health point" if cv(p, 'cost_per_health_point') is not None else '')
                               for p in priced)
            hourly = cv(ic, 'csm_hourly_cost')
            out.append({'text': 'Estimated cost of each playbook' + (f', at {_fmt_money(hourly)} an hour' if hourly is not None else '')
                                + f' (assumed figures), best value first: {listed}.', 'cites': [ic_id]})
    return out or None


def _stub_answer(question: str, scope: str, ctx: Context, narrative: Optional[dict], rows: List[dict],
                 history: Optional[List[dict]] = None, words: Tuple[str, str] = ('account', 'accounts')) -> dict:
    """Deterministic: the narrative sentences (or portfolio rows) that share
    the most keywords with the question, already carrying their citations."""
    n = settings.get('answer', 'stub_max_sentences')
    weights = _stub_keyword_weights(question, history)
    sentences: List[dict] = []
    if detect_investment_intent(question):
        money = _stub_money_answer(question, ctx, words)
        if money:
            return {'answer_sentences': money[:n], 'confidence': settings.get('answer', 'stub_confidence'),
                    'evidence_gaps': ['no model: answered from the money figures by fixed rules, not by reading the question in full']}
        if ctx.notices:
            # The question asked for money and the data has none: say only that (ask() leads with the
            # notice). Reciting the nearest-matching story sentences here is the bug this replaces.
            return {'answer_sentences': [], 'confidence': None,
                    'evidence_gaps': ['no model: the question asks for money figures this tenant does not have']}
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
    if account_id and scope == 'account' and any(p in question.lower() for p in settings.get('scope', 'portfolio_phrases')):
        # The picked account wins over the wording — but silently answering "across the portfolio…"
        # for one account reads as a wrong answer. Say which it was and how to get the other.
        ctx.notices.insert(0, f"This was answered for {meta.get('account_name')} only, because that account was picked; the question "
                              f"reads as portfolio-wide. Ask it with no account picked to cover the whole portfolio.")
        meta['scope_mismatch'] = 'portfolio_wording_account_picked'

    max_sentences = settings.get('answer', 'max_sentences')
    system = SYSTEM_PROMPT.format(max_sentences=max_sentences)
    user = USER_PROMPT.format(history_block=history_block(turns), question=question, scope_line=scope_line, context=ctx.text())

    from utils import model_provider
    if model_provider.available():
        payload, model = _call_model(customer_id, system, user)
    else:
        payload, model = _stub_answer(question, scope, ctx, narrative, rows, turns, _account_words(customer_id)), STUB_MODEL

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
    notices = list(ctx.notices)
    answer = ' '.join(notices + [s['text'] for s in sentences])
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
        'notices': notices,                    # plain statements that lead the answer; about the data, not from it
        'citations': {c: ctx.citable[c] for c in cited_ids},
        'unsupported': unsupported,
        'evidence_gaps': list(dict.fromkeys(gaps + model_gaps)),
        'confidence': confidence,
        'citation_rule': CITATION_RULE,
        'context_chars': ctx.used,
        'history_turns': len(turns),          # counted separately from context_chars: the recap is not a context block
        'model': model, 'generator': GENERATOR,
        'model_label': STUB_LABEL if model == STUB_MODEL else model,
    }
