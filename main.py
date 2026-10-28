import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import discord
from discord import app_commands

import settings as config
import textutil
import ui
from db import Database
from queryparse import Query, parse_query
from textutil import norm

log = logging.getLogger("searchbot")


# ---------------------------------------------------------------- helpers
def is_search_channel(ch) -> bool:
    if not isinstance(ch, discord.TextChannel):
        return False
    if config.SEARCH_CHANNEL_ID:
        return ch.id == config.SEARCH_CHANNEL_ID
    return ch.name.lower() == config.SEARCH_CHANNEL_NAME


def parent_indexable(ch) -> bool:
    if ch.id in config.EXCLUDE_CHANNEL_IDS or is_search_channel(ch):
        return False
    return isinstance(ch, (discord.TextChannel, discord.ForumChannel))


def is_indexable(ch) -> bool:
    if isinstance(ch, discord.Thread):
        if ch.id in config.EXCLUDE_CHANNEL_IDS or ch.is_private():
            return False
        parent = ch.parent
        return parent is not None and parent_indexable(parent)
    return isinstance(ch, discord.TextChannel) and parent_indexable(ch)


def build_row(msg: discord.Message):
    """คืน tuple ตามลำดับ db.COLS หรือ None ถ้าไม่ต้องเก็บ"""
    if msg.guild is None:
        return None
    if msg.author.bot and not config.INDEX_BOTS:
        return None
    if msg.type not in (discord.MessageType.default, discord.MessageType.reply):
        return None
    ch = msg.channel
    if not is_indexable(ch):
        return None
    parent_id = ch.parent_id if isinstance(ch, discord.Thread) else ch.id
    attachments = [(a.filename, a.content_type or "") for a in msg.attachments]
    snaps = getattr(msg, "message_snapshots", None) or []          # ข้อความที่ Forward มา
    extra = " ".join((getattr(s, "content", "") or "") for s in snaps)
    f = textutil.build_fields(msg.content, attachments, extra)
    if not f["content_norm"]:
        return None
    author = msg.author
    label = (getattr(author, "display_name", None) or author.name)[:80]
    key = norm(f"{label} {author.name}")
    return (
        msg.id, msg.guild.id, ch.id, parent_id, author.id, label, key,
        int(msg.created_at.timestamp()), f["content_norm"], f["content_ns"], f["snippet"],
        f["has_link"], f["has_image"], f["has_file"],
    )


def visible_parent_ids(guild: discord.Guild, member) -> set:
    """ห้อง (และฟอรัม) ที่คนค้นมีสิทธิ์เห็น + อ่านประวัติ  เธรดใช้สิทธิ์ของห้องแม่"""
    ids = set()
    if not isinstance(member, discord.Member):
        return ids
    for ch in guild.channels:
        if isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
            p = ch.permissions_for(member)
            if p.view_channel and p.read_message_history:
                ids.add(ch.id)
    return ids


def resolve_channels(guild: discord.Guild, tokens):
    ids, missing = set(), []
    pool = list(guild.channels) + list(guild.threads)
    for tok in tokens:
        m = re.fullmatch(r"<#(\d+)>|(\d{15,25})", tok.strip())
        if m:
            ids.add(int(m.group(1) or m.group(2)))
            continue
        name = norm(tok.strip().lstrip("#"))
        exact = [c.id for c in pool if name and norm(c.name) == name]
        hit = exact or [c.id for c in pool if name and name in norm(c.name)]
        if hit:
            ids.update(hit)
        else:
            missing.append(tok)
    return ids, missing


