# Changelog

## Unreleased

- Added GUI keyboard shortcuts (search/load, play/pause/stop, J/K/L and debounced seeking)
- Fixed ALSA device selection persistence across refresh/restart
- Added a desktop icon (`packaging/linux/tidal-bitperfect.svg`) and set `Icon=tidal-bitperfect` in the `.desktop` file
- Added AppImage build scaffolding (GitHub Actions workflow + `packaging/appimage/build.sh`)

## 0.1.0

- PySide6 GUI player (`tidal_app.py`)
- Device-code login with credential caching
- Search + URL loading (track/album/playlist)
- DASH manifest support for hi-res when available
- Direct ALSA PCM output with device selection persistence
- Pause/resume and best-effort seek bar
