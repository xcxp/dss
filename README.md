# DeepSearch Discord Bot
A comprehensive Discord bot for server moderation, message scanning, and content analysis.

---
Table of Contents
- [Features](#features)
- [Commands](#commands)
  - [Search Commands](#search-commands)
  - [Admin Commands](#admin-commands)
  - [System Commands](#system-commands)
  - [Options for Search Commands](#options-for-search-commands)
- [Installation](#installation)
- [Configuration](#configuration)
- [Requirements](#requirements)
- [License](#license)
---

## Features
- **User and Message Scanning** (Customizable): Search messages by user, keyword, or regex pattern
- **Bad Word Detection** (Customizable): Filter content with configurable strictness levels
- **Context View**: Get conversation context around specific messages
- **Data Export**: Export search results to txt, csv, or json formats
- **Message Caching**: Efficient message retrieval with configurable cache
- **Statistics Tracking:** Comprehensive usage statistics
- **Automated Scanning** (Customizable): Periodic scanning for undesirable content

## Commands
### Search Commands
- `!search @user keyword [options]` - Search for messages
- `!regex @user pattern [options]` - Search with regex patterns
- `!export @user keyword [options]` - Same as search command but this time exports results to a file
- `!context message_id [lines=5]` - Get context around a message
- `!badscan [options]` - Scan for bad words, also has an export option.

### Admin Commands
- `!setkeywords word1, word2...` - Update search keywords
- `!showkeywords` - Display current keywords
- `!toggleprints user/message` - Toggle printed output
- `!scan --users/--messages/--all` - Manually scan server
- `!autoscan on/off` - Enable/disable periodic scanning
- `!scaninterval <minutes>` - Set auto-scan interval

### System Commands
- `!clearcache` - Clear all cached data
- `!clearlogs today/all` - Delete logs
- `!sysinfo` - Show system and memory usage
- `!searchstats` - Display search statistics
- `!debug on/off` - Toggle debug mode

### Options for Search Commands
- `--a/--all` - Deep search (searches more messages)
- `--q limit` - Custom message limit (e.g. --q 10k)
- `--in #channel1,#channel2` - Search only specific channels
- `--exclude #channel3,#channel4` - Skip specific channels

## Installation
- Clone the repository: `git clone https://github.com/BENZOOgataga/DeepSearch.git`
- Install requirements: `pip install -r requirements.txt`
- Rename the [`.env.example`](.env.example) to `.env` and replace `BOT_TOKEN` with your Discord token: `BOT_TOKEN=your_token_here`
  - Note: You can learn how to create a Discord bot [here](BOT_TOKEN_TUTORIAL.md). **Your bot will need message content intent!**
- Run the bot: `python bot.py`

## Configuration
The bot uses JSON configuration files for persistent settings:

- [config.json](config.json) - Bot settings
- search_stats.json (generated automatically) - Saves search statistics to keep them after a bot restart

## Requirements
- Python 3.8+
- discord.py=2.5.2
- python-dotenv
- cachetools
- psutil

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details. Please credit me when using my code, repository (even forks) 🙏
