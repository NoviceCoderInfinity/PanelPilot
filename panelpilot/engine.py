"""Deterministic rule execution: VerifiedRule x panel -> one verdict per patient, with evidence.

No model calls. Each leaf criterion compiles to one SQL query over the normalized tables; the
rule tree combines leaves with SQL's three-valued logic, so "unknown" propagates honestly:

  leaf = TRUE   criterion met by usable records
         FALSE  not met, and nothing on file could change that
         NULL   not met, BUT a quarantined record (no usable date / unit) or a stale feed
                could change it  -> the patient is 'indeterminate', never a silent gap

Verdict status: pass | fail | indeterminate | excluded   (patients outside the denominator are not stored)
Evidence: per leaf, the records ("table:line" in the ingested export) that decided it.

Usage: python -m panelpilot.engine <db> <rules_dir> [--my 2025]
"""
import argparse
import json
import pathlib
import time

import duckdb

from panelpilot import rules as R

VALUESETS = json.loads((pathlib.Path(__file__).resolve().parent / "valuesets.json").read_text())
CONCEPT_FEED = {"observations": None, "procedures": "procedures", "encounters": "encounters",
                "immunizations": "immunizations", "conditions": "conditions"}
EMPTY = "[]::VARCHAR[]"


class NotRunnable(Exception):
    pass


def _codes(concept):
    vs = VALUESETS.get(concept)
    if not isinstance(vs, dict):
        raise NotRunnable(f"needs mapping: concept {concept!r} has no value set")
    if "unmapped" in vs:
        raise NotRunnable(f"not computable: {concept}: {vs['unmapped']}")
    return vs, ",".join(f"'{c}'" for c in vs["codes"])


def _feed_for(table, codes_sql, con):
    if table != "observations":
        return table
    cats = [r[0] for r in con.execute(f"SELECT DISTINCT category FROM observations WHERE code IN ({codes_sql})").fetchall()]
    return {"laboratory": "lab", "vital-signs": "vitals"}.get(cats[0]) if len(cats) == 1 else None


