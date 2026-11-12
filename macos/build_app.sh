#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

INSTALL_APP=1
PYTHON_BIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-install)
      INSTALL_APP=0
      shift
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
  elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_DIR/.venv/bin/python"
  elif [[ -x "$REPO_DIR/../../.venv/bin/python" ]]; then
    PYTHON_BIN="$(cd "$REPO_DIR/../.." && pwd)/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi

echo "[*] Building CamouDesk.app using Python: $PYTHON_BIN"

eval "$("$PYTHON_BIN" - << 'PY'
import pathlib, shlex, sysconfig
inc = sysconfig.get_path("include")
fw_dir = sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX") or ""
libdir = sysconfig.get_config_var("LIBDIR") or ""
ldlib = sysconfig.get_config_var("LDLIBRARY") or ""
print(f"PY_INCLUDE={shlex.quote(inc)}")
print(f"PY_FW_DIR={shlex.quote(fw_dir)}")
print(f"PY_LIBDIR={shlex.quote(libdir)}")
print(f"PY_LDLIB={shlex.quote(ldlib)}")
PY
)"

APP_DIR="$REPO_DIR/dist/CamouDesk.app"
CONTENTS="$APP_DIR/Contents"
MACOS_BIN_DIR="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

rm -rf "$APP_DIR"
mkdir -p "$MACOS_BIN_DIR" "$RESOURCES/src"

# Copy Python package into bundle resources (excluding caches)
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$REPO_DIR/src/camoudesk" "$RESOURCES/src/"

# Generate AppIcon.icns
"$PYTHON_BIN" "$SCRIPT_DIR/generate_icon.py" --out "$RESOURCES/AppIcon.icns"

# Install Info.plist and validate
cp "$SCRIPT_DIR/Info.plist.template" "$CONTENTS/Info.plist"
plutil -lint "$CONTENTS/Info.plist"

# Build native Mach-O launcher binary linked against Python framework/dylib
LINK_FLAGS=()
if [[ -n "$PY_FW_DIR" && -d "$PY_FW_DIR/Python.framework" ]]; then
  LINK_FLAGS+=("-F$PY_FW_DIR" "-framework" "Python" "-Wl,-rpath,$PY_FW_DIR")
else
  LINK_FLAGS+=("-L$PY_LIBDIR" "-lpython3" "-Wl,-rpath,$PY_LIBDIR")
fi

clang -O2 -Wall -Wextra \
  -I"$PY_INCLUDE" \
  "-DCAMOUDESK_VENV_PYTHON=\"$PYTHON_BIN\"" \
  "-DCAMOUDESK_SRC_DIR=\"$REPO_DIR/src\"" \
  "${LINK_FLAGS[@]}" \
  "$SCRIPT_DIR/launcher.c" \
  -o "$MACOS_BIN_DIR/CamouDesk"

chmod +x "$MACOS_BIN_DIR/CamouDesk"

# Ad-hoc code signature for clean local macOS launch
codesign --force --deep --sign - "$APP_DIR"
codesign --verify --deep --strict "$APP_DIR"

echo "[+] Built valid bundle: $APP_DIR"

if [[ "$INSTALL_APP" -eq 1 ]]; then
  TARGET_APPS="$HOME/Applications"
  mkdir -p "$TARGET_APPS"
  rm -rf "$TARGET_APPS/CamouDesk.app"
  cp -R "$APP_DIR" "$TARGET_APPS/CamouDesk.app"
  codesign --force --deep --sign - "$TARGET_APPS/CamouDesk.app"

  # Refresh macOS LaunchServices registration
  LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
  if [[ -x "$LSREGISTER" ]]; then
    "$LSREGISTER" -f "$TARGET_APPS/CamouDesk.app" || true
  fi

  # Create Desktop symlink to CamouDesk.app
  ln -sfn "$TARGET_APPS/CamouDesk.app" "$HOME/Desktop/CamouDesk.app"

  echo "[+] Installed: $TARGET_APPS/CamouDesk.app"
  echo "[+] Desktop link: $HOME/Desktop/CamouDesk.app"
fi
