# Telegram Claude Code Bot

Telegram bot that forwards messages to Claude Code CLI. Supports text, documents (PDF), and photos. Multiple bot instances can run in a single process, each with its own token, authorized users, and working directory.

## Project Structure

| File | Purpose |
|------|---------|
| `telegram_claude_bot.py` | Main bot script |
| `bots_config.yaml` | Multi-bot configuration (not in git) |
| `bots_config.sample.yaml` | Sample config with all options documented |
| `sessions_{name}.json` | Per-bot chat-to-session mapping (not in git) |
| `downloads_{name}/` | Per-bot downloaded files from Telegram (not in git) |

## Configuration

Copy `bots_config.sample.yaml` to `bots_config.yaml` and edit. See the sample for all available options with comments.

### Config fields

- `claude_bin`, `claude_timeout`: top-level defaults; per-bot values override.
- `token`: Telegram bot token (required per bot)
- `working_dir`: Claude's working directory (defaults to `.`)
- `name`: used for log prefixes, session files (`sessions_{name}.json`), and download dirs (`downloads_{name}/`)

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

- Multi-bot: each bot is a `BotInstance` with independent state (sessions, locks, downloads)
- Long-polling (no webhooks, no public IP needed)
- `claude --print --output-format stream-json --resume SESSION_ID` for session continuity
- Per-chat asyncio lock prevents concurrent Claude calls
- Authorization by Telegram user ID (per-bot in `bots_config.yaml`)
- `--allowedTools Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Skill,Task`
- Working directory is per-bot, configured in `bots_config.yaml`

## Dependencies

Managed with `uv`. To add a package: `uv add <package>`
