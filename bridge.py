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

# Keywords that suggest a complex task needing acknowledgment
COMPLEX_KEYWORDS = [
    "research", "find everything", "analyze", "create a", "build a", "write a",
    "generate", "scrape", "search twitter", "search x", "deep dive", "compare",
    "make a", "draft a", "compile", "investigate", "report on", "summarize all",
    "from twitter", "from polymarket", "from linkedin", "cv", "resume", "cover letter",
    "content plan", "strategy", "audit", "breakdown", "pipeline",
]


def _looks_complex(prompt: str) -> bool:
    """Quick heuristic: does this look like a multi-step task?"""
    lower = prompt.lower()
    # Multiple sentences or long prompt
    if len(prompt) > 200:
        return True
    # Contains complex keywords
    matches = sum(1 for kw in COMPLEX_KEYWORDS if kw in lower)
    if matches >= 1:
        return True
    return False


async def quick_ack(bot_name: str, cwd: str, prompt: str) -> str:
    """Fast Claude call (no tools, 1 turn) to get a casual acknowledgment."""
    ack_prompt = (
        f"User sent this task: \"{prompt}\"\n\n"
        "Reply with ONLY a casual one-line acknowledgment with a rough time estimate. "
        "Examples of good responses:\n"
        "- 'give me 3 min, pulling up twitter'\n"
        "- 'on it. 2 min for the research, another minute to write it up'\n"
        "- '5 min. need to dig through a few sources'\n"
        "- 'quick one, 30 sec'\n"
        "Keep it short, natural, lowercase. No emoji. No details about what you'll do. "
        "Just acknowledge and estimate."
    )
    opts = ClaudeAgentOptions(
        cwd=cwd,
        model="haiku",  # Fastest/cheapest model for ack
        permission_mode="bypassPermissions",
        max_turns=1,
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch", "Agent"],
    )
    if CLAUDE_CLI:
        opts.cli_path = str(CLAUDE_CLI)

    try:
        async for msg in query(prompt=ack_prompt, options=opts):
            if isinstance(msg, ResultMessage):
                if msg.result:
                    result = msg.result.replace(" — ", " - ").replace("—", "-")
                    return result
                break
    except Exception:
        pass
    return "on it. give me a minute."


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

    # ---- Message buffer: collects multi-message input ----
    DEBOUNCE_SECONDS = 4  # Wait 4s of silence before processing
    WAIT_SECONDS = 30     # Extended wait when user sends /wait
    _msg_buffers: dict[int, dict] = {}

    # ---- Active query tracking (for /stop) ----
    # chat_id -> {"task": asyncio.Task, "prompt": str, "cancelled": bool}
    _active_queries: dict[int, dict] = {}
    # chat_id -> str (last prompt that was stopped, for correction merging)
    _stopped_prompts: dict[int, str] = {}

    async def _extract_parts(update: Update, context) -> list[str]:
        """Extract text + media from a single message into prompt parts."""
        parts = []
        text = update.message.text or update.message.caption or ""

        if update.message.photo:
            photo = update.message.photo[-1]
            path = await download_file(context.bot, photo.file_id, "photo.jpg", inbox)
            if path:
                parts.append(f"[Photo saved at: {path}]")

        if update.message.video:
            vid = update.message.video
            fname = vid.file_name or "video.mp4"
            size_mb = (vid.file_size or 0) / (1024 * 1024)
            path = await download_file(context.bot, vid.file_id, fname, inbox)
            if path:
                parts.append(f"[Video ({size_mb:.1f}MB) saved at: {path}. Use ffmpeg/whisper if needed.]")
            else:
                parts.append(f"[Video ({size_mb:.1f}MB) download failed - too large for Bot API.]")

        if update.message.video_note:
            path = await download_file(context.bot, update.message.video_note.file_id, "video_note.mp4", inbox)
            if path:
                parts.append(f"[Video note saved at: {path}]")

        if update.message.document:
            doc = update.message.document
            fname = doc.file_name or "document"
            path = await download_file(context.bot, doc.file_id, fname, inbox)
            if path:
                parts.append(f"[Document '{fname}' saved at: {path}]")

        if update.message.voice:
            path = await download_file(context.bot, update.message.voice.file_id, "voice.ogg", inbox)
            if path:
                parts.append(f"[Voice message ({update.message.voice.duration}s) saved at: {path}]")

        if update.message.audio:
            audio = update.message.audio
            fname = audio.file_name or "audio.mp3"
            path = await download_file(context.bot, audio.file_id, fname, inbox)
            if path:
                parts.append(f"[Audio '{fname}' saved at: {path}]")

        if update.message.sticker:
            parts.append(f"[Sticker: {update.message.sticker.emoji or '?'}]")

        if text:
            parts.insert(0, text)

        return parts

    async def _flush_buffer(chat_id: int, context):
        """Called after debounce timer expires. Combines all buffered messages and sends to Claude."""
        buf = _msg_buffers.pop(chat_id, None)
        if not buf or not buf["parts"]:
            return

        last_update = buf["last_update"]
        all_parts = buf["parts"]

        # Combine all buffered message parts into one prompt
        prompt = "\n".join(all_parts)
        if not prompt.strip():
            return

        # If there's a stopped prompt, merge correction with original task
        if chat_id in _stopped_prompts:
            original = _stopped_prompts.pop(chat_id)
            prompt = (
                f"ORIGINAL TASK:\n{original}\n\n"
                f"USER CORRECTION (they stopped me and said):\n{prompt}\n\n"
                f"Redo the original task with this correction applied."
            )
            log.info(f"[{name}] Merging correction with stopped task for {chat_id}")

        msg_count = buf["msg_count"]
        log.info(f"[{name}] Flushing {msg_count} buffered messages from {chat_id}: {prompt[:100]}")

        # For complex tasks: send quick ack first
        is_complex = _looks_complex(prompt)
        if is_complex:
            await last_update.message.chat.send_action("typing")
            ack = await quick_ack(name, cwd, prompt)
            await last_update.message.reply_text(ack)
            log.info(f"[{name}] Ack sent: {ack}")

        # Keep sending typing indicator
        async def keep_typing():
            try:
                while True:
                    await asyncio.sleep(5)
                    await last_update.message.chat.send_action("typing")
            except asyncio.CancelledError:
                pass

        typing_task = asyncio.create_task(keep_typing())

        async def _run_query():
            try:
                current_model = context.chat_data.get("model_override", model)
                reply = await ask_claude(name, cwd, current_model, chat_id, prompt)
                for i in range(0, len(reply), 4096):
                    await last_update.message.reply_text(reply[i:i + 4096])
            except asyncio.CancelledError:
                log.info(f"[{name}] Query cancelled for {chat_id}")
            finally:
                typing_task.cancel()
                _active_queries.pop(chat_id, None)

        # Track active query so /stop can cancel it
        task = asyncio.create_task(_run_query())
        _active_queries[chat_id] = {"task": task, "prompt": prompt, "cancelled": False}

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
                # Re-set text so _extract_parts picks up cleaned version
                update.message.text = text

        # Extract parts from this message
        parts = await _extract_parts(update, context)
        if not parts:
            return

        # Add to buffer
        if chat_id not in _msg_buffers:
            _msg_buffers[chat_id] = {
                "parts": [],
                "timer": None,
                "last_update": update,
                "msg_count": 0,
                "context": context,
            }

        _msg_buffers[chat_id]["parts"].extend(parts)
        _msg_buffers[chat_id]["last_update"] = update
        _msg_buffers[chat_id]["msg_count"] += 1

        # Cancel previous timer
        if _msg_buffers[chat_id]["timer"] is not None:
            _msg_buffers[chat_id]["timer"].cancel()

        # Start new debounce timer
        _msg_buffers[chat_id]["timer"] = asyncio.get_event_loop().call_later(
            DEBOUNCE_SECONDS,
            lambda cid=chat_id, ctx=context: asyncio.create_task(_flush_buffer(cid, ctx)),
        )

        # Show typing while collecting messages
        await update.message.chat.send_action("typing")

    async def cmd_go(update: Update, context):
        """Force-flush the buffer immediately. Don't wait for the timer."""
        if not update.message:
            return
        chat_id = update.effective_chat.id
        if chat_id in _msg_buffers and _msg_buffers[chat_id]["parts"]:
            if _msg_buffers[chat_id]["timer"] is not None:
                _msg_buffers[chat_id]["timer"].cancel()
            await _flush_buffer(chat_id, context)
        else:
            await update.message.reply_text("Nothing in buffer.")

    async def cmd_wait(update: Update, context):
        """Extend the buffer timer. Tells bot 'more messages coming, hold on'."""
        if not update.message:
            return
        chat_id = update.effective_chat.id
        if chat_id in _msg_buffers:
            # Cancel current timer, set a longer one
            if _msg_buffers[chat_id]["timer"] is not None:
                _msg_buffers[chat_id]["timer"].cancel()
            _msg_buffers[chat_id]["timer"] = asyncio.get_event_loop().call_later(
                WAIT_SECONDS,
                lambda cid=chat_id, ctx=context: asyncio.create_task(_flush_buffer(cid, ctx)),
            )
            count = _msg_buffers[chat_id]["msg_count"]
            await update.message.reply_text(f"Waiting. {count} messages buffered. Send /go when done.")
        else:
            await update.message.reply_text("No messages buffered yet. Just start sending.")

    async def cmd_stop(update: Update, context):
        """Stop the current running query. Bot waits for correction."""
        if not update.message:
            return
        chat_id = update.effective_chat.id

        # Cancel active query if running
        if chat_id in _active_queries:
            aq = _active_queries[chat_id]
            if aq["task"] and not aq["task"].done():
                aq["task"].cancel()
                aq["cancelled"] = True
                _stopped_prompts[chat_id] = aq["prompt"]
                await update.message.reply_text("Stopped. What's wrong? Send your correction, I'll redo it.")
                log.info(f"[{name}] Query stopped by user in {chat_id}")
                return

        # Cancel buffer if waiting
        if chat_id in _msg_buffers:
            if _msg_buffers[chat_id]["timer"] is not None:
                _msg_buffers[chat_id]["timer"].cancel()
            count = _msg_buffers[chat_id]["msg_count"]
            del _msg_buffers[chat_id]
            await update.message.reply_text(f"Cleared {count} buffered messages.")
            return

        await update.message.reply_text("Nothing running.")

    async def cmd_clear(update: Update, context):
        """Clear buffer and forget any stopped task."""
        if not update.message:
            return
        chat_id = update.effective_chat.id
        cleared = False
        if chat_id in _msg_buffers:
            if _msg_buffers[chat_id]["timer"] is not None:
                _msg_buffers[chat_id]["timer"].cancel()
            del _msg_buffers[chat_id]
            cleared = True
        if chat_id in _stopped_prompts:
            del _stopped_prompts[chat_id]
            cleared = True
        await update.message.reply_text("Cleared." if cleared else "Nothing to clear.")

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
    app.add_handler(CommandHandler("go", cmd_go))
    app.add_handler(CommandHandler("g", cmd_go))
    app.add_handler(CommandHandler("wait", cmd_wait))
    app.add_handler(CommandHandler("w", cmd_wait))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("s", cmd_stop))
    app.add_handler(CommandHandler("clear", cmd_clear))
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