@dataclass
class Outcome:
    rows: list = field(default_factory=list)
    truncated: bool = False
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------- bot
class SearchBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True          # ต้องเปิดใน Developer Portal ด้วย
        super().__init__(intents=intents, max_messages=None)
        self.tree = app_commands.CommandTree(self)
        self.db = Database(config.DB_PATH)
        self.sync_lock = asyncio.Lock()
        self.sync_status = {"running": False, "indexed": 0, "channels": 0, "current": "", "last": None}
        self._started = False
        self._bg = []

    def guild_ok(self, guild_id: int) -> bool:
        return not config.GUILD_ID or guild_id == config.GUILD_ID

    # ---------- lifecycle
    async def setup_hook(self):
        self.add_view(ui.PanelView(self))
        if config.GUILD_ID:
            g = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()
        log.info("Slash commands synced")

    async def on_ready(self):
        log.info("Logged in as %s | guilds: %s", self.user, len(self.guilds))
        if config.AUTO_SYNC and not self._started:
            self._started = True
            self.start_sync()
            if config.SYNC_INTERVAL_HOURS > 0:
                self._bg.append(asyncio.create_task(self.periodic_sync()))

    def start_sync(self, full: bool = False) -> bool:
        if self.sync_lock.locked():
            return False
        self._bg.append(asyncio.create_task(self.sync_all(full)))
        return True

    async def periodic_sync(self):
        while not self.is_closed():
            await asyncio.sleep(config.SYNC_INTERVAL_HOURS * 3600)
            if not self.sync_lock.locked():
                await self.sync_all()

    # ---------- backfill / catch-up
    async def iter_targets(self, guild: discord.Guild):
        for ch in list(guild.text_channels):
            yield ch
        seen = set()
        for th in list(guild.threads):
            seen.add(th.id)
            yield th
        me = guild.me
        if me is None:
            return
        for parent in list(guild.text_channels) + list(guild.forums):
            if not parent_indexable(parent):
                continue
            perms = parent.permissions_for(me)
            if not (perms.view_channel and perms.read_message_history):
                continue
            try:
                async for th in parent.archived_threads(limit=None):
                    if th.id not in seen:
                        seen.add(th.id)
                        yield th
            except discord.HTTPException:
                continue

    async def sync_channel(self, ch, full: bool = False):
        if not is_indexable(ch):
            return
        me = ch.guild.me
        if me is None:
            return
        perms = ch.permissions_for(me)
        if not (perms.view_channel and perms.read_message_history):
            return

        st = self.sync_status
        st["current"] = f"#{ch.name}"
        st["channels"] += 1

        lo, hi = await self.db.bounds(ch.id)
        done = await self.db.is_backfilled(ch.id)
        if full:
            lo = hi = None
            done = False
        last_id = getattr(ch, "last_message_id", None)
        if done and hi is not None and last_id is not None and hi >= last_id:
            return                                       # ไม่มีอะไรใหม่

        buf = []

        async def flush():
            if buf:
                await self.db.upsert_many(buf)
                st["indexed"] += len(buf)
                buf.clear()

        try:
            if hi is not None:                           # 1) ตามเก็บข้อความที่พลาดตอนออฟไลน์
                async for m in ch.history(limit=None, after=discord.Object(id=hi), oldest_first=True):
                    row = build_row(m)
                    if row:
                        buf.append(row)
                    if len(buf) >= 200:
                        await flush()
                await flush()
            if not done:                                 # 2) ดึงย้อนหลังจากใหม่ไปเก่า
                before = discord.Object(id=lo) if lo is not None else None
                async for m in ch.history(limit=None, before=before, oldest_first=False):
                    row = build_row(m)
                    if row:
                        buf.append(row)
                    if len(buf) >= 200:
                        await flush()
                await flush()
                await self.db.set_backfilled(ch.id)
        except discord.Forbidden:
            log.warning("No access to history of #%s", ch.name)
        except discord.HTTPException as e:
            log.warning("HTTP error on #%s: %s", ch.name, e)
        finally:
            await flush()

    async def sync_all(self, full: bool = False):
        async with self.sync_lock:
            st = self.sync_status
            st.update(running=True, indexed=0, channels=0, current="")
            log.info("Sync started (full=%s)", full)
            try:
                for guild in list(self.guilds):
                    if not self.guild_ok(guild.id):
                        continue
                    async for ch in self.iter_targets(guild):
                        await self.sync_channel(ch, full)
            except Exception:
                log.exception("Sync failed")
            finally:
                st.update(running=False, current="", last=int(time.time()))
                log.info("Sync finished: %s messages written, %s channels checked",
                         st["indexed"], st["channels"])

    # ---------- live events
    async def on_message(self, msg: discord.Message):
        if msg.guild is None or not self.guild_ok(msg.guild.id):
            return
        if self.user and msg.author.id == self.user.id:
            return
        if is_search_channel(msg.channel):
            if not msg.author.bot and msg.content.strip():
                try:
                    await self.handle_search_message(msg)
                except Exception:
                    log.exception("search failed")
                    try:
                        await msg.reply("เกิดข้อผิดพลาดภายใน ลองใหม่อีกครั้ง", mention_author=False, delete_after=30)
                    except discord.HTTPException:
                        pass
            return
        row = build_row(msg)
        if row:
            await self.db.upsert_many([row])

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        data = payload.data
        if "content" not in data or "attachments" not in data:
            return                                       # อัปเดตแค่ embed/ลิงก์พรีวิว
        atts = [(a.get("filename", ""), a.get("content_type") or "") for a in data.get("attachments", [])]
        f = textutil.build_fields(data.get("content", ""), atts)
        if f["content_norm"]:
            await self.db.update_content(payload.message_id, f)
        else:
            await self.db.delete_ids([payload.message_id])

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await self.db.delete_ids([payload.message_id])

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        await self.db.delete_ids(list(payload.message_ids))

    async def on_guild_channel_delete(self, channel):
        await self.db.delete_channel(channel.id)

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent):
        await self.db.delete_channel(payload.thread_id)

    # ---------- search
    async def perform_search(self, guild: discord.Guild, member, q: Query) -> Outcome:
        notes = list(q.errors)
        if q.errors:
            return Outcome([], False, notes)
        allowed = visible_parent_ids(guild, member)
        chan_ids = None
        if q.channels:
            chan_ids, missing = resolve_channels(guild, q.channels)
            if missing:
                notes.append("ไม่พบห้อง: " + ", ".join(m.replace("`", "'") for m in missing))
            if not chan_ids:
                return Outcome([], False, notes)
        rows = await self.db.search(guild.id, q, allowed, chan_ids, config.MAX_RESULTS + 1)
        return Outcome(rows[:config.MAX_RESULTS], len(rows) > config.MAX_RESULTS, notes)

    async def perform_browse(self, guild: discord.Guild, member, mode: str) -> list:
        allowed = visible_parent_ids(guild, member)
        now = datetime.now(config.TZ)
        offset = int(config.TZ.utcoffset(None).total_seconds())
        start_today = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return await self.db.browse(guild.id, allowed, mode, 8, offset, now.strftime("%m-%d"), start_today)

    async def handle_search_message(self, msg: discord.Message):
        text = msg.content.strip()

        # พิมพ์ "/คำสั่ง" เป็นตัวหนังสือธรรมดา (Discord ไม่ได้ส่งเป็น slash command)
        if text.startswith("/"):
            await msg.reply(
                "ข้อความนี้ถูกส่งเป็นตัวหนังสือธรรมดา ไม่ได้เรียก slash command\n"
                "พิมพ์ `/` แล้ว **คลิกเลือกคำสั่งจากรายการที่เด้งขึ้นมา** ก่อนกด Enter\n"
                "ถ้าไม่มีคำสั่งของบอทขึ้นเลย ให้พิมพ์ `!panel` ในห้องนี้แทน (เฉพาะคนที่มีสิทธิ์ Manage Server)",
                mention_author=False, delete_after=60)
            return

        # ทางลัดสร้างแผงปุ่ม โดยไม่ต้องพึ่ง slash command
        if text.lower() in ("!panel", "!แผง"):
            if isinstance(msg.author, discord.Member) and msg.author.guild_permissions.manage_guild:
                panel = await msg.channel.send(embed=ui.panel_embed(), view=ui.PanelView(self))
                try:
                    await panel.pin()
                except discord.HTTPException:
                    pass
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
            else:
                await msg.reply("ต้องมีสิทธิ์ Manage Server", mention_author=False, delete_after=15)
            return
        q = parse_query(msg.content)
        if not q.has_criteria() and not q.errors:
            await msg.reply(embed=ui.help_embed(), mention_author=False, delete_after=90)
            return
        async with msg.channel.typing():
            out = await self.perform_search(msg.guild, msg.author, q)
        view = ui.ResultsView(
            guild=msg.guild, rows=out.rows, title="🔎 ผลการค้นหา", summary=ui.describe_query(q),
            notes=out.notes, truncated=out.truncated, requester_id=msg.author.id, source_message=msg)
        reply = await msg.reply(embed=view.build_embed(), view=view, mention_author=False)
        view.message = reply
        if config.RESULT_TTL_MIN > 0:
            await reply.delete(delay=config.RESULT_TTL_MIN * 60)
            await msg.delete(delay=config.RESULT_TTL_MIN * 60)

    async def send_results_interaction(self, interaction: discord.Interaction, q: Query, *, ephemeral: bool = True):
        out = await self.perform_search(interaction.guild, interaction.user, q)
        view = ui.ResultsView(
            guild=interaction.guild, rows=out.rows, title="🔎 ผลการค้นหา", summary=ui.describe_query(q),
            notes=out.notes, truncated=out.truncated, requester_id=interaction.user.id, ephemeral=ephemeral)
        msg = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=ephemeral, wait=True)
        view.message = msg

    async def send_browse(self, interaction: discord.Interaction, mode: str):
        rows = await self.perform_browse(interaction.guild, interaction.user, mode)
        title = "🎲 สุ่มจากสมองก้อนโต" if mode == "random" else "📅 วันนี้ในอดีต"
        view = ui.ResultsView(
            guild=interaction.guild, rows=rows, title=title, requester_id=interaction.user.id,
            ephemeral=True, preview=True)
        msg = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True, wait=True)
        view.message = msg


