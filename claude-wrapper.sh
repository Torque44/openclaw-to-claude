#!/bin/bash
# Wrapper that ensures CWD is valid before running claude CLI
# Fixes EPERM: process.cwd() on macOS launchd
export PATH="/Users/ayushya/.pyenv/shims:/Users/ayushya/.local/share/fnm/node-versions/v24.13.1/installation/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export HOME="/Users/ayushya"
cd "${CLAUDE_CWD:-$HOME}" 2>/dev/null || cd "$HOME"
exec /Users/ayushya/.local/share/fnm/node-versions/v24.13.1/installation/bin/claude "$@"
