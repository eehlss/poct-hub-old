"""POCT Hub MVP: FastAPI. RESEARCH AND DECISION-SUPPORT ONLY. Synthetic data only."""
from __future__ import annotations
import base64, hashlib, hmac, html, json, os, re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, PlainTextResponse
from . import db, gates, lots as lotmod, westgard, ai_hook, meter_png

VERSION = "0.1.0"
BANNER = "RESEARCH AND DECISION-SUPPORT ONLY. Not a medical device. Synthetic data only. Clinician review required."


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@asynccontextmanager
async def lifespan(_app):
    db.init()
    if os.getenv("SEED_ON_START", "true").lower() == "true" and db.count("clinics") == 0:
        import seed_synthetic
        seed_synthetic.seed()
    yield


app = FastAPI(title="POCT Hub MVP", version=VERSION, description=BANNER, lifespan=lifespan)

OPEN_PATHS = ("/healthz", "/health")  # Google reserves /healthz on *.run.app; /health is the public alias
OPEN_PREFIXES = ("/webhook/",)   # HMAC-verified; disabled (503) unless WHATSAPP_APP_SECRET is set


def _auth_ok(header: str) -> bool:
    """HTTP Basic auth against DASHBOARD_USER / DASHBOARD_PASSWORD (env; use Secret Manager on Cloud Run).
    Fails closed when no password is configured, unless ALLOW_NO_AUTH=true (local dev only)."""
    pw = (os.getenv("DASHBOARD_PASSWORD") or "").strip()
    if not pw:
        return os.getenv("ALLOW_NO_AUTH", "").lower() == "true"
    if not header.startswith("Basic "):
        return False
    try:
        user, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    want_user = os.getenv("DASHBOARD_USER", "demo")
    return hmac.compare_digest(user.encode(), want_user.encode()) & hmac.compare_digest(given.encode(), pw.encode())


