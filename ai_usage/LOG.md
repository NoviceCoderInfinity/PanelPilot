# AI-usage log

Required by the brief: tools, what for, 2–3 prompts that mattered, one place the AI was wrong and was caught,
total token spend. Entries are factual; keep adding as work happens. **Owner fills/edits the bracketed parts.**

## Tools

| Tool | Used for |
|---|---|
| Claude Code (Claude Opus 5.5, CLI) | Environment setup (Java, Synthea, Python venv), downloading sources, writing the messifier, source extraction, rule verifier, extractor, draft gold set, evaluator; code review of its own output |
| Anthropic API (via `panelpilot/extract.py`) | Offline rule compilation only: guideline sections → candidate rules. Never called at patient-verdict time. Spend logged per call in `ai_usage/token_log.csv` |
| [add others: e.g. ChatGPT / Copilot / none] | |

## What AI did vs. what was decided by a human

- Setup and data (2026-10-09): Claude Code installed OpenJDK 21, downloaded Synthea, generated 10,000 living
  (+1,278 deceased) patients for Corpus Christi, TX with seed 42, CSV + FHIR.
- Data realism: Claude Code wrote `panelpilot/messify.py` (deterministic defect injection + ground-truth log).
- Knowledge side: Claude Code wrote `panelpilot/sources.py`, `panelpilot/extract.py`, the draft `gold/` set and
  `panelpilot/eval_extraction.py`. **gold/ is AI-authored and pending human review** (see gold/README.md).
- [Human decisions: measurement-year handling, scope cuts, which hard questions to go deep on, ...]

## Prompts that mattered (2–3)

1. [The prompt that set the architecture constraint, e.g. "AI compiles; rules execute — no model call
   decides a verdict"; paste the actual wording you used]
2. The extractor system prompt (`panelpilot/extract.py`, `SYSTEM`): requires a character-for-character quote
   per criterion, `uncomputable` instead of approximation, and honest `coverage_notes`. [Record what changed
   between versions and why, once real runs happen.]
3. [one more]

## Where the AI was wrong and it was caught

1. **Synthea default history would have silently broken a measure.** Synthea's default
   `exporter.years_of_history=10`, run in Oct 2026, drops records before ~Oct 2016 — but CRC-SCREEN counts
   colonoscopies from 2016-01-01. Caught before generation; run with `--exporter.years_of_history=20`.
   (Caught by the AI itself while reading the measure spec — note honestly whether you count this.)
2. **The AI's own validation check was wrong.** While validating the messifier, an `awk` field-split check
   reported a duplicate-encounter Id that matched *every* line and odd HbA1c/unit pairings. Re-verifying
   with DuckDB showed the data was correct and the check was broken (wrong column index → empty grep
   pattern). Lesson: verify the verifier.
3. **Quote verification alone does not stop a fabricated rule.** A deliberately fabricated candidate
   ("screen adults aged 40 to 49") passed `rules.verify()` because that exact phrase exists in the 2021 USPSTF
   page — in an epidemiology sentence about incidence, not a recommendation. Fix (now in `rules.verify()`,
   so it binds hand-written rules too): every quote must lie inside the cited section AND the hash-checked
   document; every age/threshold/count must appear in its criterion's own quote, or be declared under
   `derived` with reasoning — such rules verify but do not run until a human approves their exact content
   hash (`rules/approvals.json`). The fabricated 40–49 rule is now rejected; 5 of 19 draft gold rules (CDC,
   derived ages such as MMR "age 4–6 years" → expected by 7) correctly sit in "awaiting approval".
4. **Engine design flaws surfaced by running gold rules through it.** (a) A rule written as "observation"
   for HCV testing crashed because Synthea stores it as a procedure — fixed by routing on the value set, not
   the rule's wording. (b) USPSTF colorectal rules became "not computable" because one alternative (CT
   colonography) is absent from the data — fixed: an uncapturable alternative inside `any` is skipped with a
   recorded caveat. (c) First messy-data run showed 9 BP verdicts silently flipping pass→fail (seconds lost in
   US-format dates broke systolic/diastolic pairing; undated readings dropped) — fixed by pairing on
   encounter + minute and recovering dates from the linked encounter. `dq_eval` now reports silent_flips = 0.

