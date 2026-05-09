<a href="packaging/linux/tidal-bitperfect.svg">
  <img src="packaging/linux/tidal-bitperfect.svg" width="128">
</a>

# TIDAL Bitperfect

<p align="center">
  <img src="assets/tidal-bitperfect-screenshot.webp" width="900">
</p>

Linux TIDAL player with direct ALSA output, smart caching, and offline support.

## Features

- **Direct ALSA playback** - bit-perfect output with device selection
- **Smart caching** - automatic cache + dedicated downloads folder with offline mode
- **Full library access** - search, collections (favorites sync), playlists, albums, artists
- **Queue & radio** - background queue with context actions and track radio
- **Gapless playback** - seamless track transitions with background prefetch
- **Lyrics panel** - line-synced lyrics when TIDAL provides them
- **FLAC downloads** - proper tagging and embedded cover art
- **Discord Rich Presence** - optional integration to show what you're playing
- **MPRIS D-Bus integration** - media keys, playerctl, KDE Connect support
- **Volume control** - PulseAudio/ALSA mixer support (disabled in bit-perfect mode)

## Build

```bash
cmake -S . -B build
cmake --build build
```

## Run

```bash
./build/tidal-qt6
```

## Usage

### Tabs

- **Search**: search for tracks/albums/playlists/artists
- **URL**: paste TIDAL links (track/album/playlist/artist)
- **Collection**: view synced favorites (tracks/albums/playlists/artists)
- **Cache**: view cached/downloaded tracks, enable offline playback

### Keyboard Shortcuts

**Navigation:**
- `Ctrl+1/2/3`: switch tabs (Search/URL/Collection)
- `Ctrl+F`: focus search box
- `Ctrl+L`: focus URL box
- `F5` / `Ctrl+R`: refresh ALSA devices / retry login

**Playback:**
- `Ctrl+Enter` / `Ctrl+Space` / `K`: play/pause
- `Ctrl+Shift+Enter`: skip to next track
- `Ctrl+.` / `Esc`: stop
- `Ctrl+Left/Right` or `J/L`: seek -10s / +10s

### Settings

- **Cache sizing**: configure max cache size, clear tracks/covers
- **Downloads**: manage downloaded tracks separately from cache
- **Diagnostics**: debug logging, disable ffmpeg/cache, force fresh login
- **Discord RPC**: enable Rich Presence with custom client ID
- **MPRIS**: enable D-Bus media player interface for desktop integration

### Context Menus

Right-click on tracks, albums, playlists, or artists for actions like:
- Play next / Append to queue / Remove from queue
- Download / Delete from cache
- Open album/artist
- Favorite/Unfavorite
- Queue track radio

## Requirements

- Linux + ALSA
- `ffmpeg` (for DASH/manifest streams and downloads)
- `cmake`
- a C++17 compiler
- Qt6 Core/Widgets/Network/DBus development packages
- ALSA development headers
- libsndfile development headers

The build produces `tidal-qt6` and `tidal-native-player`.

## Cache & Offline Mode

Cache location: `~/.cache/tidal-bitperfect`

- `audio/`: automatically cached tracks (size-limited, managed by app)
- `covers/`: downscaled album art (max 1280px)
- `downloads/`: user-triggered downloads (not counted against cache limit)

When offline, the app plays from cache and downloads. Use the Cache tab to manage stored tracks.

## Desktop Integration

The Qt6 app sets a stable app ID: `tidal-bitperfect-qt6`.

```bash
mkdir -p ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
cp packaging/linux/tidal-bitperfect-qt6.desktop ~/.local/share/applications/
cp packaging/linux/tidal-bitperfect-qt6.svg ~/.local/share/icons/hicolor/scalable/apps/
```

## How Playback Works

- **Preferred**: direct FLAC streams decoded by `tidal-native-player`
- **Fallback**: ffmpeg decodes DASH/manifest streams under native process control
- **Output**: PCM written directly to ALSA (no DSP)

## Notes

- Output is bit-perfect (no player DSP), but some DACs reject packed 24-bit (`S24_3LE`). The app outputs padded 24-in-32 for reliability.
- Seeking on streaming/DASH inputs is approximate.
- Offline mode requires cached or downloaded tracks.
- Discord timer increments continuously from last RPC update (Discord API limitation).

## Legacy Python App

The old PySide6 app and CLI are kept under `legacy/python/` for reference:

```bash
cd legacy/python
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python tidal_app.py
python tidal_bitperfect.py --query "aphex twin flim" --pick
```
