"""Demo helper: make "care actually happening" arrive in tomorrow's export, the way an EHR feed would.

Appends rows to the messy export (never edits existing rows) and records each arrival in
data/arrivals.csv, so a verified closure can be traced to the exact appended line.

  python -m panelpilot.simulate care <patient> hba1c --on 2026-08-02 --value 7.4
  python -m panelpilot.simulate care <patient> bp    --on 2026-08-02 --value 128/82
  python -m panelpilot.simulate care <patient> fobt  --on 2026-08-02
  python -m panelpilot.simulate care <patient> well_visit --on 2026-08-02
  python -m panelpilot.simulate care <patient> alcohol_screening --on 2026-08-02   # any concept in valuesets.json
  python -m panelpilot.simulate restore-lab-feed      # the dead lab feed's backlog arrives late
"""
import argparse
import csv
import hashlib
import pathlib

import duckdb

from panelpilot.ingest import VALUESETS

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPORT = ROOT / "data" / "messy_csv"
CLEAN = ROOT / "synthea" / "output" / "csv"
ARRIVALS = ROOT / "data" / "arrivals.csv"
PANEL = ROOT / "data" / "panel.duckdb"


def _append(table, row):
    f = EXPORT / f"{table}.csv"
    if f.is_symlink():
        raise SystemExit(f"{f} is a symlink to the clean export; refusing to modify it")
    with open(f, "rb") as fh:
        n_lines = sum(1 for _ in fh)
    with open(f, "a", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerow(row)
    _log(table, n_lines + 1, row)
    return n_lines + 1


def _log(table, line, row):
    new = not ARRIVALS.exists()
    with open(ARRIVALS, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["table", "line", "row"])
        w.writerow([table, line, ",".join(map(str, row))])


def _encounter(patient, ts, klass, code, desc):
    eid = hashlib.md5(f"{patient}{ts}{code}".encode()).hexdigest()
    _append("encounters", [eid, ts, ts, patient, "", "", "", klass, code, desc, "0", "0", "0", "", ""])
    return eid


def care(patient, kind, on, value=None):
    ts = f"{on}T10:00:00Z"
    if kind == "hba1c":
        e = _encounter(patient, ts, "ambulatory", "185349003", "Encounter for check up (procedure)")
        line = _append("observations", [ts, patient, e, "laboratory", "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood",
                                        value or "7.4", "%", "numeric"])
    elif kind == "bp":
        s, d = (value or "128/82").split("/")
        e = _encounter(patient, ts, "ambulatory", "185349003", "Encounter for check up (procedure)")
        _append("observations", [ts, patient, e, "vital-signs", "8462-4", "Diastolic Blood Pressure", d, "mm[Hg]", "numeric"])
        line = _append("observations", [ts, patient, e, "vital-signs", "8480-6", "Systolic Blood Pressure", s, "mm[Hg]", "numeric"])
    elif kind == "fobt":
        e = _encounter(patient, ts, "ambulatory", "185349003", "Encounter for check up (procedure)")
        line = _append("procedures", [ts, ts, patient, e, "http://snomed.info/sct", "104435004",
                                      "Screening for occult blood in feces (procedure)", "0", "", ""])
    elif kind == "well_visit":
        line = _encounter(patient, ts, "wellness", "410620009", "Well child visit (procedure)")
    elif kind in VALUESETS and VALUESETS[kind].get("codes"):
        line = _generic(patient, kind, ts, value)
    else:
        raise SystemExit(f"unknown or unmapped concept {kind}")
    print(f"arrived: {kind} for {patient} on {on} ({value or ''}) -> see data/arrivals.csv")
    return line


def _generic(patient, concept, ts, value):
    """Any mapped concept: the new row copies the shape (code, description, category, unit) of a real record
    of that concept in the panel, so it is exactly what the EHR would send."""
    vs = VALUESETS[concept]
    table, codes = vs["table"], vs["codes"]
    con = duckdb.connect(str(PANEL), read_only=True)
    if table == "observations":
        ex = con.execute(f"""SELECT r.CODE, r.DESCRIPTION, r.CATEGORY, r.VALUE, r.UNITS, r.TYPE FROM raw_observations r
                             JOIN observations o USING (_line) WHERE o.code IN ({','.join('?' * len(codes))}) AND o.usable
                             LIMIT 1""", codes).fetchone()
    else:
        ex = con.execute(f"""SELECT code, description FROM {table} WHERE code IN ({','.join('?' * len(codes))})
                             AND usable LIMIT 1""", codes).fetchone()
    con.close()
    code, desc = (ex[0], ex[1]) if ex else (codes[0], concept.replace("_", " "))
    if table == "encounters":
        return _encounter(patient, ts, vs.get("class", "ambulatory"), code, desc)
    e = _encounter(patient, ts, "ambulatory", "185349003", "Encounter for check up (procedure)")
    if table == "observations":
        cat, val, unit, typ = ex[2:] if ex else ("survey", "0", "", "numeric")
        return _append("observations", [ts, patient, e, cat, code, desc, value or val, unit, typ])
    if table == "procedures":
        return _append("procedures", [ts, ts, patient, e, "http://snomed.info/sct", code, desc, "0", "", ""])
    if table == "immunizations":
        return _append("immunizations", [ts, patient, e, code, desc, "0"])
    raise SystemExit(f"cannot simulate {concept} (table {table})")


def restore_lab_feed():
    """The dead window's lab results finally arrive (late), appended from the clean export."""
    con = duckdb.connect()
    window = con.execute(f"""SELECT detail FROM read_csv('{EXPORT / '_defects.csv'}', all_varchar = true)
                             WHERE kind = 'dead_feed' LIMIT 1""").fetchone()[0]
    start, end = window.split(":")
    rows = con.execute(f"""SELECT * FROM read_csv('{CLEAN / 'observations.csv'}', all_varchar = true)
                           WHERE CATEGORY = 'laboratory' AND DATE[:10] BETWEEN '{start}' AND '{end}'""").fetchall()
    f = EXPORT / "observations.csv"
    with open(f, "rb") as fh:
        first = sum(1 for _ in fh) + 1
    with open(f, "a", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    _log("observations", f"{first}-{first + len(rows) - 1}", [f"lab backlog {start}..{end}: {len(rows)} rows"])
    print(f"lab feed restored: {len(rows)} late results for {start}..{end} appended")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("care")
    c.add_argument("patient")
    c.add_argument("kind", help="hba1c | bp | fobt | well_visit | any mapped concept in valuesets.json")
    c.add_argument("--on", required=True)
    c.add_argument("--value")
    sub.add_parser("restore-lab-feed")
    a = ap.parse_args()
    if a.cmd == "care":
        care(a.patient, a.kind, a.on, a.value)
    else:
        restore_lab_feed()
