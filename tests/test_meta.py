"""Meta Model API client: offline tests with a fake transport (no network, fake key)."""
import base64, json, logging
import httpx
import pytest
from fastapi.testclient import TestClient
from poct_hub import ai_hook, meter_png

FAKE_KEY = "LLM_000_fake-test-key-not-real"


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


@pytest.fixture
def meta_env(monkeypatch):
    monkeypatch.setenv("META_API_BASE", "https://api.meta.ai/v1")
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    monkeypatch.delenv("META_VISION_MODEL", raising=False)
    monkeypatch.delenv("META_TEXT_MODEL", raising=False)
    calls = []

    def install(responder):
        def fake_post(url, json=None, timeout=None, headers=None):
            calls.append({"url": url, "json": json, "timeout": timeout, "headers": headers})
            return responder(len(calls), json)
        monkeypatch.setattr(ai_hook.httpx, "post", fake_post)
    return calls, install


def _chat_reply(text):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def test_vision_request_matches_model_api_docs(meta_env):
    calls, install = meta_env
    install(lambda n, body: FakeResp(200, _chat_reply(json.dumps(
        {"legible": True, "value": 7.8, "unit": "mmol/L", "error_code": None, "confidence": 0.93}))))
    assert ai_hook.mode() == "meta"
    svg = b'<svg><text id="reading" x="1">7.8</text><text id="unit">mmol/L</text></svg>'
    r = ai_hook.extract_reading(svg, "image/svg+xml")
    assert r == {"legible": True, "value": 7.8, "unit": "mmol/L", "error_code": None, "confidence": 0.93, "source": "meta"}
    c = calls[0]
    assert c["url"] == "https://api.meta.ai/v1/chat/completions"
    assert c["headers"]["Authorization"] == "Bearer " + FAKE_KEY
    b = c["json"]
    assert b["model"] == "muse-spark-1.3" and "temperature" not in b and b["reasoning_effort"] == "low"
    assert b["response_format"]["type"] == "json_schema"
    img = b["messages"][0]["content"][1]["image_url"]["url"]
    assert img.startswith("data:image/png;base64,")          # SVG was rasterised: API does not take SVG
    assert base64.b64decode(img.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"
    assert c["timeout"] == 60.0
    assert b["max_completion_tokens"] == 4096


def test_schema_rejected_falls_back_to_prompt_json(meta_env):
    calls, install = meta_env
    install(lambda n, body: FakeResp(400, {"error": {}}) if n == 1 else FakeResp(200, _chat_reply(
        'Sure: {"legible": true, "value": 5.6, "unit": "mmol/L", "error_code": null, "confidence": 0.8}')))
    r = ai_hook.extract_reading(meter_png.render(5.6), "image/png")
    assert r["value"] == 5.6 and r["source"] == "meta" and len(calls) == 2
    assert "response_format" not in calls[1]["json"]


def test_api_errors_fail_closed_and_never_leak_key(meta_env, caplog):
    calls, install = meta_env
    install(lambda n, body: FakeResp(401, {"error": {"message": "bad key"}}))
    with caplog.at_level(logging.DEBUG):
        r = ai_hook.extract_reading(meter_png.render(7.0), "image/png")
        d = ai_hook.draft_message({"analyte": "glucose", "value": 7.0, "unit": "mmol/L", "grade": "verified", "reasons": []})
        p = ai_hook.ping()
    assert r["legible"] is False and r["source"] == "meta_error:http_401"
    assert d["source"] == "template"
    assert p == {"ai_mode": "meta", "ok": False, "model": "muse-spark-1.3", "error": "http_401"}
    blob = json.dumps([r, d, p, ai_hook.public_config()]) + caplog.text
    assert FAKE_KEY not in blob and "fake-test-key" not in blob


def test_transport_timeout_fails_closed(meta_env, monkeypatch):
    def boom(*a, **k):
        raise httpx.ReadTimeout("timed out")
    monkeypatch.setattr(ai_hook.httpx, "post", boom)
    assert ai_hook.extract_reading(meter_png.render(7.0), "image/png")["source"] == "meta_error:ReadTimeout"


def test_draft_uses_model_but_number_check_guards(meta_env):
    calls, install = meta_env
    install(lambda n, body: FakeResp(200, _chat_reply("Thanks! Your glucose of 7.8 mmol/L looks fine. Take 12 units.")))
    d = ai_hook.draft_message({"analyte": "glucose", "value": 7.8, "unit": "mmol/L", "grade": "verified", "reasons": []})
    assert d["source"] == "template"                       # model added a number -> rejected
    install(lambda n, body: FakeResp(200, _chat_reply("Thanks. We checked your glucose result of 7.8 mmol/L.")))
    d = ai_hook.draft_message({"analyte": "glucose", "value": 7.8, "unit": "mmol/L", "grade": "verified", "reasons": []})
    assert d["source"] == "meta"
    n = len(calls)
    ai_hook.draft_message({"analyte": "glucose", "value": 2.1, "unit": "mmol/L", "grade": "critical", "reasons": []})
    assert len(calls) == n                                  # critical never reaches the model


def test_unknown_svg_is_not_sent(meta_env):
    calls, install = meta_env
    install(lambda n, body: FakeResp(200, _chat_reply("{}")))
    r = ai_hook.extract_reading(b"<svg><circle r='3'/></svg>", "image/svg+xml")
    assert r["source"] == "meta_unsupported_image" and calls == []


def test_health_alias_open_and_reports_mode():
    from poct_hub.main import app
    anon = TestClient(app)
    for path in ("/health", "/healthz"):
        r = anon.get(path)
        assert r.status_code == 200
        h = r.json()
        assert h["ai_mode"] == "stub" and h["ai"]["key_configured"] is False
        assert h["label"] == "research and decision-support only"
    assert anon.get("/api/ai/ping").status_code == 401
    assert anon.get("/demo/meter.png").status_code == 401


def _reply(content, finish="stop", reasoning=10):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
            "usage": {"completion_tokens": reasoning + 2, "completion_tokens_details": {"reasoning_tokens": reasoning}}}


