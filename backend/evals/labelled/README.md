# Labelled extraction runs (the extraction eval set, seeded)

One file pair per generator run under a real model: `<manifest>.<model>.jsonl`
(one row per communication: text, source_type, expected_subtypes,
extracted_subtypes, model_version) and its `.scorecard.json` (per-communication
exact/partial/miss, per-role and per-subtype precision/recall, unclassified).

Produced by `demo/generate.py --extractor model` (and by `scripts/seed_demo.py`
on a box with ANTHROPIC_API_KEY). Text is synthetic demo content authored in
each vertical's taxonomy vocabulary — a check of the extractor against the
author's labels, NOT a customer's data and NOT a measure on real communications.
Add a design partner's labelled communications here when they exist.
