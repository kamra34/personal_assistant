#!/bin/bash
set -euo pipefail

PROJECT_PATH="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$PROJECT_PATH"

if [ ! -x ".venv/bin/python" ]; then
  echo "Missing Python venv at .venv/bin/python"
  exit 1
fi

.venv/bin/python -m pip install pyinstaller

.venv/bin/python -m PyInstaller \
  --noconfirm \
  --windowed \
  --name "MeetingAssistantDesktopAgent" \
  --paths "$PROJECT_PATH" \
  "$PROJECT_PATH/helper/desktop_agent.py"

TARGET_DIR="$PROJECT_PATH/web/public/downloads"
mkdir -p "$TARGET_DIR"

if [ -d "$PROJECT_PATH/dist/MeetingAssistantDesktopAgent.app" ]; then
  rm -rf "$TARGET_DIR/MeetingAssistantDesktopAgent.app"
  cp -R "$PROJECT_PATH/dist/MeetingAssistantDesktopAgent.app" "$TARGET_DIR/"
  echo "Copied app bundle to web/public/downloads/MeetingAssistantDesktopAgent.app"
else
  echo "Built output not found at dist/MeetingAssistantDesktopAgent.app"
fi

