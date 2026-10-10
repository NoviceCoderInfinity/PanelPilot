"""FHIR R4 bundles -> the same six CSV tables ingest.py reads, so everything downstream runs unchanged.

    python -m panelpilot.fhir <fhir_dir> <out_dir> [--sample N] [--seed 42]

Each output row carries a last column FHIR_REF = "<bundle file>#<ResourceType>/<id>", so a verdict's evidence
line (`sed -n '<line>p' observations.csv`) also names the original FHIR resource (`grep -n '"id": "<id>"' <file>`).

Mapping decisions (FHIR -> Synthea CSV semantics):
  timestamps     converted to UTC 'YYYY-MM-DDTHH:MM:SSZ' (bundles carry the generating machine's offset)
  dates          conditions START/STOP, DEATHDATE: UTC date of the FHIR dateTime
  BP panel       85354-9 is split into one row per component (8480-6 systolic, 8462-4 diastolic), as in CSV
  coded values   valueCodeableConcept -> display text, TYPE 'text'; valueQuantity -> value + unit, TYPE 'numeric'
  encounter class FHIR R4 collapses wellness/ambulatory/outpatient/urgent care into 'AMB'. The well-visit codes
                 that only ever occur as wellness (410620009, 162673000, 308646001) are restored to 'wellness';
                 'Encounter for check up' (185349003) occurs in every class and cannot be restored -> 'ambulatory'.
                 This is a real loss of information in FHIR, reported by the parity check, not hidden.
Resources not used by any rule (Claim, EOB, DiagnosticReport, Medication*, ...) are skipped.
"""
import argparse
import csv
import datetime as dt
import json
import pathlib
import random

HEADERS = {
    "patients": "Id,BIRTHDATE,DEATHDATE,SSN,DRIVERS,PASSPORT,PREFIX,FIRST,MIDDLE,LAST,SUFFIX,MAIDEN,MARITAL,RACE,"
                "ETHNICITY,GENDER,BIRTHPLACE,ADDRESS,CITY,STATE,COUNTY,FIPS,ZIP,LAT,LON,HEALTHCARE_EXPENSES,"
                "HEALTHCARE_COVERAGE,INCOME",
    "encounters": "Id,START,STOP,PATIENT,ORGANIZATION,PROVIDER,PAYER,ENCOUNTERCLASS,CODE,DESCRIPTION,"
                  "BASE_ENCOUNTER_COST,TOTAL_CLAIM_COST,PAYER_COVERAGE,REASONCODE,REASONDESCRIPTION",
    "observations": "DATE,PATIENT,ENCOUNTER,CATEGORY,CODE,DESCRIPTION,VALUE,UNITS,TYPE",
    "conditions": "START,STOP,PATIENT,ENCOUNTER,SYSTEM,CODE,DESCRIPTION",
    "procedures": "START,STOP,PATIENT,ENCOUNTER,SYSTEM,CODE,DESCRIPTION,BASE_COST,REASONCODE,REASONDESCRIPTION",
    "immunizations": "DATE,PATIENT,ENCOUNTER,CODE,DESCRIPTION,BASE_COST",
}
CLASS = {"AMB": "ambulatory", "EMER": "emergency", "IMP": "inpatient", "HH": "home", "VR": "virtual",
         "ACUTE": "inpatient", "NONAC": "snf", "OBSENC": "outpatient", "SS": "outpatient", "PRENC": "ambulatory"}
WELLNESS_ONLY = {"410620009", "162673000", "308646001"}


def utc(s):
    """FHIR dateTime/instant -> CSV timestamp in UTC; date-only values pass through."""
    if not s:
        return ""
    if len(s) == 10:
        return s
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_date(s):
    return utc(s)[:10] if s else ""


def ref(r):
    return (r or {}).get("reference", "").replace("urn:uuid:", "").split("/")[-1]


def coding(cc):
    c = ((cc or {}).get("coding") or [{}])[0]
    return c.get("system", ""), c.get("code", ""), c.get("display", (cc or {}).get("text", ""))


def value(o):
    if "valueQuantity" in o:
        q = o["valueQuantity"]
        return str(float(q["value"])) if "value" in q else "", q.get("unit", q.get("code", "")), "numeric"
    if "valueCodeableConcept" in o:
        return coding(o["valueCodeableConcept"])[2], "", "text"
    if "valueString" in o:
        return o["valueString"], "", "text"
    return None


