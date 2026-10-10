"""LLM rule extractor: source sections -> candidate rules -> rules.verify() -> compiled/ or rejected/.

AI compiles; nothing here runs at patient-verdict time. The model only proposes; the verifier decides.
Rejected candidates are kept with the reason (they are the hallucination-defense evidence) and are never
auto-repaired -- a quote that is not verbatim stays rejected.

Usage:
  python panelpilot/extract.py --dry-run                 # no key needed: sections, token + cost estimate
  python panelpilot/extract.py --only uspstf_crc_2021    # one document
  python panelpilot/extract.py --limit 3 --budget 2      # first 3 sections, stop before $2

Every call is appended to ai_usage/token_log.csv (tokens + USD); the run stops before exceeding --budget.
"""
import argparse
import csv
import datetime
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from panelpilot import rules  # noqa: E402

ROOT = rules.ROOT
SECTIONS = ROOT / "sources" / "sections.jsonl"
CONCEPTS = ROOT / "rules" / "concepts.json"
OUT_OK, OUT_BAD, OUT_RAW = ROOT / "rules" / "compiled", ROOT / "rules" / "rejected", ROOT / "rules" / "raw"
LOG = ROOT / "ai_usage" / "token_log.csv"

# USD per 1M tokens: input, output, cache write (5-min, 1.25x input), cache read. Anthropic list prices, Oct 2026.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
# Default: Opus 5.5. The whole corpus is ~190K chars, so even the top model fits the $25 cap with room for
# re-runs (see --dry-run); extraction errors are expensive downstream (a wrong rule hits 10,000 patients),
# so we spend budget on quality and measure it against gold/ rather than economise first.
DEFAULT_MODEL = "claude-opus-5-5"
EST_OUTPUT_PER_SECTION = 3000  # tokens incl. adaptive thinking; refine from token_log.csv after a real run

SYSTEM = """You compile preventive-care guideline text into machine-checkable rules for a primary-care clinic.
Your output is checked by a strict verifier and then by a human. A wrong rule applied to 10,000 patients is worse
than a missing rule, so: capture what the text says, nothing more.

HARD REQUIREMENTS
1. Every leaf criterion carries "quote": a phrase copied character-for-character from the SECTION TEXT that
   states that criterion. Copy within a single field: never include field labels (TOPIC:, DESCRIPTION:,
   POPULATION:, RECOMMENDATION:, GRADE:, DATE:) and never span the " | " cell separators. Do not paraphrase,
   fix typos, reorder words, or join text from two places. Short exact quotes beat long approximate ones.
2. The rule-level source.quote is the sentence(s) stating the recommendation, copied verbatim the same way.
3. If a criterion depends on information a clinic's structured EHR data would not reliably hold (sexual
   behavior, travel, family history, exposure risk, clinician judgment, shared decision making), encode it
   as {"uncomputable": {"needs": "<what>"}, "quote": "..."} -- never drop it and never approximate it with
   something computable.
4. Use concept names from the CONCEPT VOCABULARY. If none fits, use a new snake_case name and mention it in
   coverage_notes; do not force a wrong concept.
5. Ages are whole years. Windows are measurement-year-relative date expressions: "{MY}-01-01" is Jan 1 of the
   measurement year, "{MY-9}-01-01" nine years earlier. A test due "every 10 years" for a December-31 check
   is the window ["{MY-9}-01-01", "{MY}-12-31"]; "every year" is ["{MY}-01-01", "{MY}-12-31"].
6. Recommendations that are grade C, D or I, or that say "not recommended", are NOT rules -- list them in
   coverage_notes instead.
7. coverage_notes must honestly list everything in the section you could not represent (footnote conditions,
   intervals depending on prior results, catch-up logic, contraindications, dosing schedules, product choice).
8. Every number in a criterion (ages, counts, thresholds) must appear in THAT criterion's quote. If you had
   to work a number out (e.g. "dose 2 at 4-6 years" -> two doses expected by age 7), declare it:
   "derived": {"<param>": "<one-line reasoning>"}. Derived rules go to human review; undeclared ones are rejected.
9. A section may yield zero rules (e.g. a title page, a table of contraindications). Return an empty list.

OUTPUT: JSON matching the schema. Each rule's "denominator", "numerator" and "exclusions" are JSON-encoded
STRINGS holding criterion objects in the grammar below (exclusions: a JSON list, "[]" if none).
id: short UPPER-KEBAB like "CRC-SCREEN-45-49". version: the guideline date or edition year as text.

CRITERION GRAMMAR (from the verifier's documentation):
""" + rules.__doc__.split("Criterion types", 1)[1]

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "version": {"type": "string"},
                    "title": {"type": "string"},
                    "source_quote": {"type": "string"},
                    "grade": {"type": "string"},
                    "effective_date": {"type": "string"},
                    "action": {"type": "string"},
                    "denominator": {"type": "string"},
                    "numerator": {"type": "string"},
                    "exclusions": {"type": "string"},
                },
                "required": ["id", "version", "title", "source_quote", "grade", "effective_date", "action",
                             "denominator", "numerator", "exclusions"],
                "additionalProperties": False,
            },
        },
        "coverage_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["rules", "coverage_notes"],
    "additionalProperties": False,
}


