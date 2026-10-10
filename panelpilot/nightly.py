"""The nightly job (hard questions 7 and 8): rebuild the panel from the latest export, re-run every
runnable rule, resolve pending outreach (verify or reopen), rebuild the worklist, and log timings.

Full recompute, by design: at 10k patients it is ~half a minute end to end (measured, logged in
data/ops.sqlite `runs`), which buys simplicity and correctness -- a late-arriving record, a rule version
change or a fixed value set all propagate without invalidation logic. The panel is built into a temp file
and swapped in atomically, so the app never reads a half-built panel.

Usage: python -m panelpilot.nightly --src data/messy_csv --as-of 2026-10-10 [--my 2026] [--calibration-year 2025]
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import time

import duckdb

from panelpilot import engine, ingest, ops, rule_update, worklist

ROOT = pathlib.Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "panel.duckdb"
RULE_DIRS = [ROOT / "rules" / "calibration", ROOT / "rules" / "compiled"]


def run(src, as_of, my, calibration_year=None, panel=PANEL, quiet=True, replay=False):
    started = dt.datetime.now().isoformat(timespec="seconds")
    tmp = panel.with_name(panel.stem + ".building.duckdb")
    t = {}
    t0 = time.time()
    ingest.ingest(pathlib.Path(src), tmp, as_of, quiet=quiet, replay=replay)
    t["ingest"] = time.time() - t0
    t1 = time.time()
    for year in sorted({my, calibration_year} - {None}, key=lambda y: y != my):
        verified, _ = engine.run(tmp, RULE_DIRS, year, quiet=quiet)
        if year == my:
            con = duckdb.connect(str(tmp))
            rule_update.compute(con, my, verified)
            con.close()
    t["rules"] = time.time() - t1
    t2 = time.time()
    counts = ops.verify(tmp, my, as_of)
    t["verify"] = time.time() - t2
    t3 = time.time()
    n_work = worklist.build(tmp, my, as_of)
    t["worklist"] = time.time() - t3
    con = duckdb.connect(str(tmp))
    n_pat = con.execute("SELECT count(*) FROM patients").fetchone()[0]
    feeds = con.execute("SELECT feed, status, last_record::VARCHAR, days_silent FROM feed_health").fetchall()
    con.execute("CREATE OR REPLACE TABLE run_info AS SELECT ?::DATE AS as_of, ? AS my, ? AS calibration_year",
                [as_of, my, calibration_year])
    con.close()
    os.replace(tmp, panel)
    t["total"] = time.time() - t0
    try:  # impact preview for rules awaiting approval (scratch copy; never affects the live panel)
        from panelpilot import preview
        preview.main()
    except Exception as e:
        print(f"preview skipped: {e}")
    with ops.connect() as oc:
        oc.execute("""INSERT INTO runs (started, as_of, my, panel, n_patients, seconds_ingest, seconds_rules, seconds_verify,
                      seconds_worklist, seconds_total, feeds, verified, reopened, still_pending, worklist_size)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (started, as_of, my, str(src), n_pat, t["ingest"], t["rules"], t["verify"], t["worklist"], t["total"],
                    json.dumps(feeds), counts["verified"], counts["reopened"], counts["still_pending"], n_work))
    print(f"nightly as_of={as_of} my={my}: {n_pat} patients | ingest {t['ingest']:.1f}s rules {t['rules']:.1f}s "
          f"verify {t['verify']:.2f}s worklist {t['worklist']:.1f}s total {t['total']:.1f}s | "
          f"verified {counts['verified']} reopened {counts['reopened']} pending {counts['still_pending']} | "
          f"worklist candidates {n_work} | stale feeds: {[f[0] for f in feeds if f[1] != 'OK'] or 'none'}")
    return t, counts


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(ROOT / "data" / "messy_csv"))
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    ap.add_argument("--my", type=int)
    ap.add_argument("--calibration-year", type=int, default=2025)
    ap.add_argument("--replay", action="store_true", help="treat records dated after --as-of as not yet arrived")
    a = ap.parse_args()
    run(a.src, a.as_of, a.my or int(a.as_of[:4]), a.calibration_year, replay=a.replay)
