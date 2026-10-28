"""SQLite: คอนเนกชันเดียว + lock และเรียกผ่าน thread เพื่อไม่บล็อก event loop"""
import asyncio
import os
import sqlite3
import threading

from queryparse import split_authors
from textutil import has_thai

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    parent_id    INTEGER NOT NULL,
    author_id    INTEGER NOT NULL,
    author_label TEXT    NOT NULL,
    author_key   TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    content_norm TEXT    NOT NULL,
    content_ns   TEXT,
    snippet      TEXT    NOT NULL DEFAULT '',
    has_link     INTEGER NOT NULL DEFAULT 0,
    has_image    INTEGER NOT NULL DEFAULT 0,
    has_file     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_guild_time ON messages (guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_msg_channel ON messages (channel_id, id);
CREATE TABLE IF NOT EXISTS channel_state (
    channel_id INTEGER PRIMARY KEY,
    backfilled INTEGER NOT NULL DEFAULT 0
);
"""

COLS = ("id", "guild_id", "channel_id", "parent_id", "author_id", "author_label", "author_key",
        "created_at", "content_norm", "content_ns", "snippet", "has_link", "has_image", "has_file")
RESULT_COLS = "id, channel_id, parent_id, author_id, author_label, created_at, snippet"


def _like(term: str) -> str:
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _ints(values) -> str:
    return ",".join(str(int(v)) for v in values)


def _term_clause(term: str, negate: bool):
    # COALESCE กัน NULL ทำให้ NOT (...) ตัดแถวทิ้งผิด ๆ
    if has_thai(term):
        clause = "(content_norm LIKE ? ESCAPE '\\' OR COALESCE(content_ns, '') LIKE ? ESCAPE '\\')"
        args = [_like(term), _like(term.replace(" ", ""))]
    else:
        clause = "content_norm LIKE ? ESCAPE '\\'"
        args = [_like(term)]
    return ("NOT " + clause if negate else clause), args


class Database:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _call(self, fn, *args):
        with self._lock:
            return fn(*args)

    async def _run(self, fn, *args):
        return await asyncio.to_thread(self._call, fn, *args)

    # ---------- write ----------
    def _upsert_many(self, rows):
        sql = f"INSERT OR REPLACE INTO messages ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})"
        self._conn.executemany(sql, rows)
        self._conn.commit()

    async def upsert_many(self, rows):
        if rows:
            await self._run(self._upsert_many, list(rows))

    def _update_content(self, mid, f):
        self._conn.execute(
            "UPDATE messages SET content_norm=?, content_ns=?, snippet=?, has_link=?, has_image=?, has_file=? WHERE id=?",
            (f["content_norm"], f["content_ns"], f["snippet"], f["has_link"], f["has_image"], f["has_file"], mid),
        )
        self._conn.commit()

    async def update_content(self, mid, fields):
        await self._run(self._update_content, mid, fields)

    def _delete_ids(self, ids):
        ids = [int(i) for i in ids]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            self._conn.execute(f"DELETE FROM messages WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        self._conn.commit()

    async def delete_ids(self, ids):
        if ids:
            await self._run(self._delete_ids, list(ids))

    def _delete_channel(self, cid):
        self._conn.execute("DELETE FROM messages WHERE channel_id=? OR parent_id=?", (cid, cid))
        self._conn.execute("DELETE FROM channel_state WHERE channel_id=?", (cid,))
        self._conn.commit()

    async def delete_channel(self, cid):
        await self._run(self._delete_channel, cid)

    # ---------- sync state ----------
    def _bounds(self, cid):
        r = self._conn.execute("SELECT MIN(id), MAX(id) FROM messages WHERE channel_id=?", (cid,)).fetchone()
        return r[0], r[1]

    async def bounds(self, cid):
        return await self._run(self._bounds, cid)

    def _is_backfilled(self, cid):
        r = self._conn.execute("SELECT backfilled FROM channel_state WHERE channel_id=?", (cid,)).fetchone()
        return bool(r and r[0])

    async def is_backfilled(self, cid):
        return await self._run(self._is_backfilled, cid)

    def _set_backfilled(self, cid):
        self._conn.execute("INSERT OR REPLACE INTO channel_state (channel_id, backfilled) VALUES (?, 1)", (cid,))
        self._conn.commit()

    async def set_backfilled(self, cid):
        await self._run(self._set_backfilled, cid)

    # ---------- read ----------
    def _search(self, guild_id, q, allowed, chan_ids, limit):
        if not allowed:
            return []
        where = ["guild_id = ?", f"parent_id IN ({_ints(allowed)})"]
        args = [guild_id]

        if chan_ids is not None:
            ids = _ints(chan_ids)
            where.append(f"(channel_id IN ({ids}) OR parent_id IN ({ids}))")

        if q.authors:
            a_ids, a_names = split_authors(q.authors)
            conds = []
            if a_ids:
                conds.append(f"author_id IN ({_ints(a_ids)})")
            for n in a_names:
                conds.append("author_key LIKE ? ESCAPE '\\'")
                args.append(_like(n))
            if conds:
                where.append("(" + " OR ".join(conds) + ")")

        if q.after is not None:
            where.append("created_at >= ?")
            args.append(q.after)
        if q.before is not None:
            where.append("created_at < ?")
            args.append(q.before)

        if "link" in q.has:
            where.append("has_link = 1")
        if "image" in q.has:
            where.append("has_image = 1")
        if "file" in q.has:
            where.append("has_file = 1")
        if "attachment" in q.has:
            where.append("(has_image = 1 OR has_file = 1)")

        for t in q.terms:
            clause, a = _term_clause(t, False)
            where.append(clause)
            args += a
        for t in q.exclude:
            clause, a = _term_clause(t, True)
            where.append(clause)
            args += a

        order = "DESC" if q.newest_first else "ASC"
        sql = (f"SELECT {RESULT_COLS} FROM messages WHERE {' AND '.join(where)} "
               f"ORDER BY created_at {order}, id {order} LIMIT ?")
        args.append(limit)
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    async def search(self, guild_id, q, allowed, chan_ids, limit):
        return await self._run(self._search, guild_id, q, allowed, chan_ids, limit)

    def _browse(self, guild_id, allowed, mode, limit, tz_offset, month_day, start_today):
        if not allowed:
            return []
        base = (f"SELECT {RESULT_COLS} FROM messages WHERE guild_id = ? "
                f"AND parent_id IN ({_ints(allowed)})")
        if mode == "random":
            sql = base + " AND length(content_norm) >= 20 ORDER BY RANDOM() LIMIT ?"
            args = [guild_id, limit]
        else:
            sql = (base + " AND created_at < ? AND strftime('%m-%d', created_at + ?, 'unixepoch') = ? "
                          "ORDER BY created_at DESC LIMIT ?")
            args = [guild_id, start_today, tz_offset, month_day, limit]
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    async def browse(self, guild_id, allowed, mode, limit, tz_offset, month_day, start_today):
        return await self._run(self._browse, guild_id, allowed, mode, limit, tz_offset, month_day, start_today)

    def _stats(self, guild_id):
        r = self._conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT parent_id), MIN(created_at), MAX(created_at) "
            "FROM messages WHERE guild_id=?", (guild_id,)).fetchone()
        return tuple(r)

    async def stats(self, guild_id):
        return await self._run(self._stats, guild_id)