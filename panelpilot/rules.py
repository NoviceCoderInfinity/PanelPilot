"""Rule representation + the verifier that is the only way to obtain an executable rule.

A rule is JSON (see rules/calibration/*.json). The engine accepts only `VerifiedRule`, and the only
constructor of `VerifiedRule` is `verify()`, which rejects the rule unless ALL of:
  1. its source document is in sources/MANIFEST.csv and the stored text's sha256 matches;
  2. the rule-level quote and every criterion's quote occur verbatim in that document
     (comparison after NFKC + whitespace collapse only -- PDF line breaks are not content);
  3. if the rule names a source section, every quote lies inside THAT section (a real sentence from
     elsewhere in the document cannot be borrowed to justify a rule);
  4. every number a criterion uses (ages, thresholds, counts) appears in that criterion's own quote,
     or is explicitly declared under "derived" with the reasoning.
A rule with derived numbers, or any rule compiled by the LLM, is valid but NOT runnable until a human approves its exact content
(rules/approvals.json holds the sha256 of each approved rule). Observed failure that motivated 3+4:
the real phrase "adults aged 40 to 49 years" (an incidence statistic) passed check 2 as support for an
invented 40-49 screening rule.

Criterion types (a criterion is one dict with exactly one type key plus "quote", optional "note"/"derived"):
  age          {"min": 18, "max": 75, "at": "end" | "all_year"}
  sex          {"is": "F" | "M"}
  condition    {"concept": "hypertension", "onset_before": "{MY}-06-30", "onset_by": "{MY}-12-31"}
  observation  {"concept": "hba1c", "window": [from, to], "pick": "any" | "latest",
                "value": {"op": "<", "threshold": 8.0, "unit": "%"}}
  bp           {"window": [from, to], "systolic_lt": 140, "diastolic_lt": 90}   (latest paired reading)
  procedure    {"concept": "colonoscopy", "window": [from, to]}
  encounter    {"concept": "well_visit", "window": [from, to]}
  immunization {"concept": "mmr_vaccine", "min_count": 2, "window": [from, to]}
  deceased     {"before": "{MY}-01-01"}
  uncomputable {"needs": "sexual history"}   (captured honestly; engine reports 'not computable')
  all / any    [criterion, ...]      not  criterion
  "derived": {"min": "dose 2 is due at 4-6 years, so by age 7 two doses are expected"}
               -- names each number NOT stated in the quote and why; forces human approval.
Dates are "{MY}-MM-DD" or "{MY-9}-MM-DD": relative to the measurement year, so rules don't rot.
"""
import csv
import hashlib
import json
import pathlib
import re
import unicodedata
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources"
APPROVALS = ROOT / "rules" / "approvals.json"
LEAF_TYPES = {"age", "sex", "condition", "observation", "bp", "procedure", "encounter", "immunization", "deceased", "uncomputable"}
NUMERIC_PARAMS = ("min", "max", "min_count", "systolic_lt", "diastolic_lt")
DATE_EXPR = re.compile(r"^\{MY([+-]\d+)?\}-(\d\d)-(\d\d)$")


class RuleRejected(Exception):
    pass


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("­", "")
    return re.sub(r"\s+", " ", text).strip()


def _manifest():
    with open(SOURCES / "MANIFEST.csv") as f:
        return {r["doc_id"]: r for r in csv.DictReader(f)}


_TEXT_CACHE: dict = {}
_SECTIONS: dict = {}


def source_text(doc_id: str) -> str:
    """Normalized text of a source doc, checked against the manifest hash of the extracted text."""
    if doc_id not in _TEXT_CACHE:
        m = _manifest().get(doc_id)
        if m is None:
            raise RuleRejected(f"unknown source document {doc_id!r}")
        raw = (SOURCES / "text" / f"{doc_id}.txt").read_bytes()
        if hashlib.sha256(raw).hexdigest() != m["text_sha256"]:
            raise RuleRejected(f"source text for {doc_id} does not match manifest hash")
        _TEXT_CACHE[doc_id] = norm(raw.decode("utf-8"))
    return _TEXT_CACHE[doc_id]


