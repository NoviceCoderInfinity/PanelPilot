# PanelPilot

The rulebooks go in; a trustworthy, prioritized, **verified** worklist comes out.

For one care manager at a ~10,000-patient family practice (Corpus Christi, TX) with two hours a day for
outreach. Preventive-care rules (USPSTF A/B, CDC child & adolescent immunization schedule, AAP Bright Futures)
are compiled by an LLM into rules that **cannot exist without a verbatim source quote**, then executed
deterministically, with no model calls, over a deliberately messy EHR export. The output is 20 calls for
today, each with a one-sentence reason. A gap closes only when a record proves the care happened.

All patient data is synthetic (Synthea). No real patient data is used anywhere.

---

## Setup (3 commands)

```bash
make setup     # uv + Python 3.12 venv, pinned deps, OpenJDK 21 (Homebrew), Synthea jar
make data      # 10,000 seeded Synthea patients -> messy export + defect log -> DuckDB -> first nightly run (~8 min)
make run       # http://127.0.0.1:8000   (API docs: http://127.0.0.1:8000/docs)
```

Requirements: macOS with Homebrew, about 15 GB of free disk, and about 10 minutes on the first run.
Rule extraction is the only step that calls an LLM. It needs `ANTHROPIC_API_KEY`, set in the environment
or in a git-ignored `.env`. The rules already compiled and approved are in the repo, so the app runs without a key.

Other targets: `make test` (trust guarantees, data handling, verification), `make dq` (clean-vs-messy honesty check),
`make extract-dry` / `make extract` / `make eval` (compile rulebooks and score them against gold),
`make demo-reset` (replay clock at 2026-08-01), `make scale` (measured cost at 100k),
`make fhir-import FHIR=<dir>` (run the panel from FHIR R4 bundles), `make fhir-parity` (FHIR vs CSV verdicts).

---

## Architecture (one page)

```
 sources/  (USPSTF html, CDC pdf, Bright Futures pdf, assignment pdf; sha256 in MANIFEST.csv)
    │ sources.py: deterministic text + 97 sections
    ▼
 extract.py ── LLM, offline, per section, cached prompt, $ cap ──► candidate rules (JSON)
    │
    ▼  rules.verify()  ← the ONLY constructor of VerifiedRule
    │   1 source text hash matches manifest      3 quotes inside the cited section
    │   2 every quote verbatim in the document   4 every number stated in its own quote
    │                                              (or declared "derived" → human approval)
    ├─► rules/rejected/  (kept with reason: hallucination-defense evidence)
    └─► rules/compiled/  → human approves exact content hash (UI or CLI) → runnable
                                                     │
 Synthea CSV ─ messify.py ─► messy export            │       rules/calibration/ (the 4 spec measures)
                 (+ _defects.csv ground truth)       │            │
                         │                           ▼            ▼
                         └─► nightly.py: ingest.py ─► engine.py (SQL, 3-valued logic) ─► verdicts + evidence
                               raw rows + line no.     pass | fail | indeterminate(+why) | excluded
                               normalize / quarantine         │
                               dedupe / feed health           ├─► rule_update.py: newly eligible after a guideline change
                                                              ├─► ops.py: verify or reopen every "handled" item
                                                              └─► worklist.py: 1 row per patient, score + one-sentence why
                                                                       │
                                     app.py (FastAPI)  ◄───────────────┘   panel.duckdb (atomic swap) + ops.sqlite
                                     /api/* JSON + OpenAPI  ──►  web UI (Today, Follow-ups, Rules & trust, Data health, Outcomes)
```

**AI compiles; rules execute.** Only `extract.py` calls a model, and nothing it produces runs until
`verify()` accepts it and a human approves it. Verdicts are SQL over DuckDB: the same input always gives the same output.

---

## Rule representation, and why

A rule is a small JSON tree. Every leaf criterion carries a `quote`:

```json
{"id": "BP-CTRL", "source": {"doc_id": "assignment", "quote": "Adults 18–75 with a hypertension diagnosis dated before 2025-06-30"},
 "denominator": {"all": [
    {"age": {"min": 18, "max": 75, "at": "end"}, "quote": "Adults 18–75"},
    {"condition": {"concept": "hypertension", "onset_before": "{MY}-06-30"}, "quote": "with a hypertension diagnosis dated before 2025-06-30"}]},
 "numerator": {"bp": {"window": ["{MY}-01-01", "{MY}-12-31"], "systolic_lt": 140, "diastolic_lt": 90},
               "quote": "Most recent 2025 blood-pressure reading < 140/90 (both numbers)"},
 "exclusions": [{"deceased": {"before": "{MY}-01-01"}, "quote": "deceased-before-2025 excluded everywhere"}]}
```

