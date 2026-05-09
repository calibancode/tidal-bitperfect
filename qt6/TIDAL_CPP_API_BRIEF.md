# Native TIDAL API Implementation Notes

The Qt6 app no longer starts the old JSON-lines Python TIDAL sidecar. The
`TidalSidecar` class name is kept as a compatibility facade for `MainWindow`,
but its implementation now uses QtNetwork directly.

Implemented native command surface:

- `login`: saved OAuth credential reuse, device-code login, token refresh,
  `/sessions` context loading, and subscription validation.
- `search`: typed search for tracks, albums, playlists, and artists.
- `url`: TIDAL link parsing plus track/album/playlist/artist loading.
- `collection`: favorites and collection listing, including v2 playlist folders.
- `home`: v2 home feed request with tolerant supported-item extraction.
- `lyrics`: lyrics endpoint plus LRC timestamp parsing.
- `radio`: track radio fallback endpoints and artist radio.
- `details`: album, playlist, artist, and mix detail loading.
- `favorite`: add/remove mutations for tracks, albums, artists, and playlists.
- `stream`: quality probing, playback info parsing, DASH MPD materialization, and
  direct URL fallback.
- `download`: direct FLAC download or ffmpeg FLAC transcode, plus compatible
  `~/.cache/tidal-bitperfect/index.json` download metadata.

## Normalized media-object contract

The C++ API layer should hand UI, playback, cache, Discord RPC, and MPRIS the
same normalized object shapes that the old `tidalapi` path effectively
provided.

Track objects must include:

- `id`, `title`, `duration`
- `artist`, `artist_id`, `artists`, `artist_display`
- `album`, `album_id`
- `cover_url` and `cover_thumbnail_url`
- `audio_quality` and `track_max_quality` when the catalog or stream exposes
  them
- `bit_depth` and `sample_rate` once a playback/download stream has been
  resolved

Album objects must include `id`, `album_id`, `title`, `artist`, `artist_id`,
`artists`, `artist_display`, `cover_url`, `cover_thumbnail_url`, and a `tracks`
array when details are loaded. Playlist objects must include `id`, `title`,
`creator`, `cover_url`, and a `tracks` array when details are loaded. Artist
objects must include `id`, `name`, `cover_url`, plus `tracks`, `albums`, and
`ep_singles` when details are loaded.

Stream descriptors must carry the resolved playable `input`, direct `url` when
available, DASH `mpd_path` when materialized, `duration_s`, the enriched
`track` object, `audio_quality`, `track_max_quality`, `bit_depth`, and
`sample_rate`. Playback code merges missing stream-derived fields back onto the
active track before updating the UI/integrations.

Collection responses must mark returned items as `favorite: true`, and the Qt
layer must maintain favorite-id sets for tracks, albums, playlists, and artists
so context menus can show `Favorite` or `Unfavorite` without relying on the
currently displayed tab.

Cache/download index entries should persist all navigation, artwork, and quality
fields needed for local playback: title/artist/album ids and display names,
`cover_url`, `cover_thumbnail_url`, `audio_quality`, `track_max_quality`,
`bit_depth`, and `sample_rate`. Older cache entries may lack these fields, so
local playback also reconstructs bit depth/sample rate from the decoded file
format.

Local contract smoke check:

```sh
python3 qt6/media_contract_smoke.py
```

The old sidecar file has been removed from `qt6/`. Legacy Python application
files live under `legacy/python/` and still have their own dependency set; they
are not needed to build or run `tidal-qt6`.

Smoke-test checklist before publishing this as user-ready:

- Saved login reuse and first-time device-code login.
- Search for all four media types.
- URL load for track, album, playlist, and artist links.
- Home feed rendering with unknown modules ignored.
- Album, playlist, artist, and mix expansion.
- Track and artist radio.
- Lyrics with and without synced subtitles.
- Streaming at DASH and direct URL qualities where the account/catalog permits.
- Download from direct FLAC and DASH/non-FLAC inputs.
- Favorites list and add/remove for each supported type.
- Old Python-created cache/download index entries still display and play.
