#!/usr/bin/env python3
"""Telegram bot that forwards messages to Claude Code CLI.

Supports multiple bot instances running in a single process, each with its own
token, authorized users, working directory, and independent sessions.
Configuration is read from bots_config.yaml.
"""

import asyncio
import html as html_mod
import json
import logging
import re
import signal
import sys
import time
from pathlib import Path

import yaml
from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Markdown to Telegram HTML (module-level, no per-bot state) ---

def _extract_tables(text: str) -> tuple[str, list[str]]:
    """Detect markdown tables (consecutive lines with |) and replace with placeholders."""
    lines = text.split("\n")
    tables: list[str] = []
    result_lines: list[str] = []
    i = 0

    while i < len(lines):
        if "|" in lines[i]:
            # Collect consecutive lines containing |
            table_lines: list[str] = []
            while i < len(lines) and "|" in lines[i]:
                table_lines.append(lines[i])
                i += 1

            # It's a real table if there's a separator row like |---|---|
            has_separator = any(
                re.match(r"^[\s|:\-]+$", line) and "--" in line
                for line in table_lines
            )
            if has_separator and len(table_lines) >= 2:
                idx = len(tables)
                tables.append("\n".join(table_lines))
                result_lines.append(f"\x00TBL{idx}\x00")
            else:
                result_lines.extend(table_lines)
        else:
            result_lines.append(lines[i])
            i += 1

    return "\n".join(result_lines), tables