@app.middleware("http")
async def basic_auth_and_headers(request: Request, call_next):
    path = request.url.path
    if not (path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)) and not _auth_ok(request.headers.get("authorization", "")):
        resp = Response("Authentication required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="POCT Hub demo", charset="UTF-8"'})
    else:
        resp = await call_next(request)
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


# ---------------- core logic ----------------
def refresh_lot_flags():
    out = {}
    for lot in db.q("SELECT * FROM lots"):
        checks = db.q("SELECT * FROM lot_checks WHERE lot=:l ORDER BY ts", l=lot["lot"])
        biases = [lotmod.pct_bias(c["device_value"], c["reference_value"]) for c in checks]
        st = lotmod.ewma_status(biases, lot["sd_bias"] or 4.0, lot["tea_half"] or 7.5)
        db.ex("UPDATE lots SET flag=:f WHERE lot=:l", f=st["flag"], l=lot["lot"])
        out[lot["lot"]] = st
    return out


def process_submission(sub: dict, image: bytes | None = None, content_type: str = "") -> dict:
    client = db.one("SELECT * FROM clients WHERE id=:i", i=sub["client_id"])
    device = db.one("SELECT * FROM devices WHERE client_id=:i AND analyte=:a", i=sub["client_id"], a=sub["analyte"]) \
        or db.one("SELECT * FROM devices WHERE client_id=:i", i=sub["client_id"])
    lot = db.one("SELECT * FROM lots WHERE lot=:l", l=sub.get("lot") or "")
    if image:
        sub["route"] = "photo"
        sub["extraction"] = ai_hook.extract_reading(image, content_type)
        ex = sub["extraction"]
        sub["value"] = ex.get("value") if ex.get("legible") else sub.get("typed_value")
        sub["unit"] = ex.get("unit") or sub.get("unit")
        sub["error_code"] = ex.get("error_code")
    else:
        sub["route"] = "manual"
        sub["value"] = sub.get("typed_value")
    prior = db.one("SELECT value FROM submissions WHERE client_id=:c AND analyte=:a AND grade='verified' "
                   "ORDER BY id DESC LIMIT 1", c=sub["client_id"], a=sub["analyte"])
    ev = gates.evaluate(sub, client, device, lot, prior["value"] if prior else None)
    sid = db.insert("submissions", ts=now(), client_id=sub["client_id"], device_id=device["id"] if device else None,
                    lot=sub.get("lot"), analyte=sub["analyte"], value=sub.get("value"), unit=sub.get("unit"),
                    route=sub["route"], purpose=sub.get("purpose", "monitoring"),
                    extraction=sub.get("extraction") or {}, grade=ev.grade, gates=ev.as_dict()["gates"])
    if client:
        db.insert("messages", ts=now(), client_id=sub["client_id"], direction="in", status="received",
                  submission_id=sid, source="client",
                  text=f"[{sub['route']}] {sub['analyte']} {sub.get('typed_value') or ''} lot {sub.get('lot') or '-'}")
    reasons = [r.reason for r in ev.results if r.status != gates.PASS]
    msg = None
    if client and "messaging" in (client.get("consents") or []):
        d = ai_hook.draft_message({"analyte": sub["analyte"], "value": sub.get("value"),
                                   "unit": sub.get("unit") or "", "grade": ev.grade, "reasons": reasons})
        status = {"verified": "sent", "critical": "sent"}.get(ev.grade, "draft")   # others need clinician approval
        mid = db.insert("messages", ts=now(), client_id=sub["client_id"], direction="out", text=d["text"],
                        status=status, submission_id=sid, source=d["source"])
        msg = {"id": mid, "status": status, **d}
    return {"submission_id": sid, **ev.as_dict(), "value": sub.get("value"), "extraction": sub.get("extraction"),
            "message": msg, "escalated": ev.grade == "critical", "banner": BANNER}


def qc_status():
    out = []
    for (an, inst) in {(r["analyte"], r["analyser"]) for r in db.q("SELECT analyte, analyser FROM qc_runs")}:
        rows = db.q("SELECT * FROM qc_runs WHERE analyte=:a AND analyser=:i ORDER BY ts", a=an, i=inst)
        pts = [westgard.Point(key=str(r["id"]), level=r["level"], value=r["value"], mean=r["mean"], sd=r["sd"], ts=r["ts"])
               for r in rows]
        last_ts = pts[-1].ts
        cur = [p for p in pts if p.ts == last_ts]
        hist = [p for p in pts if p.ts != last_ts]
        v = westgard.evaluate_run(cur, hist, westgard.DEFAULT_RULES)
        out.append({"analyser": inst, "analyte": an, "run_ts": last_ts, "status": westgard.worst(v.values()),
                    "points": [{"level": p.level, "value": p.value, "z": round(p.z, 2), **v[p.key].__dict__} for p in cur],
                    "history_n": len(hist)})
    return out


# ---------------- API ----------------
@app.get("/health")
@app.get("/healthz")
def healthz():
    try:
        db.one("SELECT 1 AS ok")
        dbs = "ok"
    except Exception as e:
        dbs = f"error:{type(e).__name__}"
    return {"status": "ok" if dbs == "ok" else "degraded", "db": dbs, "ai_mode": ai_hook.mode(),
            "ai": ai_hook.public_config(), "version": VERSION, "label": "research and decision-support only"}


@app.post("/api/submit")
async def api_submit(client_id: str = Form(...), analyte: str = Form("glucose"), lot: str = Form(""),
                     typed_value: float | None = Form(None), unit: str = Form("mmol/L"),
                     purpose: str = Form("monitoring"), birth_year: int | None = Form(None),
                     client_confirmed: bool = Form(False), photo: UploadFile | None = File(None)):
    image = await photo.read() if photo is not None else None
    if image and len(image) > 8 * 1024 * 1024:
        raise HTTPException(413, "image too large")
    sub = {"client_id": client_id, "analyte": analyte, "lot": lot or None, "typed_value": typed_value, "unit": unit,
           "purpose": purpose, "birth_year": birth_year, "client_confirmed": client_confirmed}
    return process_submission(sub, image or None, photo.content_type if photo else "")


@app.get("/api/clients")
def api_clients():
    return db.q("SELECT id, clinic_id, display_name, status, training_due FROM clients ORDER BY id")


@app.get("/api/submissions")
def api_submissions(limit: int = 50):
    return db.q("SELECT * FROM submissions ORDER BY id DESC LIMIT :n", n=limit)


@app.get("/api/lots")
def api_lots():
    st = refresh_lot_flags()
    return [{**l, **st.get(l["lot"], {})} for l in db.q("SELECT * FROM lots ORDER BY lot")]


@app.post("/api/lots/{lot}/check")
def api_lot_check(lot: str, device_value: float = Form(...), reference_value: float = Form(...)):
    if not db.one("SELECT lot FROM lots WHERE lot=:l", l=lot):
        raise HTTPException(404, "unknown lot")
    db.insert("lot_checks", lot=lot, ts=now(), device_value=device_value, reference_value=reference_value)
    return refresh_lot_flags()[lot]


@app.get("/api/qc")
def api_qc():
    return qc_status()


@app.get("/api/messages/{client_id}")
def api_messages(client_id: str):
    return db.q("SELECT * FROM messages WHERE client_id=:c ORDER BY id", c=client_id)


@app.post("/api/messages/{mid}/approve")
def api_approve(mid: int):
    db.ex("UPDATE messages SET status='sent' WHERE id=:i AND status='draft'", i=mid)
    return db.one("SELECT * FROM messages WHERE id=:i", i=mid)


@app.get("/api/ai/ping")
def api_ai_ping():
    """Auth-protected live check of the Meta key (one tiny text call). Never returns the key."""
    return ai_hook.ping()


@app.get("/demo/meter.png")
def demo_meter_png(value: float = 7.8, unit: str = "mmol/L", error: str = ""):
    """PNG version of the synthetic meter (Meta Model API accepts PNG/JPEG/GIF/WebP, not SVG)."""
    return Response(meter_png.render(value, unit, error), media_type="image/png")


@app.get("/demo/meter.svg")
def demo_meter(value: float = 7.8, unit: str = "mmol/L", error: str = ""):
    """Synthetic glucometer display image for the simulator (the stub extractor reads it)."""
    err = f'<text id="error" x="150" y="175" font-size="18" text-anchor="middle" fill="#b00">{html.escape(error)}</text>' if error else ""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200"><rect width="300" height="200" rx="24" fill="#334"/>
<rect x="30" y="25" width="240" height="120" rx="8" fill="#cfe8c8"/>
<text id="reading" x="150" y="105" font-size="64" font-family="monospace" text-anchor="middle">{value:g}</text>
<text id="unit" x="150" y="135" font-size="18" font-family="monospace" text-anchor="middle">{html.escape(unit)}</text>{err}
<text x="150" y="190" font-size="12" fill="#aaa" text-anchor="middle">SYNTHETIC DEMO METER</text></svg>'''
    return Response(svg, media_type="image/svg+xml")


# ---------------- WhatsApp Cloud API-shaped webhook (simulated; no real number configured) ----------------
@app.get("/webhook/whatsapp")
def wa_verify(request: Request):
    p = request.query_params
    tok = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if tok and p.get("hub.mode") == "subscribe" and hmac.compare_digest(p.get("hub.verify_token", ""), tok):
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


@app.post("/webhook/whatsapp")
async def wa_inbound(request: Request):
    body = await request.body()
    secret = os.getenv("WHATSAPP_APP_SECRET", "")
    if not secret:
        raise HTTPException(503, "webhook disabled: WHATSAPP_APP_SECRET not set")
    sig = request.headers.get("X-Hub-Signature-256", "")
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        raise HTTPException(401, "bad signature")
    data = json.loads(body)
    results = []
    for entry in data.get("entry", []):
        for ch in entry.get("changes", []):
            for m in ch.get("value", {}).get("messages", []):
                c = db.one("SELECT id FROM clients WHERE sim_phone=:p", p=m.get("from"))
                txt = (m.get("text") or {}).get("body", "")
                mm = re.match(r"\s*(\w+)\s+([0-9.]+)\s*(\S+)?", txt)
                if c and mm:
                    results.append(process_submission({"client_id": c["id"], "analyte": mm.group(1).lower(),
                                                       "typed_value": float(mm.group(2)), "lot": mm.group(3),
                                                       "unit": gates.ANALYTES.get(mm.group(1).lower(), {}).get("unit")}))
    return {"processed": len(results)}


# ---------------- HTML ----------------
CSS = """body{font-family:system-ui,sans-serif;margin:0;background:#f5f7fa;color:#1d2733}
.banner{background:#7a1f1f;color:#fff;padding:8px 16px;font-weight:600;font-size:14px;text-align:center}
nav{background:#14324a;padding:10px 16px}nav a{color:#fff;margin-right:18px;text-decoration:none;font-weight:600}
main{padding:16px;max-width:1200px;margin:auto}h2{margin:18px 0 8px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:14px}td,th{border:1px solid #dde3ea;padding:6px 8px;text-align:left}
th{background:#eef2f6}.chip{padding:2px 8px;border-radius:10px;font-weight:700;font-size:12px;color:#fff}
.verified,.ok,.accept,.sent{background:#2e7d32}.unverified,.watch,.alert,.draft{background:#ed8b00}.blocked,.hold,.reject{background:#c62828}
.critical{background:#6a1b9a}.received{background:#607d8b}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.kpi{background:#fff;padding:12px;border-radius:8px;border:1px solid #dde3ea}.kpi b{font-size:26px;display:block}
button{cursor:pointer}small{color:#556}"""


def page(title, body):
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · POCT Hub</title><style>{CSS}</style></head><body>
<div class="banner" role="alert">{BANNER}</div>
<nav><a href="/">Clinic dashboard</a><a href="/simulator">WhatsApp simulator</a><a href="/docs">API</a><a href="/health">/health</a></nav>
<main>{body}</main></body></html>""")


def chip(s):
    return f'<span class="chip {html.escape(str(s))}">{html.escape(str(s))}</span>'


@app.get("/", response_class=HTMLResponse)
def dashboard():
    subs = db.q("SELECT * FROM submissions ORDER BY id DESC LIMIT 40")
    lots_ = api_lots()
    qc = qc_status()
    drafts = db.q("SELECT * FROM messages WHERE status='draft' ORDER BY id DESC")
    k = {g: sum(1 for s in subs if s["grade"] == g) for g in ("verified", "unverified", "blocked", "critical")}
    kp = "".join(f'<div class="kpi"><small>{g}</small><b>{n}</b></div>' for g, n in k.items())
    srows = "".join(
        f"<tr><td>{s['id']}</td><td>{s['ts'][:16]}</td><td>{s['client_id']}</td><td>{s['analyte']}</td>"
        f"<td>{s['value'] if s['value'] is not None else '-'} {html.escape(s['unit'] or '')}</td><td>{s['route']}</td>"
        f"<td>{html.escape(s['lot'] or '-')}</td><td>{chip(s['grade'])}</td><td><small>"
        + "; ".join(html.escape(f"{g['gate']}: {g['reason']}") for g in (s['gates'] or []) if g['status'] != 'pass')
        + "</small></td></tr>" for s in subs) or "<tr><td colspan=9>No submissions yet: try the simulator.</td></tr>"
    lrows = "".join(f"<tr><td>{l['lot']}</td><td>{l['analyte']}</td><td>{l['expiry']}</td><td>{l.get('n')}</td>"
                    f"<td>{l.get('ewma')}%</td><td>±{l.get('limit_hold')}%</td><td>{chip(l['flag'])}</td></tr>" for l in lots_)
    qrows = "".join(f"<tr><td>{x['analyser']}</td><td>{x['analyte']}</td><td>{x['run_ts'][:16]}</td><td>{chip(x['status'])}</td><td>"
                    + "; ".join(f"L{p['level']} {p['value']} (z {p['z']}) {','.join(p['violated']) or '-'}" for p in x['points'])
                    + "</td></tr>" for x in qc)
    drows = "".join(f"<tr><td>{m['id']}</td><td>{m['client_id']}</td><td>{html.escape(m['text'])}</td><td>{m['source']}</td>"
                    f"<td><button onclick=\"fetch('/api/messages/{m['id']}/approve',{{method:'POST'}}).then(()=>location.reload())\">Approve &amp; send (simulated)</button></td></tr>"
                    for m in drafts) or "<tr><td colspan=5>No drafts awaiting approval.</td></tr>"
    return page("Dashboard", f"""<h1>Clinic dashboard: Mobile Clinic Van 1 + Central Lab Hub</h1>
<div class="grid">{kp}</div>
<h2>Clinician approval queue</h2><table><tr><th>#</th><th>Client</th><th>Draft message</th><th>Source</th><th></th></tr>{drows}</table>
<h2>Recent self-test submissions (7 quality gates)</h2><table><tr><th>#</th><th>Time (UTC)</th><th>Client</th><th>Analyte</th><th>Value</th><th>Route</th><th>Lot</th><th>Grade</th><th>Gate findings</th></tr>{srows}</table>
<h2>Strip-lot drift (EWMA λ=0.2, L=3 vs hub reference)</h2><table><tr><th>Lot</th><th>Analyte</th><th>Expiry</th><th>Paired checks</th><th>EWMA bias</th><th>Hold limit</th><th>Flag</th></tr>{lrows}</table>
<h2>Van analyser IQC (Westgard {', '.join(westgard.DEFAULT_RULES)})</h2><table><tr><th>Analyser</th><th>Analyte</th><th>Last run</th><th>Status</th><th>Points</th></tr>{qrows}</table>
<p><small>AI mode: {ai_hook.mode()} · v{VERSION}</small></p>""")


@app.get("/simulator", response_class=HTMLResponse)
def simulator():
    opts = "".join(f'<option value="{c["id"]}">{c["id"]} · {html.escape(c["display_name"])}</option>' for c in api_clients())
    lots_ = "".join(f'<option>{l["lot"]}</option>' for l in db.q("SELECT lot FROM lots ORDER BY lot"))
    return page("WhatsApp simulator", f"""<h1>WhatsApp simulator (no real messages are sent)</h1>
<div style="display:flex;gap:24px;flex-wrap:wrap">
<form id="f" style="background:#fff;padding:16px;border-radius:8px;min-width:300px">
<p>Client <select name="client_id" id="cid">{opts}</select></p>
<p>Analyte <select name="analyte" id="an"><option>glucose</option><option>inr</option><option>hb</option></select>
Unit <input name="unit" id="unit" value="mmol/L" size="7"></p>
<p>Strip lot <select name="lot"><option value="">(unknown)</option>{lots_}</select></p>
<p>Purpose <select name="purpose"><option>monitoring</option><option>diagnosis</option></select></p>
<p><label><input type="radio" name="mode" value="photo" checked> Synthetic meter photo, reading <input id="pv" value="7.8" size="5"></label></p>
<p><label><input type="radio" name="mode" value="file"> Upload photo <input type="file" id="file" accept="image/*"></label></p>
<p><label><input type="radio" name="mode" value="typed"> Type value</label> <input name="typed_value" id="tv" size="6"></p>
<p><label><input type="checkbox" name="client_confirmed" value="true" checked> I confirm the reading is correct</label></p>
<button type="submit">Send ▶</button></form>
<div style="background:#e5ddd5;border-radius:16px;padding:12px;width:380px;min-height:420px" id="thread"></div>
<pre id="out" style="flex:1;min-width:300px;background:#fff;padding:12px;font-size:12px;white-space:pre-wrap"></pre></div>
<script>
const f=document.getElementById('f');
async function load(){{const r=await fetch('/api/messages/'+cid.value);const m=await r.json();
thread.innerHTML=m.map(x=>`<div style="margin:6px;${{x.direction=='in'?'text-align:right':''}}"><span style="display:inline-block;max-width:85%;padding:8px;border-radius:8px;background:${{x.direction=='in'?'#dcf8c6':'#fff'}}">${{x.text.replace(/</g,'&lt;')}}<br><small>${{x.status}}${{x.direction=='out'?' · '+x.source:''}}</small></span></div>`).join('')}}
cid.onchange=load;load();
f.onsubmit=async e=>{{e.preventDefault();const fd=new FormData(f);const mode=fd.get('mode');fd.delete('mode');
if(mode!=='typed')fd.delete('typed_value');
if(mode==='photo'){{const b=await (await fetch('/demo/meter.svg?value='+encodeURIComponent(pv.value)+'&unit='+encodeURIComponent(unit.value))).blob();fd.append('photo',b,'meter.svg')}}
if(mode==='file'&&file.files[0])fd.append('photo',file.files[0]);
const r=await fetch('/api/submit',{{method:'POST',body:fd}});out.textContent=JSON.stringify(await r.json(),null,2);load()}};
</script>""")
