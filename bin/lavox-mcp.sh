#!/usr/bin/env bash
# Lavox memory MCP launcher.
# Keeps a slim, self-managed venv in ~/Lavox/mcp-venv so the plugin works
# without the full transcription stack. Override with:
#   LAVOX_SERVER_DIR — where server/mcp_memory.py lives
#   LAVOX_MCP_VENV   — venv location
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="${LAVOX_SERVER_DIR:-$here/server}"
VENV="${LAVOX_MCP_VENV:-$HOME/Lavox/mcp-venv}"

if [ ! -x "$VENV/bin/python3" ]; then
  echo "[lavox-memory] first run: creating venv at $VENV (about a minute)..." >&2
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip >&2
  "$VENV/bin/pip" install --quiet -r "$SERVER_DIR/requirements-mcp.txt" >&2
fi

exec "$VENV/bin/python3" "$SERVER_DIR/mcp_memory.py"