def markdown_to_telegram_html(text: str) -> str:
    """Best-effort conversion of standard markdown to Telegram-compatible HTML."""
    # 1. Extract fenced code blocks to protect their contents
    code_blocks: list[str] = []

    def _save_code_block(m: re.Match) -> str:
        idx = len(code_blocks)
        code_blocks.append(m.group(1))
        return f"\x00CB{idx}\x00"

    text = re.sub(r"```\w*\n?(.*?)```", _save_code_block, text, flags=re.DOTALL)

    # 2. Extract markdown tables
    text, tables = _extract_tables(text)

    # 3. Extract inline code
    inline_codes: list[str] = []

    def _save_inline(m: re.Match) -> str:
        idx = len(inline_codes)
        inline_codes.append(m.group(1))
        return f"\x00IC{idx}\x00"

    text = re.sub(r"`([^`\n]+)`", _save_inline, text)

    # 4. Escape HTML entities in the remaining text
    text = html_mod.escape(text)

    # 5. Headers → bold (# Heading)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 6. Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # 7. Italic: *text* or _text_ (not mid-word underscores like file_name)
    text = re.sub(r"(?<!\w)\*([^\*\n]+?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)

    # 8. Strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 9. Links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # 10. Restore protected blocks (HTML-escaped)
    for i, code in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", f"<pre>{html_mod.escape(code)}</pre>")

    for i, table in enumerate(tables):
        text = text.replace(f"\x00TBL{i}\x00", f"<pre>{html_mod.escape(table)}</pre>")

    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", f"<code>{html_mod.escape(code)}</code>")

    return text


# --- Typing indicator (module-level, no per-bot state) ---

async def send_typing_loop(chat) -> None:
    """Send typing action every 5 seconds until cancelled."""
    try:
        while True:
            await chat.send_action("typing")
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass


# --- Tool labels (module-level, no per-bot state) ---

_TOOL_LABELS = {
    "Bash": "Running command",
    "Read": "Reading file",
    "Write": "Writing file",
    "Edit": "Editing file",
    "Glob": "Searching files",
    "Grep": "Searching code",
    "WebFetch": "Fetching web page",
    "WebSearch": "Searching the web",
    "Task": "Running sub-task",
}

STATUS_THROTTLE_SECS = 8
MAX_DETAIL_LEN = 120


def _tool_detail(tool_name: str, tool_input: dict) -> str:
    """Extract a short description from tool input for status messages."""
    detail = ""
    if tool_name == "Bash":
        detail = tool_input.get("command", "")
    elif tool_name in ("Read", "Write", "Edit"):
        detail = tool_input.get("file_path", "")
    elif tool_name == "Glob":
        detail = tool_input.get("pattern", "")
    elif tool_name == "Grep":
        detail = tool_input.get("pattern", "")
    elif tool_name in ("WebFetch", "WebSearch"):
        detail = tool_input.get("url", "") or tool_input.get("query", "")
    if len(detail) > MAX_DETAIL_LEN:
        detail = detail[:MAX_DETAIL_LEN] + "…"
    return detail


# --- Message sending (module-level, no per-bot state) ---

MAX_MSG_LEN = 4096


def _split_text(text: str) -> list[str]:
    """Split text into chunks that fit Telegram's message limit."""
    if len(text) <= MAX_MSG_LEN:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= MAX_MSG_LEN:
            chunks.append(remaining)
            break

        split_at = MAX_MSG_LEN
        for sep in ["\n\n", "\n", " "]:
            idx = remaining.rfind(sep, 0, MAX_MSG_LEN)
            if idx > 0:
                split_at = idx + len(sep)
                break

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    return [c for c in chunks if c.strip()]


NETWORK_RETRY_DELAYS = [2, 5, 10]


async def _retry_on_network_error(coro_factory, retries=NETWORK_RETRY_DELAYS, retry_timeout=False):
    """Call coro_factory() and retry on network errors with backoff.

    Args:
        retry_timeout: If False (default), don't retry on TimedOut since the
            server may have already processed the request (e.g. message sent
            but response timed out). Set True for idempotent reads like
            get_file/download where retrying is safe.
    """
    last_err = None
    for attempt, delay in enumerate([0] + list(retries)):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await coro_factory()
        except TimedOut as e:
            if not retry_timeout:
                raise  # don't retry — server may have processed it
            last_err = e
            logger.warning(f"Timeout (attempt {attempt + 1}): {e}")
        except NetworkError as e:
            last_err = e
            logger.warning(f"Network error (attempt {attempt + 1}): {e}")
    raise last_err


async def send_long_message(update: Update, text: str) -> None:
    """Send a message with markdown→HTML formatting, falling back to plain text."""
    for chunk in _split_text(text):
        html_chunk = markdown_to_telegram_html(chunk)
        try:
            await _retry_on_network_error(
                lambda c=html_chunk: update.message.reply_text(c, parse_mode="HTML")
            )
        except NetworkError:
            logger.warning("HTML send failed after retries, trying plain text")
            try:
                await _retry_on_network_error(
                    lambda c=chunk: update.message.reply_text(c)
                )
            except NetworkError:
                raise NetworkError(
                    "Claude finished but the reply could not be delivered (network error). Please resend your message."
                )
        except Exception:
            logger.warning("HTML send failed, falling back to plain text")
            await update.message.reply_text(chunk)


# --- Bot commands list ---

BOT_COMMANDS = [
    ("start", "Show help"),
    ("new", "Start a fresh session"),
    ("session", "Show current session ID"),
]


# --- BotInstance ---

class BotInstance:
    """Encapsulates all per-bot state and handlers."""

    DEFAULT_ALLOWED_TOOLS = (
        "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Skill,Task"
    )

    def __init__(
        self,
        name: str,
        token: str,
        authorized_user_ids: set[int],
        working_dir: str,
        claude_bin: str = "claude",
        claude_timeout: int = 300,
        allowed_tools: str = DEFAULT_ALLOWED_TOOLS,
        verbose: bool = True,
    ):
        self.name = name
        self.token = token
        self.authorized_user_ids = authorized_user_ids
        self.working_dir = working_dir
        self.claude_bin = claude_bin
        self.claude_timeout = claude_timeout
        self.allowed_tools = allowed_tools
        self.verbose = verbose

        base = Path(__file__).parent
        self.sessions_file = base / f"sessions_{name}.json"
        self.downloads_dir = base / f"downloads_{name}"
        self.downloads_dir.mkdir(exist_ok=True)

        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._telegram_to_claude_cmd: dict[str, str] = {}
        self._app: Application | None = None

        self._log = logging.getLogger(f"{__name__}.{name}")

    # --- Session persistence ---

    def get_chat_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    def load_sessions(self) -> dict[str, str]:
        if self.sessions_file.exists():
            try:
                return json.loads(self.sessions_file.read_text())
            except (json.JSONDecodeError, OSError):
                self._log.warning("Failed to read sessions file, starting fresh")
        return {}

    def save_sessions(self, sessions: dict[str, str]) -> None:
        tmp = self.sessions_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(sessions, indent=2))
        tmp.rename(self.sessions_file)

    def get_session_id(self, chat_id: int) -> str | None:
        sessions = self.load_sessions()
        return sessions.get(str(chat_id))

    def set_session_id(self, chat_id: int, session_id: str) -> None:
        sessions = self.load_sessions()
        sessions[str(chat_id)] = session_id
        self.save_sessions(sessions)

    def clear_session(self, chat_id: int) -> None:
        sessions = self.load_sessions()
        sessions.pop(str(chat_id), None)
        self.save_sessions(sessions)

    # --- Claude CLI ---

    async def call_claude(
        self, message: str, session_id: str | None = None, chat=None
    ) -> tuple[str, str | None]:
        """Call Claude CLI with streaming output and return (response_text, session_id)."""
        args = [
            self.claude_bin,
            "--print",
            message,
            "--output-format", "stream-json",
            "--verbose",
            "--allowedTools", self.allowed_tools,
            "--disallowedTools", "AskUserQuestion,EnterPlanMode,ExitPlanMode",
        ]
        if session_id:
            args.extend(["--resume", session_id])

        self._log.info(f"Calling Claude (session={session_id or 'new'})")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_dir,
        )

        typing_task = None
        if chat:
            typing_task = asyncio.create_task(send_typing_loop(chat))

        result_text = ""
        new_session_id = session_id
        last_status_time = 0.0
        deadline = time.monotonic() + self.claude_timeout

        async def _process_line(raw: bytes) -> None:
            nonlocal result_text, new_session_id, last_status_time
            line = raw.decode().strip()
            if not line:
                return
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                return

            msg_type = data.get("type")

            if "session_id" in data:
                new_session_id = data["session_id"]

            if msg_type == "assistant":
                content = data.get("message", {}).get("content", [])
                for block in content:
                    if self.verbose and block.get("type") == "tool_use" and chat:
                        tool_name = block.get("name", "unknown")
                        now = time.monotonic()
                        if now - last_status_time >= STATUS_THROTTLE_SECS:
                            label = _TOOL_LABELS.get(tool_name, f"Using {tool_name}")
                            detail = _tool_detail(tool_name, block.get("input", {}))
                            status = f"⏳ {label}..."
                            if detail:
                                status += f"\n<code>{html_mod.escape(detail)}</code>"
                            try:
                                await chat.send_message(status, parse_mode="HTML")
                                last_status_time = now
                            except Exception:
                                pass

            elif msg_type == "result":
                result_text = data.get("result", "")

        buf = b""
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    await proc.communicate()
                    return (
                        f"Claude timed out after {self.claude_timeout}s. Try a simpler request.",
                        session_id,
                    )

                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(1024 * 1024), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    return (
                        f"Claude timed out after {self.claude_timeout}s. Try a simpler request.",
                        session_id,
                    )

                if not chunk:  # EOF
                    break

                buf += chunk

                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    await _process_line(raw_line)

            # Process any remaining data in buffer (last line without trailing \n)
            if buf.strip():
                await _process_line(buf)

            await proc.wait()

        finally:
            if typing_task:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

        if proc.returncode != 0:
            stderr_text = (await proc.stderr.read()).decode().strip()
            self._log.error(f"Claude exited with code {proc.returncode}: {stderr_text}")
            return (
                f"Claude error (exit code {proc.returncode}):\n{stderr_text[:500]}",
                session_id,
            )

        if not result_text:
            stderr_text = (await proc.stderr.read()).decode().strip()
            self._log.warning(
                f"Claude returned empty result (exit code {proc.returncode}). "
                f"stderr: {stderr_text[:500]}"
            )
            return "Claude returned an empty response.", new_session_id

        return result_text, new_session_id

    async def call_claude_with_retry(
        self, message: str, chat_id: int, chat=None
    ) -> tuple[str, str | None]:
        """Call Claude with session resume, retry without session if it fails."""
        session_id = self.get_session_id(chat_id)

        result, new_session_id = await self.call_claude(message, session_id, chat=chat)

        if session_id and result.startswith("Claude error"):
            self._log.info(f"Retrying without session for chat {chat_id}")
            self.clear_session(chat_id)
            result, new_session_id = await self.call_claude(message, None, chat=chat)

        if new_session_id:
            self.set_session_id(chat_id, new_session_id)

        return result, new_session_id

    # --- Slash command discovery ---

    def discover_claude_commands(self) -> list[tuple[str, str]]:
        """Scan .claude/commands/ and .claude/skills/ for available slash commands."""
        search_dirs = []
        for base in [Path(self.working_dir), Path.home()]:
            search_dirs.append(base / ".claude" / "commands")
            search_dirs.append(base / ".claude" / "skills")

        commands: list[tuple[str, str]] = []
        seen: set[str] = set()

        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                # Commands are .md files; skills are directories with SKILL.md
                if f.is_file() and f.suffix == ".md":
                    name = f.stem
                    desc_file = f
                elif f.is_dir() and (f / "SKILL.md").is_file():
                    name = f.name
                    desc_file = f / "SKILL.md"
                else:
                    continue
                if name in seen:
                    continue
                seen.add(name)
                try:
                    first_line = desc_file.read_text().strip().split("\n")[0].strip()
                    desc = first_line.lstrip("#").strip()
                    if not desc:
                        desc = name
                except OSError:
                    desc = name
                cmd_name = name.lower().replace(" ", "_").replace("-", "_")[:32]
                self._telegram_to_claude_cmd[cmd_name] = name
                commands.append((cmd_name, desc[:256]))

        return commands

    # --- Handler factories ---

    def _authorized(self, func):
        """Decorator that checks if user is in this bot's authorized list."""
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            if user_id not in self.authorized_user_ids:
                self._log.warning(f"Unauthorized access attempt from user {user_id}")
                await update.message.reply_text("Unauthorized.")
                return
            return await func(update, context)
        return wrapper

    def _make_handlers(self):
        """Create handler functions closed over this BotInstance."""
        bot = self

        @bot._authorized
        async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat_id = update.effective_chat.id
            user_text = update.message.text
            if not user_text:
                return

            lock = bot.get_chat_lock(chat_id)
            if lock.locked():
                await update.message.reply_text(
                    "Still processing the previous message. Please wait."
                )
                return

            async with lock:
                result, _ = await bot.call_claude_with_retry(
                    user_text, chat_id, chat=update.message.chat
                )
                await send_long_message(update, result)

        @bot._authorized
        async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat_id = update.effective_chat.id
            doc = update.message.document

            lock = bot.get_chat_lock(chat_id)
            if lock.locked():
                await update.message.reply_text(
                    "Still processing the previous message. Please wait."
                )
                return

            async with lock:
                file = await doc.get_file()
                file_name = doc.file_name or f"document_{doc.file_id}"
                save_path = bot.downloads_dir / file_name
                await file.download_to_drive(save_path)
                bot._log.info(f"Downloaded document: {save_path}")

                caption = update.message.caption or ""
                prompt = f"I'm sending you a file saved at: {save_path}\nFilename: {file_name}"
                if caption:
                    prompt += f"\n\nUser message: {caption}"
                else:
                    prompt += "\n\nPlease read and summarize this file."

                result, _ = await bot.call_claude_with_retry(
                    prompt, chat_id, chat=update.message.chat
                )
                await send_long_message(update, result)

        @bot._authorized
        async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat_id = update.effective_chat.id

            lock = bot.get_chat_lock(chat_id)
            if lock.locked():
                await update.message.reply_text(
                    "Still processing the previous message. Please wait."
                )
                return

            async with lock:
                photo = update.message.photo[-1]
                file = await photo.get_file()
                file_name = f"photo_{photo.file_unique_id}.jpg"
                save_path = bot.downloads_dir / file_name
                await file.download_to_drive(save_path)
                bot._log.info(f"Downloaded photo: {save_path}")

                caption = update.message.caption or ""
                prompt = f"I'm sending you an image saved at: {save_path}"
                if caption:
                    prompt += f"\n\nUser message: {caption}"
                else:
                    prompt += "\n\nPlease describe and analyze this image."

                result, _ = await bot.call_claude_with_retry(
                    prompt, chat_id, chat=update.message.chat
                )
                await send_long_message(update, result)

        @bot._authorized
        async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat_id = update.effective_chat.id
            bot.clear_session(chat_id)
            await update.message.reply_text(
                "Session cleared. Next message starts a fresh conversation."
            )

        @bot._authorized
        async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat_id = update.effective_chat.id
            session_id = bot.get_session_id(chat_id)
            if session_id:
                await update.message.reply_text(f"Session: {session_id}")
            else:
                await update.message.reply_text("No active session.")

        @bot._authorized
        async def handle_claude_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat_id = update.effective_chat.id
            user_text = update.message.text

            parts = user_text.split(None, 1)
            tg_cmd = parts[0].lstrip("/").split("@")[0]
            original = bot._telegram_to_claude_cmd.get(tg_cmd, tg_cmd)
            user_text = "/" + original
            if len(parts) > 1:
                user_text += " " + parts[1]

            lock = bot.get_chat_lock(chat_id)
            if lock.locked():
                await update.message.reply_text(
                    "Still processing the previous message. Please wait."
                )
                return

            async with lock:
                result, _ = await bot.call_claude_with_retry(
                    user_text, chat_id, chat=update.message.chat
                )
                await send_long_message(update, result)

        @bot._authorized
        async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            await update.message.reply_text(
                "Claude Code bot ready.\n\n"
                "Send any message to chat with Claude.\n"
                "/new - Start a fresh session\n"
                "/session - Show current session ID"
            )

        return {
            "handle_message": handle_message,
            "handle_document": handle_document,
            "handle_photo": handle_photo,
            "cmd_new": cmd_new,
            "cmd_session": cmd_session,
            "handle_claude_command": handle_claude_command,
            "cmd_start": cmd_start,
        }

    # --- Lifecycle ---

    async def build_and_start(self) -> None:
        """Build the Application, register handlers, and start polling."""
        app = Application.builder().token(self.token).build()

        handlers = self._make_handlers()

        app.add_handler(CommandHandler("start", handlers["cmd_start"]))
        app.add_handler(CommandHandler("new", handlers["cmd_new"]))
        app.add_handler(CommandHandler("session", handlers["cmd_session"]))
        app.add_handler(MessageHandler(filters.COMMAND, handlers["handle_claude_command"]))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers["handle_message"]))
        app.add_handler(MessageHandler(filters.Document.ALL, handlers["handle_document"]))
        app.add_handler(MessageHandler(filters.PHOTO, handlers["handle_photo"]))

        async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            err = context.error
            self._log.error(f"Update caused error: {err}", exc_info=err)
            if not (isinstance(update, Update) and update.effective_message):
                return
            if isinstance(err, NetworkError):
                msg = "Network error — couldn't reach Telegram servers. Your message may not have been processed. Please resend it."
            else:
                err_name = type(err).__name__
                msg = f"Something went wrong ({err_name}). Please try again."
            try:
                await update.effective_message.reply_text(msg)
            except Exception:
                pass  # if we can't even send the error message, just log

        app.add_error_handler(error_handler)

        self._app = app

        await app.initialize()

        # Discover commands and register with Telegram (post_init doesn't
        # fire with manual lifecycle, so we do it here after initialize).
        claude_cmds = self.discover_claude_commands()
        all_commands = BOT_COMMANDS + claude_cmds
        self._log.info(
            f"Registering {len(all_commands)} commands "
            f"({len(BOT_COMMANDS)} bot + {len(claude_cmds)} Claude)"
        )
        # set_my_commands is non-essential; don't let a timeout block startup
        try:
            await app.bot.set_my_commands(all_commands)
        except NetworkError:
            self._log.warning("Failed to register commands (network), will retry later")
            asyncio.get_event_loop().create_task(self._retry_set_commands(all_commands))

        await app.updater.start_polling(drop_pending_updates=True)
        await app.start()

        self._log.info(
            f"Bot '{self.name}' started (authorized users: {self.authorized_user_ids})"
        )

    async def _retry_set_commands(self, commands: list[tuple[str, str]]) -> None:
        """Retry registering commands in the background."""
        for delay in [10, 30, 60]:
            await asyncio.sleep(delay)
            try:
                await self._app.bot.set_my_commands(commands)
                self._log.info("Successfully registered commands on retry")
                return
            except Exception:
                self._log.warning(f"Retry set_my_commands failed, next in {delay}s")

    async def stop(self) -> None:
        """Gracefully shut down the bot."""
        if self._app:
            self._log.info(f"Stopping bot '{self.name}'...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._log.info(f"Bot '{self.name}' stopped.")


# --- Configuration ---

CONFIG_FILE = Path(__file__).parent / "bots_config.yaml"


def load_config() -> list[BotInstance]:
    """Load bot configuration from bots_config.yaml."""
    if not CONFIG_FILE.exists():
        logger.error(f"Config file not found: {CONFIG_FILE}")
        logger.error("Please create bots_config.yaml (see bots_config.sample.yaml)")
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f)

    # Top-level defaults
    default_claude_bin = config.get("claude_bin", "claude")
    default_claude_timeout = config.get("claude_timeout", 300)

    bots_cfg = config.get("bots", [])
    if not bots_cfg:
        logger.error("No bots configured in bots_config.json")
        sys.exit(1)

    instances = []
    for bot_cfg in bots_cfg:
        name = bot_cfg["name"]

        token = bot_cfg.get("token")
        if not token:
            logger.error(f"Bot '{name}': no token configured")
            sys.exit(1)

        working_dir = bot_cfg.get("working_dir", ".")
        authorized = set(bot_cfg.get("authorized_user_ids", []))

        claude_timeout = bot_cfg.get("claude_timeout", default_claude_timeout)
        allowed_tools = bot_cfg.get("allowed_tools", BotInstance.DEFAULT_ALLOWED_TOOLS)
        verbose = bot_cfg.get("verbose", True)

        instances.append(BotInstance(
            name=name,
            token=token,
            authorized_user_ids=authorized,
            working_dir=working_dir,
            claude_bin=default_claude_bin,
            claude_timeout=claude_timeout,
            allowed_tools=allowed_tools,
            verbose=verbose,
        ))

    return instances