- **Leaf types:**
  - `age`, `sex`, `condition`, `observation` (any/latest, value threshold), `bp` (latest paired reading),
    `procedure`, `encounter`, `immunization` (min dose count), `deceased`;
  - `uncomputable`, which captures a criterion the data can't support (sexual history, family history)
    instead of dropping it;
  - the combinators `all`, `any` and `not`.
- **Why this format:** small enough that the verifier can check every leaf, expressive enough for the
  four measures and most A/B screening rules, and it compiles to SQL, so verdicts are inspectable.
  Dates are relative to the measurement year (`{MY-9}-01-01`), so rules don't go stale.
- **Concepts, not codes.** Rules say *what* (`colonoscopy`). `panelpilot/valuesets.json` says how the EHR codes it.
  That file is human-owned and checked against the data: 59/91 concepts are mapped, and 32 are declared not
  computable with a reason. The LLM never writes codes.
- **Three-valued logic.** A criterion is TRUE, FALSE, or **NULL** when a quarantined record or a dead feed
  *could* change it. NULL propagates, and the patient is **indeterminate, with a reason**, never a silent gap.
- **What it can't express (known):**
  - dose schedules that depend on age at the first dose (HPV 2 vs 3 doses);
  - "once between ages X and Y" windows tied to birthdays;
  - "currently pregnant" (needs active-on-date semantics);
  - the Bright Futures grid cell-by-cell, because its PDF text loses the column positions.

---

## Trust: how a rule reaches a patient

| Gate | Enforced by | Tested in |
|---|---|---|
| Quote verbatim in a hash-checked source | `rules.verify()` | `tests/test_trust.py` |
| Quote inside the **cited section**, not borrowed from elsewhere | `rules.verify()` | the 40–49 fabrication test |
| Every number appears in its own quote, or is declared `derived` | `rules.verify()` | number tests |
| LLM output / derived numbers need human sign-off on the exact content hash | `VerifiedRule.approved` | approval test |
| Engine accepts only `VerifiedRule` | `engine.run_rule` | type test |

**Why section scope and number grounding.** "The quote exists somewhere in the document" is not enough: a
fabricated "screen adults 40–49" rule can quote a real incidence sentence from the same USPSTF page. Requiring
the quote inside the cited recommendation section, and every number inside its own quote, rejects it
(`test_real_sentence_from_elsewhere_cannot_justify_a_rule`).

**What structure can't catch:** a correct quote mapped to the wrong concept. That's why LLM rules
also need human approval and are scored against gold.

## Evidence of extraction quality

Method:
1. `extract.py` compiles each of the 97 sections with a cached system prompt and structured output.
2. Every candidate goes through `verify()`.
3. `eval_extraction.py` scores the compiled rules against a gold set: rule recall and precision, plus
   criterion-level agreement on type, concept, ages, windows and thresholds. It also reports rejections by reason.

**Results** (Opus 5.5, 97 sections, **$5.02 total**, 97 calls; the prompt cache was hit on every call after the first):

| | |
|---|---|
| Candidates produced | 102 |
| Rejected by the verifier | 2. One used an age (18) not stated in its quote; one invented a criterion type. Kept in `rules/rejected/` with the reason. |
| Verified | 100, all approved (each shown with its impact preview at approval); 99 live (the 2016 colorectal rule is retired, superseded by 2021). Duplicates merge into one need on the worklist; low-coverage rules stay off outreach. Of the 100: 39 compute on this panel, 53 are honestly not computable (each with the reason, e.g. "needs sexual history", "'currently pregnant' needs end-dates"), and 8 are duplicates (the CDC PDF is extracted per page *and* per footnote block). |
| Rule recall vs gold | **79%** (15/19). USPSTF 7/7, colorectal 3/3, CDC 5/6, Bright Futures 0/3 (the PDF grid problem). |
| Field agreement on matched rules | concept 100% · sex 100% · counts/thresholds 97% · criterion type 92% · ages 79% · **time windows 46%** |

What this says: the model is reliable on *what* and *who*, and unreliable on *when* (screening intervals, dose
timing). So every interval interpretation is declared as `derived` and shown at approval, next to an
**impact preview** (denominator, pass, fail and duplicates, computed on a scratch copy of the panel). The preview
is what surfaces a rule where nobody passes: in the Hib rule, the PedvaxHIB 3-dose footnote was captured but
marked uncomputable, although CVX 49 *is* that product, so `hib_pedvax_vaccine` was mapped and the rule edited
before approval (146/146 pass). It also flags simulator artifacts such as vision screening (1/428 passing),
which is kept off outreach as low-coverage.

