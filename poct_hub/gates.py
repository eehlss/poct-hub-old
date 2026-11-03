"""Seven quality gates for client self-test submissions. Pure functions, no I/O.

Research and decision-support only. The AI never sets a grade: only this module does.
Grades: verified | unverified | blocked | critical
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date

PASS, WARN, BLOCK, CRIT = "pass", "warn", "block", "critical"

# Analyte rules (defaults; clinic-editable in production). Critical limits are UNVERIFIED defaults.
ANALYTES = {
    "glucose": {"unit": "mmol/L", "range": (1.1, 33.3), "crit_lo": 3.0, "crit_hi": 20.0, "delta_pct": 60},
    "hb":      {"unit": "g/dL",   "range": (4.0, 25.0), "crit_lo": 7.0, "crit_hi": 20.0, "delta_pct": 25},
    "inr":     {"unit": "INR",    "range": (0.8, 8.0),  "crit_lo": None, "crit_hi": 5.0, "delta_pct": 50},
    "tc":      {"unit": "mmol/L", "range": (2.6, 10.4), "crit_lo": None, "crit_hi": None, "delta_pct": 30},
    "hba1c":   {"unit": "%",      "range": (4.0, 15.0), "crit_lo": None, "crit_hi": None, "delta_pct": 20},
}
VERIFY_INTERVAL_DAYS = 183     # twice-yearly device verification (CLIA D5217 spirit)
MIN_EXTRACTION_CONF = 0.80


@dataclass
class GateResult:
    gate: str
    status: str
    reason: str


@dataclass
class Evaluation:
    grade: str
    results: list[GateResult] = field(default_factory=list)

    def as_dict(self):
        return {"grade": self.grade, "gates": [r.__dict__ for r in self.results]}


def _d(x):
    if x is None or isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def g1_identity_consent(client, sub):
    if not client:
        return GateResult("G1", BLOCK, "unknown client")
    if client.get("status") != "active":
        return GateResult("G1", BLOCK, f"client status {client.get('status')}")
    if sub.get("birth_year") is not None and int(sub["birth_year"]) != int(client.get("birth_year") or -1):
        return GateResult("G1", BLOCK, "second identifier (birth year) mismatch")
    missing = {"service", "messaging", "ai_assist"} - set(client.get("consents") or [])
    if missing:
        return GateResult("G1", BLOCK, "consent missing: " + ",".join(sorted(missing)))
    return GateResult("G1", PASS, "identity and consent ok")


def g2_competency(client, sub, today):
    if (client.get("training_lapses") or 0) >= 2:
        return GateResult("G2", BLOCK, "client lockout: retraining required")
    due = _d(client.get("training_due"))
    if due is None:
        return GateResult("G2", WARN, "no self-test training record")
    if due < today:
        return GateResult("G2", WARN, f"training reassessment overdue since {due}")
    return GateResult("G2", PASS, "training current")


def g3_device(device, sub, today):
    if not device:
        return GateResult("G3", WARN, "device not registered")
    if device.get("analyte") != sub.get("analyte"):
        return GateResult("G3", BLOCK, "device not labelled for this analyte (off-label)")
    if sub.get("purpose", "monitoring") not in (device.get("labelled_use") or ["monitoring"]):
        return GateResult("G3", BLOCK, f"off-label purpose '{sub.get('purpose')}': confirm at van/central lab")
    if sub.get("analyte") == "hba1c" and not device.get("ngsp_certified"):
        return GateResult("G3", BLOCK, "HbA1c device not NGSP-certified")
    lv = _d(device.get("last_verified"))
    if lv is None or (today - lv).days > VERIFY_INTERVAL_DAYS:
        return GateResult("G3", WARN, "device verification overdue")
    if device.get("verify_pass") is False:
        return GateResult("G3", WARN, f"last verification failed (bias {device.get('bias_pct')}%)")
    return GateResult("G3", PASS, "device verified and on-label")


def g4_lot(lot, sub, today):
    if not lot:
        return GateResult("G4", WARN, "strip lot unknown")
    if _d(lot.get("expiry")) < today:
        return GateResult("G4", BLOCK, f"strip lot {lot.get('lot')} expired")
    if lot.get("flag") == "hold":
        return GateResult("G4", BLOCK, f"strip lot {lot.get('lot')} on HOLD (drift)")
    opened = _d(sub.get("vial_opened"))
    if opened and lot.get("open_vial_days") and (today - opened).days > lot["open_vial_days"]:
        return GateResult("G4", BLOCK, "open-vial stability exceeded")
    if lot.get("flag") == "watch":
        return GateResult("G4", WARN, f"strip lot {lot.get('lot')} on watch")
    return GateResult("G4", PASS, "lot in date and released")


def g5_extraction(sub):
    route = sub.get("route", "manual")
    if route == "photo":
        ex = sub.get("extraction") or {}
        if not ex.get("legible") or ex.get("value") is None:
            return GateResult("G5", WARN, "display not readable: retake photo or type value")
        if float(ex.get("confidence", 0)) < MIN_EXTRACTION_CONF:
            return GateResult("G5", WARN, "low extraction confidence")
        if sub.get("typed_value") is not None and abs(float(sub["typed_value"]) - float(ex["value"])) > 1e-6:
            return GateResult("G5", WARN, "typed value disagrees with photo")
        if not sub.get("client_confirmed"):
            return GateResult("G5", WARN, "awaiting client read-back confirmation")
        return GateResult("G5", PASS, "photo extraction confirmed")
    if route == "device_upload":
        return GateResult("G5", PASS, "device upload")
    return GateResult("G5", WARN, "manual entry: no photo evidence")


def g6_plausibility(sub, prior_value=None):
    a = ANALYTES.get(sub.get("analyte"))
    if not a:
        return GateResult("G6", BLOCK, "unsupported analyte")
    if sub.get("unit") != a["unit"]:
        return GateResult("G6", WARN, f"unit {sub.get('unit')} != {a['unit']}")
    v = sub.get("value")
    if v is None:
        return GateResult("G6", WARN, "no value")
    lo, hi = a["range"]
    if not lo <= v <= hi:
        return GateResult("G6", WARN, f"outside reportable range {lo}-{hi}")
    if sub.get("error_code"):
        return GateResult("G6", WARN, f"device error code {sub['error_code']}")
    if prior_value:
        d = abs(v - prior_value) / prior_value * 100
        if d > a["delta_pct"]:
            return GateResult("G6", WARN, f"delta check {d:.0f}% vs previous")
    return GateResult("G6", PASS, "plausible")


def g7_critical(sub):
    a = ANALYTES.get(sub.get("analyte")) or {}
    v = sub.get("value")
    if v is None:
        return GateResult("G7", PASS, "no value")
    if a.get("crit_lo") is not None and v < a["crit_lo"]:
        return GateResult("G7", CRIT, f"critical low (< {a['crit_lo']})")
    if a.get("crit_hi") is not None and v > a["crit_hi"]:
        return GateResult("G7", CRIT, f"critical high (> {a['crit_hi']})")
    return GateResult("G7", PASS, "not critical")


def evaluate(sub, client, device, lot, prior_value=None, today=None) -> Evaluation:
    today = today or date.today()
    g1 = g1_identity_consent(client, sub)
    if g1.status == BLOCK:
        return Evaluation("blocked", [g1])
    res = [g1, g2_competency(client, sub, today), g3_device(device, sub, today), g4_lot(lot, sub, today),
           g5_extraction(sub), g6_plausibility(sub, prior_value), g7_critical(sub)]
    st = {r.status for r in res}
    if CRIT in st:          # safety first: a critical value is escalated even if quality is doubtful
        grade = "critical"
    elif BLOCK in st:
        grade = "blocked"
    elif WARN in st:
        grade = "unverified"
    else:
        grade = "verified"
    return Evaluation(grade, res)
