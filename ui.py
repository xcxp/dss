from __future__ import annotations

import math
import re

import discord

import settings as config
from queryparse import HAS_MAP, parse_query


def jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _safe(s: str) -> str:
    s = (s or "").replace("[", "(").replace("]", ")")
    return discord.utils.escape_markdown(discord.utils.escape_mentions(s))


def _tick(s: str) -> str:
    return (s or "").replace("`", "'")


HELP_TEXT = (
    "**พิมพ์คำที่ต้องการหาในห้องนี้ได้เลย** ไม่ต้องมี `/`\n"
    "• `ไอเดียแอป` — หาข้อความที่มีคำนี้ (ไทยไม่ต้องเว้นวรรค ค้นกลางคำได้)\n"
    "• `ไอเดีย แอป` — ต้องมีทั้ง 2 คำ (อยู่คนละตำแหน่งก็ได้)\n"
    "• `\"ประชุมทีม\"` — วลีที่ต้องเรียงติดกันตามนี้\n"
    "• `ไอเดีย -ยกเลิก` — ไม่เอาข้อความที่มีคำว่า ยกเลิก\n\n"
    "**ตัวกรอง (พิมพ์ต่อท้ายได้)**\n"
    "• `from:สมชาย` — จากใคร (ชื่อ / @mention / ID)\n"
    "• `in:ไอเดีย` — เฉพาะห้อง (ชื่อห้อง / #mention)\n"
    "• `last:7d` — ย้อนหลัง (h ชม. · d วัน · w สัปดาห์ · m เดือน · y ปี) หรือ `last:วันนี้` `last:เมื่อวาน`\n"
    "• `after:2026-10-01` `before:2026-10-20` `on:2026-10-15` — ระบุวันที่ "
    "(ใส่เวลา: `after:\"2026-10-15 14:30\"`)\n"
    "• `has:link` `has:image` `has:file` — มีลิงก์ / รูป / ไฟล์\n"
    "• `sort:old` — เรียงเก่าสุดก่อน\n\n"
    "หรือกดปุ่ม **🔎 ค้นหาละเอียด** ในข้อความที่ปักหมุด จะมีฟอร์มให้กรอก"
)


def help_embed() -> discord.Embed:
    return discord.Embed(title="🔎 วิธีใช้ Super Search", description=HELP_TEXT, color=discord.Color.blurple())


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🧠 Super Search — ค้นข้อความทุกห้อง",
        description=(
            "พิมพ์คำที่ต้องการหาในห้องนี้ได้เลย บอทจะตอบเป็นลิงก์ไปยังข้อความนั้น\n"
            "ต้องการกรองละเอียด (คน / ห้อง / ช่วงเวลา) กดปุ่ม **🔎 ค้นหาละเอียด**\n"
            "เบื่อ ๆ อยากรื้อของเก่า กด **🎲** หรือ **📅**"
        ),
        color=discord.Color.blurple(),
    )


def describe_query(q) -> str:
    parts = []
    if q.terms:
        parts.append("คำ: " + " ".join(f"`{_tick(t)}`" for t in q.terms))
    if q.exclude:
        parts.append("ไม่รวม: " + " ".join(f"`{_tick(t)}`" for t in q.exclude))
    if q.authors:
        parts.append("จาก: " + ", ".join(_safe(a) for a in q.authors))
    if q.channels:
        parts.append("ห้อง: " + ", ".join(_safe(c) for c in q.channels))
    if q.after is not None or q.before is not None:
        a = f"<t:{q.after}:f>" if q.after is not None else "…"
        b = f"<t:{q.before - 1}:f>" if q.before is not None else "ตอนนี้"
        parts.append(f"ช่วง: {a} → {b}")
    if q.has:
        parts.append("มี: " + ", ".join(sorted(q.has)))
    if not q.newest_first:
        parts.append("เรียง: เก่า→ใหม่")
    return "  ·  ".join(parts)


