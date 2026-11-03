"""Seed SYNTHETIC demo data (no real people). Usage: python seed_synthetic.py [--reset]
Clinics, clients, devices, strip lots incl. one DRIFTING lot (LOT-G-2611B), van QC history, paired lot checks."""
from __future__ import annotations
import random, sys
from datetime import date, datetime, timedelta, timezone
from poct_hub import db

TABLES = ["messages", "submissions", "lot_checks", "qc_runs", "lots", "devices", "clients", "clinics"]


def seed(reset: bool = False, today: date | None = None, rnd_seed: int = 7) -> dict:
    db.init()
    if reset:
        for t in TABLES:
            db.ex(f"DELETE FROM {t}")
    if db.count("clinics"):
        return {"seeded": False, "reason": "already seeded (use --reset)"}
    rnd = random.Random(rnd_seed)
    today = today or date.today()
    iso = lambda d: d.isoformat()
    db.insert("clinics", id="VAN-1", name="Mobile Clinic Van 1 (synthetic)")
    db.insert("clinics", id="HUB", name="Central Lab Hub (synthetic)")

    names = ["Demo Client A", "Demo Client B", "Demo Client C", "Demo Client D", "Demo Client E", "Demo Client F"]
    for i, n in enumerate(names, 1):
        cid = f"C-{i:03d}"
        db.insert("clients", id=cid, clinic_id="VAN-1", display_name=n, birth_year=1950 + i * 5,
                  status="active", consents=["service", "messaging", "ai_assist"] if i != 6 else ["service"],
                  training_due=iso(today + timedelta(days=120)) if i != 4 else iso(today - timedelta(days=10)),
                  training_lapses=0, sim_phone=f"+000-555-01{i:02d}")
        analyte = "inr" if i == 5 else "glucose"
        db.insert("devices", id=f"D-{i:03d}", client_id=cid,
                  model="DemoINR-1" if analyte == "inr" else "DemoGluco-200", analyte=analyte,
                  labelled_use=["monitoring"], ngsp_certified=False,
                  last_verified=iso(today - timedelta(days=40 if i != 3 else 300)),
                  verify_pass=True, bias_pct=round(rnd.uniform(-4, 4), 1))

    lots = [  # lot, analyte, expiry offset, flag
        ("LOT-G-2610A", "glucose", 200, "ok"),
        ("LOT-G-2611B", "glucose", 260, "ok"),      # the drifting lot (positive bias creeping up)
        ("LOT-G-2604X", "glucose", -5, "ok"),       # expired
        ("LOT-I-2609C", "inr", 150, "ok"),
    ]
    for lot, an, off, flag in lots:
        db.insert("lots", lot=lot, analyte=an, model="DemoINR-1" if an == "inr" else "DemoGluco-200",
                  expiry=iso(today + timedelta(days=off)), open_vial_days=90, flag=flag,
                  sd_bias=4.0, tea_half=7.5 if an == "glucose" else 10.0)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    for k in range(12):  # paired comparisons: device on lot vs hub reference analyser
        ts = (now - timedelta(days=24 - 2 * k)).isoformat()
        ref = round(rnd.uniform(5, 12), 1)
        db.insert("lot_checks", lot="LOT-G-2610A", ts=ts, reference_value=ref,
                  device_value=round(ref * (1 + rnd.gauss(0, 0.03)), 2))
        drift = 0.01 * k * 1.6           # ~0 -> +18 % over 12 checks
        db.insert("lot_checks", lot="LOT-G-2611B", ts=ts, reference_value=ref,
                  device_value=round(ref * (1 + drift + rnd.gauss(0, 0.02)), 2))

    for d in range(20):  # van analyser IQC, glucose L1/L2; last run shows a 2_2s shift
        ts = (now - timedelta(days=20 - d)).isoformat()
        for lvl, mean, sd in ((1, 5.5, 0.15), (2, 16.0, 0.4)):
            z = rnd.gauss(0, 0.8) if d < 18 else 2.3
            db.insert("qc_runs", ts=ts, analyser="VAN-1 analyser", analyte="glucose", level=lvl,
                      value=round(mean + z * sd, 2), mean=mean, sd=sd)
    from poct_hub.main import refresh_lot_flags
    refresh_lot_flags()
    return {"seeded": True, "clients": len(names), "lots": len(lots)}


if __name__ == "__main__":
    print(seed(reset="--reset" in sys.argv))
