"""Does FHIR input give the same verdicts as CSV input for the same patients?

    python -m panelpilot.fhir_parity [--sample 300] [--my 2025]

Converts a seeded sample of FHIR bundles (panelpilot.fhir), cuts the SAME patients out of the Synthea CSV
export, ingests both, runs every runnable rule on both, and compares verdicts per (rule, patient).
Any disagreement is listed with both verdicts, so it can be explained, not hidden.
"""
import argparse
import json
import pathlib
import time

import duckdb

from panelpilot import engine, fhir, ingest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FHIR_DIR = ROOT / "synthea/output/fhir"
CSV_DIR = ROOT / "synthea/output/csv"
OUT = ROOT / "data/fhir_parity"
RULE_DIRS = [ROOT / "rules/calibration", ROOT / "rules/compiled"]


def cut_csv(patients, out):
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE ids (id VARCHAR)")
    con.executemany("INSERT INTO ids VALUES (?)", [(p,) for p in patients])
    for t in fhir.HEADERS:
        key = "Id" if t == "patients" else "PATIENT"
        con.execute(f"""COPY (SELECT * FROM read_csv('{CSV_DIR / t}.csv', all_varchar = true, header = true)
                        WHERE {key} IN (SELECT id FROM ids)) TO '{out / t}.csv' (HEADER, DELIMITER ',')""")


def main(sample, my, as_of):
    t0 = time.time()
    counts, n = fhir.convert(FHIR_DIR, OUT / "fhir_csv", sample)
    print(f"FHIR: {n} bundles -> {counts}  ({time.time() - t0:.0f}s)")
    pts = [r[0] for r in duckdb.sql(f"SELECT Id FROM read_csv('{OUT / 'fhir_csv/patients.csv'}', all_varchar=true)").fetchall()]
    cut_csv(pts, OUT / "csv")
    print(f"CSV: same {len(pts)} patients cut from the export  ({time.time() - t0:.0f}s)")
    for name in ("fhir_csv", "csv"):
        ingest.ingest(OUT / name, OUT / f"{name}.duckdb", as_of, quiet=True)
        engine.run(OUT / f"{name}.duckdb", RULE_DIRS, my, quiet=True)
    con = duckdb.connect()
    con.execute(f"ATTACH '{OUT / 'fhir_csv.duckdb'}' AS f (READ_ONLY)")
    con.execute(f"ATTACH '{OUT / 'csv.duckdb'}' AS c (READ_ONLY)")
    tot, same = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE f.status IS NOT DISTINCT FROM c.status)
        FROM c.verdicts c FULL JOIN f.verdicts f USING (rule_id, patient, my) WHERE coalesce(c.my, f.my) = {my}""").fetchone()
    diffs = con.execute(f"""SELECT rule_id, coalesce(c.status, '-') AS csv_status, coalesce(f.status, '-') AS fhir_status, count(*) n
        FROM c.verdicts c FULL JOIN f.verdicts f USING (rule_id, patient, my)
        WHERE coalesce(c.my, f.my) = {my} AND f.status IS DISTINCT FROM c.status GROUP BY ALL ORDER BY n DESC""").fetchall()
    rules_ran = con.execute(f"SELECT count(*) FROM c.rule_runs WHERE my = {my} AND status = 'ran'").fetchone()[0]
    res = {"patients": len(pts), "rules_ran": rules_ran, "verdicts": tot, "identical": same,
           "agreement_pct": round(100 * same / tot, 2) if tot else None,
           "differences": [dict(zip(("rule_id", "csv", "fhir", "patients"), d)) for d in diffs],
           "seconds": round(time.time() - t0)}
    (OUT / "parity.json").write_text(json.dumps(res, indent=1))
    print(f"\n{rules_ran} rules x {len(pts)} patients: {same}/{tot} verdicts identical ({res['agreement_pct']}%)")
    for d in res["differences"]:
        print(f"  {d['rule_id']:42s} csv={d['csv']:13s} fhir={d['fhir']:13s} patients={d['patients']}")
    print(f"-> {OUT / 'parity.json'}  ({res['seconds']}s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--my", type=int, default=2025)
    ap.add_argument("--as-of", default="2026-10-09")
    a = ap.parse_args()
    main(a.sample, a.my, a.as_of)
