"""Measured nightly cost at 10x scale (hard question 8).

Builds a 100k-patient export by replicating the messy 10k export 10 times with suffixed patient/encounter
ids (same defect mix, same record density), runs the real nightly pipeline steps on it, and writes the
timings next to the 10k numbers to data/scale_results.json. The replicated export is deleted afterwards.

Caveat stated in the README: replication preserves per-patient record density and defect rates, not the
variety of a real 100k panel; DuckDB's cost is dominated by row counts, which is what this measures.

Usage: python -m panelpilot.scale_test [--factor 10] [--keep]
"""
import argparse
import json
import pathlib
import shutil
import time

import duckdb

from panelpilot import engine, ingest, worklist

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "messy_csv"
RULE_DIRS = [ROOT / "rules" / "calibration", ROOT / "rules" / "compiled"]
ID_COLS = {"patients": ["Id"], "encounters": ["Id", "PATIENT"], "observations": ["PATIENT", "ENCOUNTER"],
           "conditions": ["PATIENT", "ENCOUNTER"], "procedures": ["PATIENT", "ENCOUNTER"],
           "immunizations": ["PATIENT", "ENCOUNTER"]}


def replicate(dst, factor):
    dst.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for t, cols in ID_COLS.items():
        rep = ", ".join(f"CASE WHEN r.k = 0 THEN s.{c} ELSE s.{c} || '-' || r.k END AS {c}" for c in cols)
        con.execute(f"""COPY (SELECT s.* REPLACE ({rep}) FROM read_csv('{SRC / t}.csv', all_varchar = true) s,
                              (SELECT range AS k FROM range({factor})) r)
                        TO '{dst / t}.csv' (HEADER)""")


def time_pipeline(src, db, as_of, my):
    t = {}
    t0 = time.time()
    ingest.ingest(src, db, as_of, quiet=True)
    t["ingest_s"] = round(time.time() - t0, 1)
    t1 = time.time()
    engine.run(db, RULE_DIRS, my, quiet=True)
    t["rules_s"] = round(time.time() - t1, 2)
    t2 = time.time()
    t["worklist_candidates"] = worklist.build(db, my, as_of)
    t["worklist_s"] = round(time.time() - t2, 1)
    t["total_s"] = round(time.time() - t0, 1)
    con = duckdb.connect(str(db), read_only=True)
    t["patients"] = con.execute("SELECT count(*) FROM patients").fetchone()[0]
    t["observations"] = con.execute("SELECT count(*) FROM observations").fetchone()[0]
    con.close()
    t["db_gb"] = round(db.stat().st_size / 1e9, 2)
    return t


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--factor", type=int, default=10)
    ap.add_argument("--as-of", default="2026-10-10")
    ap.add_argument("--my", type=int, default=2026)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    big = ROOT / "data" / f"scale_x{a.factor}"
    res = {"as_of": a.as_of, "my": a.my}
    res["x1"] = time_pipeline(SRC, ROOT / "data" / "scale_x1.duckdb", a.as_of, a.my)
    print("x1", res["x1"])
    t0 = time.time()
    replicate(big / "csv", a.factor)
    res["replicate_s"] = round(time.time() - t0, 1)
    res[f"x{a.factor}"] = time_pipeline(big / "csv", big / "panel.duckdb", a.as_of, a.my)
    print(f"x{a.factor}", res[f"x{a.factor}"])
    (ROOT / "data" / "scale_results.json").write_text(json.dumps(res, indent=1))
    (ROOT / "data" / "scale_x1.duckdb").unlink()
    if not a.keep:
        shutil.rmtree(big)
