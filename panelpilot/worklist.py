"""Today's worklist (hard question 10): one row per PATIENT (one call can close several gaps), ranked by a
transparent additive score, each with a one-sentence reason built from the same facts that scored it.

Score components (deterministic; weights are product judgment, stated openly, tunable in one place):
  clinical   last HbA1c >= 9% (+40) or >= 8% (+25); last BP >= 160/100 (+35) or >= 140/90 (+20)
  reopened   a prior "handled" that never produced a record (+30): a broken promise outranks routine
  new        newly eligible after a guideline update (+15): nobody has ever told them
  bundle     +10 per extra open need, at most +20: one visit closes several, but a stack of routine
             screening questions must not outrank an uncontrolled HbA1c or BP
  reach      seen in the last 18 months (+10); no visit in 3 years (-15: may have left the practice)
  base       +10 for having any open need
Who never appears: deceased patients; low-data-coverage rules; patients pending verification or snoozed;
verdicts that are 'indeterminate' (those go to the "can't tell" list with their reason, never to a call).
"""
import json

import duckdb

from panelpilot import ops

W = {"a1c9": 40, "a1c8": 25, "bp160": 35, "bp140": 20, "reopened": 30, "new": 15, "bundle": 10,
     "bundle_max": 20, "seen18m": 10, "gone3y": -15, "base": 10}
SIZE = 20
# short names for the one-sentence "why", by concept (first labelled concept of a need wins)
LABEL = {"hba1c": "HbA1c test", "hba1c:value": "HbA1c control", "blood_pressure_control": "BP control",
         "blood_pressure": "BP check", "colonoscopy": "colorectal screening", "well_visit": "well-child visit",
         "dtap_vaccine": "DTaP", "influenza_vaccine": "flu shot", "hepa_vaccine": "Hep A vaccine",
         "hepb_vaccine": "Hep B vaccine", "hpv_vaccine": "HPV vaccine", "ipv_vaccine": "polio vaccine",
         "menacwy_vaccine": "MenACWY vaccine", "mmr_vaccine": "MMR", "pcv_vaccine": "PCV", "tdap_vaccine": "Tdap",
         "alcohol_screening": "alcohol screen", "anxiety_screening": "anxiety screen", "mammogram": "mammogram",
         "cervical_cytology": "cervical screening", "depression_screening": "depression screen",
         "drug_use_screening": "drug-use screen", "hepatitis_c_test": "Hep C test", "hiv_test": "HIV test",
         "vision_screening": "vision screen"}


