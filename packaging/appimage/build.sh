#!/usr/bin/env bash
set -euo pipefail

# Builds an AppImage for x86_64 using:
# - PyInstaller (to bundle Python + deps)
# - linuxdeploy + appimagetool (to produce the AppImage)
#
# Optional:
#   BUNDLE_FFMPEG=1 to embed an `ffmpeg` binary in the AppImage.
#
# Typical usage (local):
#   python -m venv .venv && source .venv/bin/activate
#   pip install -r requirements.txt pyinstaller
#   ./packaging/appimage/build.sh
#
# This script expects `linuxdeploy-x86_64.AppImage` on PATH, or it will download
# it if `LINUXDEPLOY` is unset and `curl` is available.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK_DIR="${WORK_DIR:-"$ROOT_DIR/.appimage-build"}"
APPDIR="$WORK_DIR/AppDir"
DISTDIR="$WORK_DIR/dist"
BUILDDIR="$WORK_DIR/build"
OUTDIR="${OUTDIR:-"$ROOT_DIR/dist"}"

APP_NAME="tidal-bitperfect"
ARCH="x86_64"

mkdir -p "$WORK_DIR" "$OUTDIR"
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

LINUXDEPLOY="${LINUXDEPLOY:-}"
if [[ -z "$LINUXDEPLOY" ]]; then
  if command -v linuxdeploy-x86_64.AppImage >/dev/null 2>&1; then
    LINUXDEPLOY="$(command -v linuxdeploy-x86_64.AppImage)"
  elif command -v linuxdeploy >/dev/null 2>&1; then
    LINUXDEPLOY="$(command -v linuxdeploy)"
  fi
fi

if [[ -z "$LINUXDEPLOY" ]]; then
  if command -v curl >/dev/null 2>&1; then
    echo "[*] Downloading linuxdeploy…"
    LINUXDEPLOY="$WORK_DIR/linuxdeploy-x86_64.AppImage"
    curl -L -o "$LINUXDEPLOY" "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage"
    chmod +x "$LINUXDEPLOY"
  else
    echo "[!] linuxdeploy not found and curl unavailable."
    echo "    Install linuxdeploy (linuxdeploy-x86_64.AppImage) and re-run."
    exit 2
  fi
fi

echo "[*] Building AppImage…"
chmod +x "$LINUXDEPLOY" || true

export VERSION="${VERSION:-0.0.0+local}"
"$LINUXDEPLOY" \
  --appdir "$APPDIR" \
  -d "packaging/linux/$APP_NAME.desktop" \
  -i "packaging/linux/$APP_NAME.svg" \
  --output appimage

APPIMAGE_NAME="$APP_NAME-$VERSION-$ARCH.AppImage"
if [[ -f "$ROOT_DIR/$APPIMAGE_NAME" ]]; then
  mv "$ROOT_DIR/$APPIMAGE_NAME" "$OUTDIR/$APPIMAGE_NAME"
else
  # linuxdeploy may output something like AppName-x86_64.AppImage depending on VERSION handling.
  found="$(ls -1 "$ROOT_DIR"/*.AppImage 2>/dev/null | head -n 1 || true)"
  if [[ -n "$found" ]]; then
    mv "$found" "$OUTDIR/$APPIMAGE_NAME"
  else
    echo "[!] AppImage output not found."
    exit 2
  fi
fi

echo "[✓] Wrote: $OUTDIR/$APPIMAGE_NAME"

