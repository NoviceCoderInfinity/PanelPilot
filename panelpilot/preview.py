"""Impact preview for rules awaiting approval: what WOULD this rule do to the panel?

Runs every verified-but-unapproved rule on a scratch copy of the panel (never the live one) and writes
data/preview.json: denominator / pass / fail / can't-tell / not-computable reason / caveats per rule. The
Rules & trust page shows it next to the Approve button, so a reviewer signs off on consequences, not JSON.

Usage: python -m panelpilot.preview
"""
import json
import pathlib
import shutil
import tempfile

import duckdb

from panelpilot import engine, rules as R, worklist

ROOT = pathlib.Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "panel.duckdb"
OUT = ROOT / "data" / "preview.json"


def main():
    pending = []
    for d in ("rules/calibration", "rules/compiled"):
        for f in sorted((ROOT / d).rglob("*.json")):
            try:
                r = R.verify(json.loads(f.read_text()))
            except R.RuleRejected:
                continue
            if not r.approved:
                pending.append(r)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = pathlib.Path(tmp) / "preview.duckdb"
        shutil.copy(PANEL, scratch)
        con = duckdb.connect(str(scratch))
        my = con.execute("SELECT my FROM run_info").fetchone()[0]
        out = {}
        for r in pending:
            con.execute("DELETE FROM verdicts WHERE rule_id = ?", [r.id])
            con.execute("DELETE FROM rule_runs WHERE rule_id = ?", [r.id])
            try:
                engine.run_rule(con, r, my, preview=True)
                row = con.execute("""SELECT status, denominator, pass, fail, indeterminate, excluded, caveats
                                     FROM rule_runs WHERE rule_id = ? AND my = ?""", [r.id, my]).fetchone()
                out[r.id] = dict(zip(["status", "denominator", "pass", "fail", "indeterminate", "excluded", "caveats"], row))
                # behavioural fingerprint: identical verdicts for every patient => same rule, approve only one
                out[r.id]["fingerprint"] = con.execute("""SELECT md5(string_agg(patient || status, ',' ORDER BY patient))
                                                          FROM verdicts WHERE rule_id = ? AND my = ?""", [r.id, my]).fetchone()[0]
            except Exception as e:
                out[r.id] = {"status": f"error: {type(e).__name__}: {e}"[:300]}
            out[r.id]["my"] = my
        con.close()
    # duplicate = identical verdicts for every patient AND the same need (numerator concepts); identical
    # verdicts alone also groups different vaccines that every toddler in the panel happens to have
    need = {r.id: worklist.need_key(r.data) for r in pending}
    groups = {}
    for rid, v in out.items():
        if v.get("fingerprint") and v.get("denominator"):
            groups.setdefault((v["fingerprint"], need[rid]), []).append(rid)
    for ids in groups.values():
        for rid in ids:
            out[rid]["same_verdicts_as"] = [x for x in ids if x != rid]
    OUT.write_text(json.dumps(out, indent=1))
    ran = [k for k, v in out.items() if v["status"] == "ran"]
    print(f"previewed {len(out)} rules awaiting approval: {len(ran)} computable on this panel, "
          f"{sum(1 for v in out.values() if v['status'].startswith('not computable'))} not computable, "
          f"{sum(1 for v in out.values() if v['status'].startswith('error'))} errors -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
