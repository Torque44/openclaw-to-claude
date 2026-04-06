# OpenClaw to Claude Code - Complete Migration Guide

Your OpenClaw bot(s) can run on Claude Code using your existing Claude subscription ($20 Pro / $100 Max). Same persona, same skills, same cron jobs, same MCP servers, same browser access. Zero API token cost.

## What You Need

- **Claude Code** installed and logged in (`npm install -g @anthropic-ai/claude-code && claude login`)
- **Python 3.10+**
- **Claude subscription** (Pro or Max - Max 5x recommended for multiple bots)
- **Your existing OpenClaw setup** (we read everything from it automatically)

## Quick Start (3 commands)

```bash
# 1. Clone the migration kit
git clone https://github.com/torque44/openclaw-to-claude.git
cd openclaw-to-claude

# 2. Run the installer (discovers everything, installs deps, migrates)
chmod +x install.sh && ./install.sh

# 3. That's it. Your bots are running on Telegram via Claude Code.
```

## What Gets Migrated (automatically)

| What | From (OpenClaw) | To (Claude Code) |
|------|----------------|-------------------|
| **Persona** | SOUL.md, MEMORY.md, IDENTITY.md, USER.md | Assembled into CLAUDE.md (system prompt) |
| **Telegram bot** | channels.telegram in openclaw.json | Reused (same token, zero re-pairing) |
| **DM allowlist** | telegram.allowFrom | Reused |
| **Group behavior** | requireMention, groupPolicy | Reused |
| **Skills** | workspace/skills/*.SKILL.md | Copied to working directory (auto-discovered) |
| **Cron jobs** | cron/jobs.json (cron + interval) | APScheduler (same schedule, same prompts) |
| **MCP servers** | workspace/config/mcporter.json | Converted to Claude Code MCP format |
| **Browser** | browser.profiles (CDP) | Playwright with Chrome profile OR CDP |
| **Web search** | Tavily/Brave plugin | Same API key, or Claude Code built-in WebSearch |
| **Model config** | models.primary | Sonnet default, /deep for Opus |
| **Memory files** | MEMORY-*.md splits | Read at session start from working directory |

## For Users with Finance MCPs (Kite, OpenBB, etc.)

Your MCP servers migrate automatically. The script reads `workspace/config/mcporter.json` and converts to Claude Code format.

**Before migration:**
```json
// OpenClaw mcporter.json
{
  "mcpServers": {
    "openbb": { "command": "npx -y @openbb/mcp-server" },
    "kite": { "baseUrl": "https://api.kite.trade/mcp", "headers": {"X-Api-Key": "..."} }
  }
}
```

**After migration (auto-generated):**
```json
// ~/claude-agents/<bot>/mcp_servers.json → copy into ~/.claude/settings.json
{
  "mcpServers": {
    "openbb": { "command": "npx", "args": ["-y", "@openbb/mcp-server"] },
    "kite": { "url": "https://api.kite.trade/mcp", "headers": {"X-Api-Key": "..."} }
  }
}
```

**To activate:** Copy the `mcpServers` block into `~/.claude/settings.json`:

```bash
# The migration script outputs the file. Just merge it:
cat ~/claude-agents/<your-bot>/mcp_servers.json
# Copy the mcpServers section into ~/.claude/settings.json
```

Claude Code natively supports both stdio (local) and SSE (remote) MCP servers. Your Kite, OpenBB, Bloomberg, or any other MCP tools work unchanged.

## For Users with Browser/Web Access (FT, Business Times, etc.)

If your OpenClaw bot used Chrome/Tandem browser to read paywalled sites, you have two options:

### Option A: Playwright with Chrome's profile (recommended)

The bridge uses your actual Chrome browser profile (all cookies, logins, sessions). No need to log in again.

```bash
# Already built into the bridge. Works if Chrome is installed.
# Kai/your bot can run:
python3 ~/claude-agents/chatgpt_pipeline.py --prompt "summarize this article" --visible
```

For any paywalled site your bot needs to access, just make sure you're logged in on Chrome. The bridge copies Chrome's cookies when it needs browser access.

### Option B: Chrome with CDP (remote debugging)

If you want the bot to control your live Chrome browser:

```bash
# Quit Chrome first, then relaunch with CDP:
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 &

# Or on Linux:
google-chrome --remote-debugging-port=9222 &
```

Then tell your bot to connect via CDP in its CLAUDE.md:
```
## BROWSER
Chrome is available on CDP port 9222. Use Playwright to connect:
browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
```

### Option C: Add as MCP server

If you used specific browser MCP tools:
```json
{
  "mcpServers": {
    "browser": {
      "command": "npx",
      "args": ["-y", "@anthropic-ai/claude-browser-mcp"]
    }
  }
}
```

## Manual Steps (if needed)

### 1. Review CLAUDE.md

After migration, check the assembled CLAUDE.md in your bot's working directory:
```bash
cat <your-workspace>/CLAUDE.md
```

The migration assembles it from your existing SOUL/MEMORY/IDENTITY/USER files. Edit if anything needs tweaking.

### 2. MCP servers that need API keys

If your MCP servers need API keys, set them as environment variables:
```bash
# Add to ~/claude-agents/start.sh:
export OPENBB_API_KEY="your-key"
export KITE_API_KEY="your-key"
export TAVILY_API_KEY="your-key"
```

### 3. Skills that reference "openclaw"

The migration auto-replaces "openclaw" references in skills. Check if any need manual fixes:
```bash
grep -r "openclaw\|clawhub" ~/claude-agents/*/skills/ 2>/dev/null
```

### 4. Cron jobs with delivery targets

If your cron jobs send results to specific Telegram chats, check the chat IDs in:
```bash
cat ~/claude-agents/<bot>/cron_jobs.json | python3 -m json.tool | grep -A2 delivery
```

## Architecture

```
~/claude-agents/
  bridge.py           # Multi-bot Telegram bridge
  config.json          # Master config (auto-generated)
  start.sh             # Start script with correct PATH
  sessions/            # Persistent chat sessions
  logs/                # Bridge logs
  <bot-name>/
    telegram.json      # Bot token + allowlist
    cron_jobs.json     # Migrated cron jobs
    mcp_servers.json   # MCP server configs
    browser_config.json # Browser/CDP config

