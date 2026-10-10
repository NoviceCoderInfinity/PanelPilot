"""Guideline updates (hard question 4): what happens when a rulebook changes, e.g. colorectal 50 -> 45.

1. The new document is compiled like any other; its rule is verified and approved by a human.
2. `python -m panelpilot.rule_update <new_rule.json> <old_rule.json>` records `"supersedes"` in the new rule
   and moves the old one to rules/retired/ (kept: provenance and the diff need it). Re-approval is required,
   because the rule's content changed.
3. Every nightly run then computes, for each superseding rule, the patients inside the NEW denominator but
   outside the OLD one -- the people "nobody recomputed" -- into `newly_eligible`. Those with an open gap
   get a worklist boost and a reason that names the update; a banner reports the count.
"""
import json
import pathlib
import shutil
import sys

from panelpilot import engine, rules as R

ROOT = pathlib.Path(__file__).resolve().parent.parent
RETIRED = ROOT / "rules" / "retired"


def compute(con, my, verified_rules):
    con.execute("""CREATE OR REPLACE TABLE newly_eligible (rule_id VARCHAR, supersedes VARCHAR, patient VARCHAR,
                   status VARCHAR, note VARCHAR)""")
    for rule in verified_rules:
        sup = rule.data.get("supersedes")
        if not sup:
            continue
        old = R.verify(json.loads((ROOT / sup).read_text()))   # the old rule must still verify against its source
        before = engine.patients_where(con, old.data["denominator"], my)
        note = (f"Newly eligible under {rule.data.get('title', rule.id)} ({rule.version}); "
                f"the previous guideline ({old.version}) did not include them")
        con.execute(f"""INSERT INTO newly_eligible SELECT rule_id, ?, patient, status, ? FROM verdicts
                        WHERE my = ? AND rule_id = ? AND status <> 'excluded'
                        AND patient NOT IN (SELECT unnest(?::VARCHAR[]))""", [old.id, note, my, rule.id, list(before)])
    return con.execute("SELECT rule_id, status, count(*) FROM newly_eligible GROUP BY ALL").fetchall()


def supersede(new_file, old_file):
    new_p, old_p = pathlib.Path(new_file).resolve(), pathlib.Path(old_file).resolve()
    RETIRED.mkdir(parents=True, exist_ok=True)
    dest = RETIRED / old_p.name
    shutil.move(str(old_p), dest)
    rule = json.loads(new_p.read_text())
    rule["supersedes"] = str(dest.relative_to(ROOT))
    new_p.write_text(json.dumps(rule, indent=1, ensure_ascii=False))
    print(f"{rule['id']} now supersedes {dest.relative_to(ROOT)}; re-approve it (content changed), then run nightly")


if __name__ == "__main__":
    supersede(sys.argv[1], sys.argv[2])
