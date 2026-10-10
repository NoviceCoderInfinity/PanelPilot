# Gold rules

Reference rules written directly from the source text in `sources/text/`, independently of the extractor,
used by `eval_extraction.py` to score compiled rules. A gold file counts as evidence once it is signed off;
until every file is, read `eval_extraction.py` output as agreement with a reference, not certified accuracy.

Sign-off, per file: open the `source.quote` and each criterion `quote` in the source, confirm the
criterion says what the text says (ages, sex, concept, interval, threshold), read `gold_notes` for the
interpretation choices, then add `"reviewed_by": "<name>", "reviewed_on": "<date>"` to the JSON.

Composition (19 rules, chosen to be deliberately mixed, not representative):
- USPSTF A&B (7): AAA (risk-based but computable), breast, cervical 21–29, chlamydia (uncomputable risk),
  hepatitis C, lung LDCT (partly uncomputable), syphilis (entirely uncomputable)
- USPSTF colorectal pages (3): 2021 45–49, 2021 50–75, 2016 50–75 (the 50→45 update pair)
- CDC 2025 child schedule (6): MMR, HPV routine, HPV immunocompromised (footnote), MenACWY routine,
  MenACWY high-risk (footnote), Tdap
- Bright Futures (3): footnote-derived HIV, hearing, hepatitis C. The periodicity *grid* (which visit gets
  which service) is not recoverable from PDF text — dots lose their column — so no grid rules are gold.

Known representation limits recorded in `gold_notes`: age-at-first-dose dependent dose counts (HPV,
MenACWY catch-up), age-anchored windows ("once between 15 and 21"), "every 1 to 3 y" ranges, derived ages
(MMR window closes at 6 → check from 7). `_authoring.py` is the script used to write these files.
