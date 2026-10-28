"""แปลงข้อความค้นหา เช่น  ไอเดียแอป from:สมชาย in:brain last:7d has:link -ยกเลิก"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from settings import TZ
from textutil import norm

MAX_TERMS = 8

OPS = {
    "from": "from", "by": "from", "user": "from", "จาก": "from",
    "in": "in", "channel": "in", "ห้อง": "in",
    "after": "after", "since": "after", "ตั้งแต่": "after",
    "before": "before", "until": "before", "ก่อน": "before",
    "last": "last", "within": "last", "ล่าสุด": "last",
    "on": "on", "วันที่": "on",
    "has": "has", "มี": "has",
    "sort": "sort", "เรียง": "sort",
}

HAS_MAP = {
    "link": "link", "links": "link", "url": "link", "ลิงก์": "link", "ลิงค์": "link",
    "image": "image", "img": "image", "photo": "image", "pic": "image", "รูป": "image", "รูปภาพ": "image",
    "file": "file", "ไฟล์": "file",
    "attachment": "attachment", "attach": "attachment", "แนบ": "attachment",
}

_OP_RE = re.compile(
    r'(?<!\S)(?P<op>'
    + "|".join(sorted((re.escape(k) for k in OPS), key=len, reverse=True))
    + r'):(?:"(?P<q>[^"]*)"|(?P<v>\S+))',
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r'-?"[^"]+"|\S+')

_REL_UNITS = {
    "h": 3600, "ชม": 3600, "ชั่วโมง": 3600,
    "d": 86400, "วัน": 86400,
    "w": 604800, "สัปดาห์": 604800, "อาทิตย์": 604800,
    "m": 2592000, "เดือน": 2592000,           # m = เดือน (30 วัน)
    "y": 31536000, "ปี": 31536000,
}
_REL_RE = re.compile(
    r"^(\d+)\s*(" + "|".join(sorted((re.escape(k) for k in _REL_UNITS), key=len, reverse=True)) + r")$"
)
_DATE_RES = [
    re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?$"),   # 2026-10-28
    re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:[ T](\d{1,2}):(\d{2}))?$"),   # 28/10/2026
]
_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


@dataclass
class Query:
    terms: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    after: Optional[int] = None      # unix ts (รวม)
    before: Optional[int] = None     # unix ts (ไม่รวม)
    has: set[str] = field(default_factory=set)
    newest_first: bool = True
    errors: list[str] = field(default_factory=list)

    def has_criteria(self) -> bool:
        return bool(
            self.terms or self.authors or self.channels or self.has
            or self.after is not None or self.before is not None
        )


def parse_point(s: str):
    """คืน (datetime, มีเวลาไหม) หรือ None  รองรับปี พ.ศ. (>2400)"""
    s = s.strip()
    for i, rx in enumerate(_DATE_RES):
        m = rx.match(s)
        if not m:
            continue
        if i == 0:
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        else:
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        if y > 2400:
            y -= 543
        hh = int(m[4]) if m[4] is not None else None
        mi = int(m[5]) if m[5] is not None else 0
        try:
            return datetime(y, mo, d, hh or 0, mi, tzinfo=TZ), hh is not None
        except ValueError:
            return None
    return None


def parse_range(spec: str):
    """คืน (start_ts, end_ts|None) หรือ None ถ้าอ่านไม่ออก  end เป็นแบบไม่รวม"""
    s = re.sub(r"\s+", " ", (spec or "").strip().lower())
    if not s:
        return None
    now = datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = timedelta(days=1)

    if s in ("today", "วันนี้"):
        return int(today.timestamp()), int((today + day).timestamp())
    if s in ("yesterday", "เมื่อวาน"):
        return int((today - day).timestamp()), int(today.timestamp())

    m = _REL_RE.match(s)
    if m:
        return int(now.timestamp()) - int(m.group(1)) * _REL_UNITS[m.group(2)], None

    parts = re.split(r"\s*(?:\.\.|\bto\b|ถึง)\s*", s)
    if len(parts) == 2:
        a = b = None
        if parts[0]:
            pa = parse_point(parts[0])
            if not pa:
                return None
            a = pa[0]
        if parts[1]:
            pb = parse_point(parts[1])
            if not pb:
                return None
            b = pb[0] + (timedelta(minutes=1) if pb[1] else day)
        if a is None and b is None:
            return None
        return (int(a.timestamp()) if a else None, int(b.timestamp()) if b else None)

    p = parse_point(s)
    if p:
        dt, has_time = p
        return int(dt.timestamp()), int((dt + (timedelta(minutes=1) if has_time else day)).timestamp())
    return None


def _apply(q: Query, op: str, val: str) -> None:
    if not val:
        return
    if op == "from":
        q.authors.append(val)
    elif op == "in":
        q.channels.append(val)
    elif op in ("after", "before", "last", "on"):
        r = parse_range(val)
        if r is None:
            q.errors.append(f"อ่านวันที่/ช่วงเวลาไม่ออก: {op}:{val.replace('`', chr(39))}")
            return
        start, end = r
        if op == "after":
            if start is not None:
                q.after = start
        elif op == "before":
            q.before = end if end is not None else start
        else:
            q.after, q.before = start, end
    elif op == "has":
        for part in re.split(r"[,\s]+", val.lower()):
            if not part:
                continue
            key = HAS_MAP.get(part)
            if key:
                q.has.add(key)
            else:
                q.errors.append(f"ไม่รู้จัก has:{part.replace('`', chr(39))} (ใช้ link / image / file / attachment)")
    elif op == "sort":
        v = val.lower()
        if v in ("old", "oldest", "asc", "เก่า"):
            q.newest_first = False
        elif v in ("new", "newest", "desc", "ใหม่"):
            q.newest_first = True
        else:
            q.errors.append("sort: ใช้ได้เฉพาะ old หรือ new")


def parse_query(text: str) -> Query:
    q = Query()

    def _handle(m):
        op = OPS[m.group("op").lower()]
        val = m.group("q") if m.group("q") is not None else m.group("v")
        _apply(q, op, (val or "").strip())
        return " "

    rest = _OP_RE.sub(_handle, text or "")
    for tok in _TOKEN_RE.findall(rest):
        neg = len(tok) > 1 and tok.startswith("-")
        if neg:
            tok = tok[1:]
        t = norm(tok.strip('"'))
        if not t or t == "-":
            continue
        (q.exclude if neg else q.terms).append(t)
    q.terms = q.terms[:MAX_TERMS]
    q.exclude = q.exclude[:MAX_TERMS]
    return q


def split_authors(values):
    """แยก from: เป็น (ชุด ID, รายชื่อที่ normalize แล้ว)"""
    ids, names = set(), []
    for v in values:
        v = v.strip()
        m = _MENTION_RE.match(v)
        if m:
            ids.add(int(m.group(1)))
        elif v.isdigit() and 15 <= len(v) <= 22:
            ids.add(int(v))
        else:
            n = norm(v.lstrip("@"))
            if n:
                names.append(n)
    return ids, names