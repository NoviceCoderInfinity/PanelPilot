"""Load an EHR CSV export (Synthea layout) into DuckDB: raw -> normalized -> data-quality log -> feed health.

Principles (each answers a hard question in the brief):
  * Raw rows are kept verbatim with their source file line, so every verdict can be proven with
    `sed -n '<line>p' <file>` on the export itself.
  * Normalization only applies transforms that are unambiguous and logged (ISO/US date formats,
    unit written inside the value, IFCC->NGSP HbA1c). Anything else is QUARANTINED: kept, flagged
    `usable = false`, never guessed. A unit-less lab still proves "a test happened"; it never
    proves a value.
  * Exact re-sends are collapsed (first copy wins, the rest logged), never deleted silently.
  * Feed health compares each source's recent volume to its own baseline, so a dead feed is visible.

Usage: python -m panelpilot.ingest <csv_dir> <db_path> [--as-of YYYY-MM-DD]
"""
import argparse
import datetime as dt
import json
import pathlib
import time

import duckdb

HERE = pathlib.Path(__file__).resolve().parent
VALUESETS = json.loads((HERE / "valuesets.json").read_text())

# code -> {accepted unit: SQL expression converting `v` into the canonical unit}
UNIT_RULES = {
    "4548-4": {"%": "v", "mmol/mol": "round(v / 10.929 + 2.15, 1)"},
    "8480-6": {"mm[Hg]": "v", "mmHg": "v"},
    "8462-4": {"mm[Hg]": "v", "mmHg": "v"},
}

# which observation category / table feeds which concepts (for "this verdict may be stale")
FEEDS = {
    "lab": ("observations", "CATEGORY = 'laboratory'"),
    "vitals": ("observations", "CATEGORY = 'vital-signs'"),
    "encounters": ("encounters", "TRUE"),
    "procedures": ("procedures", "TRUE"),
    "immunizations": ("immunizations", "TRUE"),
    "conditions": ("conditions", "TRUE"),
}

TABLES = ["patients", "encounters", "observations", "conditions", "procedures", "immunizations"]