def test_ping_budget_effort_and_reports_finish_reason_without_content(meta_env):
    calls, install = meta_env
    install(lambda n, body: FakeResp(200, _reply("OK", "stop", 37)))
    p = ai_hook.ping()
    b = calls[0]["json"]
    assert b["max_completion_tokens"] == 2048 and b["reasoning_effort"] == "minimal"
    assert p["ok"] is True and p["reply_chars"] == 2 and p["finish_reason"] == "stop" and p["reasoning_tokens"] == 37
    assert "OK" not in json.dumps({k: v for k, v in p.items() if k not in ("ai_mode", "ok", "model")})
    assert not any(k in p for k in ("text", "content", "reply"))


def test_ping_empty_length_is_not_ok(meta_env):
    calls, install = meta_env
    install(lambda n, body: FakeResp(200, _reply(None, "length", 2048)))
    p = ai_hook.ping()
    assert p["ok"] is False and p["reply_chars"] == 0 and p["finish_reason"] == "length"


def test_content_as_list_of_parts(meta_env):
    calls, install = meta_env
    parts = [{"type": "text", "text": '{"legible": true, "value": 6.1, '},
             {"type": "output_text", "text": '"unit": "mmol/L", "error_code": null, "confidence": 0.9}'}]
    install(lambda n, body: FakeResp(200, _reply(parts)))
    r = ai_hook.extract_reading(meter_png.render(6.1), "image/png")
    assert r["value"] == 6.1 and r["source"] == "meta"
    assert ai_hook._content_text(["a", {"text": {"value": "b"}}, {"type": "image"}]) == "ab"


def test_reads_use_low_effort_and_empty_reply_fails_closed(meta_env):
    calls, install = meta_env
    install(lambda n, body: FakeResp(200, _reply("", "length", 4096)))
    r = ai_hook.extract_reading(meter_png.render(7.0), "image/png")
    assert r["legible"] is False and r["source"] == "meta_error:empty_length"
    assert calls[0]["json"]["reasoning_effort"] == "low" and calls[0]["json"]["max_completion_tokens"] == 4096
    d = ai_hook.draft_message({"analyte": "glucose", "value": 7.0, "unit": "mmol/L", "grade": "verified", "reasons": []})
    assert d["source"] == "template" and d["text"]
    assert calls[-1]["json"]["max_completion_tokens"] == 2048 and calls[-1]["json"]["messages"][0]["role"] == "developer"
