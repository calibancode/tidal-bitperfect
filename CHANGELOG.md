# Changelog

## 0.2.1

### Features
- Added Discord Rich Presence integration to show currently playing tracks with album art, quality info, and playback "progress"
- Added MPRIS D-Bus integration via dbus-fast for media key support, playerctl, KDE Connect, and desktop environment playback controls
- Added gapless playback with prefetch support for FLAC and DASH streams

### Improvements
- Double-click on album/artist/playlist items now expands/collapses them
- Reordered track context menu: play/queue actions grouped together, radio actions in their own section
- Removed settings button shake on cache-full

### Bug Fixes
- Fixed Qt fatal abort on close caused by background threads still running during teardown
- Fixed crash when stopping playback after a gapless transition
- Fixed desktop relaunch opening a second instance; relaunch now focuses the existing window
- Fixed gapless prefetch race conditions (stale next-track delivery after queue/playback changes)
- Fixed shared-session quality selection contention between active playback and prefetch workers

## 0.2.0

### Features
- Added volume slider control (0-100%) with persistent settings, disabled in bit-perfect mode
- Added now-playing context menus (right-click on cover, title, metadata for full track actions)
- Added cache system with size limits, cover caching, and on-disk metadata index
- Added Cache tab with separate cache/downloads lists and queue actions
- Added offline mode for cached/downloaded playback
- Added Settings window with cache controls and diagnostics (debug, ffmpeg, cache)
- Added download workflow to cache (including DASH/ffmpeg fallback) with delete action
- Added cache clearing options (tracks/covers/both) with size details
- Added download folder quick-access button to open downloads directory
- Added faster cached playback (skip network when cached)
- Added per-cover downscaling before caching
- Added Queue radio and richer context-menu actions (open album/artist)
- Added search modes for tracks/albums/playlists/artists with tree results
- Added Collection tab modes for tracks/albums/playlists/artists
- Added lazy artist/album loading to avoid heavy API calls on expand
- Added playlist/album/artist support for URL loading and context menus
- Added artist top track playback (play first, queue remaining)
- Added album cover display when selecting albums
- Added FLAC metadata tagging and cover art embedding for downloads

### Improvements
- Volume control supports both PulseAudio/PipeWire (pactl) and ALSA mixer backends
- Split playback/cache logic into a separate module for maintainability
- Extracted TIDAL URL parsing helpers into a shared module

### Bug Fixes
- Fixed offline stop/skip getting stuck on stalled DASH streams
- Fixed artist top track playback queuing behavior

## 0.1.5

- Added track radio and queue cleanup for radio playback
- Added synced favorites list with Favorite/Unfavorite actions
- Added a default cover art fallback (app icon)
- Tweaked playback controls and queue/bit-perfect labeling
- Warn when ALSA rejects padded 24-in-32 output

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
