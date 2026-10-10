"""Writes the gold rules below as one JSON file each under gold/<doc>/ (the content here is the gold)."""
import json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from panelpilot.rules import verify
EVER = ["{MY-100}-01-01", "{MY}-12-31"]
Y = lambda n: [f"{{MY-{n}}}-01-01" if n else "{MY}-01-01", "{MY}-12-31"]
G = []
def g(doc, sec, id_, title, grade, date, quote, den, num, exc=None, notes=""):
    G.append({"id": f"{doc}.{id_}", "version": date, "title": title, "grade": grade, "effective_date": date,
              "source": {"doc_id": doc, "section_id": sec, "quote": quote},
              "denominator": den, "numerator": num, "exclusions": exc or [], "gold_notes": notes, "compiled_by": "gold"})

# ---------------- USPSTF ----------------
g("uspstf_ab","uspstf_ab:000","AAA-SCREEN-MEN-SMOKERS","AAA one-time ultrasound, men 65-75 who ever smoked","B","December 2019",
  "The USPSTF recommends 1-time screening for abdominal aortic aneurysm (AAA) with ultrasonography in men aged 65 to 75 years who have ever smoked.",
  {"all":[{"sex":{"is":"M"},"quote":"men aged 65 to 75 years"},{"age":{"min":65,"max":75,"at":"end"},"quote":"men aged 65 to 75 years"},
          {"observation":{"concept":"ever_smoker","window":EVER,"pick":"any"},"quote":"who have ever smoked"}]},
  {"procedure":{"concept":"aaa_ultrasound","window":EVER},"quote":"1-time screening for abdominal aortic aneurysm (AAA) with ultrasonography"},
  notes="1-time screening encoded as 'ever'. Smoking history is in Synthea as an observation (tobacco smoking status).")
g("uspstf_ab","uspstf_ab:007","BREAST-SCREEN-40-74","Biennial mammography, women 40-74","B","April 2024",
  "The USPSTF recommends biennial screening mammography for women aged 40 to 74 years.",
  {"all":[{"sex":{"is":"F"},"quote":"women aged 40 to 74 years"},{"age":{"min":40,"max":74,"at":"end"},"quote":"women aged 40 to 74 years"}]},
  {"procedure":{"concept":"mammogram","window":Y(1)},"quote":"biennial screening mammography"},
  notes="Biennial = within MY-1..MY. The † footnote marker is not resolvable from the A&B list.")
g("uspstf_ab","uspstf_ab:009","CERVICAL-SCREEN-21-29","Cervical cytology every 3 years, women 21-29","A","August 2018",
  "The USPSTF recommends screening for cervical cancer every 3 years with cervical cytology alone in women aged 21 to 29 years.",
  {"all":[{"sex":{"is":"F"},"quote":"women aged 21 to 29 years"},{"age":{"min":21,"max":29,"at":"end"},"quote":"women aged 21 to 29 years"}]},
  {"procedure":{"concept":"cervical_cytology","window":Y(2)},"quote":"every 3 years with cervical cytology alone"},
  notes="30-65 arm (cytology q3y OR hrHPV q5y OR cotest q5y) is a second gold-worthy rule not authored here. Hysterectomy exclusion is not in this text.")
g("uspstf_ab","uspstf_ab:011","CHLAMYDIA-SCREEN-F-LE24","Chlamydia screening, sexually active women <=24","B","September 2021",
  "The USPSTF recommends screening for chlamydia in all sexually active women 24 years or younger and in women 25 years or older who are at increased risk for infection.",
  {"all":[{"sex":{"is":"F"},"quote":"sexually active women 24 years or younger"},{"age":{"max":24,"at":"end"},"quote":"24 years or younger"},
          {"uncomputable":{"needs":"sexual activity history"},"quote":"sexually active women"}]},
  {"uncomputable":{"needs":"screening interval not stated in source"},"quote":"The USPSTF recommends screening for chlamydia"},
  notes="Expected extractor behaviour: keep 'sexually active' as uncomputable, not drop it. The >=25 increased-risk arm is wholly uncomputable.")
g("uspstf_ab","uspstf_ab:023","HCV-SCREEN-18-79","Hepatitis C screening, adults 18-79","B","March 2020",
  "The USPSTF recommends screening for hepatitis C virus (HCV) infection in adults aged 18 to 79 years.",
  {"age":{"min":18,"max":79,"at":"end"},"quote":"adults aged 18 to 79 years"},
  {"observation":{"concept":"hepatitis_c_test","window":EVER,"pick":"any"},"quote":"screening for hepatitis C virus (HCV) infection"},
  notes="Source row states no interval; 'ever' is the clinical reading (one-time) but is an interpretation.")
