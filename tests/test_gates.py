from datetime import date, timedelta
from poct_hub import gates, lots, westgard, ai_hook

T = date(2026, 11, 3)
CLIENT = {"id": "C-001", "status": "active", "birth_year": 1955, "consents": ["service", "messaging", "ai_assist"],
          "training_due": "2027-03-01", "training_lapses": 0}
DEVICE = {"id": "D-001", "analyte": "glucose", "labelled_use": ["monitoring"], "last_verified": "2026-10-01",
          "verify_pass": True, "bias_pct": 1.2}
LOT = {"lot": "LOT-A", "expiry": "2027-05-01", "flag": "ok", "open_vial_days": 90}


def sub(**kw):
    s = {"client_id": "C-001", "analyte": "glucose", "value": 7.8, "unit": "mmol/L", "route": "photo",
         "purpose": "monitoring", "client_confirmed": True,
         "extraction": {"legible": True, "value": 7.8, "confidence": 0.97}}
    s.update(kw)
    return s


def ev(s=None, client=CLIENT, device=DEVICE, lot=LOT, prior=None):
    return gates.evaluate(s or sub(), client, device, lot, prior, today=T)


def status(e, g):
    return next(r.status for r in e.results if r.gate == g)


def test_happy_path_verified():
    e = ev()
    assert e.grade == "verified" and len(e.results) == 7


def test_g1_unknown_client_and_consent_and_identifier():
    assert ev(client=None).grade == "blocked"
    assert ev(client={**CLIENT, "consents": ["service"]}).grade == "blocked"
    assert ev(sub(birth_year=1990)).grade == "blocked"
    assert ev(client={**CLIENT, "status": "withdrawn"}).grade == "blocked"


def test_g2_competency():
    e = ev(client={**CLIENT, "training_due": "2026-10-01"})
    assert status(e, "G2") == "warn" and e.grade == "unverified"
    assert ev(client={**CLIENT, "training_lapses": 2}).grade == "blocked"


def test_g3_off_label_blocked_and_verification():
    assert ev(sub(purpose="diagnosis")).grade == "blocked"
    assert ev(device={**DEVICE, "analyte": "inr"}).grade == "blocked"
    assert ev(sub(analyte="hba1c", unit="%", value=7.0, extraction={"legible": True, "value": 7.0, "confidence": 0.9}),
              device={**DEVICE, "analyte": "hba1c", "ngsp_certified": False}).grade == "blocked"
    assert status(ev(device={**DEVICE, "last_verified": "2026-01-01"}), "G3") == "warn"
    assert status(ev(device=None), "G3") == "warn"


def test_g4_lot():
    assert ev(lot={**LOT, "expiry": "2026-11-01"}).grade == "blocked"
    assert ev(lot={**LOT, "flag": "hold"}).grade == "blocked"
    assert ev(sub(vial_opened=str(T - timedelta(days=120)))).grade == "blocked"
    assert ev(lot=None).grade == "unverified"
    assert ev(lot={**LOT, "flag": "watch"}).grade == "unverified"


def test_g5_extraction():
    assert ev(sub(extraction={"legible": False, "value": None})).grade == "unverified"
    assert ev(sub(extraction={"legible": True, "value": 7.8, "confidence": 0.5})).grade == "unverified"
    assert ev(sub(typed_value=8.8)).grade == "unverified"
    assert ev(sub(client_confirmed=False)).grade == "unverified"
    assert status(ev(sub(route="manual")), "G5") == "warn"


def test_g6_plausibility():
    assert ev(sub(unit="mg/dL")).grade == "unverified"
    assert ev(sub(value=40.0)).grade != "verified"
    assert status(ev(sub(error_code="E-3")), "G6") == "warn"
    assert status(ev(prior=4.0, s=sub(value=7.8)), "G6") == "warn"     # 95% delta


def test_g7_critical_overrides_and_skips_ai():
    e = ev(sub(value=2.4, extraction={"legible": True, "value": 2.4, "confidence": 0.97}))
    assert e.grade == "critical"
    e = ev(sub(value=22.0, extraction={"legible": True, "value": 22.0, "confidence": 0.97}), lot={**LOT, "flag": "hold"})
    assert e.grade == "critical"     # safety: escalate even when the lot is on hold
    d = ai_hook.draft_message({"analyte": "glucose", "value": 2.4, "unit": "mmol/L", "grade": "critical", "reasons": []})
    assert d["source"] == "template" and d["number_check"]["ok"]


def test_number_check_catches_invented_numbers():
    assert ai_hook.number_check({"value": 7.8}, "Your result is 7.8") ["ok"]
    assert not ai_hook.number_check({"value": 7.8}, "Take 12 units of insulin")["ok"]


def test_stub_extractor_reads_synthetic_svg():
    svg = b'<svg><text id="reading" x="1">9.4</text><text id="unit">mmol/L</text></svg>'
    r = ai_hook.extract_reading(svg)
    assert r["legible"] and r["value"] == 9.4 and r["unit"] == "mmol/L" and r["source"] == "stub"
    assert not ai_hook.extract_reading(b"\x89PNG random")["legible"]


def test_lot_ewma():
    assert lots.ewma_status([0.5, -1, 1, 0, -0.5, 0.8], 4.0, 7.5)["flag"] == "ok"
    drifting = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]
    assert lots.ewma_status(drifting, 4.0, 7.5)["flag"] == "hold"
    assert lots.ewma_status([4, 4, 4, 4, 4], 4.0, 7.5)["flag"] == "watch"
    assert lots.ewma_status([30, 30], 4.0, 7.5)["flag"] == "ok"   # n < 5: not enough evidence


def test_westgard_engine():
    P = westgard.Point
    hist = [P(f"h{i}", 1, 5.5, 5.5, 0.15, f"2026-10-{i+1:02d}") for i in range(10)]
    v = westgard.evaluate_run([P("c", 1, 6.1, 5.5, 0.15, "2026-11-01")], hist, westgard.DEFAULT_RULES)
    assert v["c"].status == "reject" and "1_3s" in v["c"].violated
    hist2 = hist + [P("p", 1, 5.85, 5.5, 0.15, "2026-10-30")]
    v = westgard.evaluate_run([P("c", 1, 5.85, 5.5, 0.15, "2026-11-01")], hist2, westgard.DEFAULT_RULES)
    assert "2_2s" in v["c"].violated
