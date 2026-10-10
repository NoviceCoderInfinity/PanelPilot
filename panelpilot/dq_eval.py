"""Honesty check for the messy-data path: run the same rules on the clean export and on the messy one,
then compare verdicts patient by patient.

The property we claim: bad data may make a verdict *indeterminate* (with a reason), but must never
silently flip pass <-> fail. `silent_flips` is the number that must be 0 (or each one explained).
Also scores ingest against the messifier's ground-truth defect log (_defects.csv).

Usage: python -m panelpilot.dq_eval data/clean.duckdb data/messy.duckdb data/messy_csv/_defects.csv [--my 2025]
"""
import argparse

import duckdb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clean")
    ap.add_argument("messy")
    ap.add_argument("defects")
    ap.add_argument("--my", type=int, default=2025)
    a = ap.parse_args()
    con = duckdb.connect()
    con.execute(f"ATTACH '{a.clean}' AS c (READ_ONLY); ATTACH '{a.messy}' AS m (READ_ONLY)")

    print("== verdict transitions clean -> messy (per rule)")
    con.execute(f"""CREATE TEMP TABLE t AS
        SELECT coalesce(c.rule_id, m.rule_id) AS rule_id, coalesce(c.patient, m.patient) AS patient,
               coalesce(c.status, 'not_in_denominator') AS clean, coalesce(m.status, 'not_in_denominator') AS messy,
               m.why AS messy_why
        FROM (SELECT * FROM c.verdicts WHERE my = {a.my}) c
        FULL JOIN (SELECT * FROM m.verdicts WHERE my = {a.my}) m USING (rule_id, patient)
        WHERE coalesce(c.rule_id, m.rule_id) IN (SELECT rule_id FROM c.rule_runs WHERE my = {a.my} AND status = 'ran'
                                                 INTERSECT SELECT rule_id FROM m.rule_runs WHERE my = {a.my} AND status = 'ran')""")
    print(con.execute("""SELECT rule_id, clean, messy, count(*) n FROM t WHERE clean <> messy
                         GROUP BY ALL ORDER BY rule_id, n DESC""").fetchdf().to_string(index=False))
    flips = con.execute("""SELECT count(*) FROM t WHERE (clean = 'pass' AND messy = 'fail') OR (clean = 'fail' AND messy = 'pass')
                           OR (clean IN ('pass', 'fail') AND messy = 'not_in_denominator')
                           OR (clean = 'not_in_denominator' AND messy IN ('pass', 'fail'))""").fetchone()[0]
    agree = con.execute("SELECT round(100.0 * avg((clean = messy)::INT), 2) FROM t").fetchone()[0]
    print(f"\nsilent_flips = {flips}   (agreement {agree}% of patient-rule pairs; the rest went to 'indeterminate')")
    if flips:
        print(con.execute("""SELECT rule_id, patient, clean, messy FROM t
                             WHERE (clean = 'pass' AND messy = 'fail') OR (clean = 'fail' AND messy = 'pass')
                                OR (clean IN ('pass', 'fail') AND messy = 'not_in_denominator')
                                OR (clean = 'not_in_denominator' AND messy IN ('pass', 'fail'))
                             LIMIT 15""").fetchdf().to_string(index=False))

    print("\n== ingest vs injected defects (messifier ground truth)")
    con.execute(f"CREATE TEMP TABLE inj AS SELECT * FROM read_csv('{a.defects}', all_varchar = true)")
    print(con.execute("""SELECT i.tbl, i.kind AS injected, count(*) AS n_injected FROM inj i GROUP BY ALL ORDER BY 1, 2""")
          .fetchdf().to_string(index=False))
    print(con.execute("""SELECT tbl, kind AS detected, action, count(*) n FROM m.dq_issues GROUP BY ALL ORDER BY 1, 2""")
          .fetchdf().to_string(index=False))


if __name__ == "__main__":
    main()