**Gold-set status:** the 19 gold rules were drafted from the source text independently of the extractor and
are signed off file by file (`reviewed_by` in each JSON; procedure in `gold/README.md`). Until every file is
signed off, read these numbers as agreement with a reference, not certified accuracy. The evaluator matches
`uncomputable` criteria on *what they need*, so two mostly-uncomputable rules (e.g. syphilis screening and
youth tobacco prevention) never pair up by shape alone.

---

## Messy data: what the system does, and never does

The `messify.py` script injects seeded defects into the Synthea export and logs every one in `_defects.csv`:
missing units, units inside the value, HbA1c in mmol/mol, missing dates, US-format dates, duplicates
(including visits re-sent under a new ID), and a 21-day lab outage.

| Defect | Does | Never does |
|---|---|---|
| Lab value without a unit | Uses it to prove *a test happened*; marks the value unusable, so a control verdict becomes indeterminate | Guesses the unit (a "5.7" is never assumed to be %) |
| mmol/mol HbA1c | Converts with the stated formula, logged | Converts an unlabeled value |
| Missing date | Inherits the date of the linked visit; a visit without START takes its STOP (logged); otherwise quarantined | Counts an undated record as "in 2025" |
| Duplicate / re-sent visit | Collapses it, first copy wins, logged. Dates compare by parsed value, so a re-send with a reformatted date is still a duplicate | Deletes silently or double-counts |
| Feed silent (21 days, ~670 labs/day normally) | Marks the feed **STALE**; affected patients are "can't tell", not gaps, including "latest value" rules whose last value on file fails (a newer result may be missing); verification is held | Shows "nothing changed" or 200 false gaps |

**Honesty check (`make dq`):** the same rules run on the clean and the messy panel. **silent_flips = 0**:
no verdict flips pass↔fail. 99.7% are identical, and the rest became *indeterminate* with a reason.
Every verdict can be proven from the export itself: `python -m panelpilot.explain data/panel.duckdb BP-CTRL --random 5 --my 2025`
prints each criterion, its source quote and `sed -n '<line>p' <file>` for every record.

**FHIR input.** `panelpilot/fhir.py` converts FHIR R4 bundles into the same six tables, so every rule, proof
and screen works unchanged (`make fhir-import FHIR=<dir>`); each evidence line ends with the source bundle file
and resource id. Parity (`make fhir-parity`): 2,000 seeded patients converted from FHIR and the same 2,000 cut
from the CSV export, both run through all 44 computable rules: **16,089 / 16,089 verdicts identical**
(`data/fhir_parity/parity.json`). This needs Synthea's FHIR export, which `make data` skips to save 60 GB
(`--exporter.fhir.export=true`). Ingest reads every file with explicit RFC 4180 quoting rather than sniffing the
format from the first rows, so a quoted comma deep in a large export can't change how the file is parsed.

---

## Nightly: what recomputes, and the measured cost

`nightly.py` does a **full recompute**, on purpose. It ingests the export, re-runs every runnable rule (the
current year plus the 2025 calibration year), computes newly eligible patients for superseding rules,
verifies or reopens every pending outreach item, rebuilds the worklist, and **atomically swaps** the
panel file. Full recompute means a late record, a new rule version or a fixed value set all propagate
without cache-invalidation logic.

| Panel | Patients | Observations | Ingest | Rules | Worklist | Total | DB size |
|---|---|---|---|---|---|---|---|
| 10k (`make scale`, x1) | 11,278 | 11.9M | 25.9 s | 0.23 s | 0.7 s | **26.9 s** | 0.9 GB |
| 100k (`make scale`, 10× replica) | 112,780 | 118.6M | 393 s | 2.3 s | 8.1 s | **6.7 min** | 8.1 GB |

Rule execution and the worklist scale linearly (10×). Ingest dominates and grows about 15× for 10× the rows,
so at 100k the nightly run is ~7 minutes on a laptop: fine for a nightly window. If that ever stops being
true, the first optimization is **incremental ingest**, appending only the new export rows (the raw layer is
append-only and keyed by file line), not incremental rules. Rules cost ~2 s even at 100k.
Measured on an Apple-silicon laptop (10 cores, 24 GB), DuckDB 1.5.6, with the calibration rules; raw numbers in
`data/scale_results.json`. With all 104 rules live, the 10k nightly run is **30–34 s** (ingest 26–29 s, rules
~2 s, worklist ~2.5 s, from the nightly log). Rule time scales linearly, so at 100k it stays well under a
minute next to ~6.5 minutes of ingest.

