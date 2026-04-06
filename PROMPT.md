# OpenClaw to Claude Code Migration Prompt

Fill in the blanks below, then paste the entire prompt into Claude Code:

---

## My Setup

- **Bot name(s):** ___________ (e.g., Kai, Sheldon, Jarvis - the names from your IDENTITY.md)
- **Telegram bot username(s):** ___________ (e.g., @mybot, @my_second_bot)
- **Claude subscription:** ___________ (Pro $20/mo / Max 5x $100/mo / Max 20x $200/mo)
- **Obsidian vault path (optional):** ___________ (e.g., ~/Documents/MyVault - leave blank to use OpenClaw workspace as-is)
- **MCP servers I use:** ___________ (e.g., Kite, OpenBB, Notion, none)
- **Browser sites I need logged in:** ___________ (e.g., FT.com, Bloomberg, Twitter - leave blank if none)
- **Cron jobs to keep:** ___________ (e.g., all / only these: morning briefing, vault sync / none for now)
- **Cron jobs to drop:** ___________ (e.g., tanisha content pipeline, weekly openclaw update)

---

## The Prompt (paste everything below this line into Claude Code)

I want to migrate my OpenClaw Telegram bot(s) to run on Claude Code using my Claude subscription instead of API tokens.

My bots: [FILL IN BOT NAMES AND TELEGRAM USERNAMES]
My Claude subscription: [FILL IN: Pro / Max 5x / Max 20x]
My Obsidian vault: [FILL IN PATH OR "use openclaw workspace"]
MCP servers: [FILL IN OR "none"]
Browser sites needed: [FILL IN OR "none"]
Cron jobs to keep: [FILL IN OR "all"]
Cron jobs to drop: [FILL IN OR "none"]

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
   - Fix any skills that reference "openclaw" - replace with "claude code"
   - Assemble a CLAUDE.md system prompt from the persona files
5. Install Python dependencies: claude-agent-sdk, python-telegram-bot, apscheduler, playwright
6. Install Playwright Chromium browser (for web access with existing Chrome logins)
7. Install ffmpeg if not present (for video/audio processing)
8. Run migrate.py to generate config.json
9. Review the generated config.json - filter cron jobs based on my preferences above
10. Set up bridge.py as the main service
11. Create a launchd plist (macOS) or systemd service (Linux) for auto-start
12. Set up ~/.claude/settings.json with full permissions:
    ```json
    {
      "permissions": {
        "allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "Glob(*)", "Grep(*)", "WebSearch(*)", "WebFetch(*)", "Agent(*)", "mcp__*"],
        "deny": []
      }
    }
    ```
13. If I specified MCP servers above, add them to ~/.claude/settings.json under mcpServers
14. If I specified browser sites, set up Playwright with my Chrome profile so all logins are preserved
15. If I specified an Obsidian vault, use that as the working directory and copy persona + skills there
16. Test by starting bridge.py and verifying bots respond on Telegram
17. Once verified, stop the old OpenClaw LaunchAgents/services

Important context:
- The bridge uses my Claude subscription (Pro/Max), NOT API tokens. Zero extra cost.
- Each Telegram message spawns a claude CLI process via the claude-agent-sdk
- Sessions persist per chat_id so conversations continue across restarts
- bridge.py v2 has: generation counter (/stop actually works), atomic session writes, message queue (no lost messages), auto-retry on session failures, startup orphan cleanup
- MCP servers from OpenClaw get converted to Claude Code format automatically
- Browser access uses Playwright with my Chrome profile (all cookies/logins preserved)
- Skills are SKILL.md files - Claude Code reads them from the working directory
- Cron jobs run via APScheduler inside the bridge process
- The bridge handles photos, videos, voice messages, documents (downloads to Inbox/)
- Em dashes get stripped from all responses automatically
- Available Telegram commands: /go /wait /stop /clear /reset /deep /fast /status

Do NOT ask me to run commands manually. You have full access - discover, extract, install, configure, test, and cut over. Just tell me when to verify on Telegram.
