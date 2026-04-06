#!/usr/bin/env python3
"""
Claude Code Telegram Bridge
Generic multi-bot bridge that runs any number of personas via Claude Agent SDK.
Reads config.json produced by migrate.py.

Features:
- Multi-bot: run multiple Telegram bots from one process
- Session persistence: conversations continue across restarts
- Media handling: photos, videos, voice, documents, audio
- Cron jobs: migrated from OpenClaw, runs via APScheduler
- Em dash stripping: automatic post-processing
- Skills: inherited from workspace directory
"""

import asyncio
import json
import logging
import os
import signal
import shutil
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AGENTS_DIR = Path(os.environ.get("AGENTS_DIR", str(Path.home() / "claude-agents")))
CONFIG_FILE = AGENTS_DIR / "config.json"

# Find claude CLI
CLAUDE_CLI = None
for candidate in [
    Path.home() / ".local/share/fnm/node-versions" / os.environ.get("FNM_NODE_VERSION", ""),
    Path.home() / ".local/share/fnm",
    Path("/usr/local/bin"),
    Path("/opt/homebrew/bin"),
]:
    # Search for claude binary
    if candidate.is_dir():
        for p in candidate.rglob("claude"):
            if p.is_file() and os.access(p, os.X_OK):
                CLAUDE_CLI = p
                break
    if CLAUDE_CLI:
        break

# Fallback: use shutil.which
if not CLAUDE_CLI:
    found = shutil.which("claude")
    if found:
        CLAUDE_CLI = Path(found)

# Per-bot MCP configs (loaded at startup)
_bot_mcp_configs: dict[str, dict] = {}

SESSIONS_DIR = AGENTS_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

LOG_DIR = AGENTS_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bridge.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bridge")

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def _session_file(bot_name: str) -> Path:
    return SESSIONS_DIR / f"{bot_name}_sessions.json"

def load_sessions(bot_name: str) -> dict:
    f = _session_file(bot_name)
    return json.loads(f.read_text()) if f.exists() else {}

def save_sessions(bot_name: str, sessions: dict):
    _session_file(bot_name).write_text(json.dumps(sessions, indent=2))

# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

async def download_file(bot: Bot, file_id: str, filename: str, inbox: Path) -> Path | None:
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        tg_file = await bot.get_file(file_id)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        local_path = inbox / f"{ts}-{filename}"
        await tg_file.download_to_drive(str(local_path))
        log.info(f"Downloaded {local_path} ({local_path.stat().st_size} bytes)")
        return local_path
    except Exception as e:
        log.warning(f"Download failed: {e}")
        try:
            tg_file = await bot.get_file(file_id)
            if tg_file.file_path and tg_file.file_path.startswith("http"):
                import urllib.request
                ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                local_path = inbox / f"{ts}-{filename}"
                urllib.request.urlretrieve(tg_file.file_path, str(local_path))
                return local_path
        except Exception as e2:
            log.error(f"All download methods failed: {e2}")
        return None

# ---------------------------------------------------------------------------
# Claude query
# ---------------------------------------------------------------------------

_busy: set[str] = set()

async def ask_claude(bot_name: str, cwd: str, model: str, chat_id: int, prompt: str) -> str:
    key = f"{bot_name}:{chat_id}"
    if key in _busy:
        return "(Still working on your previous message.)"
    _busy.add(key)
    try:
        sessions = load_sessions(bot_name)
        existing_sid = sessions.get(str(chat_id))

        opts = ClaudeAgentOptions(
            cwd=cwd,
            model=model,
            permission_mode="bypassPermissions",
            max_turns=25,
        )
        if CLAUDE_CLI:
            opts.cli_path = str(CLAUDE_CLI)
        if existing_sid:
            opts.resume = existing_sid

        # Load MCP servers if configured for this bot
        mcp_config = _bot_mcp_configs.get(bot_name)
        if mcp_config:
            opts.mcp_servers = mcp_config

        result = ""
        new_sid = None

        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, ResultMessage):
                new_sid = msg.session_id
                if msg.result:
                    result = msg.result
                elif msg.is_error:
                    errs = msg.errors or ["unknown"]
                    result = f"Error: {'; '.join(errs)}"
                    # Retry fresh if resume failed
                    if existing_sid and ("session" in result.lower() or "resume" in result.lower()):
                        log.info(f"Resume failed for {key}, retrying fresh")
                        opts.resume = None
                        async for msg2 in query(prompt=prompt, options=opts):
                            if isinstance(msg2, ResultMessage):
                                new_sid = msg2.session_id
                                result = msg2.result or f"Error: {'; '.join(msg2.errors or ['unknown'])}"
                                break
                break

        if new_sid:
            sessions[str(chat_id)] = new_sid
            save_sessions(bot_name, sessions)

        # Strip em dashes
        result = result.replace(" — ", " - ").replace("—", "-")
        return result or "(No response.)"

    except Exception as e:
        log.exception(f"Query failed for {key}")
        return f"(Error: {e})"
    finally:
        _busy.discard(key)

