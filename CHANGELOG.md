# Changelog

## 0.1.4

- Added a background queue with its own window and context menu actions (play next/append/remove)
- Added track download support with FLAC tagging + cover art embedding (direct FLAC or DASH via ffmpeg)
- Simplified quality/bit-perfect labels and expanded debug logging
- Secured stored credentials file permissions (chmod 600)
- Refactored stream selection/manifest resolution for clarity

## 0.1.3

- Added in-process FLAC decoding via libsndfile (`soundfile`) with ffmpeg fallback for DASH/other streams
- Prefer direct streams when ffmpeg is unavailable
- Added a debug-only toggle to disable ffmpeg usage
- Improved cover art loading, caching, and now-playing UI polish
- Added an “Open album” action from Search results to jump to the album in the URL tab
- Made bit-perfect status text more compact and added decode-path readout

## 0.1.2

- Split the GUI into a search/results panel and a playback/info panel
- Added a now-playing section with album art, track info, and stream details
- Fetch and display album covers (origin size, scaled to fit the UI)
- Made the log panel collapsible and tied Debug to log visibility
- Hid track IDs in the list display

## 0.1.1

- Added GUI keyboard shortcuts (search/load, play/pause/stop, J/K/L and debounced seeking)
- Fixed duplicate playback triggers when activating a track from the list
- Fixed ALSA device selection persistence across refresh/restart
- Added a desktop icon (`packaging/linux/tidal-bitperfect.svg`) and set `Icon=tidal-bitperfect` in the `.desktop` file

## 0.1.0

- PySide6 GUI player (`tidal_app.py`)
- Device-code login with credential caching
- Search + URL loading (track/album/playlist)
- DASH manifest support for hi-res when available
- Direct ALSA PCM output with device selection persistence
- Pause/resume and best-effort seek bar
