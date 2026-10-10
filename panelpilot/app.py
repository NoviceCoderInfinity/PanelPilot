"""PanelPilot web app + JSON API. The API is the product surface (the web UI is one client of it; a mobile
team gets the same endpoints, documented at /docs as OpenAPI -- hard question 11).

Reads the panel built by the nightly job (data/panel.duckdb, read-only, swapped atomically) and writes only
outreach outcomes (data/ops.sqlite) and rule approvals (rules/approvals.json).

Run: python -m panelpilot.app   ->  http://127.0.0.1:8000
"""
import json
import os
import pathlib
import secrets
import statistics
import subprocess
import sys

import duckdb
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from panelpilot import explain, ops, rules as R

ROOT = pathlib.Path(__file__).resolve().parent.parent
PANEL = pathlib.Path(os.environ.get("PANELPILOT_PANEL", ROOT / "data" / "panel.duckdb"))
STATIC = pathlib.Path(__file__).resolve().parent / "static"
RULE_DIRS = ["rules/calibration", "rules/compiled", "rules/rejected"]
app = FastAPI(title="PanelPilot API", version="1.0",
              description="Care-gap worklist, verdict evidence, outreach verification and rule provenance.")


TOKEN_FILE = ROOT / "data" / "approver_token"


def approver_token():
    """Rule approval and admin actions change what runs against patients, so they need the approver token:
    PANELPILOT_APPROVER_TOKEN, or a random one generated on first start in data/approver_token (mode 600)."""
    if os.environ.get("PANELPILOT_APPROVER_TOKEN"):
        return os.environ["PANELPILOT_APPROVER_TOKEN"]
    if not TOKEN_FILE.exists():
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(secrets.token_urlsafe(18))
        TOKEN_FILE.chmod(0o600)
    return TOKEN_FILE.read_text().strip()


def require_approver(x_approver_token: str = Header(default="")):
    if not secrets.compare_digest(x_approver_token.encode(), approver_token().encode()):
        raise HTTPException(403, "approver token required (X-Approver-Token header)")


def panel():
    if not PANEL.exists():
        raise HTTPException(503, "panel not built yet: run the nightly job")
    return duckdb.connect(str(PANEL), read_only=True)


def run_info(con):
    as_of, my, cal = con.execute("SELECT as_of::VARCHAR, my, calibration_year FROM run_info").fetchone()
    return {"as_of": as_of, "my": my, "calibration_year": cal}


def rows(con, sql, params=()):
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/today", summary="Today's call list (top N patients) with one-sentence reasons")
def today(n: int = 20):
    con = panel()
    info = run_info(con)
    sup = ops.active_suppressions(info["as_of"])
    wl = rows(con, """SELECT w.rank, w.patient, w.score, w.why, w.n_gaps, w.rule_ids, w.actions, w.components,
                             p.first, p.last, p.sex, datesub('year', p.birthdate, ?::DATE) AS age
                      FROM worklist w JOIN patients p USING (patient) ORDER BY w.rank""", [info["as_of"]])
    wl = [r for r in wl if r["patient"] not in sup]  # logged today -> off the list immediately
    feeds = rows(con, "SELECT feed, status, last_record::VARCHAR AS last_record, days_silent, baseline_per_day FROM feed_health ORDER BY feed")
    cant = rows(con, "SELECT rule_id, reason, patients FROM cant_tell")
    banners = [f"{f['feed'].capitalize()} feed silent since {f['last_record']} ({f['days_silent']} days; normally "
               f"~{f['baseline_per_day']:.0f} records/day). Patients whose status depends on it are held as "
               f"'can't tell', not shown as gaps." for f in feeds if f["status"] != "OK"]
    for r in wl:
        r["components"] = json.loads(r["components"])
    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'newly_eligible'").fetchone()[0]:
        for rid, n_fail, n_new in con.execute("""SELECT rule_id, count(*) FILTER (WHERE status = 'fail'), count(*)
                                             FROM newly_eligible GROUP BY rule_id""").fetchall():
            banners.append(f"Guideline update: {n_new} patients are newly eligible under {rid}; {n_fail} have no "
                           f"qualifying record and are boosted on this list.")
    with ops.connect() as oc:
        counts = {s: n for s, n in oc.execute("SELECT status, count(*) FROM outreach WHERE my = ? GROUP BY status", (info["my"],))}
    return {**info, "list": wl[:n], "candidates": len(wl), "banners": banners, "feeds": feeds, "cant_tell": cant,
            "outreach_counts": counts}


def _quotes(rule_json):
    out = {}

    def walk(c, path):
        kind = (set(c) - {"quote", "note", "derived"}).pop()
        if kind in ("all", "any"):
            for i, s in enumerate(c[kind]):
                walk(s, f"{path}.{kind}[{i}]")
        elif kind == "not":
            walk(c["not"], path + ".not")
        else:
            out[path] = {"quote": c.get("quote"), "note": c.get("note"), "criterion": {kind: c[kind]}}
    walk(rule_json["denominator"], "denominator")
    walk(rule_json["numerator"], "numerator")
    for i, e in enumerate(rule_json.get("exclusions", [])):
        walk(e, f"exclusions[{i}]")
    return out


