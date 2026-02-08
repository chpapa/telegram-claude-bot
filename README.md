# Telegram Claude Code Bot

A personal AI assistant over Telegram, like [Open Claw](https://github.com/anthropics/open-claw) but built on top of [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code). Text it from your phone, send it documents and photos, and let it work with your files using the full set of Claude Code tools.

## Why

Some of us have been running Claude Code on an Obsidian vault as a personal assistant for months. It accumulates knowledge over time through CLAUDE.md and custom slash commands/skills, and is more controllable compared to something like [Open Claw](https://github.com/anthropics/open-claw). The missing piece was mobile access -- SSH from a phone works but is painful. Talking to your AI assistant through a messenger is a much better experience, so I built this.

## Features

- **Multi-bot** -- run multiple bots in a single process, each with its own token, users, and working directory
- **Session continuity** -- conversations persist across messages using `--resume`
- **Streaming status** -- typing indicator + tool usage updates while Claude works
- **Formatted output** -- markdown converted to Telegram HTML (bold, italic, code blocks, tables)
- **Slash commands** -- auto-discovers custom Claude commands/skills and registers them in the Telegram menu
- **Multi-user** -- authorize multiple Telegram user IDs per bot
- **File support** -- send PDFs, images, and other documents for Claude to analyze

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

1. **Clone the repo**
   ```bash
   git clone https://github.com/youruser/telegram-claude-bot.git
   cd telegram-claude-bot
   ```

2. **Configure**
   ```bash
   cp bots_config.sample.yaml bots_config.yaml
   # Edit bots_config.yaml with your bot token(s), user IDs, and working directories
   ```

   To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

3. **Run the bot**
   ```bash
   uv run python telegram_claude_bot.py
   ```

## Configuration

All configuration lives in `bots_config.yaml`. See `bots_config.sample.yaml` for all available options with comments.

```yaml
claude_bin: /usr/local/bin/claude   # optional, defaults to "claude"
# claude_timeout: 300               # optional, defaults to 300s

bots:
  - name: my-bot
    token: "123456:ABC-DEF..."
    authorized_user_ids: [123456789]
    working_dir: /home/me/notes
```

| Field | Scope | Required | Default | Description |
|-------|-------|----------|---------|-------------|
| `claude_bin` | top-level | No | `claude` | Path to Claude Code CLI binary |
| `claude_timeout` | top-level / per-bot | No | `300` | Max seconds to wait for Claude response |
| `name` | per-bot | Yes | -- | Used for logs, session files, download dirs |
| `token` | per-bot | Yes | -- | Bot token from @BotFather |
| `authorized_user_ids` | per-bot | Yes | -- | List of Telegram user IDs allowed to use this bot |
| `working_dir` | per-bot | No | `.` | Working directory for Claude |

## Running as a systemd Service

Create `~/.config/systemd/user/telegram-claude-bot.service`:

```ini
[Unit]
Description=Telegram Claude Code Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/telegram-claude-bot
ExecStart=/path/to/uv run python telegram_claude_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Then enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable telegram-claude-bot.service
systemctl --user start telegram-claude-bot.service

# View logs
journalctl --user -u telegram-claude-bot.service -f
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help |
| `/new` | Clear session and start fresh |
| `/session` | Show current session ID |

Any custom commands in `.claude/commands/` or `.claude/skills/` (in the working directory or home directory) are automatically discovered and registered in the Telegram menu. Selecting them forwards the command to Claude.

## License

MIT
