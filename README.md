# TIDAL Bitperfect (ALSA)

Small TIDAL player for Linux that decodes via `ffmpeg` and writes PCM directly to an ALSA device.

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
- Hi-res support via DASH manifests when available

## Requirements

- Linux + ALSA
- `ffmpeg` on your `PATH`
- Python 3.10+ (tested on newer)

Python deps:
- `tidalapi`
- `pyalsaaudio`
- `PySide6`

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

Edit the `Exec=` line to point at your Python/venv if needed.
