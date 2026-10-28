import re
import unicodedata

_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
_WS = re.compile(r"\s+")
_THAI = re.compile("[\u0e00-\u0e7f]")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".avif")


def has_thai(text: str) -> bool:
    return bool(_THAI.search(text or ""))


def norm(text: str) -> str:
    """NFKC + ตัด zero-width + ตัวพิมพ์เล็ก + ยุบช่องว่าง (ใช้กับทั้งข้อความและคำค้น)"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _ZERO_WIDTH.sub("", t)
    t = t.casefold()
    return _WS.sub(" ", t).strip()


def build_fields(content, attachments=(), extra=""):
    """attachments = [(filename, content_type), ...]"""
    content = content or ""
    names = " ".join(name for name, _ in attachments if name)
    searchable = " ".join(p for p in (content, extra, names) if p)
    content_norm = norm(searchable)

    # ไทยมักเว้นวรรคไม่สม่ำเสมอ -> เก็บอีกเวอร์ชันที่ตัดช่องว่างทิ้งไว้ให้ค้นด้วย
    content_ns = None
    if " " in content_norm and has_thai(content_norm):
        content_ns = content_norm.replace(" ", "")

    has_image = has_file = False
    for name, ctype in attachments:
        if (ctype or "").startswith("image/") or (name or "").lower().endswith(_IMG_EXT):
            has_image = True
        else:
            has_file = True

    snippet = _WS.sub(" ", content or names).strip()[:160]
    return {
        "content_norm": content_norm,
        "content_ns": content_ns,
        "snippet": snippet,
        "has_link": 1 if _URL.search(searchable) else 0,
        "has_image": 1 if has_image else 0,
        "has_file": 1 if has_file else 0,
    }