def parse_ts(col):
    """ISO-8601 (Synthea) or US MM/DD/YYYY HH:MM (declared alternate export format). Nothing else is guessed."""
    return f"""coalesce(try_strptime({col}, '%Y-%m-%dT%H:%M:%SZ'), try_strptime({col}, '%Y-%m-%d'),
                        try_strptime({col}, '%m/%d/%Y %H:%M'))"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("db")
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    a = ap.parse_args()
    t0 = time.time()
    ingest(pathlib.Path(a.src), pathlib.Path(a.db), a.as_of)
    print(f"ingest done in {time.time() - t0:.1f}s")


def ingest(src: pathlib.Path, db: pathlib.Path, as_of: str, quiet=False, replay=False):
    """replay=True rebuilds the export *as it looked on as_of*: rows dated after as_of have not arrived yet
    (dropped before processing, original line numbers kept), births after as_of don't exist, deaths after
    as_of haven't happened. Used to demo nights passing on a fixed synthetic export."""
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    con.execute("SET preserve_insertion_order = true")
    con.execute("CREATE TABLE meta AS SELECT ? AS src_dir, ?::DATE AS as_of, now() AS ingested_at",
                [str(src.resolve()), as_of])

    # ---- raw: verbatim strings + source line number --------------------------------------
    replay_date = {"patients": "BIRTHDATE", "encounters": "START", "observations": "DATE", "conditions": "START",
                   "procedures": "START", "immunizations": "DATE"}
    for t in TABLES:
        keep = "TRUE"
        if replay:
            keep = f"coalesce({parse_ts(replay_date[t])} <= TIMESTAMP '{as_of} 23:59:59', TRUE)"
        con.execute(f"""CREATE TABLE raw_{t} AS SELECT * FROM (
                          SELECT row_number() OVER () + 1 AS _line, *
                          FROM read_csv('{src / t}.csv', all_varchar = true, header = true, quote = '"', escape = '"')) WHERE {keep}""")
    if replay:
        con.execute(f"UPDATE raw_patients SET DEATHDATE = NULL WHERE try_strptime(DEATHDATE, '%Y-%m-%d') > DATE '{as_of}'")

    con.execute("""CREATE TABLE dq_issues (tbl VARCHAR, _line BIGINT, patient VARCHAR, kind VARCHAR,
                   action VARCHAR, detail VARCHAR)""")

    def log(tbl, where, kind, action, detail="NULL"):
        con.execute(f"""INSERT INTO dq_issues SELECT '{tbl}', _line, {'Id' if tbl == 'patients' else 'PATIENT'},
                        '{kind}', '{action}', {detail} FROM raw_{tbl} WHERE {where}""")

    # ---- patients -----------------------------------------------------------------------
    con.execute(f"""CREATE TABLE patients AS SELECT Id AS patient, _line,
        try_strptime(BIRTHDATE, '%Y-%m-%d')::DATE AS birthdate, try_strptime(DEATHDATE, '%Y-%m-%d')::DATE AS deathdate,
        GENDER AS sex, FIRST AS first, LAST AS last, ZIP AS zip
        FROM raw_patients""")
    log("patients", "try_strptime(BIRTHDATE, '%Y-%m-%d') IS NULL", "birthdate_unparseable", "quarantined", "BIRTHDATE")

    # ---- dated event tables: duplicates + date resolution -------------------------------
    future = f"TIMESTAMP '{as_of} 23:59:59'"
    date_col = {"encounters": "START", "observations": "DATE", "conditions": "START",
                "procedures": "START", "immunizations": "DATE"}
    dedup_key = {
        # a re-sent visit carries a fresh Id, so its identity is everything except Id
        "encounters": "PATIENT, START, STOP, ENCOUNTERCLASS, CODE, PROVIDER",
        "observations": "DATE, PATIENT, ENCOUNTER, CODE, VALUE, UNITS",
        "conditions": "START, STOP, PATIENT, ENCOUNTER, CODE",
        "procedures": "START, PATIENT, ENCOUNTER, CODE",
        "immunizations": "DATE, PATIENT, ENCOUNTER, CODE",
    }
    # A child record (lab, procedure, ...) with a missing date inherits the date of the visit it is
    # linked to; a visit with a missing START takes its own STOP (Synthea visits last minutes to hours).
    # Deterministic, verifiable from the export, and logged. Nothing to recover from -> quarantined.
    enc_start = f"coalesce({parse_ts('START')}, {parse_ts('STOP')})"
    con.execute(f"""CREATE TEMP TABLE enc_ts AS SELECT Id, {enc_start} AS ts FROM raw_encounters
                    WHERE {enc_start} IS NOT NULL QUALIFY row_number() OVER (PARTITION BY Id ORDER BY _line) = 1""")
    # Duplicates compare dates by their parsed value to the minute (the US format carries no seconds), so a
    # re-send with a reformatted date is still a duplicate.
    def key_expr(k):
        return (f"coalesce(CAST(date_trunc('minute', {parse_ts('r.' + k)}) AS VARCHAR), r.{k})"
                if k in ("START", "STOP", "DATE") else f"r.{k}")
    for t, dcol in date_col.items():
        own = parse_ts("r." + dcol)
        rec = parse_ts("r.STOP") if t == "encounters" else "e.ts"
        rec_kind = "date_from_stop" if t == "encounters" else "date_from_encounter"
        join = "" if t == "encounters" else "LEFT JOIN enc_ts e ON e.Id = r.ENCOUNTER"
        con.execute(f"""CREATE TEMP TABLE {t}_x AS SELECT r._line,
            row_number() OVER (PARTITION BY {', '.join(key_expr(k.strip()) for k in dedup_key[t].split(','))} ORDER BY r._line) > 1 AS is_dup,
            coalesce({own}, {rec}) AS ts,
            CASE WHEN coalesce({own}, {rec}) > {future} THEN 'date_in_future'
                 WHEN {own} IS NULL AND {rec} IS NOT NULL THEN '{rec_kind}'
                 WHEN {own} IS NULL AND nullif(trim(r.{dcol}), '') IS NULL THEN 'date_missing'
                 WHEN {own} IS NULL THEN 'date_unparseable'
                 WHEN try_strptime(r.{dcol}, '%m/%d/%Y %H:%M') IS NOT NULL THEN 'date_us_format' END AS date_issue
            FROM raw_{t} r {join}""")
        con.execute(f"""INSERT INTO dq_issues SELECT '{t}', r._line, r.PATIENT, 'duplicate', 'collapsed',
                        'identical to an earlier row on ({dedup_key[t]})' FROM raw_{t} r JOIN {t}_x x USING (_line) WHERE x.is_dup""")
        con.execute(f"""INSERT INTO dq_issues SELECT '{t}', r._line, r.PATIENT, x.date_issue,
                        CASE WHEN x.date_issue IN ('date_us_format', 'date_from_encounter', 'date_from_stop') THEN 'normalized' ELSE 'quarantined' END,
                        coalesce(r.{dcol}, '') || CASE WHEN x.date_issue IN ('date_from_encounter', 'date_from_stop') THEN ' -> ' || x.ts ELSE '' END
                        FROM raw_{t} r JOIN {t}_x x USING (_line) WHERE x.date_issue IS NOT NULL AND NOT x.is_dup""")
    usable = f"NOT x.is_dup AND x.ts IS NOT NULL AND x.ts <= {future}"

    # ---- normalized tables ---------------------------------------------------------------
    con.execute(f"""CREATE TABLE encounters AS SELECT r._line, r.Id AS rec_id, r.PATIENT AS patient,
        x.ts, r.ENCOUNTERCLASS AS class, r.CODE AS code, r.DESCRIPTION AS description, x.is_dup, {usable} AS usable
        FROM raw_encounters r JOIN encounters_x x USING (_line)""")
    for t in ("conditions", "procedures", "immunizations"):
        con.execute(f"""CREATE TABLE {t} AS SELECT r._line, '{t}:' || r._line AS rec_id, r.PATIENT AS patient,
            r.ENCOUNTER AS encounter, x.ts, r.CODE AS code, r.DESCRIPTION AS description, x.is_dup, {usable} AS usable
            FROM raw_{t} r JOIN {t}_x x USING (_line)""")

    # observations: value + unit handling
    conv_cases = " ".join(
        f"WHEN code = '{code}' AND unit = '{u}' THEN {expr.replace('v', 'value_num')}"
        for code, units in UNIT_RULES.items() for u, expr in units.items())
    canon_unit = " ".join(f"WHEN code = '{code}' THEN '{VALUESETS_unit(code)}'" for code in UNIT_RULES)
    con.execute(f"""CREATE TABLE observations AS
        WITH p AS (
          SELECT r._line, r.PATIENT AS patient, r.ENCOUNTER AS encounter, x.ts, r.CATEGORY AS category,
                 r.CODE AS code, r.DESCRIPTION AS description, r.VALUE AS value_raw, r.UNITS AS unit_raw, r.TYPE AS type,
                 NOT x.is_dup AS not_dup,
                 regexp_extract(r.VALUE, '^\\s*(-?[0-9]+(?:\\.[0-9]+)?)\\s+(\\S+)\\s*$', ['n', 'u']) AS inval
          FROM raw_observations r JOIN observations_x x USING (_line)),
        q AS (
          SELECT *, coalesce(try_cast(value_raw AS DOUBLE), try_cast(inval.n AS DOUBLE)) AS value_num,
                 CASE WHEN nullif(trim(unit_raw), '') IS NOT NULL THEN trim(unit_raw)
                      WHEN inval.u <> '' THEN inval.u END AS unit
          FROM p)
        SELECT _line, 'observations:' || _line AS rec_id, patient, encounter, ts, category, code, description,
               value_raw, unit_raw, value_num, unit,
               CASE WHEN type <> 'numeric' THEN NULL
                    WHEN value_num IS NULL THEN 'value_unparseable'
                    WHEN unit IS NULL THEN 'unit_missing'
                    WHEN code IN ({','.join(f"'{c}'" for c in UNIT_RULES)}) AND CASE {conv_cases} END IS NULL THEN 'unit_unrecognized'
               END AS value_issue,
               CASE WHEN type <> 'numeric' OR unit IS NULL THEN NULL
                    WHEN code IN ({','.join(f"'{c}'" for c in UNIT_RULES)}) THEN CASE {conv_cases} END
                    ELSE value_num END AS value,
               CASE {canon_unit} ELSE unit END AS value_unit, NOT not_dup AS is_dup,
               not_dup AND ts IS NOT NULL AND ts <= {future} AS usable
        FROM q""")
    con.execute("""INSERT INTO dq_issues SELECT 'observations', _line, patient, value_issue, 'value_quarantined',
                   coalesce(value_raw, '') || ' [' || coalesce(unit_raw, '') || ']' FROM observations WHERE value_issue IS NOT NULL""")
    con.execute("""INSERT INTO dq_issues SELECT 'observations', _line, patient, 'unit_in_value', 'normalized', value_raw
                   FROM observations WHERE nullif(trim(unit_raw), '') IS NULL AND unit IS NOT NULL""")
    con.execute(f"""INSERT INTO dq_issues SELECT 'observations', _line, patient, 'unit_converted', 'normalized',
                    value_raw || ' ' || unit_raw || ' -> ' || value || ' {VALUESETS_unit('4548-4')}'
                    FROM observations WHERE code = '4548-4' AND unit = 'mmol/mol' AND value IS NOT NULL""")

    # ---- feed health ---------------------------------------------------------------------
    parts = []
    for feed, (tbl, where) in FEEDS.items():
        dcol = date_col[tbl]
        parts.append(f"""SELECT '{feed}' AS feed, {parse_ts(dcol)}::DATE AS d FROM raw_{tbl}
                         WHERE {where} AND {parse_ts(dcol)} <= {future}""")
    con.execute(f"""CREATE TABLE feed_daily AS SELECT feed, d, count(*) n FROM ({' UNION ALL '.join(parts)})
                    WHERE d > DATE '{as_of}' - INTERVAL 180 DAY GROUP BY ALL""")
    con.execute(f"""CREATE TABLE feed_health AS
        SELECT feed, max(d) AS last_record, DATE '{as_of}' - max(d) AS days_silent,
          round(sum(n) FILTER (WHERE d BETWEEN DATE '{as_of}' - INTERVAL 180 DAY AND DATE '{as_of}' - INTERVAL 30 DAY) / 150.0, 1) AS baseline_per_day,
          round(coalesce(sum(n) FILTER (WHERE d > DATE '{as_of}' - INTERVAL 14 DAY), 0) / 14.0, 1) AS last14_per_day
        FROM feed_daily GROUP BY feed""")
    # stale: silent for >7 days while baseline expects >= 1/day, or recent volume < 20% of baseline
    con.execute("""ALTER TABLE feed_health ADD COLUMN status VARCHAR;
        UPDATE feed_health SET status = CASE
          WHEN baseline_per_day >= 1 AND days_silent > 7 THEN 'STALE'
          WHEN baseline_per_day >= 5 AND last14_per_day < 0.2 * baseline_per_day THEN 'DEGRADED'
          ELSE 'OK' END""")

    if not quiet:
        print(con.execute("SELECT tbl, kind, action, count(*) n FROM dq_issues GROUP BY ALL ORDER BY tbl, n DESC").fetchdf().to_string(index=False))
        print(con.execute("SELECT * FROM feed_health ORDER BY feed").fetchdf().to_string(index=False))
    con.close()


def VALUESETS_unit(code):
    for c in VALUESETS.values():
        if isinstance(c, dict) and code in c.get("codes", []) and "unit" in c:
            return c["unit"]
    return None


if __name__ == "__main__":
    main()
