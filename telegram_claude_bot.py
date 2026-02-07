#!/usr/bin/env python3
"""Telegram bot that forwards messages to Claude Code CLI."""

import asyncio
import html as html_mod
import json
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AUTHORIZED_USER_IDS = {
    int(uid.strip())
    for uid in os.environ["AUTHORIZED_USER_IDS"].split(",")
    if uid.strip()
}
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
WORKING_DIR = os.environ.get("WORKING_DIR", ".")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
SESSIONS_FILE = Path(__file__).parent / "sessions.json"
DOWNLOADS_DIR = Path(__file__).parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

ALLOWED_TOOLS = (
    "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Skill,Task"
)

# Per-chat locks to prevent concurrent Claude calls
_chat_locks: dict[int, asyncio.Lock] = {}


def get_chat_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


# --- Session persistence ---

def load_sessions() -> dict[str, str]:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read sessions file, starting fresh")
    return {}


def save_sessions(sessions: dict[str, str]) -> None:
    tmp = SESSIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, indent=2))
    tmp.rename(SESSIONS_FILE)


def get_session_id(chat_id: int) -> str | None:
    sessions = load_sessions()
    return sessions.get(str(chat_id))


def set_session_id(chat_id: int, session_id: str) -> None:
    sessions = load_sessions()
    sessions[str(chat_id)] = session_id
    save_sessions(sessions)


def clear_session(chat_id: int) -> None:
    sessions = load_sessions()
    sessions.pop(str(chat_id), None)
    save_sessions(sessions)


# --- Authorization ---

def authorized(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in AUTHORIZED_USER_IDS:
            logger.warning(f"Unauthorized access attempt from user {user_id}")
            await update.message.reply_text("Unauthorized.")
            return
        return await func(update, context)
    return wrapper


# --- Claude CLI ---

async def send_typing_loop(chat) -> None:
    """Send typing action every 5 seconds until cancelled."""
    try:
        while True:
            await chat.send_action("typing")
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass


# Friendly names for common tools
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

STATUS_THROTTLE_SECS = 8  # Minimum seconds between status messages
MAX_DETAIL_LEN = 120  # Truncate tool detail to keep status messages short


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


async def call_claude(
    message: str, session_id: str | None = None, chat=None
) -> tuple[str, str | None]:
    """Call Claude CLI with streaming output and return (response_text, session_id)."""
    args = [
        CLAUDE_BIN,
        "--print",
        message,
        "--output-format", "stream-json",
        "--verbose",
        "--allowedTools", ALLOWED_TOOLS,
    ]
    if session_id:
        args.extend(["--resume", session_id])

    logger.info(f"Calling Claude (session={session_id or 'new'})")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=WORKING_DIR,
    )

    # Keep typing indicator alive throughout
    typing_task = None
    if chat:
        typing_task = asyncio.create_task(send_typing_loop(chat))

    result_text = ""
    new_session_id = session_id
    last_status_time = 0.0
    deadline = time.monotonic() + CLAUDE_TIMEOUT

    # Use chunked reads instead of readline() to avoid the 64KB line
    # length limit — Claude's stream-json can emit very long lines when
    # tool results contain large file contents.
    buf = b""
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                await proc.communicate()
                return (
                    f"Claude timed out after {CLAUDE_TIMEOUT}s. Try a simpler request.",
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
                    f"Claude timed out after {CLAUDE_TIMEOUT}s. Try a simpler request.",
                    session_id,
                )

            if not chunk:  # EOF
                break

            buf += chunk

            # Process all complete lines in the buffer
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                line = raw_line.decode().strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")

                # Capture session_id from any message that carries it
                if "session_id" in data:
                    new_session_id = data["session_id"]

                # Notify user about tool usage
                if msg_type == "assistant":
                    content = data.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "tool_use" and chat:
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
        logger.error(f"Claude exited with code {proc.returncode}: {stderr_text}")
        return (
            f"Claude error (exit code {proc.returncode}):\n{stderr_text[:500]}",
            session_id,
        )

    if not result_text:
        return "Claude returned an empty response.", new_session_id

    return result_text, new_session_id


async def call_claude_with_retry(
    message: str, chat_id: int, chat=None
) -> tuple[str, str | None]:
    """Call Claude with session resume, retry without session if it fails."""
    session_id = get_session_id(chat_id)

    result, new_session_id = await call_claude(message, session_id, chat=chat)

    # If we got an error and were using a session, retry without it
    if session_id and result.startswith("Claude error"):
        logger.info(f"Retrying without session for chat {chat_id}")
        clear_session(chat_id)
        result, new_session_id = await call_claude(message, None, chat=chat)

    if new_session_id:
        set_session_id(chat_id, new_session_id)

    return result, new_session_id


# --- Markdown to Telegram HTML ---

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


# --- Message sending ---

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


async def send_long_message(update: Update, text: str) -> None:
    """Send a message with markdown→HTML formatting, falling back to plain text."""
    for chunk in _split_text(text):
        html_chunk = markdown_to_telegram_html(chunk)
        try:
            await update.message.reply_text(html_chunk, parse_mode="HTML")
        except Exception:
            # If HTML parsing fails, send as plain text
            logger.warning("HTML send failed, falling back to plain text")
            await update.message.reply_text(chunk)


# --- Handlers ---

