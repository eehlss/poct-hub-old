"""Render the SYNTHETIC demo meter as PNG.

Meta Model API image understanding accepts image/jpeg, png, gif, webp and x-icon, not SVG
(https://dev.meta.ai/docs/image-understanding), so real-model reads use this PNG.
"""
from __future__ import annotations
import io, re
from PIL import Image, ImageDraw, ImageFont

_R = re.compile(rb'id="reading"[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*<')
_U = re.compile(rb'id="unit"[^>]*>\s*([^<]+?)\s*<')
_E = re.compile(rb'id="error"[^>]*>\s*([^<]+?)\s*<')


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def render(value: float | None, unit: str = "mmol/L", error: str = "") -> bytes:
    img = Image.new("RGB", (600, 400), "#333344")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((60, 50, 540, 290), radius=16, fill="#cfe8c8")
    if value is not None:
        d.text((300, 165), f"{value:g}", font=_font(128), fill="black", anchor="mm")
    d.text((300, 255), unit, font=_font(36), fill="black", anchor="mm")
    if error:
        d.text((300, 335), error, font=_font(36), fill="#bb0000", anchor="mm")
    d.text((300, 380), "SYNTHETIC DEMO METER", font=_font(22), fill="#aaaaaa", anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def is_svg(data: bytes, content_type: str = "") -> bool:
    head = data[:200].lstrip().lower()
    return "svg" in (content_type or "").lower() or head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:1000].lower())


def svg_to_png(svg: bytes) -> bytes | None:
    """Rasterise only our own synthetic meter SVG; anything else returns None."""
    m = _R.search(svg)
    u, e = _U.search(svg), _E.search(svg)
    if not m and not e:
        return None
    return render(float(m.group(1)) if m else None, u.group(1).decode() if u else "", e.group(1).decode() if e else "")