class Compiler:
    def __init__(self, con, my):
        self.con, self.my = con, my
        self.leaves = []        # (name, path, kind, sql)
        self.caveats = []       # data-capture limits the verdicts inherit
        self.feeds = {r[0]: r for r in con.execute(
            "SELECT feed, last_record, status FROM feed_health").fetchall()}

    def d(self, expr):
        return f"DATE '{R.resolve_date(expr, self.my)}'"

    def stale(self, feed, window_end_sql):
        """SQL bool: the feed died before the end of this window, so absence of records proves nothing."""
        f = self.feeds.get(feed)
        if not f or f[2] == "OK":
            return "FALSE", None
        return f"(DATE '{f[1]}' < {window_end_sql})", f"{feed} feed silent since {f[1]}"

    def node(self, c, path):
        kind = (set(c) - {"quote", "note", "derived"}).pop()
        if kind == "any":
            # One alternative the data cannot capture (e.g. CT colonography) must not void the others; it is
            # skipped with a caveat. Only if no alternative is computable is the rule not computable.
            parts, skipped = [], []
            for i, s in enumerate(c["any"]):
                n_leaves = len(self.leaves)
                try:
                    parts.append(self.node(s, f"{path}.any[{i}]"))
                except NotRunnable as e:
                    del self.leaves[n_leaves:]
                    skipped.append(str(e))
            if not parts:
                raise NotRunnable("; ".join(skipped))
            self.caveats += [f"{path}: alternative not captured in data, so patients who used it appear as gaps ({s})" for s in skipped]
            return "(" + " OR ".join(parts) + ")"
        if kind == "all":
            return "(" + " AND ".join(self.node(s, f"{path}.all[{i}]") for i, s in enumerate(c["all"])) + ")"
        if kind == "not":
            return f"(NOT {self.node(c['not'], path + '.not')})"
        name = f"c{len(self.leaves)}"
        self.leaves.append((name, path, kind, getattr(self, "leaf_" + kind)(c[kind])))
        return f"{name}.v"

    # ---- leaves: each returns SELECT patient, met, ev, sh, why ---------------------------
    def leaf_age(self, s):
        end, start = f"DATE '{self.my}-12-31'", f"DATE '{self.my}-01-01'"
        lo, hi = s.get("min", 0), s.get("max", 200)  # one-sided ranges: "through 18 years", "65 and older"
        if s.get("at", "end") == "all_year":
            cond = f"datesub('year', birthdate, {start}) >= {lo} AND datesub('year', birthdate, {end}) <= {hi}"
        else:
            cond = f"datesub('year', birthdate, {end}) BETWEEN {lo} AND {hi}"
        return f"SELECT patient, {cond} AS met, ['patients:' || _line] AS ev, {EMPTY} AS sh, NULL AS why FROM patients"

    def leaf_sex(self, s):
        return f"SELECT patient, sex = '{s['is']}' AS met, ['patients:' || _line] AS ev, {EMPTY} AS sh, NULL AS why FROM patients"

    def leaf_deceased(self, s):
        return f"""SELECT patient, coalesce(deathdate < {self.d(s['before'])}, FALSE) AS met,
                   ['patients:' || _line] AS ev, {EMPTY} AS sh, NULL AS why FROM patients"""

    def leaf_uncomputable(self, s):
        raise NotRunnable(f"not computable: needs {s.get('needs', '?')}")

    def _events(self, table, concept, lo, hi, min_count=1, label=None):
        # The rule says *what* happened (a test, a dose); where it is stored is the value set's business,
        # so neither the extractor nor a rule author needs to know the EHR's table layout.
        vs, codes = _codes(concept)
        table = vs["table"]
        if vs.get("coverage") == "low":
            self.caveats.append(f"LOW DATA COVERAGE for {concept}: not for outreach ({vs.get('note', '')})")
        cls = f"AND class = '{vs['class']}'" if "class" in vs else ""
        if "value_in" in vs:  # coded text answers, e.g. smoking status
            cls += " AND value_raw IN (" + ",".join("'" + v.replace("'", "''") + "'" for v in vs["value_in"]) + ")"
        stale_sql, stale_why = self.stale(_feed_for(table, codes, self.con), hi)
        count = "count(DISTINCT ts::DATE)" if table == "immunizations" else "count(*)"
        label = label or concept
        return f"""SELECT p.patient, coalesce(u.n, 0) >= {min_count} AS met, coalesce(u.ev, {EMPTY}) AS ev,
                   CASE WHEN coalesce(u.n, 0) < {min_count} AND {stale_sql} THEN ['feed:{_feed_for(table, codes, self.con)}']
                        ELSE coalesce(s.sh, {EMPTY}) END AS sh,
                   CASE WHEN coalesce(u.n, 0) >= {min_count} THEN NULL
                        WHEN {stale_sql} THEN '{stale_why}'
                        WHEN s.sh IS NOT NULL THEN '{label} on file with no usable date' END AS why
            FROM patients p
            LEFT JOIN (SELECT patient, {count} AS n, list('{table}:' || _line ORDER BY ts DESC)[1:3] AS ev
                       FROM {table} WHERE code IN ({codes}) {cls} AND usable AND ts::DATE BETWEEN {lo} AND {hi}
                       GROUP BY patient) u USING (patient)
            LEFT JOIN (SELECT patient, list('{table}:' || _line)[1:3] AS sh
                       FROM {table} WHERE code IN ({codes}) {cls} AND NOT usable AND NOT is_dup AND ts IS NULL
                       GROUP BY patient) s USING (patient)"""

    def _window(self, s):
        w = s.get("window", ["{MY}-01-01", "{MY}-12-31"])
        return self.d(w[0]), self.d(w[1])

    def leaf_procedure(self, s):
        return self._events("procedures", s["concept"], *self._window(s))

    def leaf_encounter(self, s):
        return self._events("encounters", s["concept"], *self._window(s))

    def leaf_immunization(self, s):
        return self._events("immunizations", s["concept"], *self._window(s), min_count=s.get("min_count", 1))

    def leaf_condition(self, s):
        lo = "DATE '1900-01-01'"
        hi = self.d(s["onset_by"]) if "onset_by" in s else f"DATE '{self.my}-12-31'"
        if "onset_before" in s:
            hi = f"({self.d(s['onset_before'])} - INTERVAL 1 DAY)::DATE"
        return self._events("conditions", s["concept"], lo, hi)

    def leaf_observation(self, s):
        lo, hi = self._window(s)
        if s.get("pick", "any") == "any" and "value" not in s:
            return self._events("observations", s["concept"], lo, hi)
        vs, codes = _codes(s["concept"])
        v = s["value"]
        if v.get("unit") and v["unit"] != vs.get("unit"):
            raise NotRunnable(f"rule unit {v['unit']} != canonical {vs.get('unit')} for {s['concept']}")
        stale_sql, stale_why = self.stale(_feed_for("observations", codes, self.con), hi)
        return f"""WITH l AS (SELECT patient, _line, value, value_issue FROM observations
                      WHERE code IN ({codes}) AND usable AND ts::DATE BETWEEN {lo} AND {hi}
                      QUALIFY row_number() OVER (PARTITION BY patient ORDER BY ts DESC, _line) = 1),
                 u AS (SELECT patient, list('observations:' || _line)[1:3] AS sh FROM observations
                      WHERE code IN ({codes}) AND NOT usable AND NOT is_dup AND ts IS NULL GROUP BY patient)
            SELECT p.patient, coalesce(l.value {v['op']} {v['threshold']}, FALSE) AS met,
                   CASE WHEN l._line IS NOT NULL THEN ['observations:' || l._line] ELSE {EMPTY} END AS ev,
                   CASE WHEN l.value_issue IS NOT NULL THEN ['observations:' || l._line]
                        WHEN NOT coalesce(l.value {v['op']} {v['threshold']}, FALSE) AND {stale_sql} THEN ['feed']
                        ELSE coalesce(u.sh, {EMPTY}) END AS sh,
                   CASE WHEN coalesce(l.value {v['op']} {v['threshold']}, FALSE) THEN NULL
                        WHEN l.value_issue IS NOT NULL THEN 'most recent {s['concept']} unusable: ' || l.value_issue
                        WHEN l._line IS NULL AND {stale_sql} THEN '{stale_why}'
                        -- a failing value from before the feed died may already have been superseded
                        WHEN {stale_sql} THEN 'last {s['concept']} on file fails, but {stale_why}: a newer result may not have arrived'
                        WHEN u.sh IS NOT NULL THEN '{s['concept']} on file with no usable date' END AS why
            FROM patients p LEFT JOIN l USING (patient) LEFT JOIN u USING (patient)"""

    def leaf_bp(self, s):
        lo, hi = self._window(s)
        _, sc = _codes("bp_systolic")
        _, dc = _codes("bp_diastolic")
        stale_sql, stale_why = self.stale("vitals", hi)
        obs = lambda codes: f"""SELECT patient, encounter, ts, _line, value, value_issue FROM observations
                                WHERE code IN ({codes}) AND usable AND ts::DATE BETWEEN {lo} AND {hi}"""
        return f"""WITH sy AS ({obs(sc)}), di AS ({obs(dc)}),
                 l AS (SELECT sy.patient, sy._line AS sl, di._line AS dl, sy.value AS sv, di.value AS dv,
                              coalesce(sy.value_issue, di.value_issue) AS issue
                       FROM sy JOIN di ON sy.patient = di.patient AND sy.encounter IS NOT DISTINCT FROM di.encounter
                            AND date_trunc('minute', sy.ts) = date_trunc('minute', di.ts)
                       QUALIFY row_number() OVER (PARTITION BY sy.patient ORDER BY sy.ts DESC, sy._line) = 1)
            SELECT p.patient, coalesce(l.sv < {s['systolic_lt']} AND l.dv < {s['diastolic_lt']}, FALSE) AS met,
                   CASE WHEN l.sl IS NOT NULL THEN ['observations:' || l.sl, 'observations:' || l.dl] ELSE {EMPTY} END AS ev,
                   CASE WHEN l.issue IS NOT NULL THEN ['observations:' || l.sl]
                        WHEN NOT coalesce(l.sv < {s['systolic_lt']} AND l.dv < {s['diastolic_lt']}, FALSE) AND {stale_sql}
                        THEN ['feed'] ELSE {EMPTY} END AS sh,
                   CASE WHEN coalesce(l.sv < {s['systolic_lt']} AND l.dv < {s['diastolic_lt']}, FALSE) THEN NULL
                        WHEN l.issue IS NOT NULL THEN 'most recent BP unusable: ' || l.issue
                        WHEN l.sl IS NULL AND {stale_sql} THEN '{stale_why}'
                        WHEN {stale_sql} THEN 'last BP on file is above goal, but {stale_why}: a newer reading may not have arrived' END AS why
            FROM patients p LEFT JOIN l USING (patient)"""