class ResultsView(discord.ui.View):
    def __init__(self, *, guild, rows, title, requester_id, summary="", notes=None,
                 truncated=False, ephemeral=False, preview=False, source_message=None):
        super().__init__(timeout=600)
        self.guild = guild
        self.rows = rows
        self.title = title
        self.summary = summary
        self.notes = notes or []
        self.truncated = truncated
        self.requester_id = requester_id
        self.preview = preview
        self.source_message = source_message
        self.page = 0
        self.pages = max(1, math.ceil(len(rows) / config.PAGE_SIZE))
        self.message = None
        if ephemeral or source_message is None and False:
            self.remove_item(self.trash_btn)
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1
        self.page_btn.label = f"{self.page + 1}/{self.pages}"
        self.preview_btn.label = "ซ่อนตัวอย่าง" if self.preview else "ดูตัวอย่าง"
        self.preview_btn.disabled = not self.rows

    def build_embed(self) -> discord.Embed:
        n = len(self.rows)
        if n == 0:
            embed = discord.Embed(title=f"{self.title} — ไม่พบ", color=discord.Color.orange())
        else:
            plus = "+" if self.truncated else ""
            embed = discord.Embed(title=f"{self.title} — {n}{plus} ข้อความ", color=discord.Color.blurple())

        head = []
        if self.summary:
            head.append(self.summary)
        head += [f"⚠️ {note}" for note in self.notes]
        if head:
            head.append("")

        start = self.page * config.PAGE_SIZE
        lines = []
        for i, r in enumerate(self.rows[start:start + config.PAGE_SIZE], start=start + 1):
            ch = self.guild.get_channel_or_thread(r["channel_id"])
            cname = ch.name if ch else "ห้องที่ไม่รู้จัก"
            label = _safe(f"#{_clip(cname, 24)} · {_clip(r['author_label'], 20)}")
            url = jump_url(self.guild.id, r["channel_id"], r["id"])
            line = f"**{i}.** [{label}]({url}) · <t:{r['created_at']}:f>"
            if self.preview and r["snippet"]:
                line += "\n> " + _safe(_clip(r["snippet"], 90))
            lines.append(line)

        embed.description = "\n".join(head + lines)[:4000]
        foot = f"หน้า {self.page + 1}/{self.pages} • แสดงเฉพาะห้องที่คุณมีสิทธิ์อ่าน"
        if self.truncated:
            foot += f" • มีเกิน {config.MAX_RESULTS} รายการ ใส่ตัวกรองเพิ่มเพื่อเจาะจง"
        embed.set_footer(text=foot)
        return embed

    async def _refresh(self, interaction: discord.Interaction):
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "ปุ่มนี้ใช้ได้เฉพาะคนที่ค้นหา ลองพิมพ์ค้นเองในห้อง search ได้เลย", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def page_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        await self._refresh(interaction)

    @discord.ui.button(label="ดูตัวอย่าง", emoji="👁", style=discord.ButtonStyle.primary, row=0)
    async def preview_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.preview = not self.preview
        await self._refresh(interaction)

    @discord.ui.button(emoji="🗑", style=discord.ButtonStyle.danger, row=0)
    async def trash_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        if self.source_message is not None:
            try:
                await self.source_message.delete()   # ลบได้เมื่อบอทมีสิทธิ์ Manage Messages
            except discord.HTTPException:
                pass
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def _split_csv(s: str):
    return [p.strip().replace('"', "") for p in re.split(r"[,，]", s or "") if p.strip()]


def _extra_to_ops(extra: str) -> str:
    out = []
    for w in (extra or "").split():
        lw = w.lower()
        if ":" in w:
            out.append(w)
        elif lw in ("old", "oldest", "เก่า"):
            out.append("sort:old")
        elif lw in HAS_MAP:
            out.append(f"has:{lw}")
    return " ".join(out)


class AdvancedSearchModal(discord.ui.Modal, title="🔎 ค้นหาละเอียด"):
    keywords = discord.ui.TextInput(
        label="คำค้นหา", required=False, max_length=200,
        placeholder='เช่น ไอเดียแอป  หรือ  "วลีเรียงติดกัน"  -คำที่ไม่เอา')
    author = discord.ui.TextInput(
        label="จากใคร (ชื่อ / @mention / ID)", required=False, max_length=100,
        placeholder="เว้นว่าง = ทุกคน  (หลายคนคั่นด้วย , )")
    channel = discord.ui.TextInput(
        label="ในห้อง (ชื่อห้อง)", required=False, max_length=100,
        placeholder="เว้นว่าง = ทุกห้อง  (หลายห้องคั่นด้วย , )")
    when = discord.ui.TextInput(
        label="ช่วงเวลา", required=False, max_length=60,
        placeholder="7d · 2w · 3m · วันนี้ · 2026-10-01..2026-10-28")
    extra = discord.ui.TextInput(
        label="อื่น ๆ (link / image / file / old)", required=False, max_length=60,
        placeholder="เช่น link  หรือ  image old")

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        text = self.keywords.value or ""
        for a in _split_csv(self.author.value):
            text += f' from:"{a}"'
        for c in _split_csv(self.channel.value):
            text += f' in:"{c}"'
        when = (self.when.value or "").strip().replace('"', "")
        if when:
            text += f' last:"{when}"'
        text += " " + _extra_to_ops(self.extra.value)

        q = parse_query(text)
        if not q.has_criteria() and not q.errors:
            await interaction.response.send_message("กรอกอย่างน้อย 1 ช่องนะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.send_results_interaction(interaction, q, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        import logging
        logging.getLogger("searchbot").exception("modal error", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("เกิดข้อผิดพลาดภายใน ลองใหม่อีกครั้ง", ephemeral=True)
            else:
                await interaction.response.send_message("เกิดข้อผิดพลาดภายใน ลองใหม่อีกครั้ง", ephemeral=True)
        except discord.HTTPException:
            pass


class PanelView(discord.ui.View):
    """ปุ่มถาวรบนข้อความที่ปักหมุด (timeout=None + custom_id ทุกปุ่ม)"""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="ค้นหาละเอียด", emoji="🔎", style=discord.ButtonStyle.primary, custom_id="ssearch:advanced")
    async def advanced_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdvancedSearchModal(self.bot))

    @discord.ui.button(label="สุ่มข้อความเก่า", emoji="🎲", style=discord.ButtonStyle.success, custom_id="ssearch:random")
    async def random_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.send_browse(interaction, "random")

    @discord.ui.button(label="วันนี้ในอดีต", emoji="📅", style=discord.ButtonStyle.success, custom_id="ssearch:today")
    async def today_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.send_browse(interaction, "today")

    @discord.ui.button(label="วิธีใช้", emoji="❓", style=discord.ButtonStyle.secondary, custom_id="ssearch:help")
    async def help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=help_embed(), ephemeral=True)