#!/bin/zsh
# Start the Claude Agents bridge with correct PATH and working directory
export PATH="/Users/ayushya/.pyenv/shims:/Users/ayushya/.local/share/fnm/node-versions/v24.13.1/installation/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export HOME="/Users/ayushya"

# CRITICAL: cd to a valid directory before running
# launchd starts in / which causes EPERM on process.cwd() in Node.js
cd /Users/ayushya/claude-agents || exit 1

exec /Users/ayushya/.pyenv/shims/python3 bridge.py