def build(panel_db, my, today):
    con = duckdb.connect(str(panel_db))
    sup = ops.active_suppressions(today)
    reo = ops.reopened(my)
    con.execute("CREATE OR REPLACE TEMP TABLE suppressed (patient VARCHAR, status VARCHAR)")
    if sup:
        con.executemany("INSERT INTO suppressed VALUES (?, ?)", list(sup.items()))
    con.execute("CREATE OR REPLACE TEMP TABLE reopened_pts (patient VARCHAR, reason VARCHAR)")
    if reo:
        con.executemany("INSERT INTO reopened_pts VALUES (?, ?)", [(p, r["reason"]) for p, r in reo.items()])
    has_new = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'newly_eligible'").fetchone()[0]
    new_src = "SELECT patient, rule_id, note FROM newly_eligible WHERE status = 'fail'" if has_new else \
              "SELECT NULL::VARCHAR patient, NULL::VARCHAR rule_id, NULL::VARCHAR note WHERE FALSE"

    con.execute(f"""CREATE OR REPLACE TABLE gaps AS
        WITH ok_rules AS (SELECT rule_id FROM rule_runs WHERE my = {my} AND status = 'ran'
                          AND coalesce(caveats, '') NOT LIKE '%LOW DATA COVERAGE%'),
        g AS (SELECT v.patient, v.rule_id, m.title, m.action FROM verdicts v JOIN rules_meta m USING (rule_id)
              JOIN patients p USING (patient)
              WHERE v.my = {my} AND v.status = 'fail' AND p.deathdate IS NULL AND v.rule_id IN (SELECT rule_id FROM ok_rules))
        -- "no HbA1c at all" fails both A1C rates; one action (order the test) covers both
        SELECT * FROM g WHERE NOT (rule_id = 'A1C-TEST.rate2'
                                   AND EXISTS (SELECT 1 FROM g g2 WHERE g2.patient = g.patient AND g2.rule_id = 'A1C-TEST'))""")

    # A "need" is what the patient lacks (the numerator's concepts). Two approved rules that both flag
    # missing colorectal screening are ONE need: counted once, one action, both rule ids kept for evidence.
    con.execute("CREATE OR REPLACE TEMP TABLE rule_need (rule_id VARCHAR, need VARCHAR, label VARCHAR)")
    con.executemany("INSERT INTO rule_need VALUES (?, ?, ?)", needs(
        {rid: (json.loads(rj), title) for rid, rj, title in con.execute("SELECT rule_id, rule_json, title FROM rules_meta").fetchall()}))
    con.execute(f"""CREATE OR REPLACE TABLE worklist_all AS
        WITH gn AS (SELECT g.*, g.action AS g_action, coalesce(rn.need, g.rule_id) AS need, coalesce(rn.label, g.title) AS label
                    FROM gaps g LEFT JOIN rule_need rn USING (rule_id)),
        pn AS (SELECT patient, need, list(rule_id ORDER BY rule_id) rids, min(label) AS title, arg_min(g_action, length(g_action)) AS act
               FROM gn GROUP BY patient, need),
        pg AS (SELECT patient, count(*) n, flatten(list(rids ORDER BY need)) rule_ids,
                      list(title ORDER BY need) titles, list(DISTINCT act ORDER BY act) actions
               FROM pn GROUP BY patient),
        a1c AS (SELECT patient, value, ts::DATE d FROM observations WHERE code = '4548-4' AND usable AND value IS NOT NULL
                QUALIFY row_number() OVER (PARTITION BY patient ORDER BY ts DESC) = 1),
        bp AS (SELECT s.patient, s.value sv, d.value dv, s.ts::DATE d FROM observations s JOIN observations d
                 ON s.patient = d.patient AND s.encounter IS NOT DISTINCT FROM d.encounter
                AND date_trunc('minute', s.ts) = date_trunc('minute', d.ts)
               WHERE s.code = '8480-6' AND d.code = '8462-4' AND s.usable AND d.usable AND s.value IS NOT NULL AND d.value IS NOT NULL
               QUALIFY row_number() OVER (PARTITION BY s.patient ORDER BY s.ts DESC) = 1),
        lv AS (SELECT patient, max(ts)::DATE d FROM encounters WHERE usable AND code <> '308646001' GROUP BY patient),
        ne AS (SELECT patient, any_value(note) note FROM ({new_src}) GROUP BY patient),
        f AS (
          SELECT pg.*, p.first, p.last, p.sex, datesub('year', p.birthdate, DATE '{today}') AS age,
                 a1c.value a1c, a1c.d a1c_date, bp.sv, bp.dv, bp.d bp_date, lv.d last_visit,
                 ro.reason reopened_reason, ne.note new_note,
                 list_contains(pg.rule_ids, 'A1C-TEST') OR list_contains(pg.rule_ids, 'A1C-TEST.rate2') AS has_a1c_gap,
                 list_contains(pg.rule_ids, 'BP-CTRL') AS has_bp_gap
          FROM pg JOIN patients p USING (patient) LEFT JOIN a1c USING (patient) LEFT JOIN bp USING (patient)
          LEFT JOIN lv USING (patient) LEFT JOIN reopened_pts ro USING (patient) LEFT JOIN ne USING (patient)
          WHERE pg.patient NOT IN (SELECT patient FROM suppressed))
        SELECT *,
          CASE WHEN has_a1c_gap AND a1c >= 9 THEN {W['a1c9']} WHEN has_a1c_gap AND a1c >= 8 THEN {W['a1c8']} ELSE 0 END AS s_a1c,
          CASE WHEN has_bp_gap AND (sv >= 160 OR dv >= 100) THEN {W['bp160']}
               WHEN has_bp_gap AND (sv >= 140 OR dv >= 90) THEN {W['bp140']} ELSE 0 END AS s_bp,
          CASE WHEN reopened_reason IS NOT NULL THEN {W['reopened']} ELSE 0 END AS s_reopened,
          CASE WHEN new_note IS NOT NULL THEN {W['new']} ELSE 0 END AS s_new,
          least({W['bundle']} * (n - 1), {W['bundle_max']}) AS s_bundle,
          CASE WHEN last_visit >= DATE '{today}' - INTERVAL 18 MONTH THEN {W['seen18m']}
               WHEN last_visit IS NULL OR last_visit < DATE '{today}' - INTERVAL 3 YEAR THEN {W['gone3y']} ELSE 0 END AS s_reach,
          {W['base']} AS s_base
        FROM f""")
    con.execute("ALTER TABLE worklist_all ADD COLUMN score INT")
    con.execute("UPDATE worklist_all SET score = s_a1c + s_bp + s_reopened + s_new + s_bundle + s_reach + s_base")
    # tie-break inside a score tier: how far the last reading is from goal (HbA1c points count 20x mmHg)
    rows = con.execute("""SELECT * FROM worklist_all ORDER BY score DESC,
        greatest(CASE WHEN has_a1c_gap THEN coalesce((a1c - 7) * 20, 0) ELSE 0 END,
                 CASE WHEN has_bp_gap THEN coalesce(sv - 130, 0) + coalesce(dv - 80, 0) ELSE 0 END) DESC,
        last_visit DESC NULLS LAST, patient""").fetchdf().to_dict("records")
    for r in rows:
        r["why"] = why(r, my)
    con.execute("""CREATE OR REPLACE TABLE worklist (rank INT, patient VARCHAR, score INT, why VARCHAR, n_gaps INT,
                   rule_ids VARCHAR[], actions VARCHAR[], components JSON)""")
    con.executemany("INSERT INTO worklist VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (i + 1, r["patient"], int(r["score"]), r["why"], int(r["n"]), list(r["rule_ids"]), list(r["actions"]),
         json.dumps({k: int(r[k]) for k in r if k.startswith("s_")})) for i, r in enumerate(rows)])
    # the "can't tell" list: indeterminate verdicts grouped by reason -- data problems, not calls
    con.execute(f"""CREATE OR REPLACE TABLE cant_tell AS
        SELECT v.rule_id, coalesce(v.why, 'unknown') AS reason, count(*) AS patients
        FROM verdicts v JOIN patients p USING (patient)
        WHERE v.my = {my} AND v.status = 'indeterminate' AND p.deathdate IS NULL GROUP BY ALL ORDER BY patients DESC""")
    n = len(rows)
    con.close()
    return n