def _raw(con, ref, src):
    if ":" not in ref:
        return {"ref": ref, "raw": None, "cmd": None}
    table, line = ref.split(":")
    r = con.execute(f"SELECT * EXCLUDE (_line) FROM raw_{table} WHERE _line = ?", [int(line)]).fetchone()
    return {"ref": ref, "file": f"{table}.csv", "line": int(line), "cmd": f"sed -n '{line}p' {src}/{table}.csv",
            "raw": ",".join("" if v is None else str(v) for v in r) if r else None}


@app.get("/api/patient/{patient}", summary="Every verdict for a patient, down to source quotes and raw export lines")
def patient(patient: str):
    con = panel()
    info = run_info(con)
    src = con.execute("SELECT src_dir FROM meta").fetchone()[0]
    p = rows(con, "SELECT patient, first, last, sex, birthdate::VARCHAR AS birthdate, deathdate::VARCHAR AS deathdate, _line FROM patients WHERE patient = ?", [patient])
    if not p:
        raise HTTPException(404, "unknown patient")
    out = {**info, "patient": p[0], "patient_record": _raw(con, f"patients:{p[0]['_line']}", src), "verdicts": []}
    for v in rows(con, """SELECT v.rule_id, v.rule_version, v.my, v.status, v.why, v.evidence, m.title, m.action, m.doc_id,
                                 m.quote AS rule_quote, m.rule_json
                          FROM verdicts v JOIN rules_meta m USING (rule_id) WHERE v.patient = ? ORDER BY v.my DESC, v.rule_id""", [patient]):
        rule_json = json.loads(v.pop("rule_json"))
        quotes = _quotes(rule_json)
        leaves = []
        for leaf in json.loads(v.pop("evidence")):
            q = quotes.get(leaf["path"], {})
            absence = None
            if leaf["v"] is False and not leaf["records"] and not leaf["path"].startswith("exclusions"):
                absence = explain.absence_cmd(rule_json["denominator" if leaf["path"].startswith("denominator") else "numerator"],
                                              leaf["path"], patient, src)
            leaves.append({**leaf, **q, "absence_cmd": absence, "records": [_raw(con, r, src) for r in (leaf["records"] or [])[:3]],
                           "unusable": [_raw(con, r, src) for r in (leaf["unusable"] or [])[:3]]})
        out["verdicts"].append({**v, "criteria": leaves})
    with ops.connect() as oc:
        out["outreach"] = [dict(r) for r in oc.execute("SELECT * FROM outreach WHERE patient = ? ORDER BY id DESC", (patient,))]
    return out


class Outcome(BaseModel):
    patient: str
    rule_ids: list[str]
    outcome: str          # scheduled | left_message | declined | bad_contact
    note: str = ""


@app.post("/api/outreach", summary="Log a contact outcome. 'scheduled' starts verification; it does not close the gap.")
def log_outreach(o: Outcome):
    if o.outcome not in ops.OUTCOMES:
        raise HTTPException(400, f"outcome must be one of {list(ops.OUTCOMES)}")
    con = panel()
    info = run_info(con)
    days = [json.loads(r[0]).get("verify_within_days", ops.DEFAULT_VERIFY_DAYS)
            for r in con.execute(f"SELECT rule_json FROM rules_meta WHERE rule_id IN ({','.join('?' * len(o.rule_ids))})", o.rule_ids).fetchall()]
    oid = ops.log_outcome(o.patient, o.rule_ids, info["my"], o.outcome, info["as_of"], o.note, max(days or [ops.DEFAULT_VERIFY_DAYS]))
    return {"id": oid, "status": ops.OUTCOMES[o.outcome][0]}


@app.get("/api/followups", summary="Outreach items: pending verification, verified (with evidence), reopened (with reason)")
def followups():
    con = panel()
    info = run_info(con)
    with ops.connect() as oc:
        items = [dict(r) for r in oc.execute("SELECT * FROM outreach WHERE my = ? ORDER BY id DESC", (info["my"],))]
    names = {r[0]: f"{r[1]} {r[2]}" for r in con.execute(
        "SELECT patient, first, last FROM patients WHERE patient IN (SELECT unnest(?::VARCHAR[]))", [[i["patient"] for i in items]]).fetchall()} if items else {}
    for i in items:
        i["name"] = names.get(i["patient"])
        i["rule_ids"] = json.loads(i["rule_ids"])
        i["evidence"] = json.loads(i["evidence"]) if i["evidence"] else []
        i["gaps"] = json.loads(i["gaps"]) if i.get("gaps") else {}
    return {**info, "items": items}


