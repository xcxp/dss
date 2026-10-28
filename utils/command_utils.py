# utils/command_utils.py
import os
import csv
import json
from datetime import datetime


def setup_command_execution(command_function):
    """Set up initial command execution state and return it if already running"""
    if not hasattr(command_function, "is_running"):
        command_function.is_running = False

    if command_function.is_running:
        return False

    command_function.is_running = True
    return True


def handle_cancel_request(command_function, args):
    """Handle cancellation request for a command"""
    if len(args) == 1 and args[0].lower() == "cancel":
        if hasattr(command_function, "is_running") and command_function.is_running:
            return True
        return False
    return None


def apply_cooldown(search_cooldowns, ctx, deep_search, custom_query, cooldown_minutes=5):
    """Apply cooldown for intensive searches"""
    if deep_search or custom_query:
        guild_id = ctx.guild.id
        current_time = datetime.now()

        if guild_id in search_cooldowns:
            last_search = search_cooldowns[guild_id]
            elapsed = (current_time - last_search).total_seconds() / 60

            # If cooldown hasn't expired yet
            if elapsed < cooldown_minutes:
                remaining = cooldown_minutes - elapsed
                return False, remaining

        search_cooldowns[guild_id] = current_time

    return True, 0


async def save_scan_results(ctx, found_messages, export_format, user=None, search_channels=None):
    """
    Save scan results to a file in the specified format

    Args:
        ctx: Discord context
        found_messages: List of found messages with metadata
        export_format: Format for export (txt, csv, json)
        user: Optional user filter used in the scan
        search_channels: Optional list of channels that were searched

    Returns:
        str: Path to the saved file
    """
    # Ensure exports directory exists
    os.makedirs("exports/badscans", exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    user_part = f"_{user.name}" if user else ""
    filename_base = f"scan_results{user_part}_{timestamp}"
    filename = f"exports/badscans/{filename_base}.{export_format}"

    if export_format == "txt":
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"Scan Results\n")
            f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Server: {ctx.guild.name}\n")
            if user:
                f.write(f"User: {user.name}\n")
            if search_channels:
                channel_names = [f"#{ch.name}" for ch in search_channels]
                f.write(f"Channels: {', '.join(channel_names)}\n")
            f.write(f"Messages found: {len(found_messages)}\n")
            f.write("=" * 80 + "\n\n")

            for i, match in enumerate(found_messages, 1):
                f.write(f"Message {i}/{len(found_messages)}\n")
                f.write(f"Author: {match['author']} (ID: {match['author_id']})\n")
                f.write(f"Channel: #{match['channel_name']} (ID: {match['channel_id']})\n")
                f.write(f"Date: {match['timestamp']}\n")
                f.write(f"Link: {match['jump_url']}\n")
                matched_words = ", ".join(match['matched_words'])
                f.write(f"Bad words detected: {matched_words}\n")
                f.write("-" * 40 + "\n\n")
                f.write(f"Content: {match['content']}\n\n")
                f.write("-" * 40 + "\n\n")

    elif export_format == "csv":
        with open(filename, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)

            # Write header
            writer.writerow([
                "Number", "Author", "Author ID", "Channel", "Channel ID",
                "Timestamp", "Content", "Matched Words", "Message Link"
            ])

            # Write data
            for i, match in enumerate(found_messages, 1):
                writer.writerow([
                    i,
                    match['author'],
                    match['author_id'],
                    f"#{match['channel_name']}",
                    match['channel_id'],
                    match['timestamp'],
                    match['content'],
                    ", ".join(match['matched_words']),
                    match['jump_url']
                ])

    elif export_format == "json":
        export_data = {
            "metadata": {
                "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "guild": ctx.guild.name,
                "guild_id": ctx.guild.id,
                "total_matches": len(found_messages),
                "user_filter": user.name if user else None,
                "user_id": user.id if user else None,
                "channels": [{"name": ch.name, "id": ch.id} for ch in search_channels] if search_channels else None
            },
            "results": found_messages
        }

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2)

    return filename