bot = SearchBot()


def _is_admin(interaction: discord.Interaction) -> bool:
    u = interaction.user
    return isinstance(u, discord.Member) and u.guild_permissions.manage_guild


# ---------------------------------------------------------------- slash commands
@bot.tree.command(name="search", description="ค้นข้อความทุกห้อง (ตัวกรองเหมือนในห้อง search)")
@app_commands.describe(query="คำค้น + ตัวกรอง เช่น ไอเดีย from:สมชาย last:7d",
                       public="แสดงผลให้ทุกคนเห็น (ปกติเห็นคนเดียว)")
@app_commands.guild_only()
async def slash_search(interaction: discord.Interaction, query: str, public: bool = False):
    q = parse_query(query)
    if not q.has_criteria() and not q.errors:
        await interaction.response.send_message(embed=ui.help_embed(), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=not public, thinking=True)
    await bot.send_results_interaction(interaction, q, ephemeral=not public)


@bot.tree.command(name="search_panel", description="โพสต์แผงปุ่มค้นหาในห้องนี้ แล้วปักหมุด (แอดมิน)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def search_panel(interaction: discord.Interaction):
    if not _is_admin(interaction):
        await interaction.response.send_message("ต้องมีสิทธิ์ Manage Server", ephemeral=True)
        return
    await interaction.response.send_message("กำลังสร้างแผง…", ephemeral=True)
    panel = await interaction.channel.send(embed=ui.panel_embed(), view=ui.PanelView(bot))
    try:
        await panel.pin()
        await interaction.followup.send("สร้างแผงและปักหมุดแล้ว", ephemeral=True)
    except discord.HTTPException:
        await interaction.followup.send("สร้างแผงแล้ว แต่ปักหมุดไม่ได้ (ต้องให้บอทมี Manage Messages) ปักเองได้เลย", ephemeral=True)


@bot.tree.command(name="reindex", description="ซิงก์ข้อความเข้าฐานค้นหา (แอดมิน)")
@app_commands.describe(full="สแกนประวัติทั้งหมดใหม่ตั้งแต่ต้น (ช้า ใช้เมื่อสงสัยว่าข้อมูลไม่ครบ)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def reindex(interaction: discord.Interaction, full: bool = False):
    if not _is_admin(interaction):
        await interaction.response.send_message("ต้องมีสิทธิ์ Manage Server", ephemeral=True)
        return
    if bot.start_sync(full):
        await interaction.response.send_message("เริ่มซิงก์แล้ว ดูความคืบหน้าด้วย /search_stats", ephemeral=True)
    else:
        await interaction.response.send_message("กำลังซิงก์อยู่แล้ว ดูความคืบหน้าด้วย /search_stats", ephemeral=True)


@bot.tree.command(name="search_stats", description="สถานะฐานค้นหาและการซิงก์")
@app_commands.guild_only()
async def search_stats(interaction: discord.Interaction):
    total, chans, oldest, newest = await bot.db.stats(interaction.guild.id)
    st = bot.sync_status
    size_mb = os.path.getsize(config.DB_PATH) / 1048576 if os.path.exists(config.DB_PATH) else 0
    e = discord.Embed(title="📊 สถานะ Super Search", color=discord.Color.blurple())
    e.add_field(name="ข้อความในฐาน", value=f"{total:,}")
    e.add_field(name="ห้อง/ฟอรัมที่มีข้อมูล", value=f"{chans:,}")
    e.add_field(name="ขนาดไฟล์ DB", value=f"{size_mb:.1f} MB")
    if oldest:
        e.add_field(name="เก่าสุด", value=f"<t:{oldest}:D>")
        e.add_field(name="ใหม่สุด", value=f"<t:{newest}:R>")
    if st["running"]:
        e.add_field(name="ซิงก์", value=f"กำลังทำงาน ({st['current']}) เขียนแล้ว {st['indexed']:,} ข้อความ", inline=False)
    elif st["last"]:
        e.add_field(name="ซิงก์ล่าสุด", value=f"<t:{st['last']}:R> (เขียน {st['indexed']:,} ข้อความ)", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("app command error", exc_info=error)
    msg = "เกิดข้อผิดพลาดภายใน ลองใหม่อีกครั้ง"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.TOKEN:
        raise SystemExit("ไม่พบ DISCORD_TOKEN ใน .env หรือ environment variables")
    bot.run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()