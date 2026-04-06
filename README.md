# OpenClaw to Claude Code Migration

Migrate your OpenClaw Telegram bots to run on Claude Code. Same persona, same skills, same cron jobs, same MCP servers, same browser access. Zero API token cost (uses your Claude subscription).

## Quick Start

Open Claude Code and paste:

```
I want to migrate my OpenClaw Telegram bot(s) to run on Claude Code using my Claude subscription instead of API tokens.

Clone this repo and run the migration: https://github.com/Torque44/openclaw-to-claude

Read PROMPT.md from the repo for the full instructions, then execute the migration end-to-end. Don't ask me to run commands manually - you handle everything.
```

That's it. Claude Code discovers your OpenClaw setup, extracts everything, installs deps, configures the bridge, and tells you when to test on Telegram.

## What gets migrated

- Persona files (SOUL.md, MEMORY.md, IDENTITY.md, USER.md)
- Telegram bot tokens + allowlists (zero re-pairing)
- All skills (workspace/skills/)
- Cron jobs (schedule + prompts)
- MCP servers (Kite, OpenBB, finance tools, etc.)
- Browser access (Chrome profile with all logins preserved)
- Web search config (Tavily/Brave API keys)

## Prerequisites

- **Claude Code** installed and logged in (`npm install -g @anthropic-ai/claude-code && claude login`)
- **Python 3.10+**
- **Claude subscription** (Pro $20/mo or Max $100/mo)
- **Existing OpenClaw setup** with at least one Telegram bot

## Manual install (alternative)

```bash
git clone https://github.com/Torque44/openclaw-to-claude.git
cd openclaw-to-claude
./install.sh
```

## Manual setup

```bash
# Install deps
pip3 install claude-agent-sdk python-telegram-bot apscheduler

# Run migration
python3 migrate.py

# Start the bridge
python3 bridge.py
```

## What gets migrated

| OpenClaw | Claude Code Bridge |
|---|---|
| `workspace/SOUL.md` | Included in `CLAUDE.md` (system prompt) |
| `workspace/MEMORY.md` | Included in `CLAUDE.md` + read at runtime |
| `workspace/IDENTITY.md` | Included in `CLAUDE.md` |
| `workspace/USER.md` | Included in `CLAUDE.md` |
| `workspace/skills/` | Copied to working directory, auto-discovered |
| `cron/jobs.json` | Converted to APScheduler (cron + interval) |
| `channels.telegram.botToken` | Reused (same bot, zero re-pairing) |
| `channels.telegram.allowFrom` | Reused (same DM allowlist) |

## Telegram commands

| Command | Action |
|---|---|
| `/reset` | Fresh conversation (new session) |
| `/deep` | Switch to Opus 4.6 (deep analysis) |
| `/fast` | Switch back to Sonnet 4.6 |
| `/status` | Show current model + session |

## Media support

Photos, videos, documents, voice messages, audio files all get downloaded to `<workspace>/Inbox/` and the file path is passed to Claude for processing.

Note: Telegram Bot API has a 20MB file download limit. Videos larger than 20MB will fail to download.

## File structure

```
~/claude-agents/
  config.json         # Master config (auto-generated)
  bridge.py           # The bridge (generic, works for any bot)
  migrate.py          # Migration tool
  start.sh            # Start script with correct PATH
  sessions/           # Persistent session IDs per chat
  logs/               # Bridge logs
  <bot-name>/         # Per-bot extracted configs
    telegram.json     # Bot token + allowlist
    cron_jobs.json    # Migrated cron jobs
```

## Multiple bots

The bridge runs any number of bots from one process. Each bot gets its own:
- Telegram polling loop
- Session store
- Working directory (with its own CLAUDE.md)
- Model config
- Cron jobs

## Cost

Zero extra. The Claude Agent SDK spawns `claude` CLI under the hood, which uses your logged-in subscription. No API key needed.

**Heads up:** Bot usage shares the same quota as your own Claude Code terminal sessions. If you hit rate limits:
1. Reduce cron frequency
2. Use Sonnet (lighter on quota than Opus)
3. Upgrade to Max 5x or 20x

## Switching off OpenClaw

After verifying the bridge works:

```bash
# macOS
launchctl unload ~/Library/LaunchAgents/ai.openclaw.*.plist

# Linux
systemctl --user stop openclaw
```

Keep OpenClaw installed as a fallback. You can always `launchctl load` it back.