# ---------------------------------------------------------------------------
# Bot factory
# ---------------------------------------------------------------------------

def create_bot_app(bot_cfg: dict) -> Application | None:
    """Create a Telegram Application for one bot."""
    name = bot_cfg["name"]
    bot_name = bot_cfg["bot_name"]
    cwd = bot_cfg["cwd"]
    model = bot_cfg.get("model", "sonnet")

    # Load telegram config
    tg_path = bot_cfg.get("telegram_config")
    if not tg_path or not Path(tg_path).exists():
        log.error(f"[{name}] No telegram config at {tg_path}")
        return None

    tg_cfg = json.loads(Path(tg_path).read_text())
    token = tg_cfg.get("botToken")
    if not token:
        log.error(f"[{name}] No bot token")
        return None

    allow_from = set(str(x) for x in tg_cfg.get("allowFrom", []))
    dm_policy = tg_cfg.get("dmPolicy", "open")
    inbox = Path(cwd) / "Inbox"

    async def handle_msg(update: Update, context):
        if not update.message:
            return

        user_id = str(update.effective_user.id) if update.effective_user else ""
        is_group = update.effective_chat.type in ("group", "supergroup")
        chat_id = update.effective_chat.id

        # DM allowlist
        if not is_group and dm_policy == "allowlist":
            if allow_from and user_id not in allow_from:
                return

        # Build prompt from text + media
        parts = []
        text = update.message.text or update.message.caption or ""

        # Groups: only respond on mention or reply
        if is_group:
            bot_info = await context.bot.get_me()
            mentioned = f"@{bot_info.username}" in text
            is_reply = (
                update.message.reply_to_message
                and update.message.reply_to_message.from_user
                and update.message.reply_to_message.from_user.id == context.bot.id
            )
            if not mentioned and not is_reply:
                return
            if mentioned and bot_info.username:
                text = text.replace(f"@{bot_info.username}", "").strip()

        # Photos
        if update.message.photo:
            photo = update.message.photo[-1]
            path = await download_file(context.bot, photo.file_id, "photo.jpg", inbox)
            if path:
                parts.append(f"[Photo saved at: {path}]")

        # Videos
        if update.message.video:
            vid = update.message.video
            fname = vid.file_name or "video.mp4"
            size_mb = (vid.file_size or 0) / (1024 * 1024)
            path = await download_file(context.bot, vid.file_id, fname, inbox)
            if path:
                parts.append(f"[Video ({size_mb:.1f}MB) saved at: {path}. Use ffmpeg/whisper if needed.]")
            else:
                parts.append(f"[Video ({size_mb:.1f}MB) download failed - too large for Bot API.]")

        # Video notes
        if update.message.video_note:
            path = await download_file(context.bot, update.message.video_note.file_id, "video_note.mp4", inbox)
            if path:
                parts.append(f"[Video note saved at: {path}]")

        # Documents
        if update.message.document:
            doc = update.message.document
            fname = doc.file_name or "document"
            path = await download_file(context.bot, doc.file_id, fname, inbox)
            if path:
                parts.append(f"[Document '{fname}' saved at: {path}]")

        # Voice
        if update.message.voice:
            path = await download_file(context.bot, update.message.voice.file_id, "voice.ogg", inbox)
            if path:
                parts.append(f"[Voice message ({update.message.voice.duration}s) saved at: {path}]")

        # Audio
        if update.message.audio:
            audio = update.message.audio
            fname = audio.file_name or "audio.mp3"
            path = await download_file(context.bot, audio.file_id, fname, inbox)
            if path:
                parts.append(f"[Audio '{fname}' saved at: {path}]")

        # Stickers
        if update.message.sticker:
            parts.append(f"[Sticker: {update.message.sticker.emoji or '?'}]")

        if text:
            parts.insert(0, text)

        prompt = "\n".join(parts)
        if not prompt.strip():
            return

        log.info(f"[{name}] From {user_id}: {prompt[:80]}")
        await update.message.chat.send_action("typing")

        # Check model override
        current_model = context.chat_data.get("model_override", model)
        reply = await ask_claude(name, cwd, current_model, chat_id, prompt)

        for i in range(0, len(reply), 4096):
            await update.message.reply_text(reply[i:i + 4096])

    async def cmd_reset(update: Update, context):
        if not update.message:
            return
        s = load_sessions(name)
        s.pop(str(update.effective_chat.id), None)
        save_sessions(name, s)
        await update.message.reply_text("Session reset.")

    async def cmd_deep(update: Update, context):
        if not update.message:
            return
        context.chat_data["model_override"] = "opus"
        await update.message.reply_text("Switched to Opus.")

    async def cmd_fast(update: Update, context):
        if not update.message:
            return
        context.chat_data.pop("model_override", None)
        await update.message.reply_text("Back to Sonnet.")

    async def cmd_status(update: Update, context):
        if not update.message:
            return
        sessions = load_sessions(name)
        sid = sessions.get(str(update.effective_chat.id), "none")
        m = context.chat_data.get("model_override", model)
        await update.message.reply_text(f"{bot_name} | {m} | session: {sid[:8] if sid != 'none' else 'new'}...")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("fast", cmd_fast))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_msg))

    log.info(f"[{name}] Configured: {bot_name} | model={model} | cwd={cwd} | token={token[:10]}...")
    return app

