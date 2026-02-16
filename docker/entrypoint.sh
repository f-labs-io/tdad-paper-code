#!/bin/bash
set -e

# Set up environment
export HOME="/home/promptsmith"
export PATH="/home/promptsmith/.local/bin:$PATH"

# Ensure .claude directory exists with correct ownership
mkdir -p /home/promptsmith/.claude
chown -R promptsmith:promptsmith /home/promptsmith/.claude 2>/dev/null || true

# Check Claude CLI
echo "Checking Claude CLI..."
CLAUDE_VERSION=$(gosu promptsmith claude --version 2>&1 || echo "not found")
echo "Claude CLI: $CLAUDE_VERSION"

if echo "$CLAUDE_VERSION" | grep -q "not found"; then
    echo "Error: Claude CLI not accessible"
    exit 1
fi

# Load OAuth token from credentials volume if it exists
TOKEN_FILE="/home/promptsmith/.claude/oauth_token"
if [ -f "$TOKEN_FILE" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN=$(cat "$TOKEN_FILE")
    echo "Authentication OK (OAuth token loaded)"
else
    echo ""
    echo "============================================"
    echo "ERROR: Claude authentication required!"
    echo "============================================"
    echo ""
    echo "Please run this command in your terminal first:"
    echo ""
    echo "  docker compose run --rm login"
    echo ""
    echo "Then re-run the compiler."
    echo "============================================"
    echo ""
    exit 1
fi

# Run command as promptsmith
echo "Starting: $@"
exec gosu promptsmith "$@"
