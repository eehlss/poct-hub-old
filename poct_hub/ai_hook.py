"""Meta model hook: photo extraction + recommendation drafting.

Defaults to a deterministic STUB. A real call happens only when META_API_KEY and META_API_BASE are set.
Target: Meta Model API (verified against https://dev.meta.ai/docs on 4 Nov 2026):
  base URL https://api.meta.ai/v1, header "Authorization: Bearer <key>", OpenAI-compatible
  POST /chat/completions, model muse-spark-1.3 (text + image input), image as an image_url
  data URL (jpeg/png/gif/webp only, not SVG), structured output via response_format json_schema.
Env: META_API_BASE, META_API_KEY (secret; never logged), META_VISION_MODEL, META_TEXT_MODEL,
     META_CHAT_PATH (/chat/completions), META_TIMEOUT_S (30), META_REASONING_EFFORT (low).
The AI never grades: gates.py does. Drafts are number-checked and always need clinician approval.
"""
from __future__ import annotations
import base64, json, logging, os, re
import httpx
from . import meter_png

log = logging.getLogger("poct_hub.ai")


def _cfg():
    return {
        "base": os.getenv("META_API_BASE", "").rstrip("/"),
        "key": os.getenv("META_API_KEY", ""),
        "vision": os.getenv("META_VISION_MODEL", "muse-spark-1.3"),
        "text": os.getenv("META_TEXT_MODEL", "muse-spark-1.3"),
        "effort": os.getenv("META_REASONING_EFFORT", "low").strip(),
        "path": os.getenv("META_CHAT_PATH", "/chat/completions"),
        "timeout": float(os.getenv("META_TIMEOUT_S", "30")),
    }


def mode() -> str:
    c = _cfg()
    return "meta" if c["base"] and c["key"].strip() else "stub"


def public_config() -> dict:
    """Non-secret settings for /health. Never includes the key."""
    c = _cfg()
    return {"base": c["base"] or None, "vision_model": c["vision"], "text_model": c["text"],
            "chat_path": c["path"], "key_configured": bool(c["key"].strip())}


# ---------- helpers ----------
def numbers_in(obj) -> set[str]:
    return set(re.findall(r"-?\d+\.?\d*", json.dumps(obj, default=str)))


def number_check(ctx: dict, out_text: str) -> dict:
    """From medlabai-qc app/ai.py: every number in the AI output must exist in the input."""
    allowed = set()
    for n in numbers_in(ctx):
        allowed.add(n)
        try:
            f = float(n)
            for d in (0, 1, 2, 3):
                allowed.add(f"{round(f, d):.{d}f}".rstrip("0").rstrip(".") if d else str(int(round(f))))
        except ValueError:
            pass
    allowed |= {"0", "1", "2", "3", "4", "5", "7", "10", "14", "24", "48"}
    found = set(re.findall(r"-?\d+\.?\d*", out_text))
    unknown = sorted(n for n in found if n not in allowed and n.lstrip("-") not in allowed)
    return {"ok": not unknown, "unknown_numbers": unknown[:20]}


class MetaAPIError(Exception):
    """Carries only the HTTP status, never headers or the key."""


