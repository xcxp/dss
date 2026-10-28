# utils/search_utils.py
from datetime import datetime

import discord


async def process_search_channels(ctx, include_channels, exclude_channels):
    """Process and return channels to search based on include/exclude filters"""
    search_channels = []

    if include_channels:
        for ch_name in include_channels:
            channel = discord.utils.get(ctx.guild.text_channels, name=ch_name)
            if channel and channel.permissions_for(ctx.guild.me).read_messages:
                search_channels.append(channel)
        if not search_channels:
            await ctx.send("⚠️ None of the specified channels were found or accessible.")
            return []
    else:
        if exclude_channels:
            search_channels = [ch for ch in ctx.guild.text_channels
                               if ch.name not in exclude_channels
                               and ch.permissions_for(ctx.guild.me).read_messages]
        else:
            search_channels = [ch for ch in ctx.guild.text_channels
                               if ch.permissions_for(ctx.guild.me).read_messages]

    return search_channels


async def update_search_status(status_msg, channels_searched, total_channels,
                               messages_searched, messages_found, start_time,
                               last_update_time, search_cancelled):
    """Update status message during search operations"""
    current_time = datetime.now()
    if (current_time - last_update_time).total_seconds() > 5:
        elapsed = (current_time - start_time).total_seconds()
        messages_per_second = messages_searched / elapsed if elapsed > 0 else 0

        status = f"🔍 Searching: {channels_searched}/{total_channels} channels, " \
                 f"{messages_searched:,} messages ({messages_per_second:.1f}/sec), " \
                 f"{messages_found} matches..."

        if search_cancelled:
            status = "⚠️ Search cancelled! Finalizing results..."

        await status_msg.edit(content=status)
        return current_time

    return last_update_time


def update_search_stats(search_stats, ctx, total_searched, found_messages, search_time):
    """Update global search statistics"""
    search_stats["total_searches"] += 1
    search_stats["total_messages_searched"] += total_searched
    search_stats["total_matches_found"] += len(found_messages)
    search_stats["search_time_total"] += search_time

    # Update guild stats
    guild_name = ctx.guild.name
    if guild_name not in search_stats["searches_by_guild"]:
        search_stats["searches_by_guild"][guild_name] = 0
    search_stats["searches_by_guild"][guild_name] += 1

    # Update user stats
    user_name = f"{ctx.author.name}"
    if user_name not in search_stats["searches_by_user"]:
        search_stats["searches_by_user"][user_name] = 0
    search_stats["searches_by_user"][user_name] += 1

    # Update last search data
    search_stats["last_search"] = {
        "user": ctx.author.name,
        "guild": ctx.guild.name,
        "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "messages_searched": total_searched,
        "matches_found": len(found_messages),
        "search_time": search_time
    }

    # Update largest search if applicable
    if total_searched > search_stats["largest_search"]["messages"]:
        search_stats["largest_search"] = {
            "messages": total_searched,
            "user": ctx.author.name,
            "guild": ctx.guild.name,
            "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