<workspace>/           # Your bot's working directory (same as before OR Obsidian vault)
  CLAUDE.md            # System prompt (assembled from persona files)
  SOUL.md              # Personality (unchanged)
  MEMORY.md            # Memory index (unchanged)
  MEMORY-*.md          # Split memory files (unchanged)
  skills/              # All skills (copied)
  Inbox/               # Downloaded media from Telegram
```

## Telegram Commands

| Command | Action |
|---------|--------|
| `/reset` | Fresh conversation (new session) |
| `/deep` | Switch to Opus 4.6 (deep analysis) |
| `/fast` | Switch back to Sonnet 4.6 |
| `/status` | Show current model + session |

## Cost

**Zero extra.** The bridge uses your Claude subscription (Pro $20/mo or Max $100/mo). No API tokens needed.

Your bot usage and your own Claude Code terminal usage share the same quota. If you hit rate limits:
1. Reduce cron frequency
2. Use Sonnet (lighter than Opus)
3. Upgrade to Max 5x ($100/mo) or Max 20x ($200/mo)

## Stopping OpenClaw

After verifying everything works:

```bash
# macOS - stop all OpenClaw LaunchAgents
launchctl unload ~/Library/LaunchAgents/ai.openclaw.*.plist

# Linux
systemctl --user stop openclaw
```

Keep OpenClaw installed as fallback. You can always reload it.

## Troubleshooting

**Bot not responding?**
```bash
tail -50 ~/claude-agents/logs/bridge.log
```

**Session errors?**
```bash
rm ~/claude-agents/sessions/*.json  # Clear sessions
# Restart bridge
```

**MCP server not connecting?**
```bash
# Test the MCP server directly
npx -y @openbb/mcp-server  # Should start without errors
```

**Rate limited?**
```
Check claude.ai/settings/usage for your current usage.
Max 5x gives ~5x Pro limits per 5-hour window.
```

**Cloudflare blocking headless browser?**
```
Use --visible flag (browser opens visibly, does its thing, closes).
ChatGPT and some sites block headless browsers.
```

## FAQ

**Q: Will my memory files actually work?**
A: Yes. Claude reads CLAUDE.md + MEMORY.md from disk at session start. When you or the bot edits these files, the next session picks up the changes. The memory sync cron writes session learnings back to disk before context dies.

**Q: Can I use both OpenClaw and Claude Code?**
A: Not simultaneously for the same Telegram bot (can't have two processes polling the same token). But you can run different bots on different systems.

**Q: What about OpenClaw skills that use specific tools?**
A: Skills are just SKILL.md files (markdown instructions). They work in Claude Code because Claude reads them from disk and follows the instructions. No code changes needed.

**Q: Can I add new MCP servers after migration?**
A: Yes. Add them to `~/.claude/settings.json` under `mcpServers`. Claude Code picks them up automatically.

**Q: What about the OpenClaw dashboard?**
A: If you had `~/shared-team/` dashboard, it keeps working. Both bots keep writing TOON activity logs.
