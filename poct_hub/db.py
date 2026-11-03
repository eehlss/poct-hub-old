"""Storage: SQLite (Cloud Run demo, /tmp, reset on cold start). DATABASE_URL may override. Synthetic data only."""
from __future__ import annotations
import json, os
from sqlalchemy import (create_engine, MetaData, Table, Column, Integer, String, Float, Boolean, Text, text)

# Cloud Run demo: SQLite file in /tmp (in-memory filesystem). Created + seeded with SYNTHETIC data on startup;
# it resets on every cold start. Run with --max-instances 1 so all requests see the same file.
URL = os.getenv("DATABASE_URL", "sqlite:////tmp/poct_hub.db")
engine = create_engine(URL, pool_pre_ping=True, future=True,
                       connect_args={"check_same_thread": False, "timeout": 15} if URL.startswith("sqlite") else {})


md = MetaData()

Table("clinics", md, Column("id", String(20), primary_key=True), Column("name", String(120)))
Table("clients", md, Column("id", String(20), primary_key=True), Column("clinic_id", String(20)),
      Column("display_name", String(80)), Column("birth_year", Integer), Column("status", String(20)),
      Column("consents", Text), Column("training_due", String(10)), Column("training_lapses", Integer),
      Column("sim_phone", String(30)))
Table("devices", md, Column("id", String(20), primary_key=True), Column("client_id", String(20)),
      Column("model", String(80)), Column("analyte", String(20)), Column("labelled_use", Text),
      Column("ngsp_certified", Boolean), Column("last_verified", String(10)), Column("verify_pass", Boolean),
      Column("bias_pct", Float))
Table("lots", md, Column("lot", String(30), primary_key=True), Column("analyte", String(20)),
      Column("model", String(80)), Column("expiry", String(10)), Column("open_vial_days", Integer),
      Column("flag", String(10)), Column("sd_bias", Float), Column("tea_half", Float))
Table("lot_checks", md, Column("id", Integer, primary_key=True, autoincrement=True), Column("lot", String(30)),
      Column("ts", String(25)), Column("device_value", Float), Column("reference_value", Float))
Table("submissions", md, Column("id", Integer, primary_key=True, autoincrement=True), Column("ts", String(25)),
      Column("client_id", String(20)), Column("device_id", String(20)), Column("lot", String(30)),
      Column("analyte", String(20)), Column("value", Float), Column("unit", String(20)), Column("route", String(20)),
      Column("purpose", String(20)), Column("extraction", Text), Column("grade", String(12)), Column("gates", Text))
Table("messages", md, Column("id", Integer, primary_key=True, autoincrement=True), Column("ts", String(25)),
      Column("client_id", String(20)), Column("direction", String(4)), Column("text", Text),
      Column("status", String(12)), Column("submission_id", Integer), Column("source", String(20)))
Table("qc_runs", md, Column("id", Integer, primary_key=True, autoincrement=True), Column("ts", String(25)),
      Column("analyser", String(40)), Column("analyte", String(20)), Column("level", Integer),
      Column("value", Float), Column("mean", Float), Column("sd", Float))

JSON_COLS = {"consents", "labelled_use", "extraction", "gates"}


def init():
    md.create_all(engine)


def _row(m):
    d = dict(m)
    for k in JSON_COLS & d.keys():
        if isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def q(sql, **p):
    with engine.connect() as c:
        return [_row(r._mapping) for r in c.execute(text(sql), p)]


def one(sql, **p):
    r = q(sql, **p)
    return r[0] if r else None


def ex(sql, **p):
    p = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in p.items()}
    with engine.begin() as c:
        return c.execute(text(sql), p)


def insert(table, **vals):
    vals = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in vals.items()}
    t = md.tables[table]
    with engine.begin() as c:
        r = c.execute(t.insert().values(**vals))
        return r.inserted_primary_key[0] if r.inserted_primary_key else None


def count(table):
    return one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