g("uspstf_ab","uspstf_ab:031","LUNG-LDCT-50-80","Annual LDCT, adults 50-80 with 20 pack-year history","B","March 2021",
  "The USPSTF recommends annual screening for lung cancer with low-dose computed tomography (LDCT) in adults aged 50 to 80 years who have a 20 pack-year smoking history and currently smoke or have quit within the past 15 years.",
  {"all":[{"age":{"min":50,"max":80,"at":"end"},"quote":"adults aged 50 to 80 years"},
          {"uncomputable":{"needs":"pack-year smoking history"},"quote":"who have a 20 pack-year smoking history"},
          {"uncomputable":{"needs":"current smoker or quit date within 15 years"},"quote":"currently smoke or have quit within the past 15 years"}]},
  {"procedure":{"concept":"ldct_lung","window":Y(0)},"quote":"annual screening for lung cancer with low-dose computed tomography (LDCT)"},
  notes="Pack-years are not reliably structured; marked uncomputable. Discontinuation clause not represented.")
g("uspstf_ab","uspstf_ab:046","SYPHILIS-SCREEN-RISK","Syphilis screening, nonpregnant persons at increased risk","A","September 2022",
  "The USPSTF recommends screening for syphilis infection in persons who are at increased risk for infection.",
  {"uncomputable":{"needs":"syphilis risk assessment"},"quote":"persons who are at increased risk for infection"},
  {"uncomputable":{"needs":"screening interval not stated in source"},"quote":"The USPSTF recommends screening for syphilis infection"},
  notes="Entirely risk-based: a correct extraction is all-uncomputable (rule exists, never fires).")
CRC_NUM = lambda: {"any":[
  {"procedure":{"concept":"colonoscopy","window":Y(9)},"quote":"Every 10 y"},
  {"procedure":{"concept":"fobt","window":Y(0)},"quote":"Every year"},
  {"procedure":{"concept":"ct_colonography","window":Y(4)},"quote":"Every 5 y"},
  {"procedure":{"concept":"flexible_sigmoidoscopy","window":Y(4)},"quote":"Every 5 y"}]}
g("uspstf_crc_2021","uspstf_crc_2021:001","CRC-SCREEN-45-49","Colorectal cancer screening, adults 45-49","B","May 2021",
  "The USPSTF recommends screening for colorectal cancer in adults aged 45 to 49 years.",
  {"age":{"min":45,"max":49,"at":"end"},"quote":"Adults aged 45 to 49 years"}, CRC_NUM(),
  notes="Cell-level quotes ('Every 10 y') are verbatim but not unique in the table; the quote proves existence, the concept pairing is the judgment. sDNA-FIT (every 1-3 y) omitted: interval is a range.")
g("uspstf_crc_2021","uspstf_crc_2021:000","CRC-SCREEN-50-75","Colorectal cancer screening, adults 50-75","A","May 2021",
  "The USPSTF recommends screening for colorectal cancer in all adults aged 50 to 75 years.",
  {"age":{"min":50,"max":75,"at":"end"},"quote":"Adults aged 50 to 75 years"}, CRC_NUM())
g("uspstf_crc_2016","uspstf_crc_2016:000","CRC-SCREEN-50-75","Colorectal cancer screening, adults 50-75 (2016 statement)","A","June 2016",
  "The USPSTF recommends screening for colorectal cancer starting at age 50 years and continuing until age 75 years.",
  {"age":{"min":50,"max":75,"at":"end"},"quote":"starting at age 50 years and continuing until age 75 years"}, CRC_NUM(),
  notes="Superseded version; exists to demonstrate the 50->45 update diff.")

# ---------------- CDC ----------------
g("cdc_child","cdc_child:026","MMR-2DOSE","MMR 2-dose series complete by age 7","routine","2025",
  "2-dose series at age 12–15 months, age 4–6 years",
  {"age":{"min":7,"max":18,"at":"end"},"quote":"2-dose series at age 12–15 months, age 4–6 years"},
  {"immunization":{"concept":"mmr_vaccine","min_count":2,"window":EVER},"quote":"2-dose series"},
  notes="Age 7 is derived (window closes at 6) -- the extractor's number gate would reject it; known tension. MMRV counts (valueset). Travel/outbreak doses not represented.")
g("cdc_child","cdc_child:024","HPV-ROUTINE","HPV series by age 13 (routine 11-12)","routine","2025",
  "HPV vaccination routinely recommended at age 11–12 years",
  {"age":{"min":13,"max":18,"at":"end"},"quote":"catch-up HPV vaccination recommended for all persons through age 18 years"},
  {"immunization":{"concept":"hpv_vaccine","min_count":2,"window":EVER},"quote":"Age 9–14 years at initial vaccination: 2-dose series"},
  notes="Footnote limitation: 3 doses are needed if dose 1 was at age >=15 -- dose count depends on age at first dose, not expressible in the grammar. min_count 2 is a lower bound.")
