"""Meta model hook: photo extraction + recommendation drafting.

Defaults to a deterministic STUB. A real call happens only when META_API_KEY and META_API_BASE are set.
All endpoint paths, model ids and payload shapes are UNVERIFIED and configurable via env:
  META_API_BASE, META_API_KEY, META_VISION_MODEL, META_TEXT_MODEL, META_CHAT_PATH
The AI never grades: gates.py does. Drafts are number-checked and always need clinician approval.
"""
from __future__ import annotations
import base64, json, os, re
import httpx


def _cfg():
    return {
        "base": os.getenv("META_API_BASE", "").rstrip("/"),
        "key": os.getenv("META_API_KEY", ""),
        "vision": os.getenv("META_VISION_MODEL", "CONFIGURE_ME_vision_model"),
        "text": os.getenv("META_TEXT_MODEL", "CONFIGURE_ME_text_model"),
        "path": os.getenv("META_CHAT_PATH", "/chat/completions"),
        "timeout": float(os.getenv("META_TIMEOUT_S", "30")),
    }


def mode() -> str:
    c = _cfg()
    return "meta" if c["base"] and c["key"] else "stub"


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


def _chat(model: str, messages: list, json_mode: bool = False) -> str:
    c = _cfg()
    body = {"model": model, "messages": messages, "temperature": 0}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = httpx.post(c["base"] + c["path"], json=body, timeout=c["timeout"],
                   headers={"Authorization": f"Bearer {c['key']}"})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ---------- extraction ----------
SVG_READING = re.compile(rb'id="reading"[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*<')
SVG_UNIT = re.compile(rb'id="unit"[^>]*>\s*([^<]+?)\s*<')
SVG_ERR = re.compile(rb'id="error"[^>]*>\s*([^<]+?)\s*<')


def extract_reading(image: bytes, content_type: str = "") -> dict:
    """Return {legible, value, unit, error_code, confidence, source}."""
    if mode() == "meta":
        try:
            b64 = base64.b64encode(image).decode()
            prompt = ("Read the glucometer/POCT display. Reply JSON only: "
                      '{"legible":bool,"value":number|null,"unit":string|null,"error_code":string|null,"confidence":0-1}')
            out = _chat(_cfg()["vision"], [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{content_type or 'image/jpeg'};base64,{b64}"}}]}],
                json_mode=True)
            d = json.loads(out[out.find("{"): out.rfind("}") + 1])
            d["source"] = "meta"
            return d
        except Exception as e:  # never fail open: fall to unreadable
            return {"legible": False, "value": None, "unit": None, "error_code": None,
                    "confidence": 0.0, "source": f"meta_error:{type(e).__name__}"}
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
                {"role": "user", "content": base}])
            chk = number_check(ctx, out)
            if chk["ok"]:
                text, source = out.strip(), "meta"
        except Exception:
            pass
    return {"text": text, "source": source, "number_check": number_check(ctx, text)}
