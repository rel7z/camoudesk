#!/usr/bin/env bash
# Build CamouDesk.app.
#
# Default: a self-contained bundle with a relocatable CPython (python-build-standalone) and all
# dependencies inside Contents/Resources/python, so the .app/.dmg runs on other Macs.
# --dev:   fast local build linked against the build machine's Python/venv (not distributable).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Pinned runtime: python-build-standalone release 20261026, CPython 3.13.18, arm64 macOS.
PBS_RELEASE="20261026"
PBS_ARCHIVE="cpython-3.13.18+${PBS_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_ARCHIVE//+/%2B}"
PBS_SHA256="e5af2fde6a937a0e0c795a0c595c4efb70578d57463a2657c819992b9b9acbac"
PY_VER="3.13"

INSTALL_APP=1
DEV_MODE=0
PYTHON_BIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-install)
      INSTALL_APP=0
      shift
      ;;
    --dev)
      DEV_MODE=1
      shift
      ;;
    --python)
      PYTHON_BIN="$2"
      DEV_MODE=1
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

APP_DIR="$REPO_DIR/dist/CamouDesk.app"
CONTENTS="$APP_DIR/Contents"
MACOS_BIN_DIR="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
CACHE_DIR="$REPO_DIR/build/cache"

rm -rf "$APP_DIR"
mkdir -p "$MACOS_BIN_DIR" "$RESOURCES"

if [[ "$DEV_MODE" -eq 1 ]]; then
  if [[ -z "$PYTHON_BIN" ]]; then
    if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
      PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
    elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
      PYTHON_BIN="$REPO_DIR/.venv/bin/python"
    else
      PYTHON_BIN="$(command -v python3)"
    fi
  fi
  echo "[*] DEV build linked against: $PYTHON_BIN (runs only on this machine)"

  eval "$("$PYTHON_BIN" - << 'PY'
import shlex, sysconfig
print(f"PY_INCLUDE={shlex.quote(sysconfig.get_path('include'))}")
print(f"PY_FW_DIR={shlex.quote(sysconfig.get_config_var('PYTHONFRAMEWORKPREFIX') or '')}")
print(f"PY_LIBDIR={shlex.quote(sysconfig.get_config_var('LIBDIR') or '')}")
print(f"PY_LDVERSION={shlex.quote(sysconfig.get_config_var('LDVERSION') or '')}")
PY
)"
  mkdir -p "$RESOURCES/src"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "$REPO_DIR/src/camoudesk" "$RESOURCES/src/"
  ICON_PYTHON="$PYTHON_BIN"

  LINK_FLAGS=()
  if [[ -n "$PY_FW_DIR" && -d "$PY_FW_DIR/Python.framework" ]]; then
    LINK_FLAGS+=("-F$PY_FW_DIR" "-framework" "Python" "-Wl,-rpath,$PY_FW_DIR")
  else
    LINK_FLAGS+=("-L$PY_LIBDIR" "-lpython$PY_LDVERSION" "-Wl,-rpath,$PY_LIBDIR")
  fi
  DEFINES=("-DCAMOUDESK_VENV_PYTHON=\"$PYTHON_BIN\"" "-DCAMOUDESK_SRC_DIR=\"$REPO_DIR/src\"")