def needs(rules):
    """rule_id -> (need, label). Rules whose numerators share a concept close with the same care (the 2016
    and 2021 colorectal rules, the two HPV rules), so they are merged into one need (union-find)."""
    parent = {rid: rid for rid in rules}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    keys = {rid: need_key(r).split("+") for rid, (r, _) in rules.items()}
    owner = {}
    for rid, ks in keys.items():
        for k in ks:
            if k in owner:
                parent[find(rid)] = find(owner[k])
            else:
                owner[k] = rid
    groups = {}
    for rid in rules:
        groups.setdefault(find(rid), []).append(rid)
    out = []
    for members in groups.values():
        cs = sorted({k for m in members for k in keys[m]})
        label = next((LABEL[k] for k in cs if k in LABEL), None) or min(rules[m][1] for m in members).split(",")[0]
        out += [(m, "+".join(cs), label) for m in members]
    return out


def need_key(rule):
    """Sorted numerator concepts (or bp/age-free shape): what would close the gap."""
    out = set()

    def walk(c):
        kind = (set(c) - {"quote", "note", "derived"}).pop()
        if kind in ("all", "any"):
            for s in c[kind]:
                walk(s)
        elif kind == "not":
            walk(c["not"])
        elif kind == "bp":
            out.add("blood_pressure_control")
        elif isinstance(c[kind], dict) and c[kind].get("concept"):
            out.add(c[kind]["concept"] + (":value" if "value" in c[kind] else ""))
    walk(rule["numerator"])
    return "+".join(sorted(out)) or rule["id"]


def _d(x):
    return x.strftime("%b %-d, %Y") if x is not None and str(x) != "NaT" else "?"


def why(r, my):
    """One sentence, from the same facts that produced the score."""
    lead = None
    if r["s_reopened"]:
        lead = f"Reopened: {r['reopened_reason']}"
    elif r["s_a1c"]:
        lead = f"Diabetic with last HbA1c {r['a1c']:.1f}% ({_d(r['a1c_date'])}), " + (
            f"no HbA1c on file in {my}" if "A1C-TEST" in list(r["rule_ids"]) else "above the 8% goal")
    elif r["s_bp"]:
        lead = f"Hypertension with last BP {r['sv']:.0f}/{r['dv']:.0f} ({_d(r['bp_date'])}), above the 140/90 goal"
    elif r["s_new"]:
        lead = r["new_note"]
    else:
        lead = "Due: " + ", ".join(r["titles"][:4]) + (f" and {len(r['titles']) - 4} more" if len(r["titles"]) > 4 else "")
    tail = []
    if r["n"] > 1:
        names = list(r["titles"])
        more = f", +{len(names) - 4} more" if len(names) > 4 else ""
        tail.append(f"one visit closes {r['n']} gaps ({', '.join(names[:4])}{more})")
    if r["s_reach"] > 0:
        tail.append(f"seen {_d(r['last_visit'])}")
    elif r["s_reach"] < 0:
        tail.append("no visit in 3+ years: confirm still a patient")
    return lead + (" — " + ", ".join(tail) if tail else "") + "."