def run_rule(con, rule: R.VerifiedRule, my: int, preview=False):
    """Execute one verified rule; append to `verdicts`, record the run in `rule_runs`.
    preview=True is only for impact previews on a scratch copy of the panel (panelpilot.preview)."""
    if not isinstance(rule, R.VerifiedRule):
        raise TypeError("engine only executes rules.VerifiedRule")
    if not (rule.approved or preview):
        raise PermissionError(f"{rule.id} needs human approval before it can run against patients")
    t0 = time.time()
    r = rule.data
    comp = Compiler(con, my)
    try:
        den = comp.node(r["denominator"], "denominator")
        num = comp.node(r["numerator"], "numerator")
        exc = " OR ".join(comp.node(e, f"exclusions[{i}]") for i, e in enumerate(r.get("exclusions", []))) or "FALSE"
        # engine-wide policy from the brief ("deceased-before-2025 excluded everywhere"), whatever the rule says
        exc = f"({exc}) OR coalesce(patients.deathdate < DATE '{my}-01-01', FALSE)"
    except NotRunnable as e:
        con.execute("INSERT INTO rule_runs VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0, 0, NULL)", [r["id"], r["version"], my, str(e)])
        return
    ctes = ",\n".join(
        f"""{n} AS (SELECT patient, met, ev, sh, why,
               CASE WHEN met THEN TRUE WHEN len(sh) > 0 OR met IS NULL THEN NULL ELSE FALSE END AS v FROM ({sql}))"""
        for n, _, _, sql in comp.leaves)
    joins = " ".join(f"JOIN {n} USING (patient)" for n, *_ in comp.leaves)
    evidence = "[" + ", ".join(
        f"{{'path': '{p}', 'kind': '{k}', 'v': {n}.v, 'records': {n}.ev, 'unusable': {n}.sh, 'why': {n}.why}}"
        for n, p, k, _ in comp.leaves) + "]"
    whys = "concat_ws('; ', " + ", ".join(f"{n}.why" for n, *_ in comp.leaves) + ")"
    con.execute(f"""INSERT INTO verdicts
        WITH {ctes}, t AS (
          SELECT patient, {den} AS den, {num} AS num, {exc} AS exc, {whys} AS why, {evidence} AS evidence
          FROM patients {joins})
        SELECT '{r['id']}', '{r['version']}', {my}, patient,
          CASE WHEN exc THEN 'excluded' WHEN den IS NULL THEN 'indeterminate'
               WHEN num THEN 'pass' WHEN num IS NULL THEN 'indeterminate' ELSE 'fail' END AS status,
          nullif(why, '') AS why, to_json(evidence) AS evidence
        FROM t WHERE den IS NOT FALSE""")
    con.execute(f"""INSERT INTO rule_runs SELECT '{r['id']}', '{r['version']}', {my}, 'ran',
        count(*) FILTER (WHERE status <> 'excluded'), count(*) FILTER (WHERE status = 'pass'),
        count(*) FILTER (WHERE status = 'fail'), count(*) FILTER (WHERE status = 'indeterminate'),
        count(*) FILTER (WHERE status = 'excluded'), {time.time() - t0:.2f}, ?
        FROM verdicts WHERE rule_id = '{r['id']}' AND my = {my}""", ["; ".join(comp.caveats) or None])


