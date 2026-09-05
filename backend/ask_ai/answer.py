"""
Ask AI over the journey contract (P10) — the answer engine.

    ask(customer_id, question, account_id=None, as_of=None) -> dict

Flow
  1. scope    one account (account_id given, or an account named in the
              question) or the portfolio (portfolio phrasing, or no match).
  2. gather   ONLY from journeys.read: get_journey (journey + evidence index
              + narrative) for an account, list_journeys rows for the
              portfolio, get_evidence for a taxonomy role the question names.
              A char budget (config) decides what the model is shown; only
              ids actually shown are citable.
  3. model    one forced tool call, `answer_with_citations`, metered through
              utils.llm_budget_controller.record_usage. Without
              ANTHROPIC_API_KEY a deterministic stub answers from the
              narrative block (model 'stub_narrative_v1').
  4. validate every sentence must cite ids that resolve to the context it
              was given (episode ids, evidence node ids, 'row:<account_id>');
              a sentence with no citation, or with one that does not
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
CITATION_RULE = ('every sentence cites >=1 id shown to the model (episode id, evidence node id, or row:<account_id>); '
                 'a sentence with no citation or an unresolved citation is dropped and listed under unsupported')

SYSTEM_PROMPT = """You are Ask AI for a B2B Customer Success platform. You answer questions about one account's journey, or a portfolio of accounts, using ONLY the context blocks in the user message. The blocks come from the platform's evidence read layer: a cited narrative, a journey (arc, phases, leading-vs-trailing series), its episodes, the evidence index behind them, and — for portfolio questions — one row per account.

RULES (the product contract; the validator enforces them after you answer)
1. Every sentence cites. Each answer sentence lists the ids it was built from: episode ids (sig:N, out:N, dec:N, hs:N, renewal), evidence node ids (the bare node_id), or row:<account_id>. Cite only ids that appear in the blocks. A sentence that cannot cite is not written.
2. Numbers are read, never computed. Quote scores, dates, counts, revenue and lead days exactly as they appear in the cited blocks. Do not add, average, subtract or estimate. If the answer needs a number the blocks do not contain, say that it is not in the evidence.
3. "Why" states its evidence tier. When you explain a cause, say what kind of evidence backs it: observed evidence (a quote from a named source, its evidence_tier, confidence, whether it still requires review) versus system-derived facts (health transitions, arc hypotheses with their confidence semantics). Rejected or unreviewed evidence is said to be so.
4. Absence of evidence is an answer. If the blocks do not support a claim, say so plainly and list what is missing under evidence_gaps (e.g. no outcome recorded after the renewal date, no evidence since a month, an arc left unclassified). Never fill a gap with general knowledge or a plausible story.
5. Never invent: no people, quotes, dates, causes, playbooks or outcomes beyond the blocks. Do not speculate about what the customer "probably" feels.
6. Time travel. If the context is marked as_of, answer as of that instant only — nothing later exists.
7. Portfolio questions aggregate the same objects: every account you mention cites its row:<account_id>; rank or compare only on values present in the rows.

Write plain, specific sentences. Prefer the narrative's own wording when it already says the thing. Keep to at most {max_sentences} sentences. confidence (0-1) is your confidence that the kept sentences answer the question from the cited evidence — low when the evidence is thin or unreviewed."""

USER_PROMPT = """QUESTION: {question}

SCOPE: {scope_line}

CONTEXT BLOCKS (the only evidence you may use; cite the ids exactly as written):
{context}"""

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

def decide_scope(customer_id: int, question: str, account_id: Optional[int], rows: List[dict]) -> Tuple[str, Optional[int]]:
    """('account', id) or ('portfolio', None). An explicit account_id wins;
    then portfolio phrasing; then an account name from the portfolio found
    in the question; otherwise portfolio."""
    if account_id:
        return 'account', int(account_id)
    q = (question or '').lower()
    if any(p in q for p in settings.get('scope', 'portfolio_phrases')):
        return 'portfolio', None
    for r in sorted(rows, key=lambda r: -len(r.get('account_name') or '')):     # longest name first: 'Acme Cloud' before 'Acme'
        name = (r.get('account_name') or '').strip().lower()
        if name and name in q:
            return 'account', int(r['account_id'])
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
                                  'lead_days', 'episodes', 'open_review_count')}


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
    ctx.add('journey', {
        'account_id': j.get('account_id'), 'account_name': j.get('account_name'), 'vertical': j.get('vertical'),
        'as_of': j.get('as_of'), 'last_scored_month': j.get('last_scored_month'), 'live_months': j.get('live_months'),
        'last_evidence_at': j.get('last_evidence_at'), 'state': j.get('state'), 'current_phase': j.get('current_phase'),
        'arc': j.get('arc'), 'summary': j.get('summary'), 'open_review_count': j.get('open_review_count'),
        'phases': j.get('phases'), 'counterfactual_hooks': j.get('counterfactual_hooks'),
        'leading_vs_trailing': {k: v for k, v in lvt.items() if k != 'series'} | {'series': series},
    })
    if row:
        ctx.add('row', _row_compact(row), cite_id=f"row:{row['account_id']}", citation=row)

    narrative = j.get('narrative') or {}
    chapters = [{'phase': ch.get('phase'), 'from': ch.get('from'), 'to': ch.get('to'),
                 'sentences': [{'text': s['text'], 'cites': s['cites']} for s in ch.get('sentences') or []]}
                for ch in narrative.get('chapters') or []]
    ctx.add('narrative', {'citation_rule': narrative.get('citation_rule'), 'chapters': chapters, 'omitted': narrative.get('omitted')})
    gaps.extend(_narrative_gaps(narrative))

    episodes = sorted(j.get('episodes') or [], key=lambda e: str(e.get('date') or ''), reverse=True)
    for e in episodes:
        if not ctx.add('episode', _episode_compact(e, quote_chars), cite_id=e['episode_id'], citation=e):
            break
    for nid, v in (j.get('evidence') or {}).items():
        if not ctx.add('evidence', _evidence_compact(v, quote_chars), cite_id=str(nid), citation=v):
            break

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