else
  mkdir -p "$CACHE_DIR"
  ARCHIVE_PATH="$CACHE_DIR/$PBS_ARCHIVE"
  if [[ ! -f "$ARCHIVE_PATH" ]]; then
    echo "[*] Downloading $PBS_ARCHIVE"
    curl -fL --retry 3 -o "$ARCHIVE_PATH.part" "$PBS_URL"
    mv "$ARCHIVE_PATH.part" "$ARCHIVE_PATH"
  fi
  echo "$PBS_SHA256  $ARCHIVE_PATH" | shasum -a 256 -c - >/dev/null || {
    echo "[!] Checksum mismatch for $ARCHIVE_PATH; delete it and retry." >&2
    exit 1
  }

  echo "[*] Bundling relocatable CPython $PY_VER"
  tar -xzf "$ARCHIVE_PATH" -C "$RESOURCES"   # creates $RESOURCES/python
  BUNDLED_PY="$RESOURCES/python/bin/python3"

  echo "[*] Installing CamouDesk and dependencies into the bundle"
  "$BUNDLED_PY" -m pip install --quiet --no-cache-dir --disable-pip-version-check --no-warn-script-location "$REPO_DIR"

  # Camoufox installs fpgen's fingerprint model into fpgen's own package directory on first use.
  # Inside a signed .app that write breaks the code seal (or fails on a read-only volume), so
  # install Camoufox's pinned, sha256-verified model now. Its files are dated 2038 so fpgen's
  # 5-week refresh never fires, which only holds while copies of the bundle preserve mtimes
  # (ditto, Finder and hdiutil do; plain `cp -R` does not).
  echo "[*] Installing Camoufox's pinned fpgen model into the bundle"
  "$BUNDLED_PY" -c "from camoufox.fpgen_model import ensure_fpgen_model, is_pinned; ensure_fpgen_model(); assert is_pinned()"

  # Drop what a shipped GUI app never uses: stdlib tests/IDLE/Tk (and the Tcl/Tk runtime with
  # them), PyObjC's own test suite, and pip (the bundle is not meant to be modified after build).
  for unused in test idlelib tkinter turtledemo ensurepip lib2to3 lib-dynload/_tkinter*.so; do
    rm -rf "$RESOURCES/python/lib/python$PY_VER/"$unused
  done
  rm -rf "$RESOURCES/python/lib/"{itcl*,tcl*,tk*,thread*,libtcl*} "$RESOURCES/python/share"
  rm -rf "$RESOURCES/python/lib/python$PY_VER/site-packages/"{PyObjCTest,pip,pip-*.dist-info}
  rm -f "$RESOURCES/python/bin/"{pip*,idle*,pydoc*}
  ICON_PYTHON="$BUNDLED_PY"

  PY_INCLUDE="$RESOURCES/python/include/python$PY_VER"
  LIBPY="$RESOURCES/python/lib/libpython$PY_VER.dylib"
  # Make the dylib resolvable via the launcher's rpath regardless of its build-time install name.
  install_name_tool -id "@rpath/libpython$PY_VER.dylib" "$LIBPY"
  codesign --force --sign - "$LIBPY"
  LINK_FLAGS=("-L$RESOURCES/python/lib" "-lpython$PY_VER" "-Wl,-rpath,@executable_path/../Resources/python/lib")
  DEFINES=()
fi

# Generate AppIcon.icns
"$ICON_PYTHON" "$SCRIPT_DIR/generate_icon.py" --out "$RESOURCES/AppIcon.icns"

# Install Info.plist and validate
cp "$SCRIPT_DIR/Info.plist.template" "$CONTENTS/Info.plist"
plutil -lint "$CONTENTS/Info.plist"

# Build native Mach-O launcher binary
clang -O2 -Wall -Wextra \
  -I"$PY_INCLUDE" \
  ${DEFINES[@]+"${DEFINES[@]}"} \
  "${LINK_FLAGS[@]}" \
  "$SCRIPT_DIR/launcher.c" \
  -o "$MACOS_BIN_DIR/CamouDesk"
chmod +x "$MACOS_BIN_DIR/CamouDesk"

# Ad-hoc code signature for clean local macOS launch
codesign --force --deep --sign - "$APP_DIR"
codesign --verify --deep --strict "$APP_DIR"

echo "[+] Built bundle: $APP_DIR ($(du -sh "$APP_DIR" | cut -f1))"

if [[ "$INSTALL_APP" -eq 1 ]]; then
  TARGET_APPS="$HOME/Applications"
  mkdir -p "$TARGET_APPS"
  rm -rf "$TARGET_APPS/CamouDesk.app"
  ditto "$APP_DIR" "$TARGET_APPS/CamouDesk.app"   # preserves mtimes (see fpgen note above)
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
