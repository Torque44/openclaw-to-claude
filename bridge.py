#!/usr/bin/env python3
"""
Claude Code Telegram Bridge v2
Production-reliable multi-bot bridge. Fixes all v1 issues:
- Generation counter stops stale results from being delivered
- Atomic session file writes (no corruption on crash)
- Message queue (no lost messages during active queries)
- Cron jobs don't block each other
- Startup orphan cleanup
- Proper /stop that actually stops
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from telegram import Bot, BotCommand, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AGENTS_DIR = Path(os.environ.get("AGENTS_DIR", str(Path.home() / "claude-agents")))
CONFIG_FILE = AGENTS_DIR / "config.json"
SESSIONS_DIR = AGENTS_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
LOG_DIR = AGENTS_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

DEBOUNCE_SECONDS = 4
WAIT_SECONDS = 30
MAX_BUFFER_MESSAGES = 100
MAX_STOPPED_PROMPTS = 50

# Find claude CLI
CLAUDE_CLI = shutil.which("claude")
if not CLAUDE_CLI:
    for p in (Path.home() / ".local/share/fnm").rglob("claude"):
        if p.is_file() and os.access(p, os.X_OK):
            CLAUDE_CLI = str(p)
            break

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

# Suppress noisy httpx logs
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Atomic session persistence
# ---------------------------------------------------------------------------

def load_sessions(bot_name: str) -> dict:
    f = SESSIONS_DIR / f"{bot_name}.json"
    try:
        if f.exists():
            return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning(f"Corrupt session file {f}, resetting")
    return {}


def save_sessions(bot_name: str, sessions: dict):
    f = SESSIONS_DIR / f"{bot_name}.json"
    tmp = f.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(sessions, indent=2))
        os.rename(tmp, f)
    except OSError as e:
        log.error(f"Failed to save sessions for {bot_name}: {e}")


# ---------------------------------------------------------------------------
# Complexity detection
# ---------------------------------------------------------------------------

COMPLEX_KEYWORDS = [
    "research", "find everything", "analyze", "create a", "build a", "write a",
    "generate", "scrape", "search twitter", "search x", "deep dive", "compare",
    "make a", "draft a", "compile", "investigate", "report on", "summarize all",
    "from twitter", "from polymarket", "from linkedin", "cv", "resume", "cover letter",
    "content plan", "strategy", "audit", "breakdown", "pipeline",
]


def _looks_complex(prompt: str) -> bool:
    lower = prompt.lower()
    if len(prompt) > 200:
        return True
    return any(kw in lower for kw in COMPLEX_KEYWORDS)


# ---------------------------------------------------------------------------
# Quick acknowledgment (Haiku, no tools, 1 turn)
# ---------------------------------------------------------------------------

async def quick_ack(cwd: str, prompt: str) -> str:
    ack_prompt = (
        f'User sent this task: "{prompt}"\n\n'
        "Reply with ONLY a casual one-line acknowledgment with a rough time estimate. "
        "Keep it short, natural, lowercase. No emoji. No details. Just acknowledge and estimate."
    )
    opts = ClaudeAgentOptions(
        cwd=cwd,
        model="haiku",
        permission_mode="bypassPermissions",
        max_turns=1,
        extra_args={"dangerously-skip-permissions": None},
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                          "WebSearch", "WebFetch", "Agent"],
    )
    if CLAUDE_CLI:
        opts.cli_path = CLAUDE_CLI
    try:
        async for msg in query(prompt=ack_prompt, options=opts):
            if isinstance(msg, ResultMessage) and msg.result:
                return msg.result.replace(" — ", " - ").replace("—", "-")
    except Exception:
        pass
    return "on it. give me a minute."


# ---------------------------------------------------------------------------
# Claude query (cancellation-aware, auto-retry on resume failure)
# ---------------------------------------------------------------------------

async def ask_claude(bot_name: str, cwd: str, model: str, chat_id: int,
                     prompt: str, generation: int, gen_counter: dict,
                     mcp_config: dict | None = None) -> str:
    """
    Query Claude. Returns empty string if cancelled (generation changed).
    gen_counter[chat_id] is checked periodically; if it no longer matches
    `generation`, the query is stale and should be abandoned.
    """
    sessions = load_sessions(bot_name)
    existing_sid = sessions.get(str(chat_id))

    opts = ClaudeAgentOptions(
        cwd=cwd,
        model=model,
        permission_mode="bypassPermissions",
        max_turns=25,
        extra_args={"dangerously-skip-permissions": None},
    )
    if CLAUDE_CLI:
        opts.cli_path = CLAUDE_CLI
    if existing_sid:
        opts.resume = existing_sid
    if mcp_config:
        opts.mcp_servers = mcp_config

    def is_stale():
        return gen_counter.get(chat_id, 0) != generation

    async def _run_query(resume: bool) -> tuple[str, str | None]:
        """Execute one query attempt. Returns (result_text, session_id)."""
        if not resume:
            opts.resume = None

        result = ""
        new_sid = None
        try:
            async for msg in query(prompt=prompt, options=opts):
                if is_stale():
                    log.info(f"[{bot_name}] Stale query (gen {generation}) abandoned for {chat_id}")
                    return "", None
                if isinstance(msg, ResultMessage):
                    new_sid = msg.session_id
                    if msg.result:
                        result = msg.result
                    elif msg.is_error:
                        result = f"__ERROR__:{'; '.join(msg.errors or ['unknown'])}"
                    break
        except asyncio.CancelledError:
            return "", None
        except Exception as e:
            result = f"__ERROR__:{e}"
        return result, new_sid

    # Attempt 1: with resume (if we have a session)
    result, new_sid = await _run_query(resume=bool(existing_sid))

    # If resume failed, retry fresh
    if result.startswith("__ERROR__:") and existing_sid and not is_stale():
        log.info(f"[{bot_name}] Resume failed for {chat_id}, retrying fresh: {result[10:80]}")
        sessions.pop(str(chat_id), None)
        save_sessions(bot_name, sessions)
        result, new_sid = await _run_query(resume=False)

    # If still an error, clean it up for display
    if result.startswith("__ERROR__:"):
        result = f"(Error: {result[10:]})"

    # Save new session
    if new_sid and not is_stale():
        sessions = load_sessions(bot_name)
        sessions[str(chat_id)] = new_sid
        save_sessions(bot_name, sessions)

    # Strip em dashes
    result = result.replace(" — ", " - ").replace("—", "-")
    return result or "(No response.)"


# ---------------------------------------------------------------------------
# Per-bot state
# ---------------------------------------------------------------------------

class BotState:
    """All mutable state for one bot, avoiding module-level dicts."""

    def __init__(self, name: str, bot_name: str, cwd: str, model: str,
                 allow_from: set, dm_policy: str, mcp_config: dict | None,
                 bot_username: str = ""):
        self.name = name
        self.bot_name = bot_name
        self.cwd = cwd
        self.model = model
        self.allow_from = allow_from
        self.dm_policy = dm_policy
        self.mcp_config = mcp_config
        self.bot_username = bot_username
        self.inbox = Path(cwd) / "Inbox"
        self.inbox.mkdir(exist_ok=True)

        # Generation counter per chat_id (monotonic, bumped on /stop)
        self.gen_counter: dict[int, int] = {}
        # Active query tasks per chat_id
        self.active: dict[int, asyncio.Task] = {}
        # Debounce buffers per chat_id
        self.buffers: dict[int, dict] = {}
        # Stopped prompts for correction merging
        self.stopped_prompts: dict[int, str] = {}
        # Queued messages that arrived during an active query
        self.queued: dict[int, list] = {}

    def next_gen(self, chat_id: int) -> int:
        self.gen_counter[chat_id] = self.gen_counter.get(chat_id, 0) + 1
        return self.gen_counter[chat_id]

    def current_gen(self, chat_id: int) -> int:
        return self.gen_counter.get(chat_id, 0)


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

async def download_file(bot: Bot, file_id: str, filename: str, inbox: Path) -> Path | None:
    inbox.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    local_path = inbox / f"{ts}-{filename}"
    try:
        tg_file = await bot.get_file(file_id)
        await tg_file.download_to_drive(str(local_path))
        return local_path
    except Exception as e:
        log.warning(f"Download failed for {filename}: {e}")
        return None


# ---------------------------------------------------------------------------
# Extract prompt parts from a message
# ---------------------------------------------------------------------------

async def extract_parts(msg, bot: Bot, inbox: Path) -> list[str]:
    parts = []
    text = msg.text or msg.caption or ""

    if msg.photo:
        path = await download_file(bot, msg.photo[-1].file_id, "photo.jpg", inbox)
        if path:
            parts.append(f"[Photo saved at: {path}]")

    if msg.video:
        vid = msg.video
        fname = vid.file_name or "video.mp4"
        mb = (vid.file_size or 0) / (1024 * 1024)
        path = await download_file(bot, vid.file_id, fname, inbox)
        if path:
            parts.append(f"[Video ({mb:.1f}MB) saved at: {path}. Use ffmpeg/whisper if needed.]")
        else:
            parts.append(f"[Video ({mb:.1f}MB) download failed.]")

    if msg.video_note:
        path = await download_file(bot, msg.video_note.file_id, "videonote.mp4", inbox)
        if path:
            parts.append(f"[Video note saved at: {path}]")

    if msg.document:
        doc = msg.document
        fname = doc.file_name or "document"
        path = await download_file(bot, doc.file_id, fname, inbox)
        if path:
            parts.append(f"[Document '{fname}' saved at: {path}]")

    if msg.voice:
        path = await download_file(bot, msg.voice.file_id, "voice.ogg", inbox)
        if path:
            parts.append(f"[Voice ({msg.voice.duration}s) saved at: {path}]")

    if msg.audio:
        fname = msg.audio.file_name or "audio.mp3"
        path = await download_file(bot, msg.audio.file_id, fname, inbox)
        if path:
            parts.append(f"[Audio '{fname}' saved at: {path}]")

    if msg.sticker:
        parts.append(f"[Sticker: {msg.sticker.emoji or '?'}]")

    if text:
        parts.insert(0, text)

    return parts


# ---------------------------------------------------------------------------
# Bot factory
# ---------------------------------------------------------------------------

def create_bot_app(bot_cfg: dict) -> tuple[Application, BotState] | None:
    name = bot_cfg["name"]
    bot_name = bot_cfg["bot_name"]
    cwd = bot_cfg["cwd"]
    model = bot_cfg.get("model", "sonnet")

    tg_path = bot_cfg.get("telegram_config")
    if not tg_path or not Path(tg_path).exists():
        log.error(f"[{name}] No telegram config")
        return None

    tg_cfg = json.loads(Path(tg_path).read_text())
    token = tg_cfg.get("botToken")
    if not token:
        log.error(f"[{name}] No bot token")
        return None

    allow_from = set(str(x) for x in tg_cfg.get("allowFrom", []))
    dm_policy = tg_cfg.get("dmPolicy", "open")

    # Load MCP config
    mcp_config = None
    mcp_path = bot_cfg.get("mcp_servers")
    if mcp_path and Path(mcp_path).exists():
        try:
            mcp_data = json.loads(Path(mcp_path).read_text())
            mcp_config = mcp_data.get("mcpServers")
        except Exception:
            pass

    state = BotState(name, bot_name, cwd, model, allow_from, dm_policy, mcp_config)

    # ------------------------------------------------------------------
    # Core: flush buffer and run query
    # ------------------------------------------------------------------

    async def flush_and_query(chat_id: int, context, last_update: Update):
        buf = state.buffers.pop(chat_id, None)
        if not buf or not buf["parts"]:
            return

        prompt = "\n".join(buf["parts"])
        if not prompt.strip():
            return

        # Merge with stopped prompt if correction flow
        if chat_id in state.stopped_prompts:
            original = state.stopped_prompts.pop(chat_id)
            prompt = (
                f"ORIGINAL TASK:\n{original}\n\n"
                f"USER CORRECTION:\n{prompt}\n\n"
                f"Redo the original task with this correction applied."
            )

        gen = state.next_gen(chat_id)
        log.info(f"[{name}] gen={gen} flushing {buf['count']} msgs from {chat_id}: {prompt[:80]}")

        # Ack for complex tasks
        if _looks_complex(prompt):
            await last_update.message.chat.send_action("typing")
            ack = await quick_ack(cwd, prompt)
            if state.current_gen(chat_id) == gen:  # still valid
                await last_update.message.reply_text(ack)

        # Typing indicator
        async def keep_typing():
            try:
                while True:
                    await asyncio.sleep(5)
                    if state.current_gen(chat_id) != gen:
                        break
                    await last_update.message.chat.send_action("typing")
            except (asyncio.CancelledError, Exception):
                pass

        typing_task = asyncio.create_task(keep_typing())

        try:
            current_model = context.chat_data.get("model_override", model)
            reply = await ask_claude(
                name, cwd, current_model, chat_id, prompt,
                generation=gen, gen_counter=state.gen_counter,
                mcp_config=mcp_config,
            )

            # Only deliver if this generation is still current
            if state.current_gen(chat_id) != gen:
                log.info(f"[{name}] gen={gen} stale, discarding result for {chat_id}")
                return

            if reply and not reply.startswith("(Error:"):
                for i in range(0, len(reply), 4096):
                    if state.current_gen(chat_id) != gen:
                        break
                    await asyncio.sleep(0.05)  # yield for /stop
                    await last_update.message.reply_text(reply[i:i + 4096])
            elif reply:
                await last_update.message.reply_text(reply)

        except asyncio.CancelledError:
            log.info(f"[{name}] gen={gen} task cancelled for {chat_id}")
        finally:
            typing_task.cancel()
            state.active.pop(chat_id, None)

            # Process queued messages if any
            if chat_id in state.queued and state.queued[chat_id]:
                queued_parts = state.queued.pop(chat_id)
                state.buffers[chat_id] = {
                    "parts": queued_parts,
                    "timer": None,
                    "last_update": last_update,
                    "count": len(queued_parts),
                }
                # Auto-flush queued messages
                await flush_and_query(chat_id, context, last_update)

    # ------------------------------------------------------------------
    # Message handler
    # ------------------------------------------------------------------

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
            mentioned = f"@{state.bot_username}" in text if state.bot_username else False
            is_reply = (
                update.message.reply_to_message
                and update.message.reply_to_message.from_user
                and update.message.reply_to_message.from_user.id == context.bot.id
            )
            if not mentioned and not is_reply:
                return
            if mentioned and state.bot_username:
                text = text.replace(f"@{state.bot_username}", "").strip()
                update.message.text = text

        parts = await extract_parts(update.message, context.bot, state.inbox)
        if not parts:
            return

        # If a query is active, queue instead of dropping
        if chat_id in state.active and not state.active[chat_id].done():
            if chat_id not in state.queued:
                state.queued[chat_id] = []
            state.queued[chat_id].extend(parts)
            log.info(f"[{name}] Queued {len(parts)} parts for {chat_id} (query active)")
            return

        # Add to debounce buffer
        if chat_id not in state.buffers:
            state.buffers[chat_id] = {
                "parts": [], "timer": None,
                "last_update": update, "count": 0,
            }

        buf = state.buffers[chat_id]
        if buf["count"] < MAX_BUFFER_MESSAGES:
            buf["parts"].extend(parts)
        buf["last_update"] = update
        buf["count"] += 1

        # Reset debounce timer
        if buf["timer"] is not None:
            buf["timer"].cancel()

        loop = asyncio.get_running_loop()
        buf["timer"] = loop.call_later(
            DEBOUNCE_SECONDS,
            lambda: asyncio.create_task(_start_query(chat_id, context, update)),
        )
        await update.message.chat.send_action("typing")

    async def _start_query(chat_id: int, context, update: Update):
        task = asyncio.create_task(flush_and_query(chat_id, context, update))
        state.active[chat_id] = task

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def cmd_go(update: Update, context):
        if not update.message:
            return
        chat_id = update.effective_chat.id
        if chat_id in state.buffers and state.buffers[chat_id]["parts"]:
            buf = state.buffers[chat_id]
            if buf["timer"]:
                buf["timer"].cancel()
            await _start_query(chat_id, context, update)
        else:
            await update.message.reply_text("Nothing in buffer.")

    async def cmd_wait(update: Update, context):
        if not update.message:
            return
        chat_id = update.effective_chat.id
        if chat_id in state.buffers:
            buf = state.buffers[chat_id]
            if buf["timer"]:
                buf["timer"].cancel()
            loop = asyncio.get_running_loop()
            buf["timer"] = loop.call_later(
                WAIT_SECONDS,
                lambda: asyncio.create_task(_start_query(chat_id, context, update)),
            )
            await update.message.reply_text(f"Waiting. {buf['count']} msgs buffered. /go when done.")
        else:
            await update.message.reply_text("No messages buffered. Just start sending.")

    async def cmd_stop(update: Update, context):
        if not update.message:
            return
        chat_id = update.effective_chat.id
        stopped = False

        # Cancel active query by bumping generation (stale results auto-discarded)
        if chat_id in state.active:
            task = state.active[chat_id]
            old_gen = state.current_gen(chat_id)
            state.next_gen(chat_id)  # bump generation
            if not task.done():
                task.cancel()
            state.active.pop(chat_id, None)
            # Kill orphaned claude processes
            try:
                subprocess.run(["pkill", "-f", "claude.*--session-id"],
                               capture_output=True, timeout=3)
            except Exception:
                pass
            state.stopped_prompts[chat_id] = state.buffers.get(chat_id, {}).get("prompt", "")
            # Trim stopped_prompts
            while len(state.stopped_prompts) > MAX_STOPPED_PROMPTS:
                state.stopped_prompts.pop(next(iter(state.stopped_prompts)))
            stopped = True

        # Clear queued messages
        state.queued.pop(chat_id, None)

        # Cancel buffer
        if chat_id in state.buffers:
            buf = state.buffers[chat_id]
            if buf["timer"]:
                buf["timer"].cancel()
            # Save the buffered prompt for correction
            if buf["parts"]:
                state.stopped_prompts[chat_id] = "\n".join(buf["parts"])
            state.buffers.pop(chat_id)
            stopped = True

        if stopped:
            await update.message.reply_text("Stopped. What went wrong?")
        else:
            await update.message.reply_text("Nothing running.")

    async def cmd_clear(update: Update, context):
        if not update.message:
            return
        chat_id = update.effective_chat.id
        if chat_id in state.buffers:
            buf = state.buffers[chat_id]
            if buf["timer"]:
                buf["timer"].cancel()
            state.buffers.pop(chat_id)
        state.stopped_prompts.pop(chat_id, None)
        state.queued.pop(chat_id, None)
        await update.message.reply_text("Cleared.")

    async def cmd_reset(update: Update, context):
        if not update.message:
            return
        chat_id = update.effective_chat.id
        sessions = load_sessions(name)
        sessions.pop(str(chat_id), None)
        save_sessions(name, sessions)
        state.next_gen(chat_id)  # invalidate any active query
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
        chat_id = update.effective_chat.id
        sessions = load_sessions(name)
        sid = sessions.get(str(chat_id), "new")
        m = context.chat_data.get("model_override", model)
        active = "yes" if chat_id in state.active and not state.active[chat_id].done() else "no"
        buf_count = state.buffers.get(chat_id, {}).get("count", 0)
        await update.message.reply_text(
            f"{bot_name} | {m} | session: {sid[:8]}... | active: {active} | buffered: {buf_count}"
        )

    # Build app
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

    log.info(f"[{name}] Configured: {bot_name} | model={model} | cwd={cwd}")
    return app, state


# ---------------------------------------------------------------------------
# Cron job runner
# ---------------------------------------------------------------------------

def setup_cron_jobs(scheduler: AsyncIOScheduler, bots: list[dict], bot_states: dict[str, BotState]):
    for bot_cfg in bots:
        name = bot_cfg["name"]
        cwd = bot_cfg["cwd"]
        model = bot_cfg.get("model", "sonnet")
        cron_path = bot_cfg.get("cron_jobs")

        if not cron_path or not Path(cron_path).exists():
            continue

        jobs = json.loads(Path(cron_path).read_text())
        tg_cfg = json.loads(Path(bot_cfg["telegram_config"]).read_text())
        token = tg_cfg.get("botToken")
        st = bot_states.get(name)
        mcp_config = st.mcp_config if st else None

        for job in jobs:
            if not job.get("enabled"):
                continue
            job_name = job["name"]
            prompt = job.get("prompt", "")
            if not prompt:
                continue

            delivery_to = job.get("delivery", {}).get("to")
            tz = job.get("timezone", "UTC")
            # Unique chat_id per cron job so they don't block each other
            cron_chat_id = abs(hash(f"{name}:{job_name}")) % (10**9) + 1000000

            async def run_job(
                _name=name, _cwd=cwd, _model=model, _prompt=prompt,
                _token=token, _to=delivery_to, _job_name=job_name,
                _chat_id=cron_chat_id, _mcp=mcp_config, _st=st,
            ):
                log.info(f"[{_name}] Cron: {_job_name}")
                try:
                    gen = _st.next_gen(_chat_id) if _st else 0
                    gen_counter = _st.gen_counter if _st else {}
                    result = await ask_claude(
                        _name, _cwd, _model, _chat_id, _prompt,
                        generation=gen, gen_counter=gen_counter, mcp_config=_mcp,
                    )
                    if _to and _token and result:
                        bot = Bot(token=_token)
                        for i in range(0, len(result), 4096):
                            await bot.send_message(chat_id=_to, text=result[i:i + 4096])
                    log.info(f"[{_name}] Cron {_job_name} done ({len(result)} chars)")
                except Exception as e:
                    log.exception(f"[{_name}] Cron {_job_name} failed: {e}")

            if job.get("cron"):
                parts = job["cron"].split()
                if len(parts) == 5:
                    scheduler.add_job(
                        run_job, "cron",
                        minute=parts[0], hour=parts[1], day=parts[2],
                        month=parts[3], day_of_week=parts[4],
                        timezone=tz, name=f"{name}:{job_name}",
                        misfire_grace_time=300,
                    )
                    log.info(f"[{name}] Cron: {job_name} = '{job['cron']}' ({tz})")
            elif job.get("interval_seconds"):
                scheduler.add_job(
                    run_job, "interval", seconds=job["interval_seconds"],
                    name=f"{name}:{job_name}", misfire_grace_time=300,
                )
                log.info(f"[{name}] Interval: {job_name} = every {job['interval_seconds']}s")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    if not CONFIG_FILE.exists():
        log.error(f"Config not found: {CONFIG_FILE}")
        sys.exit(1)

    config = json.loads(CONFIG_FILE.read_text())
    bots_cfg = config.get("bots", [])
    if not bots_cfg:
        log.error("No bots in config")
        sys.exit(1)

    if not CLAUDE_CLI:
        log.error("Claude CLI not found")
        sys.exit(1)

    # Startup: kill orphaned claude processes from previous crash
    try:
        subprocess.run(["pkill", "-f", "claude.*--session-id"], capture_output=True, timeout=3)
    except Exception:
        pass

    log.info(f"Claude CLI: {CLAUDE_CLI}")
    log.info(f"Starting bridge v2 with {len(bots_cfg)} bot(s)")

    # Build bot apps
    apps: list[tuple[str, Application, BotState]] = []
    bot_states: dict[str, BotState] = {}

    for bot_cfg in bots_cfg:
        result = create_bot_app(bot_cfg)
        if result:
            app, state = result
            apps.append((bot_cfg["name"], app, state))
            bot_states[bot_cfg["name"]] = state

    if not apps:
        log.error("No bots configured")
        sys.exit(1)

    # Set up cron
    scheduler = AsyncIOScheduler()
    setup_cron_jobs(scheduler, bots_cfg, bot_states)
    scheduler.start()
    log.info(f"Scheduler: {len(scheduler.get_jobs())} jobs")

    # Telegram commands
    our_commands = [
        BotCommand("go", "Process buffered messages now"),
        BotCommand("wait", "Hold on, more messages coming"),
        BotCommand("stop", "Stop current task"),
        BotCommand("clear", "Clear message buffer"),
        BotCommand("reset", "Fresh conversation"),
        BotCommand("deep", "Switch to Opus"),
        BotCommand("fast", "Switch to Sonnet"),
        BotCommand("status", "Current state"),
    ]

    # Start all bots
    for name, app, state in apps:
        await app.initialize()
        await app.start()
        # Cache bot username
        me = await app.bot.get_me()
        state.bot_username = me.username or ""
        # Register commands (replaces old OpenClaw commands)
        await app.bot.set_my_commands(our_commands)
        await app.updater.start_polling(drop_pending_updates=True)
        log.info(f"[{name}] Polling (@{state.bot_username}), commands registered")

    log.info(f"Bridge v2 running. {len(apps)} bot(s), {len(scheduler.get_jobs())} cron(s). PID={os.getpid()}")

    # Wait for shutdown signal
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    # Graceful shutdown
    log.info("Shutting down...")
    scheduler.shutdown(wait=False)
    for name, app, state in apps:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info(f"[{name}] Stopped")


if __name__ == "__main__":
    asyncio.run(main())
