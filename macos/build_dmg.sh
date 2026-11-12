#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
APP_PATH="$REPO_DIR/dist/CamouDesk.app"
DMG_PATH="$REPO_DIR/dist/CamouDesk-0.1.0-arm64.dmg"

if [[ ! -d "$APP_PATH" ]]; then
  bash "$SCRIPT_DIR/build_app.sh" --no-install
fi

STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

cp -R "$APP_PATH" "$STAGE_DIR/CamouDesk.app"
ln -s /Applications "$STAGE_DIR/Applications"

rm -f "$DMG_PATH"
hdiutil create \
  -volname "CamouDesk" \
  -srcfolder "$STAGE_DIR" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

echo "[+] Created DMG installer: $DMG_PATH"
