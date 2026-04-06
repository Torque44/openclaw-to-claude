#!/bin/bash
set -e

# ============================================================================
# OpenClaw → Claude Code Telegram Bridge — One-Command Installer
# ============================================================================
#
# Prerequisites:
#   - macOS or Linux
#   - Claude Code installed and logged in (run 'claude' in terminal to verify)
#   - Python 3.10+ (via pyenv, system, or brew)
#   - At least one OpenClaw profile with a Telegram bot configured
#
# Usage:
#   curl -sL <raw-url>/install.sh | bash
#   -- or --
#   git clone <repo> && cd openclaw-migration && ./install.sh
#
# What this does:
#   1. Checks prerequisites (python, claude CLI, OpenClaw profiles)
#   2. Installs Python dependencies (claude-agent-sdk, python-telegram-bot, apscheduler)
#   3. Runs migrate.py to discover and extract all OpenClaw bots
#   4. Optionally installs a launchd/systemd service for auto-start
#
# ============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo "============================================"
echo "  OpenClaw → Claude Code Migration"
echo "============================================"
echo ""

# ---------------------------------------------------------------------------
# 1. Check Python
# ---------------------------------------------------------------------------
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        version=$($candidate --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo -e "${RED}Python 3.10+ not found.${NC}"
    echo "Install via: brew install python@3.12 or pyenv install 3.12"
    exit 1
fi
echo -e "${GREEN}✓${NC} Python: $($PYTHON --version)"

# ---------------------------------------------------------------------------
# 2. Check Claude CLI
# ---------------------------------------------------------------------------
CLAUDE_CLI=""
if command -v claude &>/dev/null; then
    CLAUDE_CLI=$(command -v claude)
else
    # Check common locations
    for loc in \
        "$HOME/.local/share/fnm/node-versions/"*/installation/bin/claude \
        "$HOME/.nvm/versions/node/"*/bin/claude \
        /usr/local/bin/claude \
        /opt/homebrew/bin/claude; do
        if [ -x "$loc" ] 2>/dev/null; then
            CLAUDE_CLI="$loc"
            break
        fi
    done
fi

if [ -z "$CLAUDE_CLI" ]; then
    echo -e "${RED}Claude Code CLI not found.${NC}"
    echo "Install: npm install -g @anthropic-ai/claude-code"
    echo "Then: claude login"
    exit 1
fi
echo -e "${GREEN}✓${NC} Claude CLI: $($CLAUDE_CLI --version 2>&1 | head -1)"

# Verify logged in
if ! $CLAUDE_CLI -p "reply PING" --output-format json 2>/dev/null | grep -q "PING"; then
    echo -e "${YELLOW}⚠${NC} Claude CLI may not be logged in. Run: claude login"
fi

# ---------------------------------------------------------------------------
# 3. Check OpenClaw profiles
# ---------------------------------------------------------------------------
PROFILES=()
if [ -f "$HOME/.openclaw/openclaw.json" ]; then
    PROFILES+=("default:$HOME/.openclaw")
fi
for d in "$HOME"/.openclaw-*/; do
    if [ -f "$d/openclaw.json" ]; then
        name=$(basename "$d" | sed 's/\.openclaw-//')
        PROFILES+=("$name:$d")
    fi
done

if [ ${#PROFILES[@]} -eq 0 ]; then
    echo -e "${RED}No OpenClaw profiles found.${NC}"
    echo "Expected: ~/.openclaw/ or ~/.openclaw-<name>/"
    exit 1
fi
echo -e "${GREEN}✓${NC} Found ${#PROFILES[@]} OpenClaw profile(s):"
for p in "${PROFILES[@]}"; do
    name="${p%%:*}"
    path="${p#*:}"
    echo "    - $name ($path)"
done

# ---------------------------------------------------------------------------
# 4. Install dependencies
# ---------------------------------------------------------------------------
echo ""
echo "Installing Python dependencies..."
$PYTHON -m pip install --quiet claude-agent-sdk python-telegram-bot apscheduler 2>&1 | tail -3
echo -e "${GREEN}✓${NC} Dependencies installed"

# ---------------------------------------------------------------------------
# 5. Install ffmpeg + whisper (for video/audio processing)
# ---------------------------------------------------------------------------
if ! command -v ffmpeg &>/dev/null; then
    echo "Installing ffmpeg..."
    if command -v brew &>/dev/null; then
        brew install ffmpeg 2>&1 | tail -2
    elif command -v apt-get &>/dev/null; then
        sudo apt-get install -y ffmpeg 2>&1 | tail -2
    else
        echo -e "${YELLOW}⚠${NC} ffmpeg not found. Install manually for video support."
    fi
fi

# ---------------------------------------------------------------------------
# 6. Set up directory
# ---------------------------------------------------------------------------
AGENTS_DIR="$HOME/claude-agents"
mkdir -p "$AGENTS_DIR"/{sessions,logs}

# Copy bridge files
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/migrate.py" "$AGENTS_DIR/migrate.py"
cp "$SCRIPT_DIR/bridge.py" "$AGENTS_DIR/bridge.py"
chmod +x "$AGENTS_DIR/bridge.py"

echo -e "${GREEN}✓${NC} Bridge files installed to $AGENTS_DIR"

# ---------------------------------------------------------------------------
# 7. Run migration
# ---------------------------------------------------------------------------
echo ""
echo "Running migration..."
echo ""
$PYTHON "$AGENTS_DIR/migrate.py"

# ---------------------------------------------------------------------------
# 8. Create start script
# ---------------------------------------------------------------------------
cat > "$AGENTS_DIR/start.sh" << STARTEOF
#!/bin/bash
export PATH="$(dirname "$PYTHON"):\$(dirname "$CLAUDE_CLI"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
export HOME="$HOME"
cd "$AGENTS_DIR"
exec $PYTHON bridge.py
STARTEOF
chmod +x "$AGENTS_DIR/start.sh"

echo -e "${GREEN}✓${NC} Start script: $AGENTS_DIR/start.sh"

# ---------------------------------------------------------------------------
# 9. Offer launchd/systemd setup
# ---------------------------------------------------------------------------
echo ""
read -p "Install as auto-start service? (y/n) " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS launchd
        PLIST="$HOME/Library/LaunchAgents/ai.claude.agents.plist"
        cat > "$PLIST" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.claude.agents</string>
    <key>ProgramArguments</key>
    <array>
        <string>$AGENTS_DIR/start.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$AGENTS_DIR/logs/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$AGENTS_DIR/logs/launchd-stderr.log</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
PLISTEOF
        echo -e "${GREEN}✓${NC} LaunchAgent installed: $PLIST"
        echo "  To start now: launchctl load $PLIST"
        echo "  To stop: launchctl unload $PLIST"
    else
        # Linux systemd
        SERVICE="$HOME/.config/systemd/user/claude-agents.service"
        mkdir -p "$(dirname "$SERVICE")"
        cat > "$SERVICE" << SVCEOF
[Unit]
Description=Claude Agents Telegram Bridge
After=network.target

[Service]
Type=simple
ExecStart=$AGENTS_DIR/start.sh
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
SVCEOF
        systemctl --user daemon-reload
        echo -e "${GREEN}✓${NC} systemd service installed: $SERVICE"
        echo "  To start: systemctl --user start claude-agents"
        echo "  To enable on boot: systemctl --user enable claude-agents"
    fi
fi

# ---------------------------------------------------------------------------
# 10. Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================"
echo -e "  ${GREEN}Migration Complete!${NC}"
echo "============================================"
echo ""
echo "Files:"
echo "  Config:  $AGENTS_DIR/config.json"
echo "  Bridge:  $AGENTS_DIR/bridge.py"
echo "  Start:   $AGENTS_DIR/start.sh"
echo "  Logs:    $AGENTS_DIR/logs/"
echo "  Sessions: $AGENTS_DIR/sessions/"
echo ""
echo "Quick start:"
echo "  $AGENTS_DIR/start.sh"
echo ""
echo "Commands (in Telegram):"
echo "  /reset  - Fresh conversation"
echo "  /deep   - Switch to Opus model"
echo "  /fast   - Switch to Sonnet model"
echo "  /status - Check current state"
echo ""
echo "To stop OpenClaw after verifying everything works:"
if [[ "$(uname)" == "Darwin" ]]; then
    echo "  launchctl unload ~/Library/LaunchAgents/ai.openclaw.*.plist"
else
    echo "  systemctl --user stop openclaw"
fi
echo ""
