"""Outreach state + the verify-or-reopen loop (hard question 9).

"Handled" is never the end state. When the care manager logs an outcome:
  scheduled     -> status 'pending': the patient is off today's list. Each gap on the row is tracked on its own
                   (one visit rarely closes all of them). A gap is VERIFIED only when the nightly run finds a
                   record dated on/after the contact that makes its rule pass. Gaps still open at `verify_by`
                   are REOPENED with the reason; the verified ones keep their credit and evidence.
                   Closed without credit: the patient left the denominator (aged out, died, excluded) or the
                   rule passes only on a record dated BEFORE the contact (late-arriving data). Those are not
                   counted as outreach successes.
  left_message  -> 'retry' for 3 days       declined -> 'declined' for 90 days
  pcp_review    -> 'pcp_review' for 30 days (care happened but goal unmet: the PCP's call, not a reminder)
  bad_contact   -> 'bad_contact' (off the list; surfaced as a data-quality item)
State lives in SQLite (data/ops.sqlite), separate from the panel DB that is rebuilt nightly.
"""
import datetime as dt
import json
import os
import pathlib
import sqlite3

import duckdb

ROOT = pathlib.Path(__file__).resolve().parent.parent
OPS_DB = pathlib.Path(os.environ.get("PANELPILOT_OPS", ROOT / "data" / "ops.sqlite"))
OUTCOMES = {"scheduled": ("pending", None), "left_message": ("retry", 3), "declined": ("declined", 90),
            "pcp_review": ("pcp_review", 30), "bad_contact": ("bad_contact", None)}
DEFAULT_VERIFY_DAYS = 30


def connect(path=None):
    path = path or OPS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS outreach (
            id INTEGER PRIMARY KEY, patient TEXT NOT NULL, rule_ids TEXT NOT NULL, my INTEGER NOT NULL,
            outcome TEXT NOT NULL, status TEXT NOT NULL, logged_on TEXT NOT NULL, retry_after TEXT,
            verify_by TEXT, resolved_on TEXT, evidence TEXT, reason TEXT, note TEXT, by_user TEXT, gaps TEXT);
        CREATE INDEX IF NOT EXISTS outreach_patient ON outreach(patient);
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, started TEXT, as_of TEXT, my INTEGER, panel TEXT, n_patients INTEGER,
            seconds_ingest REAL, seconds_rules REAL, seconds_verify REAL, seconds_worklist REAL, seconds_total REAL,
            feeds TEXT, verified INTEGER, reopened INTEGER, still_pending INTEGER, worklist_size INTEGER);
    """)
    if "gaps" not in [r[1] for r in con.execute("PRAGMA table_info(outreach)")]:
        con.execute("ALTER TABLE outreach ADD COLUMN gaps TEXT")  # per-rule progress, JSON
    return con


def log_outcome(patient, rule_ids, my, outcome, today, note="", verify_days=None, by_user="care_manager"):
    status, days = OUTCOMES[outcome]
    today_d = dt.date.fromisoformat(today)
    retry_after = (today_d + dt.timedelta(days=days)).isoformat() if days else None
    verify_by = (today_d + dt.timedelta(days=verify_days or DEFAULT_VERIFY_DAYS)).isoformat() if status == "pending" else None
    with connect() as con:
        # a new contact supersedes an earlier reopened item for the same patient
        con.execute("UPDATE outreach SET status = 'superseded' WHERE patient = ? AND status = 'reopened'", (patient,))
        cur = con.execute("""INSERT INTO outreach (patient, rule_ids, my, outcome, status, logged_on, retry_after,
                             verify_by, note, by_user) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                          (patient, json.dumps(sorted(rule_ids)), my, outcome, status, today, retry_after, verify_by,
                           note, by_user))
        return cur.lastrowid


def active_suppressions(today):
    """Patients who must not be on today's list: pending verification, retry/declined not yet due, bad contact."""
    with connect() as con:
        rows = con.execute("""SELECT patient, status FROM outreach WHERE status IN ('pending', 'bad_contact')
                              OR (status IN ('retry', 'declined', 'pcp_review') AND retry_after > ?)""", (today,)).fetchall()
    return {r["patient"]: r["status"] for r in rows}


def reopened(my):
    with connect() as con:
        return {r["patient"]: dict(r) for r in con.execute(
            "SELECT * FROM outreach WHERE status = 'reopened' AND my = ? ORDER BY id", (my,)).fetchall()}