# --- Main ---

async def _retry_bot_start(inst: BotInstance, delays=(15, 30, 60, 120)) -> None:
    """Retry starting a bot that failed on initial startup."""
    for delay in delays:
        logger.info(f"Retrying bot '{inst.name}' in {delay}s...")
        await asyncio.sleep(delay)
        try:
            await inst.build_and_start()
            logger.info(f"Bot '{inst.name}' started on retry")
            return
        except Exception:
            logger.warning(f"Retry for bot '{inst.name}' failed", exc_info=True)
    logger.error(f"Bot '{inst.name}' failed all retry attempts")


async def run_all() -> None:
    """Start all configured bots and wait for shutdown signal."""
    instances = load_config()

    # Start all bots, tolerating individual failures
    started = 0
    for inst in instances:
        try:
            await inst.build_and_start()
            started += 1
        except Exception:
            logger.error(f"Failed to start bot '{inst.name}', will retry in background", exc_info=True)
            asyncio.get_event_loop().create_task(_retry_bot_start(inst))

    if started == 0:
        logger.error("No bots started successfully, exiting")
        sys.exit(1)

    logger.info(f"{started}/{len(instances)} bot(s) running. Press Ctrl+C to stop.")

    # Wait for shutdown signal
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    # Graceful shutdown
    logger.info("Shutting down...")
    for inst in instances:
        await inst.stop()
    logger.info("All bots stopped.")


def main() -> None:
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
