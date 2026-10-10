# AI-usage log

## Tools

| Tool | Used for |
|---|---|
| Claude Code (Claude Opus 5.5, CLI) | Pair-programming: environment setup, code under my direction, review of its own output |
| Anthropic API (`panelpilot/extract.py`) | Offline rule compilation only (guideline sections → candidate rules). Never called at verdict time |

## Prompts that mattered (paraphrased)

1. "List every requirement and deliverable in the brief, and separate what must be decided from what must be built."
2. "Add FHIR input with a short timebox and test it on a sample before trusting it: the same patients
   through FHIR and CSV must give the same verdicts." → `panelpilot/fhir.py`; 16,089/16,089 identical on 2,000 patients.
3. "Fix what the review found: verified closures that count aged-out patients, stale-feed handling for
   latest values, re-sends with reformatted dates, and unauthenticated rule approval."

## Where the AI was wrong, and how it was caught

1. **A fabricated rule passed the first quote check.** "Screen adults 40–49" quoted a real incidence sentence
   from the same page. Caught by a deliberate fabrication test; fixed with section scoping and number grounding.
2. **Silent pass→fail flips on messy data.** 9 BP verdicts flipped because US-format dates broke reading pairing.
   Caught by the clean-vs-messy comparison (`make dq`); now 0 flips.
3. **The evaluator inflated recall.** It paired "syphilis screening" with "youth tobacco prevention".
   Caught by reading the matched pairs; fixed by matching on what each criterion needs.
4. **"Handled" could almost never be verified.** One visit had to close every gap on the row.
   Caught while rehearsing the demo; verification is now per gap.

## Token spend

| Item | Tokens | USD |
|---|---|---|
| Rule extraction, 97 calls, Opus 5.5 (`ai_usage/token_log.csv`) | 94.1K in + 227.3K out + 376.8K cache-read + 3.9K cache-write | **$5.02** |
