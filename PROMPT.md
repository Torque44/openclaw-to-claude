# OpenClaw to Claude Code Migration Prompt

Copy everything below and paste it into Claude Code:

---

I want to migrate my OpenClaw Telegram bot(s) to run on Claude Code using my Claude subscription instead of API tokens.

Clone this repo first: https://github.com/Torque44/openclaw-to-claude
Read MIGRATION-GUIDE.md to understand the full process.

Before you start, ask me these questions one by one:

1. What are your bot names and their Telegram usernames? (e.g., "Kai @atclawnbot, Sheldon @sheldontrekbot")
2. Which Claude subscription are you on? (Pro $20/mo, Max 5x $100/mo, or Max 20x $200/mo)
3. Do you have an Obsidian vault you want to use as the working directory? If yes, what's the path? If not, I'll use your OpenClaw workspace as-is.
4. Do you use any MCP servers? (e.g., Kite, OpenBB, Notion, custom ones) If yes, list them. If not, say none.
5. Are there any websites where you need to stay logged in for the bot to access? (e.g., FT.com, Bloomberg, Twitter) If not, say none.
6. Which cron jobs do you want to keep, and which should I drop? I'll show you the full list after I discover your setup.

After I answer, discover all my OpenClaw profiles at ~/.openclaw and ~/.openclaw-*/ and do the full migration:

- Extract Telegram bot tokens and allowlists from openclaw.json
- Extract persona files (SOUL.md, MEMORY.md, IDENTITY.md, USER.md, MEMORY-*.md)
- Extract cron jobs from cron/jobs.json (prompts from payload.text OR payload.message)
- Extract MCP server configs from workspace/config/mcporter.json
- Extract browser config from openclaw.json
- Copy skills from workspace/skills/ and fix any "openclaw" references
- Assemble CLAUDE.md system prompt from persona files
- Install deps: claude-agent-sdk, python-telegram-bot, apscheduler, playwright
- Install Playwright Chromium + ffmpeg
- Run migrate.py to generate config.json
- Filter cron jobs based on my answers
- Set up bridge.py v2 as the main service
- Set up ~/.claude/settings.json with full permissions (Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Agent, mcp - all allowed)
- If I use MCP servers, add them to ~/.claude/settings.json
- If I need browser access, set up Playwright with my Chrome profile
- If I have an Obsidian vault, use it as working directory and copy persona + skills there
- Create launchd (macOS) or systemd (Linux) auto-start service
- Test bridge.py and tell me when to verify on Telegram
- Once I confirm it works, stop the old OpenClaw services

Key facts:
- Uses my Claude subscription, NOT API tokens. Zero extra cost.
- bridge.py v2: generation counter (/stop works), atomic sessions, message queue, auto-retry, orphan cleanup
- Handles photos, videos, voice, documents (downloads to Inbox/)
- Telegram commands: /go /wait /stop /clear /reset /deep /fast /status
- Em dashes auto-stripped from responses

Do NOT ask me to run commands manually. You handle everything. Just tell me when to test on Telegram.
