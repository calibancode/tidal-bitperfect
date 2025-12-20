<a href="packaging/linux/tidal-bitperfect.svg">
  <img src="packaging/linux/tidal-bitperfect.svg" width="128">
</a>

# TIDAL Bitperfect (ALSA)

Linux TIDAL player that decodes FLAC in-process when possible (libsndfile) or falls back to ffmpeg, and writes PCM directly to ALSA.

This repo contains:
- `tidal_app.py`: PySide6 GUI player (recommended)
- `tidal_core.py`: shared TIDAL helpers (login/search/link parsing/WAV header parsing)
- `tidal_bitperfect.py`: legacy CLI player

## Who this is for (and who it is not)

For:
- People who want direct ALSA output and are fine with Linux-only tooling.
- Anyone who values a simple player over a full media library.

Not for:
- If you want official TIDAL features, integrations, or cross-platform support.
- If you expect bulletproof streaming under every network/device setup.
- If you want a full library manager with rich metadata and playlist tooling.

## What this proves

- Direct ALSA playback from TIDAL streams can be done with minimal DSP.
- Cached FLAC can be replayed quickly without network calls.
- A small, keyboard-friendly GUI can stay fast without a web stack.

## What will probably break

- ALSA device quirks (especially 24-bit packed formats) and exclusive access.
- DASH/manifest edge cases if TIDAL or tidalapi changes response formats.
- Offline mode when caches are empty (offline only plays cached/downloaded tracks).
- Cache metadata drift if files are moved outside the app.

## Highlights

- Device-code login with cached credentials (`~/.config/tidal/credentials.json`)
- Search (tracks only) and URL loading (track/album/playlist)
- Direct ALSA output with device picker and bit-perfect status hints
- Queue window with play-next/append/remove and radio mixes
- Cache + Downloads tab with offline playback support
- Track downloads to cache with tagging and embedded cover art
- Settings window with cache sizing and diagnostics toggles

## How playback works

- Preferred path: direct FLAC stream decoded in-process via `soundfile`.
- Fallback: ffmpeg decodes to WAV PCM (also used for DASH/manifest streams).
- Output: PCM written straight to ALSA.

## Cache, downloads, and offline

Cache root: `~/.cache/tidal-bitperfect`

- `audio/`: cached tracks for fast replay (size-limited)
- `covers/`: downscaled cover art (max 1280px)
- `downloads/`: user-triggered downloads (not counted against cache size)

The Cache tab shows two lists (Cache + Downloads) and supports offline playback when the app is launched without internet.

Clearing cache:
- Clear tracks, covers, or both.
- Downloads are managed separately and have their own Clear button.

Diagnostics:
- Enable debug log (opens log window)
- Disable ffmpeg
- Disable cache (prevents cache reads and writes)

## Track context menu

- Play / Play next / Append to queue
- Play radio or queue radio
- Favorite/Unfavorite
- Copy track/album link
- Open album (loads the URL tab)
- Download track / Delete track (if already cached/downloaded)

## Requirements

- Linux + ALSA
- `ffmpeg` on your PATH (DASH/manifest playback and downloads)
- libsndfile (for in-process FLAC via `soundfile`)
- Python 3.10+ (tested on newer)

Python deps:
- `tidalapi`
- `pyalsaaudio`
- `PySide6`
- `soundfile`
- `mutagen` (FLAC tagging for downloads)

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

Global (use modifiers so typing is not affected):
- `Ctrl+1`: switch to Search tab
- `Ctrl+2`: switch to URL tab
- `Ctrl+3`: switch to Favorites tab
- `Ctrl+F`: focus Search box (select all)
- `Ctrl+L`: focus URL box (select all)
- `F5` / `Ctrl+R`: refresh ALSA device list (also re-attempts login when offline)

Playback:
- `Ctrl+Enter`: play/pause (plays selected if idle)
- `Ctrl+Shift+Enter`: skip to next queued track
- `Ctrl+Space`: play/pause (plays selected if idle)
- `Ctrl+.`: stop
- `Ctrl+Left` / `Ctrl+Right`: seek -10s / +10s (debounced; slider previews immediately)
- Also (when not typing in a text field): `J` / `L` seek -10s / +10s, `K` play/pause, `Esc` stop

CLI (legacy):

```bash
python tidal_bitperfect.py --query "aphex twin flim" --pick
```

## Notes on bit-perfect

- Output is written straight to ALSA (no player DSP), but:
  - Seeking is approximate on streaming/DASH inputs.
  - Some DACs do not accept packed 24-bit (`S24_3LE`). For reliability this app may output padded 24-in-32 PCM; sample rate is preserved.
  - If padded 24-in-32 is rejected by ALSA, replug the DAC or use `plughw`/`default`.

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
