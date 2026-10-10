"""Score extracted rules (rules/compiled/**) against gold rules (gold/**).

Matching is per source document: each gold rule is paired with the compiled rule from the same doc whose leaf
criteria overlap most (Jaccard over criterion signatures, greedy, threshold 0.3). Section ids are not required
to match because the CDC PDF is extracted both per page and per footnote block, so the same rule can
legitimately come from either unit.

Reports:
  rule recall      matched gold / gold, per doc that has been extracted at all
  rule precision   matched compiled / compiled rules from sections that contain gold (gold is a subset,
                   so precision over the whole corpus would be meaningless)
  criterion agreement on matched pairs: exact-signature recall, plus per-field agreement
                   (type, concept, ages, sex, windows, counts/thresholds)
  rejected         candidates the verifier/gates refused, by reason -- the hallucination-defense evidence
Writes rules/eval_report.json. Runs (and says so) when nothing has been compiled yet.
"""
import json
import pathlib
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from panelpilot.rules import ROOT  # noqa: E402

GOLD, COMPILED, REJECTED = ROOT / "gold", ROOT / "rules" / "compiled", ROOT / "rules" / "rejected"
MATCH_THRESHOLD = 0.3
FIELDS = {
    "type": lambda k, p: k,
    "concept": lambda k, p: p.get("concept"),
    "ages": lambda k, p: (p.get("min"), p.get("max"), p.get("at")) if k == "age" else None,
    "sex": lambda k, p: p.get("is") if k == "sex" else None,
    "window": lambda k, p: tuple(p["window"]) if "window" in p else None,
    "count_threshold": lambda k, p: (p.get("min_count"), (p.get("value") or {}).get("threshold"),
                                     p.get("systolic_lt"), p.get("diastolic_lt")),
}


def load(root):
    out = []
    for f in sorted(root.rglob("*.json")):
        try:
            r = json.loads(f.read_text())
            r["_file"] = str(f.relative_to(ROOT)) if f.is_relative_to(ROOT) else str(f)
            out.append(r)
        except json.JSONDecodeError:
            pass
    return out


def leaves(c, part):
    if isinstance(c, list):
        for x in c:
            yield from leaves(x, part)
    elif isinstance(c, dict):
        kind = next((k for k in c if k not in ("quote", "note", "derived")), None)
        if kind in ("all", "any"):
            yield from leaves(c[kind], part)
        elif kind == "not":
            for k, p, _ in leaves(c["not"], part):
                yield "not:" + k, p, part
        elif kind:
            yield kind, c[kind] if isinstance(c[kind], dict) else {}, part


def signature(kind, p, part):
    return (part, kind, p.get("concept"), p.get("min"), p.get("max"), p.get("is"), p.get("min_count"),
            tuple(p.get("window", ())), json.dumps(p.get("value"), sort_keys=True))


def sigs(rule):
    return {signature(k, p, part) for part in ("denominator", "numerator", "exclusions")
            for k, p, part in leaves(rule.get(part), part)}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def soft(rule):
    """Looser signature for pairing: what population and what test, ignoring parameters.
    'uncomputable' leaves pair only on WHAT they need: matching on the bare type paired gold 'syphilis
    screening' with compiled 'youth tobacco prevention' (both all-uncomputable) and inflated recall."""
    def key(k, p):
        if k.replace("not:", "") == "uncomputable":
            return re.sub(r"[^a-z]+", " ", str(p.get("needs", "")).lower()).strip()
        return p.get("concept") or (p.get("min"), p.get("max"))
    return {(part, k.replace("not:", ""), key(k, p))
            for part in ("denominator", "numerator") for k, p, part in leaves(rule.get(part), part)}


# docs whose sections are one recommendation each: a gold rule can only match a rule from its own section
SECTION_KEYED = {"uspstf_ab"}


def pair_score(doc, g, c):
    if doc in SECTION_KEYED:
        if g["source"].get("section_id") != c["source"].get("section_id"):
            return 0.0
        # same section = same recommendation: always a candidate pair; field agreement then measures quality
        return max(jaccard(soft(g), soft(c)), MATCH_THRESHOLD)
    return jaccard(soft(g), soft(c))


def field_agreement(g, c):
    res = {}
    gl = [(k, p) for part in ("denominator", "numerator") for k, p, _ in leaves(g.get(part), part)]
    cl = [(k, p) for part in ("denominator", "numerator") for k, p, _ in leaves(c.get(part), part)]
    for name, fn in FIELDS.items():
        gv = Counter(str(fn(k, p)) for k, p in gl if fn(k, p) is not None)
        cv = Counter(str(fn(k, p)) for k, p in cl if fn(k, p) is not None)
        if gv:
            res[name] = sum((gv & cv).values()) / sum(gv.values())
    return res