# ---------------------------------------------------------------------------
# Cron job runner
# ---------------------------------------------------------------------------

def setup_cron_jobs(scheduler: AsyncIOScheduler, bots_config: list[dict]):
    """Set up cron jobs from migrated config."""
    for bot_cfg in bots_config:
        name = bot_cfg["name"]
        cwd = bot_cfg["cwd"]
        model = bot_cfg.get("model", "sonnet")
        cron_path = bot_cfg.get("cron_jobs")

        if not cron_path or not Path(cron_path).exists():
            continue

        jobs = json.loads(Path(cron_path).read_text())
        tg_cfg = json.loads(Path(bot_cfg["telegram_config"]).read_text())
        token = tg_cfg.get("botToken")

        for job in jobs:
            if not job.get("enabled"):
                continue

            job_name = job["name"]
            prompt = job.get("prompt", "")
            if not prompt:
                continue

            delivery = job.get("delivery", {})
            delivery_to = delivery.get("to")
            tz = job.get("timezone", "UTC")

            async def run_cron_job(
                _name=name, _cwd=cwd, _model=model, _prompt=prompt,
                _token=token, _to=delivery_to, _job_name=job_name
            ):
                log.info(f"[{_name}] Running cron: {_job_name}")
                try:
                    result = await ask_claude(_name, _cwd, _model, 0, _prompt)

                    # Deliver result via Telegram if configured
                    if _to and _token:
                        bot = Bot(token=_token)
                        for i in range(0, len(result), 4096):
                            await bot.send_message(chat_id=_to, text=result[i:i + 4096])
                        log.info(f"[{_name}] Cron {_job_name} delivered to {_to}")
                    else:
                        log.info(f"[{_name}] Cron {_job_name} completed (no delivery target)")
                except Exception as e:
                    log.exception(f"[{_name}] Cron {_job_name} failed: {e}")

            # Schedule based on type
            if job.get("cron"):
                cron_expr = job["cron"]
                parts = cron_expr.split()
                if len(parts) == 5:
                    scheduler.add_job(
                        run_cron_job,
                        "cron",
                        minute=parts[0], hour=parts[1],
                        day=parts[2], month=parts[3], day_of_week=parts[4],
                        timezone=tz,
                        name=f"{name}:{job_name}",
                        misfire_grace_time=300,
                    )
                    log.info(f"[{name}] Cron scheduled: {job_name} = '{cron_expr}' ({tz})")

            elif job.get("interval_seconds"):
                interval = job["interval_seconds"]
                scheduler.add_job(
                    run_cron_job,
                    "interval",
                    seconds=interval,
                    name=f"{name}:{job_name}",
                    misfire_grace_time=300,
                )
                log.info(f"[{name}] Interval scheduled: {job_name} = every {interval}s")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    if not CONFIG_FILE.exists():
        print(f"Config not found at {CONFIG_FILE}")
        print("Run migrate.py first to generate it.")
        sys.exit(1)

    config = json.loads(CONFIG_FILE.read_text())
    bots = config.get("bots", [])

    if not bots:
        print("No bots in config.json")
        sys.exit(1)

    if not CLAUDE_CLI:
        print("Claude CLI not found. Install Claude Code first: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    log.info(f"Claude CLI: {CLAUDE_CLI}")
    log.info(f"Starting bridge with {len(bots)} bot(s)")

    # Load MCP configs per bot
    for bot_cfg in bots:
        mcp_path = bot_cfg.get("mcp_servers")
        if mcp_path and Path(mcp_path).exists():
            mcp_data = json.loads(Path(mcp_path).read_text())
            _bot_mcp_configs[bot_cfg["name"]] = mcp_data.get("mcpServers", {})
            log.info(f"[{bot_cfg['name']}] Loaded {len(_bot_mcp_configs[bot_cfg['name']])} MCP servers")

    # Set up cron scheduler
    scheduler = AsyncIOScheduler()
    setup_cron_jobs(scheduler, bots)
    scheduler.start()
    log.info(f"Scheduler started with {len(scheduler.get_jobs())} jobs")

    # Create and start bot apps
    apps = []
    for bot_cfg in bots:
        app = create_bot_app(bot_cfg)
        if app:
            apps.append((bot_cfg["name"], app))

    if not apps:
        print("No bots could be configured.")
        sys.exit(1)

    for name, app in apps:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info(f"[{name}] Polling started")

    log.info(f"Bridge running. {len(apps)} bot(s), {len(scheduler.get_jobs())} cron job(s). Ctrl+C to stop.")

    # Wait for shutdown
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)
    await stop.wait()

    log.info("Shutting down...")
    scheduler.shutdown(wait=False)
    for name, app in apps:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info(f"[{name}] Stopped")


if __name__ == "__main__":
    import sys
    asyncio.run(main())
