#!/usr/bin/env bash
set -euo pipefail

# Builds the AppDir using PyInstaller (no AppImage creation).
# Used by appimage-builder in CI/local container builds.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK_DIR="${WORK_DIR:-"$ROOT_DIR/.appimage-build"}"
APPDIR="$WORK_DIR/AppDir"
DISTDIR="$WORK_DIR/dist"
BUILDDIR="$WORK_DIR/build"

APP_NAME="tidal-bitperfect"

mkdir -p "$WORK_DIR"
rm -rf "$APPDIR" "$DISTDIR" "$BUILDDIR"

cd "$ROOT_DIR"

python -m pip --version >/dev/null
python -m pip install -U pip >/dev/null
python -m pip install -U pyinstaller >/dev/null
python -m pip install -r requirements.txt >/dev/null

echo "[*] Building PyInstaller bundle…"
python -m PyInstaller \
  --noconfirm \
  --clean \
  --workpath "$BUILDDIR" \
  --distpath "$DISTDIR" \
  --name "$APP_NAME" \
  --onedir \
  tidal_app.py

echo "[*] Creating AppDir…"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/scalable/apps"

cp "packaging/linux/$APP_NAME.desktop" "$APPDIR/usr/share/applications/$APP_NAME.desktop"
cp "packaging/linux/$APP_NAME.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/$APP_NAME.svg"

mkdir -p "$APPDIR/usr/lib/$APP_NAME"
cp -a "$DISTDIR/$APP_NAME/." "$APPDIR/usr/lib/$APP_NAME/"
ln -s "../lib/$APP_NAME/$APP_NAME" "$APPDIR/usr/bin/$APP_NAME"

if [[ "${BUNDLE_FFMPEG:-0}" == "1" ]]; then
  if command -v ffmpeg >/dev/null 2>&1; then
    echo "[*] Bundling system ffmpeg from PATH…"
    cp "$(command -v ffmpeg)" "$APPDIR/usr/bin/ffmpeg"
  else
    echo "[!] BUNDLE_FFMPEG=1 but no ffmpeg found on PATH."
    echo "    Provide an ffmpeg binary on PATH, or handle bundling in CI."
    exit 2
  fi
fi