def system_blocks():
    concepts = json.loads(CONCEPTS.read_text())
    vocab = "\n".join(f"{k}: {v}" for k, v in concepts.items())  # file is key-sorted -> stable bytes -> cache hits
    return [{"type": "text", "text": SYSTEM + "\n\nCONCEPT VOCABULARY:\n" + vocab,
             "cache_control": {"type": "ephemeral"}}]


def load_sections(only, limit):
    secs = [json.loads(line) for line in SECTIONS.read_text().splitlines()]
    if only:
        secs = [s for s in secs if s["doc_id"] in only]
    return secs[:limit] if limit else secs


def cost(model, u):
    pi, po, pw, pr = PRICES[model]
    return (u["input"] * pi + u["output"] * po + u["cache_write"] * pw + u["cache_read"] * pr) / 1e6


def log_call(row):
    LOG.parent.mkdir(exist_ok=True)
    new = not LOG.exists()
    with open(LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def spent_so_far():
    if not LOG.exists():
        return 0.0
    with open(LOG) as f:
        return sum(float(r["cost_usd"]) for r in csv.DictReader(f))


def safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:120]


def to_rule(c, sec):
    """Turn one structured-output candidate into the rules.py JSON shape. Parse only -- no repair."""
    rule = {
        "id": f"{sec['doc_id']}.{c['id']}",
        "version": c["version"],
        "title": c["title"],
        "source": {"doc_id": sec["doc_id"], "section_id": sec["section_id"], "quote": c["source_quote"]},
        "grade": c["grade"],
        "effective_date": c["effective_date"],
        "action": c["action"],
        "compiled_by": "llm",
    }
    for k in ("denominator", "numerator", "exclusions"):
        try:
            rule[k] = json.loads(c[k])
        except json.JSONDecodeError as e:
            rule[k] = None
            rule.setdefault("_parse_errors", []).append(f"{k}: {e}")
    if rule.get("exclusions") is None and "_parse_errors" not in rule:
        rule["exclusions"] = []
    return rule


def judge(rule, section_text):
    if rule.get("_parse_errors"):
        return False, "unparseable criteria JSON: " + "; ".join(rule["_parse_errors"])
    try:
        # section scope + number grounding live in rules.verify(), so hand-written rules get them too
        r = rules.verify(rule)
        return True, ("needs human approval: derived " + "; ".join(f"{k}={why}" for _, k, why in r.derived)) if r.derived else ""
    except rules.RuleRejected as e:
        return False, str(e)
    except Exception as e:  # malformed shapes the verifier didn't anticipate are still rejections
        return False, f"{type(e).__name__}: {e}"


def dry_run(secs):
    sys_chars = len(system_blocks()[0]["text"])
    sec_chars = sum(len(s["text"]) for s in secs)
    sys_tok, sec_tok = sys_chars / 4, sec_chars / 4
    print(f"sections: {len(secs)}   section chars: {sec_chars}   system prompt chars: {sys_chars}")
    by_doc = {}
    for s in secs:
        by_doc.setdefault(s["doc_id"], [0, 0])
        by_doc[s["doc_id"]][0] += 1
        by_doc[s["doc_id"]][1] += len(s["text"])
    for d, (n, ch) in by_doc.items():
        print(f"  {d:18s} {n:3d} sections  ~{ch / 4:,.0f} tokens")
    out_tok = EST_OUTPUT_PER_SECTION * len(secs)
    print(f"estimate: ~{sec_tok:,.0f} section tokens, system ~{sys_tok:,.0f} tokens "
          f"(1 cache write + {len(secs) - 1} cache reads), ~{out_tok:,} output tokens incl. thinking")
    for m in PRICES:
        u = {"input": sec_tok + 200 * len(secs), "output": out_tok, "cache_write": sys_tok,
             "cache_read": sys_tok * max(len(secs) - 1, 0)}
        print(f"  {m:20s} ~${cost(m, u):.2f}")