g("cdc_child","cdc_child:024","HPV-IMMUNOCOMPROMISED","HPV 3-dose series for immunocompromising conditions incl. HIV","special","2025",
  "Immunocompromising conditions, including HIV infection: 3-dose series, even for those who initiate vaccination at age 9 through 14 years.",
  {"all":[{"age":{"min":13,"max":18,"at":"end"},"quote":"catch-up HPV vaccination recommended for all persons through age 18 years"},
          {"any":[{"condition":{"concept":"immunocompromised"},"quote":"Immunocompromising conditions"},
                  {"condition":{"concept":"hiv_infection"},"quote":"including HIV infection"}]}]},
  {"immunization":{"concept":"hpv_vaccine","min_count":3,"window":EVER},"quote":"3-dose series"},
  notes="Footnote-conditioned rule (special situations).")
g("cdc_child","cdc_child:027","MENACWY-ROUTINE","MenACWY 2 doses (11-12 and 16)","routine","2025",
  "2-dose series at age 11–12 years; 16 years",
  {"age":{"min":17,"max":18,"at":"end"},"quote":"2-dose series at age 11–12 years; 16 years"},
  {"immunization":{"concept":"menacwy_vaccine","min_count":2,"window":EVER},"quote":"2-dose series"},
  notes="Age 17 derived (booster at 16). Catch-up: 1 dose suffices if first dose at 16-18 -- not representable (age-at-dose).")
g("cdc_child","cdc_child:027","MENACWY-HIGH-RISK","MenACWY for asplenia, HIV, complement deficiency (age >=2)","special","2025",
  "Dose 1 at age 24 months or older: 2-dose series at least 8 weeks apart",
  {"all":[{"age":{"min":2,"max":18,"at":"end"},"quote":"Dose 1 at age 24 months or older"},
          {"any":[{"condition":{"concept":"asplenia"},"quote":"Anatomic or functional asplenia (including sickle cell disease)"},
                  {"condition":{"concept":"hiv_infection"},"quote":"HIV infection"},
                  {"condition":{"concept":"complement_deficiency"},"quote":"persistent complement component deficiency"}]}]},
  {"immunization":{"concept":"menacwy_vaccine","min_count":2,"window":EVER},"quote":"2-dose series at least 8 weeks apart"},
  notes="Footnote-conditioned. Under-2 Menveo schedules and booster schedule not represented. 24 months -> 2 years is a unit conversion.")
g("cdc_child","cdc_child:034","TDAP-ADOLESCENT","Tdap adolescent booster by age 13","routine","2025",
  "Age 11–12 years: 1 dose Tdap (adolescent booster)",
  {"age":{"min":13,"max":18,"at":"end"},"quote":"Age 13–18 years who have not received Tdap"},
  {"immunization":{"concept":"tdap_vaccine","min_count":1,"window":Y(11)},"quote":"1 dose Tdap (adolescent booster)"},
  notes="Window ~ since age 7 (catch-up minimum). DTaP given at >=10 counts as the booster -- not represented. Pregnancy dose not represented.")

# ---------------- Bright Futures (footnotes; the grid itself is not recoverable as text) ----------------
g("bright_futures","bright_futures:001","HIV-SCREEN-15-21","HIV screen at least once between 15 and 21","footnote 30","2025",
  "Screen adolescents for HIV at least once between the ages of 15 and 21",
  {"age":{"min":21,"max":21,"at":"end"},"quote":"between the ages of 15 and 21"},
  {"observation":{"concept":"hiv_test","window":Y(6),"pick":"any"},"quote":"at least once between the ages of 15 and 21"},
  notes="Age-anchored window (ages 15-21) approximated by MY-6..MY evaluated at age 21. Grammar lacks age-anchored windows.")
g("bright_futures","bright_futures:000","HEARING-AUDIOMETRY-18-21","Audiometry once between 18 and 21","footnote 10","2025",
  "Screen with audiometry including 6,000 and 8,000 Hz high frequencies once between 11 and 14 years, once between 15 and 17 years, and once between 18 and 21 years.",
  {"age":{"min":21,"max":21,"at":"end"},"quote":"once between 18 and 21 years"},
  {"procedure":{"concept":"hearing_screening","window":Y(3)},"quote":"Screen with audiometry"},
  notes="Only the 18-21 band encoded; 11-14 and 15-17 bands are analogous. Age-anchored window approximated.")
g("bright_futures","bright_futures:001","HCV-SCREEN-18-79","Hepatitis C screening at least once 18-79","footnote 32","2025",
  "All individuals should be screened for hepatitis C virus (HCV) infection",
  {"age":{"min":18,"max":79,"at":"end"},"quote":"at least once between the ages of 18 and 79"},
  {"observation":{"concept":"hepatitis_c_test","window":EVER,"pick":"any"},"quote":"at least once between the ages of 18 and 79"},
  notes="Overlaps USPSTF HCV rule: cross-source duplicate, useful for dedup testing.")

ok = 0
for r in G:
    try:
        verify(r); ok += 1
    except Exception as e:
        print("FAIL", r["id"], e)
    d = ROOT / "gold" / r["source"]["doc_id"]; d.mkdir(parents=True, exist_ok=True)
    (d / f"{r['id'].split('.',1)[1]}.json").write_text(json.dumps(r, indent=1, ensure_ascii=False))
print(f"gold rules: {len(G)}  verify() passed: {ok}")
