#!/bin/bash
if [ "$CLAUDE_CODE_REMOTE" = "true" ]; then
  LOG="/tmp/claude_remote_setup_stderr.log"
  exec 2> >(tee "$LOG" >&2)

  # Install pre-commit hooks and sync v2 dev dependencies
  (cd "$CLAUDE_PROJECT_DIR" && uvx pre-commit install --install-hooks) >&2
  (cd "$CLAUDE_PROJECT_DIR/v2" && uv sync --group dev) >&2

  WEB_MD="$CLAUDE_PROJECT_DIR/CLAUDE_WEB.md"
  if [ -f "$WEB_MD" ]; then
    cat "$WEB_MD"
  fi
fi
exit 0
