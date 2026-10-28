import os
from datetime import timedelta, timezone

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = _int("GUILD_ID", 0)                      # 0 = ทุกเซิร์ฟเวอร์ที่บอทอยู่
SEARCH_CHANNEL_NAME = os.getenv("SEARCH_CHANNEL_NAME", "search").strip().lower()
SEARCH_CHANNEL_ID = _int("SEARCH_CHANNEL_ID", 0)    # ถ้าใส่ จะใช้ ID แทนชื่อ
DB_PATH = os.getenv("DB_PATH", "data/search.db")
TZ = timezone(timedelta(hours=_int("TZ_OFFSET_HOURS", 7)))  # ไทย UTC+7 ไม่มี DST
INDEX_BOTS = _bool("INDEX_BOTS", False)
AUTO_SYNC = _bool("AUTO_SYNC", True)
SYNC_INTERVAL_HOURS = _int("SYNC_INTERVAL_HOURS", 6)
PAGE_SIZE = max(1, min(_int("PAGE_SIZE", 10), 15))
MAX_RESULTS = max(10, min(_int("MAX_RESULTS", 100), 500))
RESULT_TTL_MIN = _int("RESULT_TTL_MIN", 0)          # ลบผลในห้อง search อัตโนมัติ (นาที) 0 = ไม่ลบ
EXCLUDE_CHANNEL_IDS = {
    int(x) for x in os.getenv("EXCLUDE_CHANNEL_IDS", "").replace(" ", "").split(",") if x.isdigit()
}