def run(secs, model, budget, effort, workers=1, force=False):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import anthropic

    client = anthropic.Anthropic()
    system = system_blocks()
    total = spent_so_far()
    lock = threading.Lock()
    print(f"prior spend in log: ${total:.4f}")
    todo = [s for s in secs if force or not (OUT_RAW / f"{safe(s['section_id'])}.json").exists()]
    if len(todo) < len(secs):
        print(f"skipping {len(secs) - len(todo)} already-extracted sections (use --force to redo)")

    def one(sec):
        nonlocal total
        with lock:
            if total >= budget:
                return f"{sec['section_id']}: skipped, budget ${budget} reached"
        user = (f"DOCUMENT: {sec['doc_id']}\nSECTION: {sec['section_id']} -- {sec['title']}\n\n"
                f"SECTION TEXT:\n<<<\n{sec['text']}\n>>>")
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            # server-side fallback: if a safety classifier declines (unlikely on guideline text), retry on another model
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        )
        used_model = resp.model if resp.model in PRICES else model
        u = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens,
             "cache_write": resp.usage.cache_creation_input_tokens or 0,
             "cache_read": resp.usage.cache_read_input_tokens or 0}
        c = cost(used_model, u)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        n_ok = n_bad = 0
        with lock:
            total += c
            OUT_RAW.mkdir(parents=True, exist_ok=True)
            (OUT_RAW / f"{safe(sec['section_id'])}.json").write_text(json.dumps(
                {"section": sec["section_id"], "model": resp.model, "stop_reason": resp.stop_reason, "text": text}, indent=1))
            if resp.stop_reason == "refusal" or not text:
                print(f"{sec['section_id']}: no output (stop_reason={resp.stop_reason})")
            else:
                out = json.loads(text)
                for cand in out["rules"]:
                    rule = to_rule(cand, sec)
                    ok, why = judge(rule, sec["text"])
                    d = (OUT_OK if ok else OUT_BAD) / sec["doc_id"]
                    d.mkdir(parents=True, exist_ok=True)
                    if not ok:
                        rule["_rejected_because"] = why
                    (d / f"{safe(rule['id'])}.json").write_text(json.dumps(rule, indent=1, ensure_ascii=False))
                    n_ok += ok
                    n_bad += not ok
                (OUT_RAW / f"{safe(sec['section_id'])}.coverage.json").write_text(
                    json.dumps(out["coverage_notes"], indent=1, ensure_ascii=False))
            log_call({"ts": datetime.datetime.now().isoformat(timespec="seconds"), "section": sec["section_id"],
                      "model": resp.model, "effort": effort, "input_tokens": u["input"], "output_tokens": u["output"],
                      "cache_write_tokens": u["cache_write"], "cache_read_tokens": u["cache_read"],
                      "cost_usd": f"{c:.6f}", "verified": n_ok, "rejected": n_bad})
            return (f"{sec['section_id']:22s} verified={n_ok} rejected={n_bad} cache_read={u['cache_read']} "
                    f"${c:.4f}  total=${total:.4f}")

    if todo:  # first call alone, so the cached system prompt is written once and read by the rest
        print(one(todo[0]), flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for line in pool.map(one, todo[1:]):
            print(line, flush=True)


def load_dotenv():
    """ANTHROPIC_API_KEY may live in a gitignored .env at the repo root (KEY=value lines)."""
    import os
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", nargs="*", help="doc_id(s) to process")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(PRICES))
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--budget", type=float, default=20.0, help="stop once cumulative logged spend reaches this (USD)")
    ap.add_argument("--workers", type=int, default=4, help="parallel sections (after the first, which warms the cache)")
    ap.add_argument("--force", action="store_true", help="re-extract sections that already have raw output")
    a = ap.parse_args()
    secs = load_sections(a.only, a.limit)
    if a.dry_run:
        dry_run(secs)
    else:
        run(secs, a.model, a.budget, a.effort, a.workers, a.force)


if __name__ == "__main__":
    main()