def load_rules(rules_dirs):
    verified, skipped = [], []
    for d in rules_dirs:
        if pathlib.Path(d).exists():
            ok, bad = R.load_dir(d, recursive=True)
            verified += ok
            skipped += bad
    return verified, skipped


def run(db, rules_dirs, my, quiet=False):
    con = duckdb.connect(str(db))
    verified, skipped = load_rules([rules_dirs] if isinstance(rules_dirs, (str, pathlib.Path)) else rules_dirs)
    for msg in skipped:
        if not quiet:
            print("SKIPPED", msg)
    con.execute("""CREATE OR REPLACE TABLE rules_meta (rule_id VARCHAR, version VARCHAR, title VARCHAR, action VARCHAR,
                   doc_id VARCHAR, quote VARCHAR, rule_json JSON)""")
    for rule in verified:
        d = rule.data
        con.execute("INSERT INTO rules_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [rule.id, rule.version, d.get("title"), d.get("action"), d["source"]["doc_id"], d["source"]["quote"],
                     json.dumps(d, ensure_ascii=False)])
    con.execute("""CREATE TABLE IF NOT EXISTS verdicts (rule_id VARCHAR, rule_version VARCHAR, my INT, patient VARCHAR,
                   status VARCHAR, why VARCHAR, evidence JSON)""")
    con.execute("""CREATE TABLE IF NOT EXISTS rule_runs (rule_id VARCHAR, rule_version VARCHAR, my INT, status VARCHAR,
                   denominator INT, pass INT, fail INT, indeterminate INT, excluded INT, seconds DOUBLE, caveats VARCHAR)""")
    con.execute("ALTER TABLE rule_runs ADD COLUMN IF NOT EXISTS caveats VARCHAR")
    for rule in verified:
        con.execute("DELETE FROM verdicts WHERE rule_id = ? AND my = ?", [rule.id, my])
        con.execute("DELETE FROM rule_runs WHERE rule_id = ? AND my = ?", [rule.id, my])
        try:
            run_rule(con, rule, my)
        except Exception as e:  # one malformed rule must not stop the nightly run; it is reported, not hidden
            con.execute("DELETE FROM verdicts WHERE rule_id = ? AND my = ?", [rule.id, my])
            con.execute("INSERT INTO rule_runs VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0, 0, NULL)",
                        [rule.id, rule.version, my, f"error: {type(e).__name__}: {e}"[:300]])
    if not quiet:
        print(con.execute(f"""SELECT * EXCLUDE (caveats), round(100.0 * pass / nullif(denominator, 0), 1) AS pass_pct,
                              left(caveats, 70) AS caveats
                              FROM rule_runs WHERE my = {my} ORDER BY rule_id""").fetchdf().to_string(index=False))
    con.close()
    return verified, skipped


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("rules_dirs", nargs="+")
    ap.add_argument("--my", type=int, default=2025)
    a = ap.parse_args()
    run(a.db, a.rules_dirs, a.my)


def patients_where(con, tree, my):
    """Patients for whom a criterion tree is TRUE (used to evaluate a superseded rule's denominator)."""
    comp = Compiler(con, my)
    expr = comp.node(tree, "t")
    ctes = ",\n".join(f"""{n} AS (SELECT patient, CASE WHEN met THEN TRUE WHEN len(sh) > 0 OR met IS NULL THEN NULL
                                   ELSE FALSE END AS v FROM ({sql}))""" for n, _, _, sql in comp.leaves)
    joins = " ".join(f"JOIN {n} USING (patient)" for n, *_ in comp.leaves)
    return {r[0] for r in con.execute(f"WITH {ctes} SELECT patient FROM patients {joins} WHERE {expr}").fetchall()}
