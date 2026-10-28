# 🧪 Step-by-Step: Create a Discord Bot & Get the Token
## 1. Go to the Discord Developer Portal
🔗 [discord.com/developers/applications](https://discord.com/developers/applications)

## 2. Click "New Application"
- Name it whatever you want (e.g., DeepSearch, ScannerBot, MessageFinder, ...)
- Hit **Create**

## 3. Go to the "Bot" Tab
- Click “Bot” on the left sidebar
- Click “Add Bot” → Confirm with Yes, do it!

## 4. Enable Required Intents (Very Important)
- In the "Bot" section, enable all the intents you can under the "Privileged Gateway Intents".
  - Verified Discord Bots need to apply for message intent, it is recommended to create a separate Discord bot so that it isn't verified and you have access to the message intent without any application.

## 5. (Optional) Rename & Set Bot Avatar
You can change the bot’s username and icon here.

## 6. Get the Bot Token
In the same "Bot" tab, click “Reset Token” or “Copy Token”, keep it, you'll need it in the code.

## 7. Invite your Bot
- Click on "Installation" and do the following:
  - Disable "User Install" under "Installation Contexts"
  - Add the "bot" scope under the "Scopes" section in "Default Install Settings"
  - Add necessary permissions under "Permissions" section in "Default Install Settings"
    - It is recommended to put `Administrator` but you can always just put `READ_MESSAGES_HISTORY` and `SEND_MESSAGES` and see what happens I guess (not tested)?

⚠️ Save the token somewhere safe – this is the secret key to control your bot, don't let someone access it.