def _content_text(content) -> str:
    if isinstance(content, list):  # tolerate content-part arrays
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _chat(model: str, messages: list, schema: dict | None = None, max_tokens: int = 800) -> str:
    c = _cfg()
    # Muse Spark docs: leave temperature/top_p at defaults; reasoning_effort "none" is rejected.
    body = {"model": model, "messages": messages, "max_completion_tokens": max_tokens}
    if c["effort"]:
        body["reasoning_effort"] = c["effort"]
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "reading", "schema": schema}}
    try:
        r = httpx.post(c["base"] + c["path"], json=body, timeout=c["timeout"],
                       headers={"Authorization": f"Bearer {c['key'].strip()}", "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        log.warning("meta api transport error: %s", type(e).__name__)
        raise MetaAPIError(type(e).__name__) from None
    if r.status_code >= 400:
        log.warning("meta api HTTP %s for model %s", r.status_code, model)
        raise MetaAPIError(f"http_{r.status_code}")
    return _content_text(r.json()["choices"][0]["message"].get("content"))


def ping() -> dict:
    """Tiny live call used by /api/ai/ping (auth-protected). Returns status only."""
    if mode() != "meta":
        return {"ai_mode": "stub", "ok": None}
    try:
        out = _chat(_cfg()["text"], [{"role": "user", "content": "Reply with the single word OK."}], max_tokens=200)
        return {"ai_mode": "meta", "ok": True, "model": _cfg()["text"], "reply_chars": len(out)}
    except Exception as e:
        return {"ai_mode": "meta", "ok": False, "error": str(e) if isinstance(e, MetaAPIError) else type(e).__name__}


# ---------- extraction ----------
SVG_READING = re.compile(rb'id="reading"[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*<')
SVG_UNIT = re.compile(rb'id="unit"[^>]*>\s*([^<]+?)\s*<')
SVG_ERR = re.compile(rb'id="error"[^>]*>\s*([^<]+?)\s*<')
READING_SCHEMA = {"type": "object", "properties": {
    "legible": {"type": "boolean"},
    "value": {"type": ["number", "null"]},
    "unit": {"type": ["string", "null"]},
    "error_code": {"type": ["string", "null"]},
    "confidence": {"type": "number"}},
    "required": ["legible", "value", "unit", "error_code", "confidence"]}


def extract_reading(image: bytes, content_type: str = "") -> dict:
    """Return {legible, value, unit, error_code, confidence, source}."""
    if mode() == "meta":
        try:
            if meter_png.is_svg(image, content_type):   # Model API does not accept SVG
                png = meter_png.svg_to_png(image)
                if png is None:
                    return {"legible": False, "value": None, "unit": None, "error_code": None,
                            "confidence": 0.0, "source": "meta_unsupported_image"}
                image, content_type = png, "image/png"
            ct = (content_type or "image/jpeg").split(";")[0].strip().lower()
            if ct not in ("image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"):
                ct = "image/jpeg"
            b64 = base64.b64encode(image).decode()
            prompt = ("Read the glucometer/POCT display in this photo. Report only what is shown on the display. "
                      "If the screen is not clearly readable, set legible to false and value to null.")
            img_part = {"type": "image_url", "image_url": {"url": f"data:{ct};base64,{b64}"}}
            try:
                out = _chat(_cfg()["vision"], [{"role": "user", "content": [
                    {"type": "text", "text": prompt}, img_part]}], schema=READING_SCHEMA)
            except MetaAPIError as e:
                if str(e) != "http_400":
                    raise
                # schema rejected (nullable unions are UNVERIFIED on Model API): ask for JSON in the prompt instead
                out = _chat(_cfg()["vision"], [{"role": "user", "content": [
                    {"type": "text", "text": prompt + " Reply with JSON only: " + json.dumps(READING_SCHEMA["properties"])},
                    img_part]}])
            d = json.loads(out[out.find("{"): out.rfind("}") + 1])
            v = d.get("value")
            return {"legible": bool(d.get("legible")) and isinstance(v, (int, float)),
                    "value": float(v) if isinstance(v, (int, float)) else None,
                    "unit": d.get("unit"), "error_code": d.get("error_code"),
                    "confidence": float(d.get("confidence") or 0.0), "source": "meta"}
        except Exception as e:  # never fail open: fall to unreadable
            tag = str(e) if isinstance(e, MetaAPIError) else type(e).__name__
            return {"legible": False, "value": None, "unit": None, "error_code": None,
                    "confidence": 0.0, "source": f"meta_error:{tag}"}
    # STUB: understands the synthetic SVG meter images served at /static/demo/*.svg
    m = SVG_READING.search(image)
    if m:
        u = SVG_UNIT.search(image)
        er = SVG_ERR.search(image)
        return {"legible": True, "value": float(m.group(1)), "unit": u.group(1).decode() if u else None,
                "error_code": er.group(1).decode() if er else None, "confidence": 0.97, "source": "stub"}
    return {"legible": False, "value": None, "unit": None, "error_code": None, "confidence": 0.0, "source": "stub"}


# ---------- recommendation drafting ----------
TEMPLATES = {
    "verified": "Thank you. Your {analyte} result of {value} {unit} has been received and checked. "
                "Keep following your care plan. Next check: as agreed with your clinic.",
    "unverified": "Thank you. We received your {analyte} result of {value} {unit}, but it could not be fully "
                  "checked ({reasons}). Please repeat the test following your training card, or bring your meter "
                  "to the next mobile clinic visit.",
    "blocked": "We could not accept this {analyte} result ({reasons}). Please do not act on it. "
               "A clinic team member will contact you.",
    "critical": "Your {analyte} reading of {value} {unit} needs attention now. A clinician has been alerted and "
                "will call you. If you feel unwell, seek emergency care immediately.",
}


def draft_message(ctx: dict) -> dict:
    """ctx: {analyte, value, unit, grade, reasons}. Returns {text, source, number_check}."""
    base = TEMPLATES[ctx["grade"]].format(**{**ctx, "reasons": "; ".join(ctx.get("reasons") or []) or "n/a"})
    text, source = base, "template"
    if mode() == "meta" and ctx["grade"] != "critical":   # critical path never goes through the model
        try:
            out = _chat(_cfg()["text"], [
                {"role": "system", "content": "Rewrite the clinic message in plain, kind language (max 60 words). "
                 "Do not add numbers, doses, diagnoses or advice not in the input."},
                {"role": "user", "content": base}], max_tokens=600)
            chk = number_check(ctx, out)
            if chk["ok"]:
                text, source = out.strip(), "meta"
        except Exception:
            pass
    return {"text": text, "source": source, "number_check": number_check(ctx, text)}