def verify(panel_db, my, today):
    """Nightly: resolve every pending item against the freshly computed verdicts, one gap (rule) at a time.

    Per rule:  verified       passes on a record dated on/after the contact (credited, with evidence)
               prior_record   passes only on records dated before the contact (late-arriving data; no credit)
               no_longer_due  left the denominator: aged out, deceased, excluded (no credit)
               open           still failing / can't tell
    Item: all rules resolved -> 'verified' if any rule was verified, else 'closed' (no credit).
          past verify_by with open rules -> 'reopened' for the open rules only (or held if a feed is dead).
    """
    pan = duckdb.connect(str(panel_db))
    from panelpilot.worklist import needs  # short plain names ("colorectal screening") for the reasons
    has_meta = pan.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'rules_meta'").fetchone()[0]
    label = {rid: lab for rid, _, lab in needs({r: (json.loads(j), t) for r, j, t in
                                               pan.execute("SELECT rule_id, rule_json, title FROM rules_meta").fetchall()})} if has_meta else {}
    names = lambda rids: ", ".join(dict.fromkeys(label.get(r, r) for r in rids))
    counts = {"verified": 0, "reopened": 0, "still_pending": 0, "closed": 0}
    with connect() as con:
        for row in con.execute("SELECT * FROM outreach WHERE status = 'pending' AND my = ?", (my,)).fetchall():
            rule_ids = json.loads(row["rule_ids"])
            vs = {r[0]: (r[1], r[2], r[3]) for r in pan.execute(
                f"""SELECT rule_id, status, why, evidence FROM verdicts WHERE my = ? AND patient = ?
                    AND rule_id IN ({','.join('?' * len(rule_ids))})""", [my, row["patient"], *rule_ids]).fetchall()}
            gaps = {}
            for r in rule_ids:
                st, why, ev = vs.get(r, ("not_in_denominator", None, None))
                if st in ("not_in_denominator", "excluded"):
                    gaps[r] = {"state": "no_longer_due"}
                elif st == "pass":
                    recs = [{"record": ref, "date": _record_date(pan, ref)}
                            for leaf in json.loads(ev) if leaf["path"].startswith("numerator") and leaf["v"]
                            for ref in leaf["records"] or []]
                    after = [x for x in recs if x["date"] and x["date"] >= row["logged_on"]]
                    gaps[r] = {"state": "verified", "evidence": after} if after else {"state": "prior_record", "evidence": recs}
                else:
                    new = [d for leaf in json.loads(ev or "[]") if leaf["path"].startswith("numerator")
                           for d in (_record_date(pan, x) for x in leaf["records"] or []) if d and d >= row["logged_on"]]
                    gaps[r] = {"state": "open", "verdict": st, "why": why, "care_since_contact": sorted(set(new))}
            ver = [r for r, g in gaps.items() if g["state"] == "verified"]
            opn = [r for r, g in gaps.items() if g["state"] == "open"]
            evidence = [{"rule_id": r, **x} for r in ver for x in gaps[r]["evidence"]]
            done = f"verified: {names(ver)}" if ver else ""
            if not opn:
                status = "verified" if ver else "closed"
                reason = done or ("no credit: " + "; ".join(
                    f"{r} {'passes on a record dated before the contact (late-arriving data)' if g['state'] == 'prior_record' else 'no longer due (aged out, deceased or excluded)'}"
                    for r, g in gaps.items()))
                con.execute("UPDATE outreach SET status=?, resolved_on=?, evidence=?, reason=?, gaps=? WHERE id=?",
                            (status, today, json.dumps(evidence), reason, json.dumps(gaps), row["id"]))
                counts[status] += 1
            elif today > row["verify_by"]:
                waiting = [gaps[r]["why"] for r in opn if gaps[r]["verdict"] == "indeterminate" and gaps[r]["why"]]
                if waiting:  # a dead feed cannot prove the care didn't happen: hold, and say why
                    con.execute("UPDATE outreach SET reason=?, evidence=?, gaps=? WHERE id=?",
                                ("verification on hold: " + "; ".join(sorted(set(waiting))), json.dumps(evidence),
                                 json.dumps(gaps), row["id"]))
                    counts["still_pending"] += 1
                    continue
                # Distinguish "nothing happened" (call again) from "care happened, goal still not met" (PCP).
                happened = [f"{label.get(r, r)} record {d}" for r in opn for d in gaps[r]["care_since_contact"]]
                missing = [r for r in opn if not gaps[r]["care_since_contact"]]
                parts = [done] if done else []
                if happened:
                    parts.append(f"care happened ({', '.join(happened)}) but the goal is still not met: "
                                 f"clinical follow-up with PCP, not another reminder call")
                if missing:
                    parts.append(f"marked '{row['outcome']}' on {row['logged_on']}, but no qualifying record by "
                                 f"{row['verify_by']} for {names(missing)}")
                con.execute("UPDATE outreach SET status='reopened', resolved_on=?, evidence=?, reason=?, gaps=? WHERE id=?",
                            (today, json.dumps(evidence), " | ".join(parts), json.dumps(gaps), row["id"]))
                counts["reopened"] += 1
            else:
                con.execute("UPDATE outreach SET evidence=?, gaps=?, reason=? WHERE id=?",
                            (json.dumps(evidence), json.dumps(gaps),
                             f"{len(ver)} of {len(rule_ids)} gaps verified so far" if ver else None, row["id"]))
                counts["still_pending"] += 1
    pan.close()
    return counts


def _record_date(pan, ref):
    table, line = ref.split(":")
    if table not in ("observations", "procedures", "encounters", "immunizations", "conditions"):
        return None
    r = pan.execute(f"SELECT ts::DATE::VARCHAR FROM {table} WHERE _line = ?", [int(line)]).fetchone()
    return r[0] if r else None
