"""
Ask AI over the journey contract (backlog P10).

A question interface over journey v3 — not over raw tables. The model is
shown the narrative block, the compact journey, its episodes and evidence
index (one account) or the portfolio rows (many accounts), and must
answer in sentences that each cite ids from that context. The validator
drops every sentence whose citations do not resolve; the read layer
supplies every number.

    from ask_ai.answer import ask
    ask(customer_id, 'why did health fall in March?', account_id=12)
"""