5. **A misleading reopen reason.** In the first verify-or-reopen run, a patient whose BP check *did* happen
   (152/96, still above goal) was reopened as "no qualifying record". That would have sent the care manager to
   call someone who had already come in. Fixed: the reason now separates "nothing happened" (call again) from
   "care happened, goal unmet" (PCP follow-up), and a "Sent to PCP" outcome exists.
6. **Time travel broke the demo design.** Simply moving the clock forward a month made every feed look dead,
   because the export ends on the generation date, and that emptied the worklist. Fixed with a replay mode
   (records dated after as-of "haven't arrived yet"; original line numbers kept), rather than faking a healthy feed.
7. **A FAIL had no proof.** `explain` showed records for passes but nothing for "not met". The walkthrough asks
   to prove verdicts from raw data, and a fail needs proof of absence. Added a `grep` absence check per criterion.

8. **The evaluator itself was wrong.** The first scoring run reported 100% USPSTF recall, but it had matched gold
   "syphilis screening" to compiled "youth tobacco prevention", because both rules are mostly `uncomputable`
   and the matcher compared shapes only. Fixed by matching uncomputable criteria on what they need, and by
   requiring the same section for one-recommendation-per-section documents.
9. **Engine crashed on 58 of 100 compiled rules** in the first impact preview. The LLM put `derived` notes on
   criteria, and the engine's type detection didn't exclude that key. This was caught by previewing every rule
   before anyone could approve it, not in production.
10. **Approving 30 rules broke the worklist's priorities.** Right after approval, the top patients carried 9
   "gaps" each. The old and new colorectal rules counted twice, and routine screening questions (alcohol,
   drug use, anxiety) stacked +20 points each, outranking clinical severity. Fixed: rules that close with the same
   care merge into one need (shared numerator concept), bundling is capped at +20, and the sentence names
   at most 4 short needs. Lesson: every approval changes the product, not just the rule count.
11. **The AI under-read a footnote it had quoted correctly.** The Hib rule quoted "PedvaxHIB: 3-dose series" but
   marked it uncomputable, so every 2-year-old failed (146/146) in the impact preview. CVX 49 *is* PedvaxHIB
   (PRP-OMP). Fixed by a human edit: a `hib_pedvax_vaccine` value set and a computable 3-dose alternative.
   Result: 146 pass, 0 fail. Approval is per content hash, so the edited rule needed fresh approval.
12. **A CSV-reader bug that only showed up on a bigger sample.** DuckDB guessed the quote style from the
   first rows; in a 2,000-patient FHIR conversion, a later description containing a comma ("Cystic fibrosis,
   prenatal detection") broke parsing. On a different export (e.g. the walkthrough's), the same thing could
   have happened. Fixed by forcing standard CSV quoting in `ingest.py`. Verdicts on the main panel didn't change.
13. **"Handled" could almost never be verified.** Logging *Scheduled* attached every gap on the patient's row
   (4–9 once the screening rules were live), and the item verified only if all of them passed, so a visit that
   closed 4 of 6 gaps got no credit and reopened in full. Caught while scripting the demo's verify cycle.
   Verification is now per gap, with credit kept for verified gaps and only open gaps reopening; records dated
   before the contact and patients who left the denominator are "closed without credit".
14. **A variable-shadowing bug made Today show 449 calls.** The guideline-update banner loop reused `n`, the
   list-size parameter. Caught when an API check returned 449 rows for `?n=20`.

## Token spend

| Item | Tokens | USD |
|---|---|---|
| Rule extraction (`ai_usage/token_log.csv`, 97 calls, Opus 5.5) | 94.1K input + 227.3K output + 376.8K cache-read + 3.9K cache-write | **$5.02** |
| Dry-run estimate, all 97 sections, Opus 5.5 | ~51K in + ~291K out (est.) | ~$6.15 (est.) |
| Claude Code session usage | [from /cost or account usage] | [fill] |
| **Total** | | **[fill]** |
