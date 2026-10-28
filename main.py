import json
import logging
import logging.handlers
import os
import platform
import queue
import re
import shutil
import sys
from datetime import datetime
from datetime import timedelta

import discord
import psutil
from cachetools import TTLCache
from discord.ext import commands, tasks
from dotenv import load_dotenv

from utils.cache_utils import get_cache_stats, load_bad_words
from utils.command_utils import setup_command_execution, handle_cancel_request, apply_cooldown, save_scan_results
from utils.search_utils import process_search_channels, update_search_status, update_search_stats


# --- Environment check ---
def check_environment():
    """Check the runtime environment and return any warnings."""
    warnings = []

    # Check Python version
    python_version = tuple(map(int, platform.python_version_tuple()))
    if python_version < (3, 8):
        raise Exception("DeepSearch requires Python 3.8 or higher to work because it uses the Discord.py library. Please update your Python version.")

    # Check if running in IDE first (takes precedence over venv detection)
    in_ide = any(ide in os.environ.get('PYTHONPATH', '').lower() for ide in ['pycharm', 'vscode', 'eclipse', 'intellij', 'idle']) or \
             'PYCHARM_HOSTED' in os.environ or 'VSCODE_CLI' in os.environ or 'VSCODE_CWD' in os.environ or \
             'SPYDER' in os.environ or 'JUPYTER' in os.environ or 'IDLE' in os.environ

    # Define environments to check with custom messages
    env_checks = {
        "Docker": (os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"),
                   "Running in Docker container"),
        "Pterodactyl": (os.environ.get("P_SERVER_LOCATION") is not None or
                        os.path.exists("/etc/pterodactyl") or
                        "pterodactyl" in os.environ.get("HOSTNAME", "").lower(),
                        "Running in Pterodactyl environment"),
        "Virtual Environment": (not in_ide and  # Only report venv if not in IDE
                                (hasattr(sys, 'real_prefix') or
                                 (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)),
                                "Running in a virtual environment"),
        "IDE": (in_ide, "Running in an IDE environment"),
        "Raspberry Pi": (platform.system() == 'Linux' and 'arm' in platform.machine().lower(),
                         "Running on Raspberry Pi. Performance may be limited"),
        "Low Memory": (psutil.virtual_memory().total < 2 * 1024 * 1024 * 1024,
                       "Less than 2GB of RAM detected. Performance may be affected"),
        "Headless System": (os.environ.get('DISPLAY', '') == '', None)  # Don't warn about headless
    }

    # Add warnings for detected environments that need caution
    for env_type, (detected, message) in env_checks.items():
        if detected and message:  # Only add warning if there's a message to display
            warnings.append(message)

    # Check internet connection (only on posix systems)
    if os.name == 'posix':
        try:
            import subprocess
            subprocess.run(
                ["ping", "-c", "1", "8.8.8.8"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2  # Add timeout to prevent hanging
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            warnings.append("No internet connection detected. DeepSearch cannot connect to Discord.")
            exit()  # Exit if no internet connection

    return warnings

# Display environment warnings
print("🔍 Environment Check:")
warnings = check_environment()
if warnings:
    print("⚠️ Notices:")
    for warning in warnings:
        print(f"  • {warning}")
else:
    print("✅ All environment checks passed")


# --- Load env and config ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
CONFIG_FILE = "config.json"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

# --- Setup logs ---
LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)
msg_log_path = os.path.join(LOGS_DIR, "message_logs.log")
user_log_path = os.path.join(LOGS_DIR, "user_logs.log")


def setup_logging():
    # Create date-based directory structure
    current_datetime = datetime.now()
    date_hour_dir = os.path.join(LOGS_DIR, current_datetime.strftime('%Y-%m-%d_%H'))
    os.makedirs(date_hour_dir, exist_ok=True)

    # Define log paths with date-based directory
    msg_log_path = os.path.join(date_hour_dir, "message_logs.log")
    user_log_path = os.path.join(date_hour_dir, "user_logs.log")

    # Set up logging with queue handlers for thread safety
    log_queue = queue.Queue(-1)  # No limit on queue size
    queue_handler = logging.handlers.QueueHandler(log_queue)

    # Configure root logger to use the queue
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for handler in root_logger.handlers[:]:  # Remove any existing handlers
        root_logger.removeHandler(handler)
    root_logger.addHandler(queue_handler)

    # Configure file handlers for the listener
    msg_handler = logging.FileHandler(msg_log_path, mode='a', encoding='utf-8')
    user_handler = logging.FileHandler(user_log_path, mode='a', encoding='utf-8')

    formatter = logging.Formatter('%(asctime)s - %(message)s')
    msg_handler.setFormatter(formatter)
    user_handler.setFormatter(formatter)

    # Create and configure loggers
    msg_logger = logging.getLogger('message_log')
    user_logger = logging.getLogger('user_log')

    msg_logger.setLevel(logging.INFO)
    user_logger.setLevel(logging.INFO)

    msg_logger.propagate = False
    user_logger.propagate = False

    msg_logger.addHandler(msg_handler)
    user_logger.addHandler(user_handler)

    # Set up the queue listener
    listener = logging.handlers.QueueListener(log_queue, msg_handler, user_handler)
    listener.start()

    return msg_logger, user_logger, msg_log_path, user_log_path, listener


# --- Init bot ---
intents = discord.Intents.all()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

msg_logger, user_logger, msg_log_path, user_log_path, log_listener = setup_logging()

BAD_WORDS = load_bad_words()

# --- Caches ---
member_cache = TTLCache(maxsize=500, ttl=3600)  # Cache for 1 hour, store up to 500 guilds
message_cache = TTLCache(maxsize=1000, ttl=300)  # Cache for 5 minutes, store up to 1000 channel histories
user_cache = TTLCache(maxsize=2000, ttl=3600)  # Cache for 1 hour, store up to 2000 users
keyword_match_cache = TTLCache(maxsize=10000, ttl=3600)  # Cache for 1 hour


# --- Utils ---
def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")


def log_to_file(path, content):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        print(f"Error writing to log {path}: {e}")


def is_admin(ctx):
    return ctx.author.guild_permissions.administrator


def format_time_interval(minutes):
    """Format time interval in appropriate units"""
    if minutes < 1:
        # Less than a minute, show seconds
        seconds = int(minutes * 60)
        return f"{seconds} seconds"
    elif minutes < 60:
        # Less than an hour, show minutes
        return f"{minutes} minutes"
    elif minutes < 1440:  # 24 hours
        # Less than a day, show hours and minutes
        hours = int(minutes // 60)
        mins = int(minutes % 60)
        if mins == 0:
            return f"{hours} hours"
        else:
            return f"{hours} hours, {mins} minutes"
    elif minutes < 10080:  # 7 days
        # Less than a week, show days and hours
        days = int(minutes // 1440)
        hours = int((minutes % 1440) // 60)
        if hours == 0:
            return f"{days} days"
        else:
            return f"{days} days, {hours} hours"
    else:
        # More than a week, show weeks and days
        weeks = int(minutes // 10080)
        days = int((minutes % 10080) // 1440)
        if days == 0:
            return f"{weeks} weeks"
        else:
            return f"{weeks} weeks, {days} days"


def update_scheduled_tasks():
    """Update scheduled tasks based on current config"""
    if CONFIG.get("auto_scan_enabled", False):
        interval = CONFIG.get("auto_scan_interval_minutes", 60)

        # Convert to seconds for more precision
        seconds = int(interval * 60)

        # Need to restart the task to change interval
        if auto_scan.is_running():
            auto_scan.cancel()

        # Update interval and restart
        auto_scan.change_interval(seconds=seconds)
        auto_scan.start()

        print(f"🔄 Auto-scan enabled (every {format_time_interval(interval)})")
    else:
        if auto_scan.is_running():
            auto_scan.cancel()
            print("🛑 Auto-scan disabled")


def get_cached_members(guild_id):
    """Get or create cached member list for a guild"""
    if guild_id not in member_cache:
        guild = bot.get_guild(guild_id)
        if guild:
            member_cache[guild_id] = list(guild.members)
    return member_cache.get(guild_id, [])


async def get_cached_messages(channel_id, limit=100, force_refresh=False):
    """Get or create cached message history for a channel"""
    cache_key = f"{channel_id}_{limit}"
    if force_refresh or cache_key not in message_cache:
        channel = bot.get_channel(channel_id)
        if channel:
            messages = []
            async for msg in channel.history(limit=limit):
                messages.append(msg)
            message_cache[cache_key] = messages
    return message_cache.get(cache_key, [])


async def get_cached_user(user_id):
    """Get or create cached user information"""
    if user_id not in user_cache:
        try:
            user = await bot.fetch_user(int(user_id))
            user_cache[user_id] = user
        except Exception:
            return None
    return user_cache.get(user_id)


# Create a set of lowercase keywords for faster matching
KEYWORD_SET = {k.lower() for k in CONFIG["search_keywords"]}


def keyword_match(text):
    """Check if text contains any keywords with caching"""
    # Use a short hash of the text as a cache key
    cache_key = hash(text) % 10000000

    if cache_key in keyword_match_cache:
        return keyword_match_cache[cache_key]

    # Convert text to lowercase once
    text_lower = text.lower()

    # Check for any keyword in the set
    result = any(k in text_lower for k in KEYWORD_SET)

    # Cache the result
    keyword_match_cache[cache_key] = result
    return result


def parse_query_limit(limit_str):
    """Parse query limit with support for k/m suffixes (e.g., 5k = 5000)"""
    limit_str = limit_str.lower()
    try:
        if limit_str.endswith('k'):
            return int(float(limit_str[:-1]) * 1000)
        elif limit_str.endswith('m'):
            return int(float(limit_str[:-1]) * 1000000)
        else:
            return int(limit_str)
    except ValueError:
        return None


# Unified argument parsing function
def parse_command_args(args):
    """Parse command arguments and separate flags from positional arguments"""
    processed_args = []
    flags = {}
    i = 0

    while i < len(args):
        arg = args[i].lower()

        # Handle flags that take a value
        if i + 1 < len(args) and arg in ("--q", "--query"):
            limit = parse_query_limit(args[i + 1])
            if limit is not None:
                flags["query_limit"] = limit
                i += 1  # Skip the value
            else:
                flags["error"] = "Invalid query limit. Must be a number (e.g., 100, 5k, 1m)."
        elif i + 1 < len(args) and arg in ("--in", "--channel"):
            flags["include_channels"] = [c.strip() for c in args[i + 1].split(",")]
            i += 1  # Skip the value
        elif i + 1 < len(args) and arg in ("--exclude", "--not"):
            flags["exclude_channels"] = [c.strip() for c in args[i + 1].split(",")]
            i += 1  # Skip the value
        # Handle boolean flags
        elif arg in ("--all", "-a"):
            flags["deep_search"] = True
        if "--debug" in args or "-d" in args:
            flags["debug"] = True
        elif arg in ("--users", "-u"):
            flags["scan_users"] = True
        elif arg in ("--messages", "-m"):
            flags["scan_messages"] = True
        else:
            processed_args.append(args[i])

        i += 1

    return processed_args, flags


def debug_print(message, debug_enabled=False):
    """Print debug messages if debug mode is enabled"""
    if debug_enabled:
        timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        print(f"[DEBUG {timestamp}] {message}")


# --- Events ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    print("🔍 Keywords:", ', '.join(CONFIG["search_keywords"]))
    print(f"📁 Logs at {os.path.dirname(msg_log_path)}")

    update_scheduled_tasks()

    # Track message matches for initial scan
    initial_message_matches = 0
    initial_member_matches = 0
    total_members_scanned = 0
    total_messages_scanned = 0
    guild_count = len(bot.guilds)

    print(f"\n🔎 Starting initial scan across {guild_count} guilds...")

    for guild in bot.guilds:
        # Chunk only if necessary
        if not guild.chunked:
            await guild.chunk(cache=True)

        # Scan members
        member_count = len(guild.members)
        total_members_scanned += member_count

        member_matches = 0
        for member in get_cached_members(guild.id):
            name_fields = f"{member.name} {member.display_name}"
            if keyword_match(name_fields):
                initial_member_matches += 1
                member_matches += 1
                entry = f"[AUTO] {member} ({member.id}) in {guild.name}"
                user_logger.info(entry)
                if CONFIG["print_user_matches"]:
                    print(entry)

        # Initial scan of messages
        message_scan_count = 0
        channels = [c for c in guild.text_channels if c.permissions_for(guild.me).read_messages]

        if channels:
            # Calculate messages per channel to reach approximately 5000 total
            messages_per_channel = max(1, 5000 // len(channels))

            for channel in channels:
                try:
                    async for msg in channel.history(limit=messages_per_channel):
                        message_scan_count += 1
                        if keyword_match(msg.content):
                            initial_message_matches += 1
                            entry = f"[INIT] {msg.author} in #{channel.name} ({guild.name}) > {msg.content}"
                            msg_logger.info(entry)
                            if CONFIG["print_message_matches"]:
                                print(entry)
                except (discord.Forbidden, Exception):
                    continue

        total_messages_scanned += message_scan_count
        print(f"  • {guild.name}: {member_matches}/{member_count} members, {message_scan_count} messages scanned")

    print(f"\n✅ Initial scan complete! Found {initial_member_matches}/{total_members_scanned} matching members and {initial_message_matches}/{total_messages_scanned} matching messages.")


@bot.event
async def on_message(msg):
    if msg.author.bot or not msg.guild:
        return
    if keyword_match(msg.content):
        entry = f"[AUTO] {msg.author} in #{msg.channel} ({msg.guild.name}) > {msg.content}"
        msg_logger.info(entry)
        if CONFIG["print_message_matches"]:
            print(entry)
    await bot.process_commands(msg)


# --- Commands Section---
@bot.command(name="setkeywords")
async def set_keywords(ctx, *, words):
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")
    new_words = [w.strip() for w in words.split(",") if w.strip()]
    CONFIG["search_keywords"] = new_words
    save_config()
    await ctx.send(f"✅ Keywords updated: {', '.join(new_words)}")


@bot.command(name="toggleprints")
async def toggle_prints(ctx, category: str):
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")
    if category not in ["user", "message"]:
        return await ctx.send("⚠️ Use `user` or `message`.")
    key = f"print_{category}_matches"
    CONFIG[key] = not CONFIG[key]
    save_config()
    await ctx.send(f"✅ {category.capitalize()} print set to {CONFIG[key]}")


@bot.command(name="showkeywords")
async def show_keywords(ctx):
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")
    await ctx.send("🔍 Current keywords:\n" + ", ".join(CONFIG["search_keywords"]))


@bot.command(name="scan")
async def scan_members(ctx, *args):
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    global search_cancelled

    # Handle cancel request
    if len(args) == 1 and args[0].lower() == "cancel":
        if hasattr(scan_members, "is_running") and scan_members.is_running:
            search_cancelled = True
            return await ctx.send("⚠️ Scan cancelled.")
        else:
            return await ctx.send("⚠️ No scan is currently running.")

    if not hasattr(scan_members, "is_running"):
        scan_members.is_running = False

    if scan_members.is_running:
        return await ctx.send("⚠️ A scan is already running. Please wait for it to complete or use `!scan cancel` to stop it.")

    search_cancelled = False

    # Use the unified argument parsing function
    processed_args, flags = parse_command_args(args)

    # Extract flags with default values
    deep_search = flags.get("deep_search", False)
    query_limit = flags.get("query_limit", 500)  # Default limit
    custom_query = "query_limit" in flags
    include_channels = flags.get("include_channels", [])
    exclude_channels = flags.get("exclude_channels", [])
    scan_users = flags.get("scan_users", False)
    scan_messages = flags.get("scan_messages", False)

    # If no specific scan type is selected, inform the user
    if not scan_users and not scan_messages:
        if "deep_search" in flags or "query_limit" in flags:
            # User specified search options but not what to scan
            scan_messages = True  # Default to messages if options were provided
        else:
            return await ctx.send("⚠️ Usage: `!scan [--users/-u] [--messages/-m] [--all/-a] [--q limit]`")

    # If deep_search is enabled, consider both users and messages
    if deep_search and not (scan_users or scan_messages):
        scan_users = True
        scan_messages = True

    # Create a description of what's being scanned
    scan_targets = []
    if scan_users:
        scan_targets.append("members")
    if scan_messages:
        scan_targets.append("messages")
    scan_description = " and ".join(scan_targets)

    # Fix: Capitalize first letter when not deep searching
    scanning_text = "Scanning" if not deep_search else "Deep scanning"
    status_msg = await ctx.send(f"🔍 {scanning_text} {scan_description}...")

    scan_members.is_running = True

    try:
        user_count = 0
        message_count = 0
        start_time = datetime.now()
        last_update_time = start_time

        # Scan members if requested
        if scan_users:
            # Ensure guild is chunked for complete member list
            if not ctx.guild.chunked:
                await ctx.guild.chunk(cache=True)

            total_members = len(ctx.guild.members)
            members_scanned = 0

            for member in ctx.guild.members:
                members_scanned += 1

                # Update status periodically
                current_time = datetime.now()
                if (current_time - last_update_time).total_seconds() > 5:
                    progress = members_scanned / total_members * 100
                    time_elapsed = (current_time - start_time).total_seconds()
                    await status_msg.edit(content=f"🔍 {scanning_text} members... ({members_scanned}/{total_members}, {progress:.1f}%, {time_elapsed:.1f}s)")
                    last_update_time = current_time

                # Check for cancellation
                if search_cancelled:
                    await status_msg.edit(content=f"⚠️ Scan cancelled after checking {members_scanned} members.")
                    return

                name_fields = f"{member.name} {member.display_name}"
                if keyword_match(name_fields):
                    user_count += 1
                    entry = f"[SCAN] {member.name} ({member.id}) in {ctx.guild.name}"
                    user_logger.info(entry)
                    if CONFIG["print_user_matches"]:
                        print(entry)

        # Scan messages if requested
        if scan_messages:
            # Prepare search channels
            search_channels = []
            if include_channels:
                for ch_name in include_channels:
                    ch_name = ch_name.strip('#')
                    channel = discord.utils.get(ctx.guild.text_channels, name=ch_name)
                    if channel:
                        search_channels.append(channel)
            else:
                if exclude_channels:
                    exclude_ch_names = [ch.strip('#') for ch in exclude_channels]
                    search_channels = [ch for ch in ctx.guild.text_channels
                                       if ch.name not in exclude_ch_names]
                else:
                    search_channels = ctx.guild.text_channels

            total_channels = len(search_channels)
            channels_scanned = 0
            total_messages_scanned = 0

            for channel in search_channels:
                channels_scanned += 1

                # Check for cancellation
                if search_cancelled:
                    await status_msg.edit(content=f"⚠️ Scan cancelled after scanning {channels_scanned}/{total_channels} channels.")
                    return

                # Update status periodically
                current_time = datetime.now()
                if (current_time - last_update_time).total_seconds() > 5:
                    progress = channels_scanned / total_channels * 100
                    time_elapsed = (current_time - start_time).total_seconds()
                    await status_msg.edit(content=f"🔍 {scanning_text} messages... ({channels_scanned}/{total_channels} channels, {total_messages_scanned} msgs, {progress:.1f}%, {time_elapsed:.1f}s)")
                    last_update_time = current_time

                try:
                    # Use different limits based on deep search setting
                    limit = query_limit if deep_search or custom_query else 100
                    async for msg in channel.history(limit=limit):
                        total_messages_scanned += 1

                        if keyword_match(msg.content):
                            message_count += 1
                            entry = f"[SCAN] {msg.author} in #{channel.name} ({ctx.guild.name}) > {msg.content}"
                            msg_logger.info(entry)
                            if CONFIG["print_message_matches"]:
                                print(entry)
                except discord.Forbidden:
                    continue

        # Calculate scan time
        scan_time = (datetime.now() - start_time).total_seconds()

        # Format the result message
        result_parts = []
        if scan_users:
            result_parts.append(f"{user_count} matching members")
        if scan_messages:
            result_parts.append(f"{message_count} matching messages (from {total_messages_scanned} messages)")

        result_text = " and ".join(result_parts)
        await status_msg.edit(content=f"✅ Scan complete! Found {result_text} in {scan_time:.1f}s.")

    except Exception as e:
        await ctx.send(f"⚠️ Error during scan: {e}")
    finally:
        scan_members.is_running = False
        search_cancelled = False

# Define search cooldowns dictionary
search_cooldowns = {}

# Global search cancellation flag
search_cancelled = False


@bot.command(name="search")
async def search_messages(ctx, *args):
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    global search_cancelled, search_cooldowns

    # Handle cancel request
    cancel_result = handle_cancel_request(search_messages, args)
    if cancel_result is not None:
        if cancel_result:
            search_cancelled = True
            return await ctx.send("⚠️ Search cancelled.")
        else:
            return await ctx.send("⚠️ No search is currently running.")

    # Setup command execution
    if not setup_command_execution(search_messages):
        return await ctx.send("⚠️ A search is already running. Please wait for it to complete or use `!search cancel` to stop it.")

    search_cancelled = False

    # Use the unified argument parsing function
    processed_args, flags = parse_command_args(args)

    # Extract flags with default values
    deep_search = flags.get("deep_search", False)
    query_limit = flags.get("query_limit", 500)  # Default limit
    custom_query = "query_limit" in flags
    include_channels = flags.get("include_channels", [])
    exclude_channels = flags.get("exclude_channels", [])

    # Check if there was an error in parsing arguments
    if "error" in flags:
        search_messages.is_running = False
        return await ctx.send(f"⚠️ {flags['error']}")

    # Check for required user and keyword arguments
    if len(processed_args) < 2:
        search_messages.is_running = False
        return await ctx.send("⚠️ Usage: `!search @user keyword [--a/--all] [--q limit] [--in #channel1,#channel2] [--exclude #channel3]`")

    # Extract user from the first processed argument
    try:
        user_arg = processed_args[0]
        if user_arg.startswith("<@") and user_arg.endswith(">"):
            user_id = user_arg[2:-1].strip()
            if user_id.startswith('!'):
                user_id = user_id[1:]
        else:
            user_id = user_arg

        user_id = int(user_id)
        user = await get_cached_user(user_id)
        if not user:
            search_messages.is_running = False
            return await ctx.send("⚠️ User not found. Please make sure you've provided a valid user ID or @mention.")

        # Extract keyword from remaining processed arguments
        keyword = " ".join(processed_args[1:])
    except Exception:
        search_messages.is_running = False
        return await ctx.send("⚠️ Invalid user format. Use @mention or user ID.")

    # Apply cooldown check for deep searches
    cooldown_ok, remaining = apply_cooldown(search_cooldowns, ctx, deep_search, custom_query)
    if not cooldown_ok:
        search_messages.is_running = False
        return await ctx.send(f"⚠️ Please wait {remaining:.1f} minutes before performing another deep search in this server.")

    # Prepare search channels
    search_channels = await process_search_channels(ctx, include_channels, exclude_channels)
    if not search_channels:
        search_messages.is_running = False
        return

    # Status message based on search type
    search_msg_prefix = ""
    if include_channels:
        channel_names = ", ".join([f"#{ch.name}" for ch in search_channels])
        search_msg_prefix = f" in channels: {channel_names}"
    elif exclude_channels:
        channel_names = ", ".join(exclude_channels)
        search_msg_prefix = f" (excluding channels: {channel_names})"

    # Fix: Capitalize first letter when not deep searching
    searching_text = "Searching" if not deep_search else "Deep searching"
    status_msg = await ctx.send(
        f"🔍 {searching_text} for messages from {user.name} containing '{keyword}'{search_msg_prefix}..."
    )

    try:
        total_channels = len(search_channels)
        found_messages = []
        total_searched = 0
        start_time = datetime.now()
        last_update_time = start_time
        channels_searched = 0

        for channel in search_channels:
            channels_searched += 1

            # Check for cancellation
            if search_cancelled:
                await status_msg.edit(content=f"⚠️ Search cancelled after checking {channels_searched}/{total_channels} channels.")
                return

            try:
                # Use different limits based on deep search setting
                limit = query_limit if deep_search or custom_query else 100

                messages_checked = 0
                async for msg in channel.history(limit=limit):
                    messages_checked += 1
                    total_searched += 1

                    # Update status message periodically
                    last_update_time = await update_search_status(
                        status_msg,
                        channels_searched,
                        total_channels,
                        total_searched,
                        len(found_messages),
                        start_time,
                        last_update_time,
                        search_cancelled
                    )

                    # Check for cancellation
                    if search_cancelled:
                        break

                    # Check if message is from target user and contains keyword
                    if msg.author.id == user.id and keyword.lower() in msg.content.lower():
                        found_messages.append(msg)

            except discord.Forbidden:
                continue
            except Exception as e:
                await ctx.send(f"⚠️ Error searching channel {channel.name}: {e}")
                continue

        # Calculate search time
        search_time = (datetime.now() - start_time).total_seconds()

        if not found_messages:
            await status_msg.edit(content=f"✅ Search complete! No messages found from {user.name} containing '{keyword}' (searched {total_searched:,} messages in {search_time:.1f}s)")
        else:
            # Format results
            result_text = f"✅ Found {len(found_messages)} messages from {user.name} containing '{keyword}' (searched {total_searched:,} messages in {search_time:.1f}s)"

            # List first few results
            result_text += "\nLatest messages:"
            count = 0
            for msg in sorted(found_messages, key=lambda m: m.created_at, reverse=True):
                if count < 5:  # Show at most 5 messages
                    channel_name = msg.channel.name
                    date = msg.created_at.strftime('%Y-%m-%d %H:%M:%S')
                    result_text += f"\n- {date} #{channel_name}: {msg.content[:100]}{'...' if len(msg.content) > 100 else ''}"
                    count += 1
                else:
                    break

            result_text += f"\n\nUse `!export {user.id} {keyword}` to export all messages."
            await status_msg.edit(content=result_text[:2000])  # Discord message limit

        # Update search statistics
        update_search_stats(search_stats, ctx, total_searched, found_messages, search_time)
        save_search_stats()

    finally:
        search_messages.is_running = False
        search_cancelled = False


# Handle cooldown error
@search_messages.error
async def search_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        remaining = int(error.retry_after)
        minutes = remaining // 60
        seconds = remaining % 60
        await ctx.send(f"⚠️ Command on cooldown! Try again in {minutes}m {seconds}s.")


STATS_FILE = "search_stats.json"


def load_search_stats():
    """Load search statistics from file if it exists"""
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                loaded_stats = json.load(f)
                return loaded_stats
        else:
            # Return default stats structure if file doesn't exist
            return {
                "total_searches": 0,
                "total_messages_searched": 0,
                "total_matches_found": 0,
                "cancelled_searches": 0,
                "deep_searches": 0,
                "search_time_total": 0,
                "searches_by_guild": {},
                "searches_by_user": {},
                "last_search": None,
                "largest_search": {"messages": 0, "time": 0, "keyword": "", "guild": ""}
            }
    except Exception as e:
        print(f"Error loading search stats: {e}")
        # Return default stats structure on error
        return {
            "total_searches": 0,
            "total_messages_searched": 0,
            "total_matches_found": 0,
            "cancelled_searches": 0,
            "deep_searches": 0,
            "search_time_total": 0,
            "searches_by_guild": {},
            "searches_by_user": {},
            "last_search": None,
            "largest_search": {"messages": 0, "time": 0, "keyword": "", "guild": ""}
        }


# Global dictionaries for tracking search stats
search_stats = load_search_stats()


def save_search_stats():
    """Save current search statistics to file"""
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(search_stats, f, indent=2)
    except Exception as e:
        print(f"Error saving search stats: {e}")


@bot.command(name="searchstats")
async def search_stats_command(ctx):
    """Show statistics about searches performed"""
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    global search_stats

    # No stats available
    if search_stats["total_searches"] == 0:
        return await ctx.send("ℹ️ No search statistics available yet.")

    # Calculate average time and messages
    avg_time = search_stats["search_time_total"] / search_stats["total_searches"]
    avg_messages = search_stats["total_messages_searched"] / search_stats["total_searches"]

    # Create embed with stats
    embed = discord.Embed(
        title="🔍 Search Statistics",
        color=0x3498db,
        description="Statistics about searches performed with this bot"
    )

    # General stats
    embed.add_field(
        name="General Stats",
        value=f"• Total searches: **{search_stats['total_searches']}**\n"
              f"• Messages searched: **{search_stats['total_messages_searched']:,}**\n"
              f"• Matches found: **{search_stats['total_matches_found']}**\n"
              f"• Deep searches: **{search_stats['deep_searches']}**\n"
              f"• Cancelled searches: **{search_stats['cancelled_searches']}**",
        inline=False
    )

    # Performance stats
    embed.add_field(
        name="Performance",
        value=f"• Average messages per search: **{avg_messages:,.1f}**\n"
              f"• Average search time: **{avg_time:.2f}s**\n"
              f"• Total search time: **{search_stats['search_time_total']:.2f}s**",
        inline=False
    )

    # Last search info
    if search_stats["last_search"]:
        last = search_stats["last_search"]
        embed.add_field(
            name="Last Search",
            value=f"• User: **{last['user']}**\n"
                  f"• Keyword: **{last['keyword']}**\n"
                  f"• Messages: **{last['messages']:,}**\n"
                  f"• Time: **{last['time']:.2f}s**\n"
                  f"• Matches: **{last['matches']}**\n"
                  f"• Guild: **{last['guild']}**",
            inline=False
        )

    # Largest search info
    if search_stats["largest_search"]["messages"] > 0:
        largest = search_stats["largest_search"]
        embed.add_field(
            name="Largest Search",
            value=f"• Messages: **{largest['messages']:,}**\n"
                  f"• Time: **{largest['time']:.2f}s**\n"
                  f"• Keyword: **{largest['keyword']}**\n"
                  f"• Guild: **{largest['guild']}**",
            inline=False
        )

    # Most frequent searchers
    if search_stats["searches_by_user"]:
        top_users = sorted(search_stats["searches_by_user"].items(), key=lambda x: x[1], reverse=True)[:5]
        users_text = "\n".join([f"• **{user}**: {count} searches" for user, count in top_users])
        embed.add_field(name="Top Searchers", value=users_text, inline=False)

    # Most searched servers
    if search_stats["searches_by_guild"]:
        top_guilds = sorted(search_stats["searches_by_guild"].items(), key=lambda x: x[1], reverse=True)[:5]
        guilds_text = "\n".join([f"• **{guild}**: {count} searches" for guild, count in top_guilds])
        embed.add_field(name="Most Searched Servers", value=guilds_text, inline=False)

    # Footer - update to show stats are persistent
    embed.set_footer(text="Stats persist across bot restarts")

    await ctx.send(embed=embed)

    # Save stats to ensure they're synced with file
    save_search_stats()


@bot.command(name="context")
async def get_context(ctx, message_id: int = None, lines: int = 5):
    """Get context around a specific message"""
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    # Check for required message ID
    if message_id is None:
        return await ctx.send("⚠️ Usage: `!context message_id [lines=5]`\nYou must provide a message ID to get context.")

    # Validate lines parameter
    if lines < 1:
        lines = 1
    elif lines > 15:  # Limit to reasonable number
        lines = 15
        await ctx.send("⚠️ Maximum context limited to 15 messages.")

    # Status message
    status_msg = await ctx.send(f"🔍 Searching for message {message_id} and retrieving {lines} messages of context...")

    # Find the message across all channels
    target_message = None
    target_channel = None

    for channel in ctx.guild.text_channels:
        try:
            # First check cached messages to avoid unnecessary API calls
            messages = await get_cached_messages(channel.id, limit=100)
            for msg in messages:
                if msg.id == message_id:
                    target_message = msg
                    target_channel = channel
                    break

            if target_message:
                break

            # If not found in cache, try to fetch directly
            target_message = await channel.fetch_message(message_id)
            if target_message:
                target_channel = channel
                break
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Message not in this channel or can't access
            continue

    if not target_message:
        return await status_msg.edit(content=f"❌ Message with ID {message_id} not found in any channel.")

    # Calculate how many messages to get before and after
    before_count = lines // 2
    after_count = lines - before_count

    # Get context messages
    context_messages = []

    # Try to use cached messages first
    cached_messages = await get_cached_messages(target_channel.id, limit=100)
    if cached_messages:
        # Find the target message index in the cached messages
        target_index = None
        for i, msg in enumerate(cached_messages):
            if msg.id == message_id:
                target_index = i
                break

        if target_index is not None:
            # Get context from cache when possible
            start_index = max(0, target_index - before_count)
            end_index = min(len(cached_messages), target_index + after_count + 1)
            context_messages = cached_messages[start_index:end_index]

    # If we couldn't get enough context from cache, fall back to API
    if not context_messages:
        # Get messages before target
        try:
            before_msgs = []
            async for msg in target_channel.history(limit=before_count, before=target_message):
                before_msgs.append(msg)
            # Reverse order to show oldest first
            before_msgs.reverse()
            context_messages.extend(before_msgs)
        except discord.HTTPException:
            pass

        # Add target message
        context_messages.append(target_message)

        # Get messages after target
        try:
            after_msgs = []
            async for msg in target_channel.history(limit=after_count, after=target_message):
                after_msgs.append(msg)
            context_messages.extend(after_msgs)
        except discord.HTTPException:
            pass

    # Add target message
    context_messages.append(target_message)

    # Get messages after target
    try:
        # Fix: Convert async generator to list manually instead of using flatten()
        after_msgs = []
        async for msg in target_channel.history(limit=after_count, after=target_message):
            after_msgs.append(msg)
        context_messages.extend(after_msgs)
    except discord.HTTPException:
        pass

    # Create the embed for displaying context
    embed = discord.Embed(
        title=f"Message Context in #{target_channel.name}",
        color=0x3498db,
        description=f"Context around [message]({target_message.jump_url}) from {target_message.author.name}"
    )

    # Format the messages
    context_content = ""
    for i, msg in enumerate(context_messages):
        timestamp = msg.created_at.strftime('%H:%M:%S')
        is_target = msg.id == message_id

        # Format the message differently if it's the target message
        if is_target:
            author_part = f"**→ {msg.author.name}**"
        else:
            author_part = f"{msg.author.name}"

        # Truncate long messages
        content = msg.content if len(msg.content) <= 300 else f"{msg.content[:297]}..."

        # Add message to context string
        context_content += f"[{timestamp}] {author_part}: {content}\n"

        # Add attachments if any
        if msg.attachments:
            attachment_list = ", ".join([f"[{a.filename}]({a.url})" for a in msg.attachments])
            context_content += f"📎 {attachment_list}\n"

        # Add message separator
        context_content += "\n"

    # Add context to embed
    embed.description = f"Context around [message]({target_message.jump_url}) from {target_message.author.name}\n\n{context_content}"

    # Add footer with navigation help
    embed.set_footer(text=f"Use !context {message_id} [lines] to adjust context size")

    await status_msg.edit(content=None, embed=embed)

    # Add direct jump link as a separate message for easy clicking
    await ctx.send(f"🔗 **Direct link to message:** {target_message.jump_url}")


@bot.command(name="regex", aliases=["regexsearch", "regexsearcher", "regsea", "rs", "rsearch", "reg"])
async def regex_search(ctx, *args):
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    global search_cancelled, search_cooldowns

    # Handle cancel request
    if len(args) == 1 and args[0].lower() == "cancel":
        if hasattr(regex_search, "is_running") and regex_search.is_running:
            search_cancelled = True
            return await ctx.send("⚠️ Search cancelled.")
        else:
            return await ctx.send("⚠️ No search is currently running.")

    if not hasattr(regex_search, "is_running"):
        regex_search.is_running = False

    if regex_search.is_running:
        return await ctx.send("⚠️ A regex search is already running. Please wait for it to complete or use `!regex cancel` to stop it.")

    search_cancelled = False

    # Use the unified argument parsing function
    processed_args, flags = parse_command_args(args)

    # Extract flags with default values
    deep_search = flags.get("deep_search", False)
    query_limit = flags.get("query_limit", 500)  # Default limit
    custom_query = "query_limit" in flags
    include_channels = flags.get("include_channels", [])
    exclude_channels = flags.get("exclude_channels", [])

    # Check if there was an error in parsing arguments
    if "error" in flags:
        return await ctx.send(f"⚠️ {flags['error']}")

    # Check for required user and pattern
    if len(processed_args) < 2:
        return await ctx.send("⚠️ Usage: `!regex @user pattern [options]`\nExample: `!regex @user \"\\b\\w+ing\\b\"`")

    # Extract user
    try:
        user_arg = processed_args[0]
        if user_arg.startswith("<@") and user_arg.endswith(">"):
            user_id = user_arg[2:-1]
            if user_id.startswith("!"):
                user_id = user_id[1:]
        else:
            user_id = user_arg

        user_id = int(user_id)
        user = await get_cached_user(user_id)
        if not user:
            return await ctx.send("⚠️ User not found. Check if the ID is correct.")

        # Rest of args is the pattern
        regex_pattern = " ".join(processed_args[1:])

    except Exception as e:
        return await ctx.send(f"⚠️ Error: {e}\nUse @mention or user ID.")

    # Compile regex pattern
    try:
        pattern = re.compile(regex_pattern, re.IGNORECASE)
    except re.error as e:
        return await ctx.send(f"⚠️ Invalid regex pattern: {e}")

    # Check cooldown for deep searches
    if deep_search or custom_query:
        search_stats["deep_searches"] += 1
        guild_id = ctx.guild.id
        current_time = datetime.now()
        if guild_id in search_cooldowns:
            time_diff = (current_time - search_cooldowns[guild_id]).total_seconds()
            if time_diff < 600:  # 10 minutes cooldown
                remaining = int(600 - time_diff)
                minutes = remaining // 60
                seconds = remaining % 60
                return await ctx.send(f"⚠️ Deep search cooldown! Please wait {minutes}m {seconds}s before running another deep search.")
        search_cooldowns[guild_id] = current_time

    regex_search.is_running = True

    try:
        # Status message based on search type
        search_msg_prefix = ""
        if include_channels:
            channel_names = []
            for ch_id in include_channels:
                channel = discord.utils.get(ctx.guild.channels, name=ch_id.strip('#'))
                if channel:
                    channel_names.append(f"#{channel.name}")
            search_msg_prefix = f"in {', '.join(channel_names)} " if channel_names else ""
        elif exclude_channels:
            channel_names = []
            for ch_id in exclude_channels:
                channel = discord.utils.get(ctx.guild.channels, name=ch_id.strip('#'))
                if channel:
                    channel_names.append(f"#{channel.name}")
            search_msg_prefix = f"excluding {', '.join(channel_names)} " if channel_names else ""

        # Fix: Capitalize first letter when not deep searching
        searching_text = "Searching" if not deep_search else "Deep searching"
        status_msg = await ctx.send(
            f"🔍 {searching_text} {search_msg_prefix}for messages from {user.name} matching `{regex_pattern}`. This may take a while..."
        )

        # Prepare search channels
        search_channels = []
        if include_channels:
            for ch_name in include_channels:
                ch_name = ch_name.strip('#')
                channel = discord.utils.get(ctx.guild.text_channels, name=ch_name)
                if channel:
                    search_channels.append(channel)
        else:
            if exclude_channels:
                exclude_ch_names = [ch.strip('#') for ch in exclude_channels]
                search_channels = [ch for ch in ctx.guild.text_channels
                                   if ch.name not in exclude_ch_names]
            else:
                search_channels = ctx.guild.text_channels

        total_channels = len(search_channels)
        found_messages = []
        total_searched = 0
        start_time = datetime.now()
        last_update_time = start_time
        channels_searched = 0

        # Search through channels
        for channel in search_channels:
            channels_searched += 1

            # Check if search was cancelled
            if search_cancelled:
                await status_msg.edit(content="⚠️ Search cancelled.")
                search_stats["cancelled_searches"] += 1
                return

            try:
                messages = await get_cached_messages(channel.id, limit=query_limit, force_refresh=deep_search)
                for msg in messages:
                    if msg.author.id == user.id and pattern.search(msg.content):
                        found_messages.append((msg, channel))
                    total_searched += 1

                    # Update status message every 30 seconds to show progress
                    current_time = datetime.now()
                    if (current_time - last_update_time).total_seconds() > 30:
                        progress = int(channels_searched / total_channels * 100)
                        await status_msg.edit(content=f"🔍 {'Deep ' if deep_search else ''}searching {search_msg_prefix}for messages from {user.name} matching `{regex_pattern}`... {progress}% ({channels_searched}/{total_channels} channels, {total_searched:,} messages checked)")
                        last_update_time = current_time

            except discord.Forbidden:
                continue

        # Calculate search time
        search_time = (datetime.now() - start_time).total_seconds()

        if not found_messages:
            await status_msg.edit(
                content=f"❌ No messages found from {user.name} matching '{regex_pattern}'. Searched {total_searched:,} messages in {search_time:.1f}s."
            )
        else:
            # Format results
            result = f"✅ Found {len(found_messages)} regex matches for pattern `{regex_pattern}` from {user.name} (searched {total_searched:,} messages in {search_time:.1f}s):\n\n"

            for i, (msg, channel) in enumerate(found_messages, 1):
                timestamp = msg.created_at.strftime('%Y-%m-%d %H:%M:%S')
                content = msg.content if len(msg.content) <= 500 else f"{msg.content[:497]}..."
                result += f"{i}. **#{channel.name}** ({timestamp}):\n{content}\n[Jump to message]({msg.jump_url})\n\n"
                if len(result) > 1800:
                    await ctx.send(result)
                    result = ""
            if result:
                await ctx.send(result)
            await status_msg.edit(content=f"✅ Found {len(found_messages)} messages from {user.name} matching '{regex_pattern}'.")

        # Update global search stats
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
            "user": user.name,
            "keyword": regex_pattern,
            "messages": total_searched,
            "time": search_time,
            "matches": len(found_messages),
            "guild": ctx.guild.name
        }

        # Update largest search if applicable
        if total_searched > search_stats["largest_search"]["messages"]:
            search_stats["largest_search"] = {
                "messages": total_searched,
                "time": search_time,
                "keyword": regex_pattern,
                "guild": ctx.guild.name
            }

        save_search_stats()

    finally:
        regex_search.is_running = False
        search_cancelled = False


@bot.command(name="export")
async def export_results(ctx, *args):
    """Export search results to a file"""
    global search_cancelled, search_cooldowns

    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    # Handle cancel request
    if len(args) == 1 and args[0].lower() == "cancel":
        if hasattr(export_results, "is_running") and export_results.is_running:
            search_cancelled = True
            return await ctx.send("⚠️ Export cancelled.")
        else:
            return await ctx.send("⚠️ No export is currently running.")

    if not hasattr(export_results, "is_running"):
        export_results.is_running = False

    if export_results.is_running:
        return await ctx.send("⚠️ An export is already running. Please wait for it to complete or use `!export cancel` to stop it.")

    search_cancelled = False

    # Use the unified argument parsing function
    processed_args, flags = parse_command_args(args)

    # Extract flags with default values
    deep_search = flags.get("deep_search", False)
    query_limit = flags.get("query_limit", 500)  # Default limit
    custom_query = "query_limit" in flags
    include_channels = flags.get("include_channels", [])
    exclude_channels = flags.get("exclude_channels", [])

    # Check if there was an error in parsing arguments
    if "error" in flags:
        return await ctx.send(f"⚠️ {flags['error']}")

    # Check for required user and keyword arguments
    if len(processed_args) < 2:
        return await ctx.send("⚠️ Usage: `!export @user keyword [options]`")

    # Extract user from the first processed argument
    try:
        user_arg = processed_args[0]
        if user_arg.startswith("<@") and user_arg.endswith(">"):
            user_id = user_arg[2:-1]
            if user_id.startswith("!"):
                user_id = user_id[1:]
        else:
            user_id = user_arg

        user_id = int(user_id)
        user = await get_cached_user(user_id)
        if not user:
            return await ctx.send("⚠️ User not found. Check if the ID is correct.")

        # Extract keyword from remaining processed arguments
        keyword = " ".join(processed_args[1:])
    except Exception:
        return await ctx.send("⚠️ Invalid user format. Use @mention or user ID.")

    # Create exports directory if it doesn't exist
    os.makedirs("exports", exist_ok=True)

    filename = f"exports/{user.name}_{keyword.replace(' ', '_')[:20]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    # Apply cooldown check for deep searches
    if deep_search or custom_query:
        # Same cooldown logic as in search_messages
        guild_id = ctx.guild.id
        current_time = datetime.now()
        if guild_id in search_cooldowns:
            time_diff = (current_time - search_cooldowns[guild_id]).total_seconds()
            if time_diff < 600:  # 10 minutes cooldown
                remaining = int(600 - time_diff)
                minutes = remaining // 60
                seconds = remaining % 60
                return await ctx.send(f"⚠️ Deep search cooldown! Please wait {minutes}m {seconds}s before running another deep search.")
        search_cooldowns[guild_id] = current_time

    # Fix: Capitalize first letter when not deep searching
    searching_text = "Searching" if not deep_search else "Deep searching"
    status_msg = await ctx.send(
        f"🔍 {searching_text} for messages from {user.name} containing '{keyword}'... Results will be exported to a file."
    )

    export_results.is_running = True

    try:
        # Prepare search channels
        search_channels = []
        if include_channels:
            for ch_name in include_channels:
                ch_name = ch_name.strip('#')
                channel = discord.utils.get(ctx.guild.text_channels, name=ch_name)
                if channel:
                    search_channels.append(channel)
        else:
            if exclude_channels:
                exclude_ch_names = [ch.strip('#') for ch in exclude_channels]
                search_channels = [ch for ch in ctx.guild.text_channels
                                  if ch.name not in exclude_ch_names]
            else:
                search_channels = ctx.guild.text_channels

        found_messages = []
        total_searched = 0
        start_time = datetime.now()
        last_update_time = start_time
        channels_searched = 0

        # Search through channels
        for channel in search_channels:
            channels_searched += 1

            # Check if search was cancelled
            if search_cancelled:
                await status_msg.edit(content=f"⚠️ Export cancelled after searching {total_searched:,} messages.")
                return

            # Update status message every 5 seconds
            current_time = datetime.now()
            if (current_time - last_update_time).total_seconds() > 5:
                progress = channels_searched / len(search_channels) * 100
                time_elapsed = (current_time - start_time).total_seconds()
                await status_msg.edit(content=f"🔍 {searching_text}... ({channels_searched}/{len(search_channels)} channels, {total_searched:,} messages, {progress:.1f}%, {time_elapsed:.1f}s)")
                last_update_time = current_time

            try:
                messages = []
                if deep_search or custom_query:
                    # Use specified limit for deep searches
                    async for msg in channel.history(limit=query_limit):
                        messages.append(msg)
                        total_searched += 1
                else:
                    # Use default limit for regular searches
                    async for msg in channel.history(limit=100):
                        messages.append(msg)
                        total_searched += 1

                # Search messages
                for msg in messages:
                    if msg.author.id == user.id and keyword.lower() in msg.content.lower():
                        found_messages.append((msg, channel))

            except discord.Forbidden:
                continue

        # Calculate search time
        search_time = (datetime.now() - start_time).total_seconds()

        # Export results to file
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"Export of messages from {user.name} containing '{keyword}'\n")
            f.write(f"Searched {total_searched:,} messages in {search_time:.1f}s\n")
            f.write(f"Found {len(found_messages)} matching messages\n")
            f.write(f"Export date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")

            if not found_messages:
                f.write("No matching messages found.")
            else:
                for i, (msg, channel) in enumerate(found_messages, 1):
                    timestamp = msg.created_at.strftime('%Y-%m-%d %H:%M:%S')
                    f.write(f"Message {i}/{len(found_messages)}\n")
                    f.write(f"Channel: #{channel.name}\n")
                    f.write(f"Date: {timestamp}\n")
                    f.write(f"Link: {msg.jump_url}\n")
                    f.write(f"Content: {msg.content}\n")

                    # Add attachments info
                    if msg.attachments:
                        f.write("Attachments:\n")
                        for a in msg.attachments:
                            f.write(f"  - {a.filename}: {a.url}\n")

                    f.write("\n" + "-" * 40 + "\n\n")

        # Update status and send file
        await status_msg.edit(content=f"✅ Export complete! Found {len(found_messages)} messages from {user.name} containing '{keyword}' (searched {total_searched:,} messages in {search_time:.1f}s)")

        # Send the file
        file = discord.File(filename, filename=os.path.basename(filename))
        await ctx.send(f"📁 Results file:", file=file)

        # Update global search stats
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
            "user": user.name,
            "keyword": keyword,
            "messages": total_searched,
            "time": search_time,
            "matches": len(found_messages),
            "guild": ctx.guild.name
        }

        # Update largest search if applicable
        if total_searched > search_stats["largest_search"]["messages"]:
            search_stats["largest_search"] = {
                "messages": total_searched,
                "time": search_time,
                "keyword": keyword,
                "guild": ctx.guild.name
            }

        save_search_stats()

    except Exception as e:
        await ctx.send(f"⚠️ Error during export: {e}")
    finally:
        export_results.is_running = False
        search_cancelled = False


@bot.command(name="help")
async def help_command(ctx):
    if not is_admin(ctx):
        return
    embed = discord.Embed(
        title="🛠️ Bot Commands",
        color=0x3498db,
        description="Available admin-only commands:"
    )
    embed.add_field(name="!setkeywords word1, word2...", value="Update search keywords", inline=False)
    embed.add_field(name="!toggleprints user/message", value="Toggle printed output", inline=False)
    embed.add_field(name="!showkeywords", value="Display current keywords", inline=False)
    embed.add_field(name="!scan --users/--messages/--all",
                    value="Manually scan server for matches (requires one option)", inline=False)
    embed.add_field(name="!search @user keyword [options]",
                    value="Search user messages with options:\n"
                          "• `--a/--all` for deep search\n"
                          "• `--q limit` for custom message limit (e.g. `--q 10k`)\n"
                          "• `--in #channel1,#channel2` to search only specific channels\n"
                          "• `--exclude #channel3,#channel4` to skip specific channels", inline=False)
    embed.add_field(name="!search cancel", value="Cancel a running search operation", inline=False)
    embed.add_field(name="!badscan [options]",
                    value="Scan for bad words with options:\n"
                          "--user @mention: Only check specific user\n"
                          "--strictness low/medium/high: Detection sensitivity\n"
                          "--list : sample list of bad words\n"
                          "--in/--exclude: Channel filtering",
                    inline=False)
    embed.add_field(name="!searchstats", value="Display statistics about searches performed", inline=False)
    embed.add_field(name="!regex @user pattern [options]",
                    value="Search using regex patterns with the same options as !search", inline=False)
    embed.add_field(name="!export @user keyword [options]",
                    value="Export search results to a text file with the same options as !search", inline=False)
    embed.add_field(name="!context message_id [lines=5]", value="Get conversation context around a specific message",
                    inline=False)
    embed.add_field(name="!autoscan on/off", value="Enable/disable periodic auto-scanning", inline=False)
    embed.add_field(name="!scaninterval <minutes>", value="Set auto-scan interval in minutes", inline=False)
    embed.add_field(name="!clearlogs today/all", value="Delete logs for today or all logs", inline=False)
    await ctx.send(embed=embed)


# --- Scheduled tasks ---
@tasks.loop(seconds=3600)
async def auto_scan():
    print(f"🔄 Running scheduled auto-scan ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")

    scan_count = 0
    message_count = 0

    for guild in bot.guilds:
        if not guild.chunked:
            await guild.chunk(cache=True)

        for member in get_cached_members(guild.id):
            name_fields = f"{member.name} {member.display_name}"
            if keyword_match(name_fields):
                entry = f"[AUTO-SCAN] {member} ({member.id}) in {guild.name}"
                user_logger.info(entry)
                scan_count += 1

        for channel in guild.text_channels:
            try:
                messages = await get_cached_messages(channel.id, limit=100, force_refresh=True)
                for msg in messages:
                    if keyword_match(msg.content):
                        entry = f"[AUTO-SCAN] {msg.author} in #{channel.name} ({msg.guild.name}) > {msg.content}"
                        msg_logger.info(entry)
                        message_count += 1
            except discord.Forbidden:
                continue

    next_scan_time = datetime.now() + timedelta(seconds=auto_scan.seconds)
    next_scan_str = next_scan_time.strftime('%Y-%m-%d %H:%M:%S')

    print(f"✅ Auto-scan complete! Found {scan_count} matching members and {message_count} messages.")
    print(f"⏰ Next auto-scan scheduled for: {next_scan_str} (in {format_time_interval(auto_scan.seconds / 60)})")


@auto_scan.before_loop
async def before_auto_scan():
    """Wait for the bot to be ready before starting auto-scan"""
    await bot.wait_until_ready()


@bot.command(name="autoscan")
async def toggle_auto_scan(ctx, enabled: str = None):
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    if enabled is None:
        status = "enabled" if CONFIG.get("auto_scan_enabled", False) else "disabled"
        interval = CONFIG.get("auto_scan_interval_minutes", 60)
        return await ctx.send(f"🔄 Auto-scan is {status} (interval: {format_time_interval(interval)})")

    if enabled.lower() in ("on", "true", "yes", "1"):
        CONFIG["auto_scan_enabled"] = True
        save_config()
        update_scheduled_tasks()
        interval = CONFIG.get("auto_scan_interval_minutes", 60)
        await ctx.send(f"✅ Auto-scan enabled (every {format_time_interval(interval)})")
    elif enabled.lower() in ("off", "false", "no", "0"):
        CONFIG["auto_scan_enabled"] = False
        save_config()
        update_scheduled_tasks()
        await ctx.send("❌ Auto-scan disabled")
    else:
        await ctx.send("⚠️ Use 'on' or 'off'")


@bot.command(name="scaninterval")
async def set_scan_interval(ctx, minutes: str):
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    try:
        interval = float(minutes)

        # Check minimum value
        if interval < 0.5:
            return await ctx.send("⚠️ Interval must be at least 0.5 minutes (30 seconds)")

        # Store the interval in minutes
        CONFIG["auto_scan_interval_minutes"] = interval
        save_config()

        # Update the task
        update_scheduled_tasks()

        # Provide feedback with formatted time
        await ctx.send(f"✅ Auto-scan interval set to {format_time_interval(interval)}")

    except ValueError:
        await ctx.send("⚠️ Please provide a valid number for the interval")


@bot.command(name="clearlogs", aliases=["clearlog", "cl", "logclear", "logsclear"])
async def clear_logs(ctx, scope: str = "today"):
    """Clear logs based on scope (today/all)"""
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    try:
        global msg_logger, user_logger, msg_log_path, user_log_path, log_listener

        # Stop the current listener
        log_listener.stop()

        if scope.lower() == "today":
            # Clear today's logs
            with open(msg_log_path, 'w', encoding='utf-8') as f:
                pass
            with open(user_log_path, 'w', encoding='utf-8') as f:
                pass
            await ctx.send("✅ Today's logs cleared successfully.")

        elif scope.lower() == "all":
            # Delete all log directories and files
            shutil.rmtree(LOGS_DIR)
            os.makedirs(LOGS_DIR, exist_ok=True)
            await ctx.send("✅ All logs cleared successfully.")

        else:
            await ctx.send("⚠️ Invalid scope. Use 'today' or 'all'.")

        # Re-initialize logging
        msg_logger, user_logger, msg_log_path, user_log_path, log_listener = setup_logging()

    except Exception as e:
        await ctx.send(f"❌ Error clearing logs: {str(e)}")


@bot.command(name="badscan", aliases=["scanwords", "wordscan", "badwords"])
async def scan_bad_words(ctx, *args):
    """Scan for messages containing bad words including obfuscated variants"""
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    global search_cancelled

    # Parse arguments first - before any other processing
    processed_args, flags = parse_command_args(args)

    # Check for list flag - handle it immediately
    if "--list" in args or "-l" in args or any(arg == "list" for arg in processed_args):
        if not BAD_WORDS:
            return await ctx.send("⚠️ Bad words list is empty. Check if file exists at `utils/badwords_en.txt`.")

        word_sample = sorted(list(BAD_WORDS))[:50]
        sample_text = ", ".join(word_sample)
        return await ctx.send(f"📋 **Bad words list sample** (showing 50 of {len(BAD_WORDS)} words):\n```{sample_text}...```")

    export_results = False
    export_format = "txt"  # Default format
    for i, arg in enumerate(args):
        if arg == "--export" or arg == "-e":
            export_results = True
            # Check if a format is specified
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                format_value = args[i + 1].lower()
                if format_value in ["txt", "csv", "json"]:
                    export_format = format_value
        elif arg.startswith("--export=") or arg.startswith("-e="):
            export_results = True
            format_value = arg.split("=")[1].lower()
            if format_value in ["txt", "csv", "json"]:
                export_format = format_value

    # Handle cancel request
    if "cancel" in processed_args:
        if hasattr(scan_bad_words, "is_running") and scan_bad_words.is_running:
            search_cancelled = True
            return await ctx.send("🛑 Cancelling bad words scan...")
        else:
            return await ctx.send("⚠️ No bad words scan is currently running.")

    # Setup command execution
    if not setup_command_execution(scan_bad_words):
        return await ctx.send("⚠️ A bad words scan is already running. Use `!badscan cancel` to stop it.")

    search_cancelled = False

    # Extract debug mode from config or flags
    debug_mode = CONFIG.get("debug_mode", False) or "--debug" in args or "-d" in args

    # Extract flags with default values
    query_limit = int(flags.get("query_limit", 500))  # Default limit

    # Handle channel filters directly from args to ensure proper parsing
    include_channels = []

    # Process --in flag for channels
    for i, arg in enumerate(args):
        if arg == "--in" and i + 1 < len(args):
            channel_input = args[i + 1]
            include_channels.append(channel_input)
        elif arg.startswith("--in="):
            channel_input = arg[5:]
            include_channels.append(channel_input)

    # Process strictness flag
    strictness = "medium"  # default
    for i, arg in enumerate(args):
        if arg.startswith("--strictness="):
            strictness = arg[13:]
        elif arg == "--strictness" and i + 1 < len(args):
            strictness = args[i + 1]

    # Process language flag
    lang = "en"  # default
    for i, arg in enumerate(args):
        if arg.startswith("--lang="):
            lang = arg[7:]
        elif arg == "--lang" and i + 1 < len(args):
            lang = args[i + 1]

    # Validate strictness level
    if strictness not in ["low", "medium", "high"]:
        strictness = "medium"
        await ctx.send("⚠️ Invalid strictness level. Using 'medium' instead. Valid options: low, medium, high")

    # Load language-specific bad words
    bad_words_path = f"utils/badwords_{lang}.txt"
    if lang != "en" or not BAD_WORDS:
        try:
            with open(bad_words_path, "r", encoding="utf-8") as f:
                bad_words = set(line.strip().lower() for line in f if line.strip())
        except FileNotFoundError:
            return await ctx.send(f"⚠️ Bad words file not found for language '{lang}'. Expected at `{bad_words_path}`.")
        except Exception as e:
            return await ctx.send(f"⚠️ Error loading bad words: {e}")
    else:
        bad_words = BAD_WORDS

    # Verify bad words list is loaded
    if not bad_words:
        return await ctx.send(f"⚠️ Bad words list is empty for language '{lang}'. Check if the file exists at `{bad_words_path}`.")

    # Process target user if provided
    user = None
    for i, arg in enumerate(args):
        if arg.startswith("--user="):
            user_input = arg[7:]
            try:
                # Handle user mention format
                if user_input.startswith("<@") and user_input.endswith(">"):
                    user_id = user_input.replace("<@", "").replace("<@!", "").replace(">", "")
                    user = await bot.fetch_user(int(user_id))
                else:
                    # Try to get user by ID
                    user = await bot.fetch_user(int(user_input))
            except (ValueError, discord.NotFound, discord.HTTPException):
                return await ctx.send(f"⚠️ Could not find user: {user_input}")

        elif arg == "--user" and i + 1 < len(args):
            user_input = args[i + 1]
            try:
                # Handle user mention format
                if user_input.startswith("<@") and user_input.endswith(">"):
                    user_id = user_input.replace("<@", "").replace("<@!", "").replace(">", "")
                    user = await bot.fetch_user(int(user_id))
                else:
                    # Try to get user by ID
                    user = await bot.fetch_user(int(user_input))
            except (ValueError, discord.NotFound, discord.HTTPException):
                return await ctx.send(f"⚠️ Could not find user: {user_input}")

    # Prepare search channels
    search_channels = []
    channel_names_to_find = []

    # Process include_channels to extract any channel mentions or find by channel name
    if include_channels:
        for ch_item in include_channels:
            # Handle channel mention format: <#ID>
            if ch_item.startswith("<#") and ch_item.endswith(">"):
                try:
                    channel_id = int(ch_item[2:-1])
                    channel = ctx.guild.get_channel(channel_id)
                    if channel and isinstance(channel, discord.TextChannel) and channel.permissions_for(ctx.guild.me).read_messages:
                        search_channels.append(channel)
                    else:
                        channel_names_to_find.append(ch_item)  # Keep the full mention for error reporting
                except (ValueError, AttributeError):
                    channel_names_to_find.append(ch_item)
            else:
                # For channel names, try to find by name
                channel_names_to_find.append(ch_item)

        # Process any channel names that need to be looked up
        if channel_names_to_find:
            for ch_name in channel_names_to_find:
                channel = discord.utils.get(ctx.guild.text_channels, name=ch_name.lstrip('#'))
                if channel and channel.permissions_for(ctx.guild.me).read_messages:
                    search_channels.append(channel)

        if not search_channels:
            scan_bad_words.is_running = False
            return await ctx.send(f"⚠️ None of the specified channels were found or accessible. Looking for: {', '.join(channel_names_to_find)}")
    else:
        search_channels = [ch for ch in ctx.guild.text_channels
                           if ch.permissions_for(ctx.guild.me).read_messages]

    if not search_channels:
        scan_bad_words.is_running = False
        return await ctx.send("⚠️ No channels to search.")


    user_filter = f" from {user.name}" if user else ""
    channel_filter = ""
    if include_channels:
        channel_names = [f"#{ch.name}" for ch in search_channels]
        channel_filter = f" in {', '.join(channel_names)}"

    export_notice = f" (results will be exported to `exports/badscans` folder)" if export_results else ""
    status_msg = await ctx.send(f"🔍 Scanning for messages containing bad words{user_filter}{channel_filter} with {strictness} detection ({lang} language, limit: {query_limit}){export_notice}...")

    try:
        total_channels = len(search_channels)
        found_messages = []
        total_messages = 0
        start_time = datetime.now()
        last_update_time = start_time
        channels_searched = 0

        def text_contains_bad_word(text, strictness_level):
            """Check if text contains bad words and return the matched words"""
            text = text.lower()
            found_words = []

            if strictness_level == "low":
                # Simple exact match
                for word in bad_words:
                    if f" {word} " in f" {text} " or text == word or text.startswith(f"{word} ") or text.endswith(f" {word}"):
                        found_words.append(word)

            elif strictness_level == "medium":
                # Consider word boundaries
                for word in bad_words:
                    pattern = r'\b' + re.escape(word) + r'\b'
                    if re.search(pattern, text):
                        found_words.append(word)

            elif strictness_level == "high":
                # Include obfuscation detection
                for word in bad_words:
                    # Basic obfuscation patterns
                    pattern = ''.join(f"[{c}1!iI|]{{'0,2}}" if c.lower() in 'aeiou' else f"[{c.lower()}{c.upper()}]{{1,2}}" for c in word)
                    if re.search(pattern, text):
                        found_words.append(word)

            return found_words

        # Search for messages with bad words in channels
        for channel in search_channels:
            if search_cancelled:
                break

            channels_searched += 1

            # Update status message every 5 seconds
            current_time = datetime.now()
            if (current_time - last_update_time).total_seconds() >= 5:
                elapsed = (current_time - start_time).total_seconds()
                messages_per_second = total_messages / elapsed if elapsed > 0 else 0
                status = f"🔍 Searching: {channels_searched}/{total_channels} channels, {total_messages:,} messages ({messages_per_second:.1f}/sec), {len(found_messages)} matches..."
                if search_cancelled:
                    status = "⚠️ Search cancelled. Finalizing results..."
                await status_msg.edit(content=status)
                last_update_time = current_time

            try:
                # Get channel messages
                async for message in channel.history(limit=query_limit):
                    # Skip bot's own messages
                    if message.author.id == bot.user.id:
                        continue

                    # Skip messages not from the target user if one is specified
                    if user and message.author.id != user.id:
                        continue

                    total_messages += 1

                    # Add debug info for message processing
                    debug_print(f"Reading message from {message.author.name} in #{channel.name}", debug_mode)

                    # Performance tracking
                    if total_messages % 100 == 0 and debug_mode:
                        elapsed = (datetime.now() - start_time).total_seconds()
                        messages_per_second = total_messages / elapsed if elapsed > 0 else 0
                        debug_print(f"Speed: {messages_per_second:.1f} messages/sec - Total: {total_messages:,}", debug_mode)

                # Check if message contains bad words according to strictness
                    if text_contains_bad_word(message.content, strictness):
                        # Format the message for display
                        content = message.content
                        if len(content) > 300:
                            content = content[:297] + "..."

                        # Strip markdown to avoid formatting issues
                        content = content.replace("```", "'''").replace("`", "'")

                        matched_words = text_contains_bad_word(message.content, strictness)
                        found_messages.append({
                            "id": message.id,
                            "author": f"{message.author.name}",
                            "author_id": message.author.id,
                            "content": content,
                            "timestamp": message.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                            "channel_name": channel.name,
                            "channel_id": channel.id,
                            "jump_url": message.jump_url,
                            "matched_words": matched_words
                        })

                    # Check for message limit to avoid throttling
                    if search_cancelled or len(found_messages) >= 1000:
                        break

            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

        # Calculate search time
        search_time = (datetime.now() - start_time).total_seconds()

        # Show results
        if not found_messages:
            await status_msg.edit(content=f"✅ No bad words found in {total_messages:,} messages across {channels_searched} channels (took {search_time:.1f}s).")
        else:
            # Export results if requested
            if export_results:
                try:
                    export_file = await save_scan_results(ctx, found_messages, export_format, user, search_channels)
                    await ctx.send(f"📊 Exported {len(found_messages)} results to `{export_file}`",
                                   file=discord.File(export_file))
                except Exception as e:
                    await ctx.send(f"⚠️ Failed to export results: {e}")

            # Create chunks to handle Discord's 2000 character limit
            message_chunks = []
            current_chunk = f"🔴 **Found {len(found_messages)} messages containing bad words** (searched {total_messages:,} messages in {search_time:.1f}s):\n\n"

            if export_results:
                current_chunk += f"*Results exported to {export_format.upper()} file*\n\n"
            # Create embed for results
            embed = discord.Embed(
                title=f"🔎 Bad Words Scan Results",
                color=0xe74c3c,
                description=f"Found {len(found_messages)} message{'s' if len(found_messages) != 1 else ''} with potential bad words"
            )

            # Add search parameters
            search_params = f"• **Strictness:** {strictness}\n• **Language:** {lang}\n• **Messages scanned:** {total_messages:,}"
            if user:
                search_params += f"\n• **User filter:** {user.name}"
            embed.add_field(name="Search Parameters", value=search_params, inline=False)

            # Sort messages by timestamp (newest first)
            found_messages.sort(key=lambda x: x['timestamp'], reverse=True)

            # Create message outputs with the new format
            results_text = ""
            for i, match in enumerate(found_messages[:15]):  # Show only first 15
                matched_word_list = ", ".join([f"`{word}`" for word in match['matched_words']])

                results_text += f"[{i+1}] **{match['author']}** in #{match['channel_name']}:\n"
                results_text += f"↳ ⁠{match['content']}\n"
                results_text += f"↳ ⁠**Bad words detected:** {matched_word_list}\n"
                results_text += f"↳ ⁠[Jump to message]({match['jump_url']})\n\n"

            if len(found_messages) > 15:
                results_text += f"*...and {len(found_messages) - 15} more messages*"

            embed.add_field(name="Results", value=results_text, inline=False)

            # Add footer with search time
            embed.set_footer(text=f"Scan completed in {search_time:.1f} seconds, {len(found_messages)} matches found")

            await status_msg.edit(content=None, embed=embed)

    except Exception as e:
        await ctx.send(f"⚠️ Error during scan: {e}")
    finally:
        scan_bad_words.is_running = False
        search_cancelled = False


# --- Utility Commands ---
@bot.command(name="debug")
async def toggle_debug(ctx, state: str = None):
    if not is_admin(ctx):
        pass

    global CONFIG

    if state is None:
        current_state = CONFIG.get("debug_mode", False)
        return await ctx.send(f"🛠️ Debug mode is currently {'enabled' if current_state else 'disabled'}")

    if state.lower() in ("on", "true", "yes", "1"):
        CONFIG["debug_mode"] = True
        save_config()
        await ctx.send("🛠️ Debug mode enabled - performance data will be printed to console")
    elif state.lower() in ("off", "false", "no", "0"):
        CONFIG["debug_mode"] = False
        save_config()
        await ctx.send("🛠️ Debug mode disabled")
    else:
        await ctx.send("⚠️ Use 'on' or 'off'")

@bot.command(name="clearcache")
async def clear_cache(ctx):
    """Clear all cached data"""
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    global member_cache, message_cache, user_cache, keyword_match_cache

    member_cache.clear()
    message_cache.clear()
    user_cache.clear()
    keyword_match_cache.clear()

    await ctx.send("✅ All caches cleared successfully.")


@bot.command(name="sysinfo", aliases=["sys", "info", "system", "botinfo", "bot", "sinfo", "si", "bi"])
async def memory_info(ctx):
    """Show system and memory usage statistics"""
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    loading_msg = await ctx.send("⏳ Building system information embed, please wait...")

    try:
        # Get cache statistics using the utility function
        cache_stats = get_cache_stats(member_cache, message_cache, user_cache, keyword_match_cache)

        # Get memory usage
        process = psutil.Process()
        memory_usage = process.memory_info().rss / 1024 / 1024  # Convert to MB
        system_memory = psutil.virtual_memory()

        # Create embed
        embed = discord.Embed(
            title="🖥️ System Information",
            color=0x3498db,
            description="Bot and system resource usage stats"
        )

        # System information
        embed.add_field(
            name="System",
            value=(f"**OS:** {platform.system()} {platform.release()}\n"
                   f"**Python:** {platform.python_version()}\n"
                   f"**discord.py:** {discord.__version__}\n"
                   f"**Process ID:** {os.getpid()}"),
            inline=False
        )

        # Discord info
        embed.add_field(
            name="Discord",
            value=(f"**API Latency:** {round(bot.latency * 1000)}ms\n"
                   f"**Guilds:** {len(bot.guilds)}\n"
                   f"**Users Available:** {sum(g.member_count for g in bot.guilds):,}"),
            inline=True
        )

        # Memory usage
        embed.add_field(
            name="Memory",
            value=(f"**Bot Usage:** {memory_usage:.2f} MB\n"
                   f"**System Free:** {system_memory.available/1024/1024:.2f} MB\n"
                   f"**System Total:** {system_memory.total/1024/1024:.2f} MB"),
            inline=True
        )

        # Cache statistics
        embed.add_field(
            name="Cache Statistics",
            value=(f"**Members:** {cache_stats['member_count']:,} in {cache_stats['member_guilds']} guilds\n"
                   f"**Messages:** {cache_stats['message_count']:,} across {cache_stats['message_entries']} channels\n"
                   f"**Users:** {cache_stats['user_count']:,}\n"
                   f"**Keyword Matches:** {cache_stats['keyword_matches']:,}"),
            inline=False
        )

        # Memory used by caches
        embed.add_field(
            name="Cache Memory Usage",
            value=(f"**Member Cache:** {cache_stats['sizes']['member_size']:.2f} KB\n"
                   f"**Message Cache:** {cache_stats['sizes']['message_size']:.2f} KB\n"
                   f"**User Cache:** {cache_stats['sizes']['user_size']:.2f} KB\n"
                   f"**Keyword Cache:** {cache_stats['sizes']['keyword_size']:.2f} KB\n"
                   f"**Total Cache Size:** {cache_stats['sizes']['total_size']:.2f} KB"),
            inline=True
        )

        # Log file info
        embed.add_field(
            name="Logs",
            value=(f"**Directory:** `{os.path.dirname(msg_log_path)}`\n"
                   f"**Message Log:** `{os.path.basename(msg_log_path)}`\n"
                   f"**User Log:** `{os.path.basename(user_log_path)}`"),
            inline=True
        )

        # Auto-scan status
        auto_scan_status = "Enabled" if CONFIG.get("auto_scan_enabled", False) else "Disabled"
        auto_scan_interval = CONFIG.get("auto_scan_interval_minutes", 60)

        embed.add_field(
            name="Auto-Scan Status",
            value=(f"**Status:** {auto_scan_status}\n"
                   f"**Interval:** {format_time_interval(auto_scan_interval)}\n"),
            inline=False
        )

        # Search statistics
        if search_stats["total_searches"] > 0:
            avg_time = search_stats["search_time_total"] / search_stats["total_searches"]
            avg_messages = search_stats["total_messages_searched"] / search_stats["total_searches"]

            embed.add_field(
                name="Search Statistics",
                value=(f"**Total Searches:** {search_stats['total_searches']}\n"
                       f"**Deep Searches:** {search_stats.get('deep_searches', 0)}\n"
                       f"**Messages Searched:** {search_stats['total_messages_searched']:,}\n"
                       f"**Matches Found:** {search_stats['total_matches_found']:,}\n"
                       f"**Avg Search Time:** {avg_time:.2f}s\n"
                       f"**Avg Messages/Search:** {avg_messages:.1f}"),
                inline=False
            )

        # Set footer with timestamp
        embed.set_footer(text=f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        await loading_msg.edit(content=None, embed=embed)

    except Exception as e:
        await loading_msg.edit(content=f"❌ Error generating system info: {str(e)}")


@bot.command(name="listcache", aliases=["cacheinfo", "cache"])
async def list_cache(ctx):
    """Display information about currently cached data"""
    if not is_admin(ctx):
        return await ctx.send("❌ You must be a server admin to use this.")

    embed = discord.Embed(title="🗂️ Cache Information", color=0x3498db)

    # Member cache information
    member_cache_text = f"**Guilds cached:** {len(member_cache)}\n"
    member_cache_text += f"**Total members cached:** {sum(len(members) for members in member_cache.values())}\n"
    embed.add_field(name="Member Cache", value=member_cache_text, inline=False)

    # Message cache information
    message_cache_text = f"**Channel entries:** {len(message_cache)}\n"
    message_cache_text += f"**Total messages cached:** {sum(len(messages) for messages in message_cache.values())}\n"
    embed.add_field(name="Message Cache", value=message_cache_text, inline=False)

    # User cache information
    user_cache_text = f"**Users cached:** {len(user_cache)}\n"
    embed.add_field(name="User Cache", value=user_cache_text, inline=False)

    await ctx.send(embed=embed)


try:
    bot.run(TOKEN)
except discord.LoginFailure:
    print("Error: Invalid token. Please check your .env file.")
except Exception as e:
    print(f"Error starting bot: {e}")