@app.get("/api/rules", summary="Every rule: provenance, verification state, results per year, caveats")
def rules_view():
    con = panel()
    runs = rows(con, "SELECT * FROM rule_runs ORDER BY rule_id, my")
    cat = R.catalog(RULE_DIRS)
    for e in cat:
        e["runs"] = [r for r in runs if r["rule_id"] == e["id"]]
    preview = ROOT / "data" / "preview.json"
    pv = json.loads(preview.read_text()) if preview.exists() else {}
    for e in cat:
        e["preview"] = pv.get(e["id"]) if e["state"] == "awaiting_approval" else None
    eval_report = ROOT / "rules" / "eval_report.json"
    ev = json.loads(eval_report.read_text()) if eval_report.exists() else None
    if ev:
        ev = {"overall_rule_recall": ev.get("overall_recall_extracted_docs"), "criterion_agreement": ev.get("criterion_agreement"),
              "per_doc": {d: {k: v.get(k) for k in ("gold", "compiled", "recall", "unmatched_gold")} for d, v in ev["docs"].items()},
              "rejected_candidates": ev.get("rejected_candidates"), "rejections_by_reason": ev.get("rejections_by_reason"),
              "note": "provisional until every gold file is signed off (gold/README.md)"}
    return {"rules": cat, "eval": ev}


class Approval(BaseModel):
    file: str


@app.post("/api/rules/approve", summary="Human sign-off on a rule's exact content (re-verified first)")
def approve(a: Approval, _=Depends(require_approver)):
    f = (ROOT / a.file).resolve()
    if ROOT not in f.parents or not f.exists():
        raise HTTPException(400, "unknown rule file")
    try:
        rid = R.approve(f)
    except R.RuleRejected as e:
        raise HTTPException(400, str(e))
    return {"approved": rid, "note": "takes effect at the next nightly run"}


@app.get("/api/health", summary="Feed freshness, data-quality actions, nightly run history with timings")
def health():
    con = panel()
    with ops.connect() as oc:
        runs = [dict(r) for r in oc.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 30")]
    return {**run_info(con),
            "feeds": rows(con, "SELECT feed, status, last_record::VARCHAR AS last_record, days_silent, baseline_per_day, last14_per_day FROM feed_health ORDER BY feed"),
            "dq": rows(con, "SELECT tbl, kind, action, count(*) AS n FROM dq_issues GROUP BY ALL ORDER BY n DESC"),
            "runs": runs}


@app.get("/api/metrics", summary="Is it working? Verified closures, reopen rate, time to verified care, measure rates")
def metrics():
    con = panel()
    info = run_info(con)
    with ops.connect() as oc:
        items = [dict(r) for r in oc.execute("SELECT * FROM outreach WHERE my = ?", (info["my"],))]
    scheduled = [i for i in items if i["outcome"] == "scheduled"]
    verified = [i for i in scheduled if i["status"] == "verified"]
    reopened = [i for i in scheduled if i["status"] in ("reopened", "superseded")]
    closed = [i for i in scheduled if i["status"] == "closed"]
    # a verified closure = one gap closed by a record dated on/after the contact (no credit for aged-out
    # patients or for records that predate the call)
    gap_states = [g["state"] for i in scheduled if i.get("gaps") for g in json.loads(i["gaps"]).values()]
    days = []
    import datetime as dt
    for i in verified:
        days.append((dt.date.fromisoformat(i["resolved_on"]) - dt.date.fromisoformat(i["logged_on"])).days)
    resolved = len(verified) + len(reopened)
    return {**info,
            "contacts_logged": len(items),
            "scheduled": len(scheduled),
            "verified_closures": gap_states.count("verified"),
            "patients_verified": len(verified),
            "reopened": len(reopened),
            "closed_without_credit": len(closed),
            "verified_rate": round(len(verified) / resolved, 3) if resolved else None,
            "median_days_to_verified": statistics.median(days) if days else None,
            "measure_rates": rows(con, """SELECT rule_id, my, denominator, pass, fail, indeterminate,
                                          round(100.0 * pass / nullif(denominator, 0), 1) AS pass_pct
                                          FROM rule_runs WHERE status = 'ran' ORDER BY rule_id, my"""),
            "note": "Pilot success = verified closures (gaps closed by a record dated after the contact), falling reopen rate, rising measure pass rate."}


@app.post("/api/admin/nightly", summary="Demo helper: run the nightly job now for a given as-of date")
def nightly_now(as_of: str, replay: bool = True, _=Depends(require_approver)):
    r = subprocess.run([sys.executable, "-m", "panelpilot.nightly", "--as-of", as_of] + (["--replay"] if replay else []),
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        raise HTTPException(500, r.stderr[-2000:])
    return {"log": r.stdout.strip()}


if __name__ == "__main__":
    import uvicorn
    approver_token()
    print(f"approver token: {TOKEN_FILE} (or set PANELPILOT_APPROVER_TOKEN)")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