def reject_reason(r):
    why = r.get("_rejected_because", "")
    if "unparseable" in why:
        return "unparseable output"
    if "number" in why and "not stated" in why:
        return "number not in its quote"
    if "not in the extracted section" in why:
        return "quote outside section"
    if "not found verbatim" in why or "not verbatim" in why:
        return "quote not verbatim in document"
    if "missing quote" in why:
        return "missing quote"
    return "other: " + re.sub(r"\s+", " ", why)[:60]


def main():
    gold, comp, rej = load(GOLD), load(COMPILED), load(REJECTED)
    report = {"gold_rules": len(gold), "compiled_rules": len(comp), "rejected_candidates": len(rej), "docs": {}}
    print(f"gold={len(gold)} compiled={len(comp)} rejected={len(rej)}")
    if not comp:
        print("no compiled rules yet -- run panelpilot/extract.py first; reporting gold inventory only")

    by_doc_g, by_doc_c = defaultdict(list), defaultdict(list)
    for r in gold:
        by_doc_g[r["source"]["doc_id"]].append(r)
    for r in comp:
        by_doc_c[r["source"]["doc_id"]].append(r)

    tot_g = tot_m = 0
    agree = defaultdict(list)
    for doc, gs in sorted(by_doc_g.items()):
        cs = by_doc_c.get(doc, [])
        entry = {"gold": len(gs), "compiled": len(cs), "matches": [], "unmatched_gold": []}
        if not cs:
            entry["status"] = "not extracted"
            report["docs"][doc] = entry
            print(f"  {doc:18s} gold={len(gs):2d}  not extracted yet")
            continue
        pairs = sorted(((pair_score(doc, g, c), gi, ci) for gi, g in enumerate(gs) for ci, c in enumerate(cs)),
                       reverse=True)
        used_g, used_c = set(), set()
        for s, gi, ci in pairs:
            if s < MATCH_THRESHOLD or gi in used_g or ci in used_c:
                continue
            used_g.add(gi)
            used_c.add(ci)
            g, c = gs[gi], cs[ci]
            fa = field_agreement(g, c)
            exact = len(sigs(g) & sigs(c)) / len(sigs(g)) if sigs(g) else 0.0
            for k, v in fa.items():
                agree[k].append(v)
            agree["exact_signature"].append(exact)
            entry["matches"].append({"gold": g["id"], "compiled": c["id"], "pair_score": round(s, 2),
                                     "exact_signature_recall": round(exact, 2),
                                     "fields": {k: round(v, 2) for k, v in fa.items()}})
        entry["unmatched_gold"] = [gs[i]["id"] for i in range(len(gs)) if i not in used_g]
        gold_sections = {g["source"].get("section_id") for g in gs}
        in_scope = [c for c in cs if c["source"].get("section_id") in gold_sections]
        matched_in_scope = sum(1 for i in used_c if cs[i]["source"].get("section_id") in gold_sections)
        entry["recall"] = round(len(used_g) / len(gs), 3)
        entry["precision_in_gold_sections"] = round(matched_in_scope / len(in_scope), 3) if in_scope else None
        tot_g += len(gs)
        tot_m += len(used_g)
        report["docs"][doc] = entry
        print(f"  {doc:18s} gold={len(gs):2d} compiled={len(cs):3d} recall={entry['recall']:.2f} "
              f"precision(gold sections)={entry['precision_in_gold_sections']}  unmatched={entry['unmatched_gold']}")

    report["overall_recall_extracted_docs"] = round(tot_m / tot_g, 3) if tot_g else None
    report["criterion_agreement"] = {k: round(sum(v) / len(v), 3) for k, v in agree.items() if v}
    report["rejections_by_reason"] = dict(Counter(reject_reason(r) for r in rej).most_common())
    if tot_g:
        print(f"overall rule recall (extracted docs): {report['overall_recall_extracted_docs']}")
    if report["criterion_agreement"]:
        print("criterion agreement on matched pairs:", report["criterion_agreement"])
    print("rejections by reason:", report["rejections_by_reason"] or "none")
    print("NOTE: provisional until every gold file is signed off (see gold/README.md)")
    (ROOT / "rules" / "eval_report.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
