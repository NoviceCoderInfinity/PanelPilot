"""Prove a verdict from raw data: verdict -> criteria -> the exact lines of the ingested export.

Usage:
  python -m panelpilot.explain <db> <rule_id> <patient_id> [--my 2025]
  python -m panelpilot.explain <db> <rule_id> --random 5 [--my 2025]     # the walkthrough drill
Each record is printed with `sed -n '<line>p' <file>` so anyone can re-check it without our code.
"""
import argparse
import json
import pathlib
import subprocess

import duckdb



def explain(con, rule_id, patient, my):
    src = pathlib.Path(con.execute("SELECT src_dir FROM meta").fetchone()[0])
    p = con.execute("SELECT first, last, birthdate, deathdate, sex, _line FROM patients WHERE patient = ?", [patient]).fetchone()
    v = con.execute("SELECT status, why, evidence, rule_version FROM verdicts WHERE rule_id = ? AND patient = ? AND my = ?",
                    [rule_id, patient, my]).fetchone()
    print(f"\n=== {rule_id} / {patient}  ({p[0]} {p[1]}, {p[4]}, born {p[2]}{', died ' + str(p[3]) if p[3] else ''})")
    if v is None:
        print(f"    not in denominator for {my}")
        return
    status, why, evidence, version = v
    print(f"    VERDICT: {status.upper()}   (rule {rule_id} {version}, measurement year {my})" + (f"\n    why: {why}" if why else ""))
    row = con.execute("SELECT rule_json FROM rules_meta WHERE rule_id = ?", [rule_id]).fetchone()
    quotes = {}
    if row:
        rule = json.loads(row[0])
        walk(rule["denominator"], "denominator", quotes)
        walk(rule["numerator"], "numerator", quotes)
        for i, e in enumerate(rule.get("exclusions", [])):
            walk(e, f"exclusions[{i}]", quotes)
    for leaf in json.loads(evidence):
        mark = {True: "MET", False: "not met", None: "UNKNOWN"}[leaf["v"]]
        print(f"  - {leaf['path']} [{leaf['kind']}] -> {mark}" + (f'   source: "{quotes[leaf["path"]]}"' if leaf["path"] in quotes else ""))
        for ref in (leaf["records"] or [])[:3]:
            show(ref, src, "record")
        for ref in (leaf["unusable"] or [])[:3]:
            show(ref, src, "unusable")
        if leaf["why"]:
            print(f"      why: {leaf['why']}")
        if leaf["v"] is False and not leaf["records"] and leaf["path"].startswith(("denominator", "numerator")):
            cmd = absence_cmd(rule["denominator"] if leaf["path"].startswith("denominator") else rule["numerator"],
                              leaf["path"], patient, src) if row else None
            if cmd:
                print(f"      absence check (should print nothing in the window): {cmd}")


def absence_cmd(tree, path, patient, src):
    """grep command proving no qualifying record exists for this patient (a FAIL needs proof of absence)."""
    from panelpilot.engine import VALUESETS
    c = tree
    for part in path.split(".")[1:]:
        k, i = part.rstrip("]").split("[")
        c = c[k][int(i)]
    kind = (set(c) - {"quote", "note", "derived"}).pop()
    vs = VALUESETS.get(c[kind].get("concept") if isinstance(c[kind], dict) else None, {})
    if kind == "bp":
        vs = {"table": "observations", "codes": ["8480-6", "8462-4"]}
    if "codes" not in vs:
        return None
    return f"grep '{patient}' {src}/{vs['table']}.csv | grep -E ',({'|'.join(vs['codes'])}),'"


def walk(c, path, out):
    kind = (set(c) - {"quote", "note", "derived"}).pop()
    if kind in ("all", "any"):
        for i, s in enumerate(c[kind]):
            walk(s, f"{path}.{kind}[{i}]", out)
    elif kind == "not":
        walk(c["not"], path + ".not", out)
    else:
        out[path] = c.get("quote", "")


def show(ref, src, label):
    if ":" not in ref:
        print(f"      {label}: {ref}")
        return
    table, line = ref.split(":")
    f = src / f"{table}.csv"
    row = subprocess.run(["sed", "-n", f"{line}p;{line}q", str(f)], capture_output=True, text=True).stdout
    print(f"      {label}: sed -n '{line}p' {f.name}\n          {row.rstrip()[:220]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("rule_id")
    ap.add_argument("patient", nargs="?")
    ap.add_argument("--random", type=int)
    ap.add_argument("--my", type=int, default=2025)
    a = ap.parse_args()
    con = duckdb.connect(a.db, read_only=True)
    pts = [a.patient] if a.patient else [r[0] for r in con.execute(
        "SELECT patient FROM verdicts WHERE rule_id = ? AND my = ? ORDER BY random() LIMIT ?", [a.rule_id, a.my, a.random]).fetchall()]
    for pt in pts:
        explain(con, a.rule_id, pt, a.my)
