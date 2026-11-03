import os
from fastapi.testclient import TestClient


def client():
    if os.path.exists("/tmp/poct_hub_pytest.db"):
        os.remove("/tmp/poct_hub_pytest.db")
    import seed_synthetic
    from poct_hub.main import app
    seed_synthetic.seed(reset=True)
    import base64
    tok = base64.b64encode(b"demo:pytest-only-not-a-secret").decode()
    return TestClient(app, headers={"Authorization": "Basic " + tok})


def test_end_to_end():
    c = client()
    h = c.get("/healthz").json()
    assert h["status"] == "ok" and h["ai_mode"] == "stub"
    assert "RESEARCH AND DECISION-SUPPORT ONLY" in c.get("/").text
    assert "WhatsApp simulator" in c.get("/simulator").text
    lots = {l["lot"]: l for l in c.get("/api/lots").json()}
    assert lots["LOT-G-2611B"]["flag"] == "hold" and lots["LOT-G-2610A"]["flag"] == "ok"
    svg = c.get("/demo/meter.svg", params={"value": 7.8}).content
    r = c.post("/api/submit", data={"client_id": "C-001", "lot": "LOT-G-2610A", "client_confirmed": "true"},
               files={"photo": ("m.svg", svg, "image/svg+xml")}).json()
    assert r["grade"] == "verified" and r["value"] == 7.8 and r["message"]["status"] == "sent"
    r = c.post("/api/submit", data={"client_id": "C-002", "lot": "LOT-G-2611B", "client_confirmed": "true"},
               files={"photo": ("m.svg", svg, "image/svg+xml")}).json()
    assert r["grade"] == "blocked" and r["message"]["status"] == "draft"
    r = c.post("/api/submit", data={"client_id": "C-001", "lot": "LOT-G-2610A", "typed_value": "2.1"}).json()
    assert r["grade"] == "critical" and r["escalated"]
    mid = c.post("/api/submit", data={"client_id": "C-003", "lot": "LOT-G-2610A", "typed_value": "6.0"}).json()["message"]["id"]
    assert c.post(f"/api/messages/{mid}/approve").json()["status"] == "sent"
    qc = c.get("/api/qc").json()
    assert qc and qc[0]["status"] in ("alert", "reject")
    assert c.post("/webhook/whatsapp", content=b"{}").status_code in (401, 503)


def test_basic_auth_gates_everything_but_healthz_and_webhook():
    from poct_hub.main import app
    anon = TestClient(app)
    assert anon.get("/healthz").status_code == 200
    for path in ("/", "/simulator", "/api/lots", "/api/qc", "/docs", "/demo/meter.svg"):
        r = anon.get(path)
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"], path
    assert anon.get("/", auth=("demo", "wrong")).status_code == 401
    assert anon.get("/", auth=("other", "pytest-only-not-a-secret")).status_code == 401
    assert anon.post("/webhook/whatsapp", content=b"{}").status_code in (401, 503)   # reaches app, not basic auth
    r = anon.get("/", auth=("demo", "pytest-only-not-a-secret"))
    assert r.status_code == 200 and "RESEARCH AND DECISION-SUPPORT ONLY" in r.text
    assert r.headers["x-robots-tag"].startswith("noindex")


def test_fails_closed_without_password(monkeypatch):
    from poct_hub.main import app
    monkeypatch.delenv("DASHBOARD_PASSWORD")
    assert TestClient(app).get("/", auth=("demo", "")).status_code == 401
    assert TestClient(app).get("/healthz").status_code == 200