def portfolio_context(rows: List[dict], as_of: Optional[datetime]) -> Tuple[Context, List[str], dict]:
    ctx = Context(settings.get('context', 'max_chars'))
    gaps = []
    cap = settings.get('context', 'portfolio_max_rows')
    ctx.add('portfolio', {'accounts': len(rows), 'shown': min(len(rows), cap),
                          'note': 'one row per account, computed from cited evidence; latest = last month of the leading-vs-trailing series'})
    for r in rows[:cap]:
        if not ctx.add('row', _row_compact(r), cite_id=f"row:{r['account_id']}", citation=r):
            break
    if len(rows) > cap:
        gaps.append(f'portfolio has {len(rows)} accounts; only the first {cap} rows (by name) were shown')
    if not rows:
        gaps.append('no journeys for this customer yet — run process_data')
    if as_of:
        gaps.append('as_of applies to one account\'s journey; portfolio rows are as of their last build')
    if ctx.truncated:
        gaps.append('context budget reached; not every row was shown to the model')
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
                                  'description': 'ids from the context: episode ids (sig:N, out:N, dec:N, hs:N, renewal), evidence node ids, or row:<account_id>'},
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

def _stub_answer(question: str, scope: str, ctx: Context, narrative: Optional[dict], rows: List[dict]) -> dict:
    """Deterministic: the narrative sentences (or portfolio rows) that share
    the most keywords with the question, already carrying their citations."""
    n = settings.get('answer', 'stub_max_sentences')
    kws = _keywords(question)
    sentences: List[dict] = []
    if scope == 'account':
        pool = [{'text': s['text'], 'cites': list(s['cites'])}
                for ch in (narrative or {}).get('chapters') or [] for s in ch.get('sentences') or []]
        scored = [(sum(1 for k in kws if k in s['text'].lower()), i, s) for i, s in enumerate(pool)]
        hits = sorted([t for t in scored if t[0] > 0], key=lambda t: (-t[0], t[1]))
        chosen = [t[2] for t in hits[:n]] or [s for s in pool[:n]]
        sentences = sorted(chosen, key=lambda s: pool.index(s))
        gaps = ['stub: keyword match over the narrative block, no model reading'] if pool else ['the narrative block has no sentences to answer from']
    else:
        def rank(r):
            latest = r.get('latest') or {}
            hit = sum(1 for k in kws if k in json.dumps(_row_compact(r), default=str).lower())
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

def ask(customer_id: int, question: str, account_id: Optional[int] = None, as_of=None) -> dict:
    question = (question or '').strip()
    if not question:
        raise ValueError('question is required')
    when = _parse_as_of(as_of)
    from journeys.read import list_journeys
    rows = list_journeys(int(customer_id))
    scope, aid = decide_scope(int(customer_id), question, account_id, rows)

    narrative = None
    if scope == 'account':
        row = next((r for r in rows if int(r['account_id']) == aid), None)
        ctx, gaps, meta, narrative = account_context(customer_id, aid, question, when, row)
        scope_line = f"one account — {meta.get('account_name')} (account_id {aid})" + (f", as of {meta['as_of']}" if when else '')
    else:
        ctx, gaps, meta = portfolio_context(rows, when)
        scope_line = f"portfolio — {meta['shown']} of {meta['accounts']} accounts shown"

    max_sentences = settings.get('answer', 'max_sentences')
    system = SYSTEM_PROMPT.format(max_sentences=max_sentences)
    user = USER_PROMPT.format(question=question, scope_line=scope_line, context=ctx.text())

    if os.environ.get('ANTHROPIC_API_KEY'):
        payload, model = _call_model(customer_id, system, user)
    else:
        payload, model = _stub_answer(question, scope, ctx, narrative, rows), STUB_MODEL

    sentences, unsupported = validate_answer(payload, ctx.citable, max_sentences)
    cited_ids = list(dict.fromkeys(c for s in sentences for c in s['cites']))
    model_gaps = [str(g) for g in (payload.get('evidence_gaps') or []) if str(g).strip()]
    try:
        confidence = max(0.0, min(1.0, float(payload.get('confidence'))))
    except (TypeError, ValueError):
        confidence = None
    return {
        'question': question, 'scope': scope, 'scope_detail': meta,
        'answer': ' '.join(s['text'] for s in sentences),
        'sentences': sentences,
        'citations': {c: ctx.citable[c] for c in cited_ids},
        'unsupported': unsupported,
        'evidence_gaps': list(dict.fromkeys(gaps + model_gaps)),
        'confidence': confidence,
        'citation_rule': CITATION_RULE,
        'context_chars': ctx.used,
        'model': model, 'generator': GENERATOR,
    }
