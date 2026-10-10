"""Turn clean Synthea CSV into a realistically messy EHR export.

Writes the messy tables plus `_defects.csv`: a ground-truth log of every defect injected,
so the engine's data-quality handling can be scored against it instead of eyeballed.

Deterministic: row selection is hash(row number, seed), so same input + seed -> same output.

Defects (the clinic's reality, per the assignment brief):
  units_dropped   lab value with no unit
  unit_in_value   "7.2 %" in the value field, unit column blank
  hba1c_ifcc      HbA1c sent by a second lab vendor in mmol/mol instead of %
  date_missing    date column blank
  date_format     date as MM/DD/YYYY HH:MM instead of ISO-8601
  duplicate       row re-sent (exact copy; encounters get a new Id, as EHR re-sends do)
  dead_feed       every laboratory observation in a 3-week window silently never arrives
"""
import argparse
import pathlib

import duckdb

HBA1C = "4548-4"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="clean Synthea csv dir")
    ap.add_argument("dst", help="output dir for messy csv")
    ap.add_argument("--seed", default="42")
    ap.add_argument("--dead-feed", help="START:END (YYYY-MM-DD) lab outage; default = last 21 days of lab data")
    a = ap.parse_args()

    src, dst = pathlib.Path(a.src), pathlib.Path(a.dst)
    dst.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = true")
    s = a.seed

    def load(name):
        con.execute(f"""CREATE OR REPLACE TABLE {name} AS
            SELECT *, row_number() OVER () AS _rn FROM read_csv('{src / name}.csv', all_varchar=true, header=true)""")
        con.execute(f"""CREATE OR REPLACE TABLE {name} AS SELECT *,
            (hash(_rn::VARCHAR || '{s}{name}') % 1000000) / 1e6 AS _u,
            (hash(_rn::VARCHAR || '{s}{name}dup') % 1000000) / 1e6 AS _u2 FROM {name}""")

    con.execute("""CREATE TABLE defects (tbl VARCHAR, kind VARCHAR, patient VARCHAR, row_ref VARCHAR,
                   orig_date VARCHAR, code VARCHAR, orig_value VARCHAR, orig_units VARCHAR, detail VARCHAR)""")

    # ---- observations -------------------------------------------------------
    load("observations")
    if a.dead_feed:
        dead_start, dead_end = a.dead_feed.split(":")
    else:
        dead_end = con.execute("SELECT max(DATE[:10]) FROM observations WHERE CATEGORY='laboratory'").fetchone()[0]
        dead_start = con.execute(f"SELECT (DATE '{dead_end}' - INTERVAL 20 DAY)::DATE::VARCHAR").fetchone()[0]
    con.execute(f"""INSERT INTO defects SELECT 'observations','dead_feed',PATIENT,_rn::VARCHAR,DATE,CODE,VALUE,UNITS,
                    '{dead_start}:{dead_end}' FROM observations
                    WHERE CATEGORY='laboratory' AND DATE[:10] BETWEEN '{dead_start}' AND '{dead_end}'""")
    con.execute(f"""DELETE FROM observations
                    WHERE CATEGORY='laboratory' AND DATE[:10] BETWEEN '{dead_start}' AND '{dead_end}'""")

    lab_num = "CATEGORY='laboratory' AND TYPE='numeric'"
    con.execute(f"""ALTER TABLE observations ADD COLUMN _kind VARCHAR;
        UPDATE observations SET _kind = CASE
            WHEN {lab_num} AND _u < 0.03 THEN 'units_dropped'
            WHEN {lab_num} AND _u < 0.05 THEN 'unit_in_value'
            WHEN _u >= 0.05 AND _u < 0.055 THEN 'date_missing'
            WHEN _u >= 0.055 AND _u < 0.065 THEN 'date_format'
            WHEN CODE='{HBA1C}' AND _u2 < 0.15 AND try_cast(VALUE AS DOUBLE) IS NOT NULL THEN 'hba1c_ifcc'
        END""")
    con.execute("""INSERT INTO defects SELECT 'observations',_kind,PATIENT,_rn::VARCHAR,DATE,CODE,VALUE,UNITS,NULL
                   FROM observations WHERE _kind IS NOT NULL""")
    con.execute(f"""UPDATE observations SET
        VALUE = CASE _kind WHEN 'unit_in_value' THEN VALUE || ' ' || coalesce(UNITS,'')
                           WHEN 'hba1c_ifcc' THEN round((VALUE::DOUBLE - 2.15) * 10.929)::INT::VARCHAR
                           ELSE VALUE END,
        UNITS = CASE _kind WHEN 'units_dropped' THEN NULL WHEN 'unit_in_value' THEN NULL
                           WHEN 'hba1c_ifcc' THEN 'mmol/mol' ELSE UNITS END,
        DATE  = CASE _kind WHEN 'date_missing' THEN NULL
                           WHEN 'date_format' THEN strftime(DATE::TIMESTAMP, '%m/%d/%Y %H:%M')
                           ELSE DATE END
        WHERE _kind IS NOT NULL""")
    dup_and_write("observations", "_rn::VARCHAR", con, dst, rate=0.01)

    # ---- encounters: re-sent visits get a fresh Id --------------------------
    load("encounters")
    blank_dates("encounters", "START", con)
    con.execute("""INSERT INTO defects SELECT 'encounters','duplicate',PATIENT,Id,START,CODE,NULL,NULL,
                   'resent as ' || md5(Id || 'dup') FROM encounters WHERE _u2 > 0.98""")
    con.execute("ALTER TABLE encounters ALTER COLUMN _rn TYPE DOUBLE")
    con.execute("""INSERT INTO encounters SELECT * REPLACE (md5(Id || 'dup') AS Id, _rn + 0.5 AS _rn)
                   FROM encounters WHERE _u2 > 0.98""")
    write("encounters", con, dst)

    # ---- dated event tables ---------------------------------------------------
    for name, col in [("procedures", "START"), ("immunizations", "DATE"), ("conditions", "START")]:
        load(name)
        blank_dates(name, col, con)
        dup_and_write(name, "_rn::VARCHAR", con, dst, rate=0.01)

    # ---- everything else passes through untouched ---------------------------
    touched = {"observations", "encounters", "procedures", "immunizations", "conditions"}
    for f in src.glob("*.csv"):
        if f.stem not in touched and not (dst / f.name).exists():
            (dst / f.name).symlink_to(f.resolve())

    con.execute(f"COPY defects TO '{dst / '_defects.csv'}' (HEADER)")
    print(con.execute("SELECT tbl, kind, count(*) n FROM defects GROUP BY ALL ORDER BY ALL").fetchdf().to_string(index=False))
    print(f"lab feed dead: {dead_start} .. {dead_end}")


def blank_dates(name, col, con):
    con.execute(f"""INSERT INTO defects SELECT '{name}','date_missing',PATIENT,_rn::VARCHAR,{col},CODE,NULL,NULL,NULL
                    FROM {name} WHERE _u < 0.005""")
    con.execute(f"UPDATE {name} SET {col} = NULL WHERE _u < 0.005")


def dup_and_write(name, ref, con, dst, rate):
    con.execute(f"""INSERT INTO defects SELECT '{name}','duplicate',PATIENT,{ref},NULL,CODE,NULL,NULL,NULL
                    FROM {name} WHERE _u2 > {1 - rate}""")
    con.execute(f"""ALTER TABLE {name} ALTER COLUMN _rn TYPE DOUBLE;
                    INSERT INTO {name} SELECT * REPLACE (_rn + 0.5 AS _rn) FROM {name} WHERE _u2 > {1 - rate}""")
    write(name, con, dst)


def write(name, con, dst):
    con.execute(f"""COPY (SELECT * EXCLUDE (_rn, _u, _u2{', _kind' if name == 'observations' else ''})
                    FROM {name} ORDER BY _rn) TO '{dst / name}.csv' (HEADER)""")


if __name__ == "__main__":
    main()