def convert_bundle(path, rows):
    b = json.loads(path.read_text())
    src = path.name
    for e in b.get("entry", []):
        r = e["resource"]
        t = r["resourceType"]
        fref = f"{src}#{t}/{r.get('id', '')}"
        if t == "Patient":
            name = next((n for n in r.get("name", []) if n.get("use") == "official"), (r.get("name") or [{}])[0])
            addr = (r.get("address") or [{}])[0]
            rows["patients"].append({
                "Id": r["id"], "BIRTHDATE": r.get("birthDate", ""), "DEATHDATE": utc_date(r.get("deceasedDateTime")),
                "FIRST": (name.get("given") or [""])[0], "LAST": name.get("family", ""),
                "GENDER": {"male": "M", "female": "F"}.get(r.get("gender"), ""),
                "ADDRESS": " ".join(addr.get("line", [])), "CITY": addr.get("city", ""), "STATE": addr.get("state", ""),
                "ZIP": addr.get("postalCode", ""), "FHIR_REF": fref})
        elif t == "Encounter":
            _, code, desc = coding((r.get("type") or [{}])[0])
            cls = "wellness" if code in WELLNESS_ONLY else CLASS.get(r.get("class", {}).get("code"), "ambulatory")
            per = r.get("period", {})
            prov = next((ref(p.get("individual")) for p in r.get("participant", []) if p.get("individual")), "")
            rows["encounters"].append({
                "Id": r["id"], "START": utc(per.get("start")), "STOP": utc(per.get("end")), "PATIENT": ref(r.get("subject")),
                "ORGANIZATION": ref(r.get("serviceProvider")), "PROVIDER": prov, "ENCOUNTERCLASS": cls, "CODE": code,
                "DESCRIPTION": desc, "FHIR_REF": fref})
        elif t == "Observation":
            base = {"DATE": utc(r.get("effectiveDateTime") or r.get("issued")), "PATIENT": ref(r.get("subject")),
                    "ENCOUNTER": ref(r.get("encounter")),
                    "CATEGORY": coding((r.get("category") or [{}])[0])[1], "FHIR_REF": fref}
            parts = r.get("component") or [] if value(r) is None else [r]
            for p in parts:
                v = value(p)
                if v is None:
                    continue
                _, code, desc = coding(p["code"])
                rows["observations"].append({**base, "CODE": code, "DESCRIPTION": desc,
                                             "VALUE": v[0], "UNITS": v[1], "TYPE": v[2]})
        elif t == "Condition":
            sysm, code, desc = coding(r.get("code"))
            rows["conditions"].append({
                "START": utc_date(r.get("onsetDateTime")), "STOP": utc_date(r.get("abatementDateTime")),
                "PATIENT": ref(r.get("subject")), "ENCOUNTER": ref(r.get("encounter")), "SYSTEM": sysm,
                "CODE": code, "DESCRIPTION": desc, "FHIR_REF": fref})
        elif t == "Procedure":
            sysm, code, desc = coding(r.get("code"))
            per = r.get("performedPeriod", {})
            start = per.get("start") or r.get("performedDateTime")
            rows["procedures"].append({
                "START": utc(start), "STOP": utc(per.get("end")), "PATIENT": ref(r.get("subject")),
                "ENCOUNTER": ref(r.get("encounter")), "SYSTEM": sysm, "CODE": code, "DESCRIPTION": desc,
                "FHIR_REF": fref})
        elif t == "Immunization":
            _, code, desc = coding(r.get("vaccineCode"))
            rows["immunizations"].append({
                "DATE": utc(r.get("occurrenceDateTime")), "PATIENT": ref(r.get("patient")),
                "ENCOUNTER": ref(r.get("encounter")), "CODE": code, "DESCRIPTION": desc, "FHIR_REF": fref})


def convert(fhir_dir, out_dir, sample=None, seed=42):
    """Streams bundle by bundle (a full 10k panel is ~60 GB of FHIR; never held in memory)."""
    files = sorted(p for p in pathlib.Path(fhir_dir).glob("*.json")
                   if not p.name.startswith(("hospitalInformation", "practitionerInformation")))
    if sample:
        files = sorted(random.Random(seed).sample(files, sample))
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fhs = {t: open(out / f"{t}.csv", "w", newline="") for t in HEADERS}
    ws = {t: csv.DictWriter(fhs[t], fieldnames=cols.split(",") + ["FHIR_REF"], extrasaction="ignore")
          for t, cols in HEADERS.items()}
    for w in ws.values():
        w.writeheader()
    counts = {t: 0 for t in HEADERS}
    for f in files:
        rows = {t: [] for t in HEADERS}
        convert_bundle(f, rows)
        for t, rs in rows.items():
            ws[t].writerows(rs)
            counts[t] += len(rs)
    for fh in fhs.values():
        fh.close()
    return counts, len(files)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("fhir_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--sample", type=int)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    counts, n = convert(a.fhir_dir, a.out_dir, a.sample, a.seed)
    print(f"converted {n} bundles -> {a.out_dir}: " + ", ".join(f"{t} {c}" for t, c in counts.items()))
