"""Messy-data handling and the verify-or-reopen loop, on tiny hand-made exports (run: python -m pytest -q)."""
import json

import duckdb
import pytest
from fastapi import HTTPException

from panelpilot import app, fhir, ingest, ops

PID = "p1"


def export(tmp_path, **rows):
    """Write a minimal six-table export; `rows` maps table -> list of dicts (missing columns left empty)."""
    base = {"patients": [{"Id": PID, "BIRTHDATE": "1960-01-01", "FIRST": "Ana", "LAST": "Test", "GENDER": "F"}]}
    base.update(rows)
    src = tmp_path / "export"
    src.mkdir()
    for t, header in fhir.HEADERS.items():
        cols = header.split(",")
        lines = [header] + [",".join(r.get(c, "") for c in cols) for r in base.get(t, [])]
        (src / f"{t}.csv").write_text("\n".join(lines) + "\n")
    db = tmp_path / "panel.duckdb"
    ingest.ingest(src, db, "2025-12-31", quiet=True)
    return duckdb.connect(str(db))


def obs(date, value="7.0"):
    return {"DATE": date, "PATIENT": PID, "ENCOUNTER": "e1", "CATEGORY": "laboratory", "CODE": "4548-4",
            "DESCRIPTION": "HbA1c", "VALUE": value, "UNITS": "%", "TYPE": "numeric"}


def test_resend_with_reformatted_date_is_a_duplicate(tmp_path):
    con = export(tmp_path, observations=[obs("2025-03-01T10:00:37Z"), obs("03/01/2025 10:00")])
    assert con.execute("SELECT count(*) FILTER (WHERE is_dup), count(*) FILTER (WHERE usable) FROM observations").fetchone() == (1, 1)


def test_different_values_on_the_same_day_are_not_duplicates(tmp_path):
    con = export(tmp_path, observations=[obs("2025-03-01T10:00:00Z", "7.0"), obs("2025-03-01T10:00:00Z", "8.1")])
    assert con.execute("SELECT count(*) FROM observations WHERE usable").fetchone()[0] == 2


def test_visit_without_start_is_dated_by_its_stop(tmp_path):
    con = export(tmp_path, encounters=[{"Id": "e1", "START": "", "STOP": "2025-05-02T11:00:00Z", "PATIENT": PID,
                                        "ENCOUNTERCLASS": "wellness", "CODE": "410620009"}])
    ts, usable = con.execute("SELECT ts::VARCHAR, usable FROM encounters").fetchone()
    assert ts.startswith("2025-05-02") and usable
    assert con.execute("SELECT action FROM dq_issues WHERE kind = 'date_from_stop'").fetchone()[0] == "normalized"


def test_unit_less_value_counts_as_a_test_but_never_as_a_value(tmp_path):
    con = export(tmp_path, observations=[{**obs("2025-03-01T10:00:00Z", "9.5"), "UNITS": ""}])
    value, usable = con.execute("SELECT value, usable FROM observations").fetchone()
    assert usable and value is None


# ---- verify-or-reopen, one gap at a time ------------------------------------------------------------------
def panel(tmp_path, verdicts, records):
    db = tmp_path / "panel.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE verdicts (rule_id VARCHAR, patient VARCHAR, my INT, status VARCHAR, why VARCHAR, evidence VARCHAR)")
    for t in ("observations", "procedures", "encounters", "immunizations", "conditions"):
        con.execute(f"CREATE TABLE {t} (_line BIGINT, ts TIMESTAMP)")
    for rid, status, line in verdicts:
        ev = json.dumps([{"path": "numerator", "v": status == "pass", "records": [f"procedures:{line}"] if line else []}])
        con.execute("INSERT INTO verdicts VALUES (?, ?, 2026, ?, NULL, ?)", [rid, PID, status, ev])
    for line, ts in records:
        con.execute("INSERT INTO procedures VALUES (?, ?)", [line, ts])
    con.close()
    return db


@pytest.fixture
def ops_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "OPS_DB", tmp_path / "ops.sqlite")


def item():
    with ops.connect() as con:
        return dict(con.execute("SELECT * FROM outreach").fetchone())


def test_partial_care_is_credited_and_only_open_gaps_reopen(tmp_path, ops_db):
    ops.log_outcome(PID, ["ALC", "HCV"], 2026, "scheduled", "2026-08-01")
    db = panel(tmp_path, [("ALC", "pass", 7), ("HCV", "fail", None)], [(7, "2026-08-03 10:00")])
    ops.verify(db, 2026, "2026-08-04")
    i = item()
    assert i["status"] == "pending" and json.loads(i["gaps"])["ALC"]["state"] == "verified"
    ops.verify(db, 2026, "2026-09-01")  # past verify_by (30 days)
    i = item()
    gaps = json.loads(i["gaps"])
    assert i["status"] == "reopened" and gaps["ALC"]["state"] == "verified" and gaps["HCV"]["state"] == "open"
    assert "verified: ALC" in i["reason"] and "for HCV" in i["reason"]


def test_record_from_before_the_contact_gets_no_credit(tmp_path, ops_db):
    ops.log_outcome(PID, ["ALC"], 2026, "scheduled", "2026-08-01")
    db = panel(tmp_path, [("ALC", "pass", 7)], [(7, "2026-07-20 10:00")])  # late-arriving, predates the call
    ops.verify(db, 2026, "2026-08-04")
    assert item()["status"] == "closed"


def test_patient_who_left_the_denominator_gets_no_credit(tmp_path, ops_db):
    ops.log_outcome(PID, ["ALC"], 2026, "scheduled", "2026-08-01")
    db = panel(tmp_path, [], [])
    ops.verify(db, 2026, "2026-08-04")
    assert item()["status"] == "closed"


def test_approval_endpoints_require_the_approver_token(monkeypatch):
    monkeypatch.setenv("PANELPILOT_APPROVER_TOKEN", "s3cret")
    with pytest.raises(HTTPException):
        app.require_approver("")
    with pytest.raises(HTTPException):
        app.require_approver("wrong")
    app.require_approver("s3cret")