Every run is logged with timings in `data/ops.sqlite` (`runs`) and shown under **Data health**.

---

## The product

- **Today:**
  - 20 patients, one row per patient (one call can close several gaps), each with one sentence on why
    them, e.g. *"Diabetic with last HbA1c 9.6% (Mar 2, 2026), no HbA1c on file in 2026 — one visit closes 3 gaps (…), seen Jul 1, 2026."*
  - Ranked by a transparent additive score (`worklist.py`; breakdown shown per row): clinical severity, reopened
    promises, newly eligible after a guideline update, bundling, reachability.
  - Rules that close with the same care (the 2016 and 2021 colorectal rules, the two HPV rules) are merged into one
    need, and bundling is capped at +20, so a stack of routine screening questions can't outrank an uncontrolled BP.
  - Deceased patients, low-data-coverage rules and indeterminate verdicts never appear. A banner explains stale feeds.
- **"Handled" isn't closed.**
  - *Scheduled/done* puts the patient in **pending verification**; each gap on the row is tracked on its own.
  - A gap is **verified** only by a record dated on/after the contact, with the exact export line as evidence.
    One visit that closes 4 of 6 gaps gets credit for 4.
  - Gaps still open at the verify-by date (30 days; 60 for colonoscopy) **reopen**. The reason distinguishes
    *"nothing happened"* (call again) from *"care happened, goal still unmet"* (PCP follow-up). While a feed
    the gap depends on is dead, verification is held instead.
  - No credit when the patient simply left the denominator (aged out, died) or the rule passes on a record
    dated before the call (late-arriving data): those are *closed without credit*.
- **Rules & trust:** every rule with its quote, state (live, awaiting approval, rejected), results per year,
  caveats and an Approve button. Approval (and the admin nightly endpoint) requires the approver token:
  `PANELPILOT_APPROVER_TOKEN`, or the one generated in `data/approver_token` on first start.
- **Data health:** feeds, what was done with bad data, nightly timings.
- **Outcomes:** verified closures (gaps closed by a record after the contact, not calls made), reopen rate,
  closed without credit, median days to verified care, measure rates.
- **API:** the UI is just one client. `/docs` is the OpenAPI spec a mobile team would build on.

### Demo script (replay mode)

```bash
make demo-reset                                            # export as of 2026-08-01, empty outreach history
make run                                                   # log calls on Today in the UI
python -m panelpilot.simulate care <patient> hba1c --on 2026-08-03 --value 7.4   # care arrives (any mapped concept works)
make nightly ASOF=2026-08-04 REPLAY=1                      # -> those gaps verified, with evidence
make nightly ASOF=2026-09-02 REPLAY=1                      # -> gaps still open past verify-by reopen
make nightly ASOF=2026-10-10 REPLAY=1                      # -> the lab feed has been dead for 3 weeks
```

---

## Known limitations

- **Rulebook coverage:**
  - The Bright Futures grid can't be extracted cell-by-cell from the PDF text.
  - Rules depending on age-at-first-dose, pregnancy status, family or sexual history, pack-years or
    medications are captured as `uncomputable` / not mapped (medications aren't ingested).
- **Simulator artifacts:** Synthea barely simulates mammography, HIV testing and hearing tests. Those
  rules run but are flagged *low data coverage* and kept off the outreach list.
- **FHIR is converted, not stored natively.** FHIR R4 merges Synthea's *wellness* and *ambulatory* visit
  classes; the converter restores wellness for the well-visit codes, but a generic "check up" (185349003)
  can't be restored. Synthea's CSV-only QOLS/QALY/DALY scores have no FHIR equivalent (no rule uses them).
- **Weights are judgment calls:** the priority weights are stated product judgment, not fitted to outcomes data.
- **Gold set sign-off:** extraction accuracy numbers are provisional until every gold file is signed off.
- **Scale test:** the 100k measurement replicates the 10k panel. It measures row-count cost, not the
  variety of a real 100k panel.
- **Verification follows the current verdict:** a gap verified by a BP reading reopens if a later reading is
  above goal (the measure is "most recent reading"), and is routed to the PCP as "care happened, goal unmet".

## Repo map

`panelpilot/` code · `rules/calibration` spec measures · `rules/compiled|rejected|retired` LLM output ·
`gold/` gold labels · `sources/` documents + text + sections · `tests/` trust guarantees and data handling ·
`ai_usage/` AI log + token spend · `docs/` decision memo, OpenAPI spec
