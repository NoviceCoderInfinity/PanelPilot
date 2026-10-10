"""The structural guarantees, as executable checks (run: python -m pytest -q)."""
import copy
import json
import pathlib

import pytest

from panelpilot import rules as R

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAL = ROOT / "rules" / "calibration"


def cal(name):
    return json.loads((CAL / f"{name}.json").read_text())


def test_calibration_rules_verify_and_run_without_approval():
    ok, bad = R.load_dir(CAL)
    assert {r.id for r in ok} == {"A1C-TEST", "A1C-TEST.rate2", "BP-CTRL", "CRC-SCREEN", "WELL-CHILD"} and not bad


def test_verified_rule_cannot_be_forged():
    with pytest.raises(R.RuleRejected):
        R.VerifiedRule("X", "1", {}, (), object())


def test_paraphrased_quote_is_rejected():
    r = cal("CRC-SCREEN")
    r["numerator"]["any"][0]["quote"] = "Colonoscopy procedure 2014–2025"
    with pytest.raises(R.RuleRejected, match="not found verbatim"):
        R.verify(r)


def test_missing_quote_is_rejected():
    r = cal("BP-CTRL")
    del r["numerator"]["quote"]
    with pytest.raises(R.RuleRejected, match="missing quote"):
        R.verify(r)


def test_number_not_in_its_quote_is_rejected():
    r = cal("CRC-SCREEN")
    r["denominator"]["age"]["min"] = 45          # quote says "Adults 46–75"
    with pytest.raises(R.RuleRejected, match="not stated in its quote"):
        R.verify(r)


def test_declared_derived_number_needs_human_approval():
    r = cal("CRC-SCREEN")
    r["id"] = "CRC-TEST-DERIVED"
    r["denominator"]["age"]["min"] = 45
    r["denominator"]["derived"] = {"min": "test"}
    v = R.verify(r)
    assert v.derived and v.needs_approval and not v.approved


def test_real_sentence_from_elsewhere_cannot_justify_a_rule():
    """The observed failure: 'adults aged 40 to 49 years' exists in the 2021 USPSTF page (incidence text)."""
    gold = json.loads((ROOT / "gold" / "uspstf_crc_2021" / "CRC-SCREEN-45-49.json").read_text())
    fake = copy.deepcopy(gold)
    fake["id"] = "FAKE-40-49"

    def set_age(c):
        k = (set(c) - {"quote", "note", "derived"}).pop()
        if k == "age":
            c["age"].update(min=40, max=49)
            c["quote"] = "adults aged 40 to 49 years"
            return True
        return k in ("all", "any") and any(set_age(s) for s in c[k])
    assert set_age(fake["denominator"])
    with pytest.raises(R.RuleRejected):
        R.verify(fake)


def test_llm_compiled_rule_needs_approval_even_when_grounded():
    r = cal("WELL-CHILD")
    r["id"], r["compiled_by"] = "WELL-CHILD-LLM", "llm"
    v = R.verify(r)
    assert v.needs_approval and not v.approved


def test_engine_refuses_unverified_rules():
    from panelpilot import engine
    with pytest.raises(TypeError):
        engine.run_rule(None, cal("BP-CTRL"), 2025)