@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages."""
    chat_id = update.effective_chat.id
    user_text = update.message.text

    if not user_text:
        return

    lock = get_chat_lock(chat_id)
    if lock.locked():
        await update.message.reply_text(
            "Still processing the previous message. Please wait."
        )
        return

    async with lock:
        result, _ = await call_claude_with_retry(
            user_text, chat_id, chat=update.message.chat
        )
        await send_long_message(update, result)


@authorized
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming documents (PDF, etc.)."""
    chat_id = update.effective_chat.id
    doc = update.message.document

    lock = get_chat_lock(chat_id)
    if lock.locked():
        await update.message.reply_text(
            "Still processing the previous message. Please wait."
        )
        return

    async with lock:
        # Download file
        file = await doc.get_file()
        file_name = doc.file_name or f"document_{doc.file_id}"
        save_path = DOWNLOADS_DIR / file_name
        await file.download_to_drive(save_path)
        logger.info(f"Downloaded document: {save_path}")

        # Build prompt with caption if present
        caption = update.message.caption or ""
        prompt = f"I'm sending you a file saved at: {save_path}\nFilename: {file_name}"
        if caption:
            prompt += f"\n\nUser message: {caption}"
        else:
            prompt += "\n\nPlease read and summarize this file."

        result, _ = await call_claude_with_retry(
            prompt, chat_id, chat=update.message.chat
        )
        await send_long_message(update, result)


@authorized
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming photos."""
    chat_id = update.effective_chat.id

    lock = get_chat_lock(chat_id)
    if lock.locked():
        await update.message.reply_text(
            "Still processing the previous message. Please wait."
        )
        return

    async with lock:
        # Get the largest photo size
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_name = f"photo_{photo.file_unique_id}.jpg"
        save_path = DOWNLOADS_DIR / file_name
        await file.download_to_drive(save_path)
        logger.info(f"Downloaded photo: {save_path}")

        # Build prompt with caption if present
        caption = update.message.caption or ""
        prompt = f"I'm sending you an image saved at: {save_path}"
        if caption:
            prompt += f"\n\nUser message: {caption}"
        else:
            prompt += "\n\nPlease describe and analyze this image."

        result, _ = await call_claude_with_retry(
            prompt, chat_id, chat=update.message.chat
        )
        await send_long_message(update, result)


@authorized
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear session and start fresh."""
    chat_id = update.effective_chat.id
    clear_session(chat_id)
    await update.message.reply_text("Session cleared. Next message starts a fresh conversation.")


@authorized
async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current session ID."""
    chat_id = update.effective_chat.id
    session_id = get_session_id(chat_id)
    if session_id:
        await update.message.reply_text(f"Session: {session_id}")
    else:
        await update.message.reply_text("No active session.")


@authorized
async def handle_claude_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward unrecognized /commands to Claude as slash commands."""
    chat_id = update.effective_chat.id
    user_text = update.message.text

    # Translate Telegram command name back to original Claude name.
    # e.g. "/writing_clearly_and_concisely arg" → "/writing-clearly-and-concisely arg"
    parts = user_text.split(None, 1)
    tg_cmd = parts[0].lstrip("/").split("@")[0]  # strip / and @botname
    original = _telegram_to_claude_cmd.get(tg_cmd, tg_cmd)
    user_text = "/" + original
    if len(parts) > 1:
        user_text += " " + parts[1]

    lock = get_chat_lock(chat_id)
    if lock.locked():
        await update.message.reply_text(
            "Still processing the previous message. Please wait."
        )
        return

    async with lock:
        result, _ = await call_claude_with_retry(
            user_text, chat_id, chat=update.message.chat
        )
        await send_long_message(update, result)


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "Claude Code bot ready.\n\n"
        "Send any message to chat with Claude.\n"
        "/new - Start a fresh session\n"
        "/session - Show current session ID"
    )


# --- Slash command discovery ---

BOT_COMMANDS = [
    ("start", "Show help"),
    ("new", "Start a fresh session"),
    ("session", "Show current session ID"),
]


# Mapping from Telegram command name (underscores) back to original Claude
# command name (hyphens etc.), populated at startup by discover_claude_commands().
_telegram_to_claude_cmd: dict[str, str] = {}


def discover_claude_commands() -> list[tuple[str, str]]:
    """Scan .claude/commands/ and .claude/skills/ for available slash commands."""
    search_dirs = []
    for base in [Path(WORKING_DIR), Path.home()]:
        search_dirs.append(base / ".claude" / "commands")
        search_dirs.append(base / ".claude" / "skills")

    commands: list[tuple[str, str]] = []
    seen: set[str] = set()

    for d in search_dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if not f.suffix == ".md" or not f.is_file():
                continue
            name = f.stem
            if name in seen:
                continue
            seen.add(name)
            # Use first non-empty line of the file as description
            try:
                first_line = f.read_text().strip().split("\n")[0].strip()
                # Strip leading markdown heading markers
                desc = first_line.lstrip("#").strip()
                if not desc:
                    desc = name
            except OSError:
                desc = name
            # Telegram command names: lowercase, max 32 chars, no spaces
            cmd_name = name.lower().replace(" ", "_").replace("-", "_")[:32]
            _telegram_to_claude_cmd[cmd_name] = name
            commands.append((cmd_name, desc[:256]))

    return commands


async def post_init(application) -> None:
    """Register bot + Claude slash commands with Telegram on startup."""
    claude_cmds = discover_claude_commands()
    all_commands = BOT_COMMANDS + claude_cmds
    logger.info(
        f"Registering {len(all_commands)} commands "
        f"({len(BOT_COMMANDS)} bot + {len(claude_cmds)} Claude)"
    )
    await application.bot.set_my_commands(all_commands)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Bot-specific commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))

    # Forward any other /command to Claude
    app.add_handler(MessageHandler(filters.COMMAND, handle_claude_command))

    # Regular text, documents, photos
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info(f"Bot starting (authorized users: {AUTHORIZED_USER_IDS})")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
