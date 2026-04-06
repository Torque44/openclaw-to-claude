# OpenClaw to Claude Code Migration Prompt

Paste this entire prompt into Claude Code (terminal, desktop app, or IDE):

---

I want to migrate my OpenClaw Telegram bot(s) to run on Claude Code using my Claude subscription instead of API tokens.

Clone this repo and run the migration: https://github.com/Torque44/openclaw-to-claude

Specifically:

1. Clone the repo to ~/claude-agents/openclaw-migration/
2. Read the MIGRATION-GUIDE.md to understand the full process
3. Discover all my OpenClaw profiles at ~/.openclaw and ~/.openclaw-*/
4. For each profile found:
   - Extract Telegram bot token and allowlist from openclaw.json (channels.telegram section)
   - Extract persona files from workspace/ (SOUL.md, MEMORY.md, IDENTITY.md, USER.md, and any MEMORY-*.md splits)
   - Extract cron jobs from cron/jobs.json (both cron expressions and interval-based schedules, get prompts from payload.text OR payload.message)
   - Extract MCP server configs from workspace/config/mcporter.json if it exists
   - Extract browser config from openclaw.json browser section if it exists
   - Copy skills from workspace/skills/ to the working directory
   - Assemble a CLAUDE.md system prompt from the persona files
5. Install Python dependencies: claude-agent-sdk, python-telegram-bot, apscheduler
6. Install ffmpeg if not present (for video/audio processing)
7. Run migrate.py to generate config.json
8. Set up bridge.py as the main service
9. Create a launchd plist (macOS) or systemd service (Linux) for auto-start
10. Test by starting bridge.py and verifying both bots respond on Telegram
11. Once verified, stop the old OpenClaw LaunchAgents/services

Important context:
- The bridge uses my Claude subscription (Pro/Max), NOT API tokens. Zero extra cost.
- Each Telegram message spawns a claude CLI process via the claude-agent-sdk
- Sessions persist per chat_id so conversations continue across restarts
- MCP servers from OpenClaw (Kite, OpenBB, finance tools, etc.) get converted to Claude Code format
- Browser access uses Playwright with my Chrome profile (all logins preserved)
- Skills are SKILL.md files - Claude Code reads them from the working directory
- Cron jobs run via APScheduler inside the bridge process
- The bridge handles photos, videos, voice messages, documents (downloads to Inbox/)
- Em dashes get stripped from responses (common persona rule)
- /reset, /deep, /fast, /status commands are available in Telegram

If I have an Obsidian vault I want to use as the working directory, ask me for the path. Otherwise use the OpenClaw workspace directory as-is.

Do NOT ask me to run commands manually. You have full access - discover, extract, install, configure, test, and cut over. Just tell me when to verify on Telegram.