def section_text(doc_id: str, section_id: str) -> str:
    """Normalized section text; the section itself must be verbatim inside its (hash-checked) document."""
    if not _SECTIONS:
        for line in (SOURCES / "sections.jsonl").read_text().splitlines():
            s = json.loads(line)
            _SECTIONS[(s["doc_id"], s["section_id"])] = norm(s["text"])
    sec = _SECTIONS.get((doc_id, section_id))
    if sec is None:
        raise RuleRejected(f"unknown section {section_id!r} in {doc_id}")
    return sec


def resolve_date(expr: str, my: int) -> str:
    m = DATE_EXPR.match(expr)
    if not m:
        raise RuleRejected(f"bad date expression {expr!r}")
    return f"{my + int(m.group(1) or 0)}-{m.group(2)}-{m.group(3)}"


def content_hash(rule: dict) -> str:
    return hashlib.sha256(json.dumps(rule, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class VerifiedRule:
    id: str
    version: str
    data: dict          # the full rule JSON
    derived: tuple      # (path, param, reason) for numbers not stated in their quote
    _token: object      # only verify() holds this

    def __post_init__(self):
        if self._token is not _VERIFY_TOKEN:
            raise RuleRejected("VerifiedRule can only be created by rules.verify()")

    @property
    def needs_approval(self) -> bool:
        # LLM output always gets a human sign-off before it touches patients: verify() makes fabrication
        # impossible, approval is the check that the quote was *interpreted* correctly.
        return bool(self.derived) or self.data.get("compiled_by") == "llm"

    @property
    def approved(self) -> bool:
        if not self.needs_approval:
            return True
        approvals = json.loads(APPROVALS.read_text()) if APPROVALS.exists() else {}
        return approvals.get(self.id) == content_hash(self.data)


_VERIFY_TOKEN = object()


class _Scope:
    """`q in scope` is true only if q is verbatim in the cited section and in the document."""
    def __init__(self, doc, section):
        self.doc, self.section = doc, section

    def __contains__(self, q):
        return q in self.section and q in self.doc


def _numbers(kind, spec):
    if not isinstance(spec, dict):
        return []
    out = [(k, spec[k]) for k in NUMERIC_PARAMS if isinstance(spec.get(k), (int, float))]
    if isinstance(spec.get("value"), dict) and isinstance(spec["value"].get("threshold"), (int, float)):
        out.append(("threshold", spec["value"]["threshold"]))
    return out


def _stated(n, quote):
    s = str(int(n)) if float(n).is_integer() else str(n)
    return re.search(rf"(?<![\d.]){re.escape(s)}(?![\d])", quote) is not None


def _walk(c, path, scope, problems, derived):
    if not isinstance(c, dict):
        problems.append(f"{path}: criterion must be an object")
        return
    keys = set(c) - {"quote", "note", "derived"}
    if len(keys) != 1:
        problems.append(f"{path}: criterion needs exactly one type key, got {sorted(keys)}")
        return
    (kind,) = keys
    if kind in ("all", "any"):
        for i, sub in enumerate(c[kind]):
            _walk(sub, f"{path}.{kind}[{i}]", scope, problems, derived)
        return
    if kind == "not":
        _walk(c["not"], f"{path}.not", scope, problems, derived)
        return
    if kind not in LEAF_TYPES:
        problems.append(f"{path}: unknown criterion type {kind!r}")
        return
    q = norm(c.get("quote") or "")
    if not q:
        problems.append(f"{path} ({kind}): missing quote")
        return
    if q not in scope:
        problems.append(f"{path} ({kind}): quote not found verbatim in source scope: {q[:80]!r}")
    declared = c.get("derived") or {}
    grounded = set()
    for param, n in _numbers(kind, c[kind]):
        if _stated(n, q):
            grounded.add(param)
            continue
        if param in declared and str(declared[param]).strip():
            derived.append((path, param, declared[param]))
            grounded.add(param)
        else:
            problems.append(f"{path} ({kind}): number {n} ({param}) is not stated in its quote and not declared derived")
    # any other declared interpretation (e.g. "every 10 y" -> window {MY-9}..{MY}) is surfaced for review too
    derived += [(path, k, v) for k, v in declared.items() if k not in grounded and str(v).strip()]
    spec = c[kind] if isinstance(c[kind], dict) else {}
    for k in ("onset_before", "onset_by", "before"):
        if k in spec and not DATE_EXPR.match(str(spec[k])):
            problems.append(f"{path}: bad date expression {spec[k]!r}")
    for d in spec.get("window", []):
        if not DATE_EXPR.match(str(d)):
            problems.append(f"{path}: bad date expression {d!r}")


def verify(rule: dict) -> VerifiedRule:
    problems = []
    for k in ("id", "version", "title", "source", "denominator", "numerator"):
        if k not in rule:
            problems.append(f"missing field {k!r}")
    if problems:
        raise RuleRejected(f"{rule.get('id', '?')}: " + "; ".join(problems))
    src = rule["source"]
    doc = source_text(src.get("doc_id", ""))
    # scope = the section the rule cites (if any) AND the hash-checked document: a quote must be in both,
    # so an edited sections.jsonl cannot introduce text the real document does not contain
    scope = _Scope(doc, section_text(src["doc_id"], src["section_id"]) if src.get("section_id") else doc)
    if not src.get("quote") or norm(src["quote"]) not in scope:
        problems.append("rule-level source quote missing or not verbatim in source scope")
    derived = []
    _walk(rule["denominator"], "denominator", scope, problems, derived)
    _walk(rule["numerator"], "numerator", scope, problems, derived)
    for i, ex in enumerate(rule.get("exclusions", [])):
        _walk(ex, f"exclusions[{i}]", scope, problems, derived)
    if problems:
        raise RuleRejected(f"{rule['id']}: " + "; ".join(problems))
    return VerifiedRule(rule["id"], rule["version"], rule, tuple(derived), _VERIFY_TOKEN)


def load_dir(path, recursive=False) -> tuple[list[VerifiedRule], list[str]]:
    """Load rule JSON files. Returns (runnable rules, messages for rejected or awaiting-approval rules)."""
    ok, bad = [], []
    p = pathlib.Path(path)
    for f in sorted(p.rglob("*.json") if recursive else p.glob("*.json")):
        try:
            r = verify(json.loads(f.read_text()))
        except (RuleRejected, json.JSONDecodeError) as e:
            bad.append(f"{f.name}: REJECTED {e}")
            continue
        if r.approved:
            ok.append(r)
        else:
            bad.append(f"{f.name}: AWAITING APPROVAL (derived: " + "; ".join(f"{p}.{k}: {why}" for p, k, why in r.derived) + ")")
    return ok, bad


def approve(rule_file):
    """Human sign-off: record the exact content hash of a reviewed rule with derived numbers."""
    r = verify(json.loads(pathlib.Path(rule_file).read_text()))
    approvals = json.loads(APPROVALS.read_text()) if APPROVALS.exists() else {}
    approvals[r.id] = content_hash(r.data)
    APPROVALS.write_text(json.dumps(approvals, indent=1, sort_keys=True))
    return r.id


if __name__ == "__main__":
    import sys
    for f in sys.argv[1:]:   # python -m panelpilot.rules <rule.json> [...]: human sign-off, one file each
        print("approved", approve(f))


def catalog(dirs):
    """Every rule file with its state, for the review UI: runnable | awaiting_approval | rejected."""
    out = []
    for d in dirs:
        for f in sorted((ROOT / d).resolve().rglob("*.json")):
            entry = {"file": str(f.relative_to(ROOT)), "id": None, "state": None, "reason": None, "derived": [], "rule": None}
            try:
                raw = json.loads(f.read_text())
                entry["rule"], entry["id"] = raw, raw.get("id")
                r = verify(raw)
                entry["derived"] = [{"path": p, "param": k, "why": w} for p, k, w in r.derived]
                entry["state"] = "runnable" if r.approved else "awaiting_approval"
                if r.needs_approval and not r.approved:
                    entry["reason"] = "derived numbers need sign-off" if r.derived else "LLM-compiled: needs human sign-off"
            except (RuleRejected, json.JSONDecodeError) as e:
                entry["state"], entry["reason"] = "rejected", str(e)
            out.append(entry)
    return out
