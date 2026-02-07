# Telegram Claude Code Bot

Telegram bot that forwards messages to Claude Code CLI. Supports text, documents (PDF), and photos.

## Project Structure

| File | Purpose |
|------|---------|
| `telegram_claude_bot.py` | Main bot script |
| `.env` | Bot token + authorized user IDs (not in git) |
| `sessions.json` | Chat-to-session mapping (not in git) |
| `downloads/` | Downloaded files from Telegram (not in git) |

## Service Management

The bot runs as a systemd user service.

```bash
# Check status
systemctl --user status telegram-claude-bot.service

# View logs (follow mode)
journalctl --user -u telegram-claude-bot.service -f

# Restart after code changes
systemctl --user restart telegram-claude-bot.service

# Stop
systemctl --user stop telegram-claude-bot.service

# Start
systemctl --user start telegram-claude-bot.service
```

The service unit file is at `~/.config/systemd/user/telegram-claude-bot.service`. If you change it, run `systemctl --user daemon-reload` before restarting.

## Live Debug Workflow

1. Stop the service: `systemctl --user stop telegram-claude-bot.service`
2. Run manually to see live output: `uv run python telegram_claude_bot.py`
3. Send test messages on Telegram, observe logs in terminal
4. When done, restart the service: `systemctl --user start telegram-claude-bot.service`

## Key Design Decisions

- Long-polling (no webhooks, no public IP needed)
- `claude --print --output-format stream-json --resume SESSION_ID` for session continuity
- Per-chat asyncio lock prevents concurrent Claude calls
- Authorization by Telegram user ID (set in `.env`)
- `--allowedTools Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Skill,Task`
- Working directory is configurable via `WORKING_DIR` env var (defaults to `.`)

## Dependencies

Managed with `uv`. To add a package: `uv add <package>`
