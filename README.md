<a href="packaging/linux/tidal-bitperfect.svg">
  <img src="packaging/linux/tidal-bitperfect.svg" width="128">
</a>

# TIDAL Bitperfect (ALSA)

Small TIDAL player for Linux that decodes via in-process FLAC (when possible) or `ffmpeg` fallback, and writes PCM directly to an ALSA device.

This repo contains:
- `tidal_app.py`: PySide6 GUI player (recommended)
- `tidal_core.py`: shared TIDAL helpers (login/search/link parsing/WAV header parsing)
- `tidal_bitperfect.py`: legacy CLI player

## Features

- TIDAL device-code login (token saved to `~/.config/tidal/credentials.json`)
- Search or paste a TIDAL URL (track/album/playlist)
- Direct ALSA output (pick any ALSA PCM string, e.g. `hw:CARD=BTR5,DEV=0`)
- Pause/resume (stops ALSA + freezes `ffmpeg`)
- Seek bar (best-effort seek by restarting `ffmpeg` at an offset)
- Split-view UI with a now-playing panel, album art, and stream details
- Collapsible log panel with Debug toggle
- Hi-res support via DASH manifests when available
- Track downloads (FLAC) with tagging + cover art (when a direct FLAC or DASH manifest is available)
- Keyboard-friendly controls (search/play/pause/stop/seek)

## Requirements

- Linux + ALSA
- `ffmpeg` on your `PATH` (fallback for DASH/manifest streams)
- libsndfile (for in-process FLAC via `soundfile`)
- Python 3.10+ (tested on newer)

Python deps:
- `tidalapi`
- `pyalsaaudio`
- `PySide6`
- `soundfile`
- `mutagen` (for FLAC tagging on downloads)

## Install

Create a venv and install deps:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Or install as a package (adds a `tidal-bitperfect` command):

```bash
pip install .
```

## Run

GUI:

```bash
python tidal_app.py
```

Or, if installed:

```bash
tidal-bitperfect
```

## Keyboard shortcuts (GUI)

Text fields:
- `Enter` in Search box: search
- `Enter` in URL box: load URL

Global (use modifiers so typing isn’t affected):
- `Ctrl+1`: switch to Search tab
- `Ctrl+2`: switch to URL tab
- `Ctrl+3`: switch to Favorites tab
- `Ctrl+F`: focus Search box (select all)
- `Ctrl+L`: focus URL box (select all)
- `F5` / `Ctrl+R`: refresh ALSA device list

Playback:
- `Ctrl+Enter`: play selected track
- `Ctrl+Space`: play/pause (plays selected if idle)
- `Ctrl+.`: stop
- `Ctrl+Left` / `Ctrl+Right`: seek -10s / +10s (debounced; slider previews immediately)
- Also (when not typing in a text field): `J` / `L` seek -10s / +10s, `K` play/pause, `Esc` stop

CLI (legacy):

```bash
python tidal_bitperfect.py --query "aphex twin flim" --pick
```

## Notes on “bit-perfect”

- Output is written straight to ALSA (no player DSP), but:
  - Seeking is approximate on streaming/DASH inputs.
  - Some DACs do not accept packed 24-bit (`S24_3LE`). For reliability this app may output padded 24-in-32 PCM; sample rate is preserved.

## Desktop integration (Wayland)

The app sets a stable desktop file name / app id: `tidal-bitperfect`.

For best integration, install the desktop file:

```bash
mkdir -p ~/.local/share/applications
cp packaging/linux/tidal-bitperfect.desktop ~/.local/share/applications/
```

Install the icon (SVG):

```bash
mkdir -p ~/.local/share/icons/hicolor/scalable/apps
cp packaging/linux/tidal-bitperfect.svg ~/.local/share/icons/hicolor/scalable/apps/tidal-bitperfect.svg
```

You may need to restart your shell/session for the icon to appear in menus.

Edit the `Exec=` line to point at your Python/venv if needed.
