#!/usr/bin/env python3
"""Static smoke checks for the Qt6 native TIDAL media-object contract."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def require(path: str, pattern: str, note: str) -> None:
    text = read(path)
    if not re.search(pattern, text, re.S):
        raise AssertionError(f"{path}: missing {note}")


def forbid(path: str, pattern: str, note: str) -> None:
    text = read(path)
    if re.search(pattern, text, re.S):
        raise AssertionError(f"{path}: forbidden {note}")


def main() -> int:
    checks = [
        ("qt6/tidal_sidecar_models.cpp", r"parseTrack.*artist_id.*artists.*artist_display.*album_id.*duration.*audio_quality.*track_max_quality.*cover_url.*cover_thumbnail_url", "normalized track contract"),
        ("qt6/tidal_sidecar_models.cpp", r"parseAlbum.*album_id.*artist_id.*artists.*artist_display.*cover_url.*cover_thumbnail_url", "normalized album contract"),
        ("qt6/tidal_sidecar_models.cpp", r"parsePlaylist.*id.*title.*creator.*cover_url", "normalized playlist contract"),
        ("qt6/tidal_sidecar_models.cpp", r"parseArtist.*id.*name.*cover_url", "normalized artist contract"),
        ("qt6/tidal_sidecar_endpoints.cpp", r"playableUrlFromEncodedManifest", "base64 JSON manifest URL extraction"),
        ("qt6/tidal_sidecar_endpoints.cpp", r"hasDashManifest", "DASH manifest detection"),
        ("qt6/tidal_sidecar_endpoints.cpp", r"streamDescriptorFromCandidate.*track.*duration_s.*track_max_quality.*audio_quality.*bit_depth.*sample_rate", "stream descriptor enrichment"),
        ("qt6/tidal_sidecar_endpoints.cpp", r"storeDownload.*cover_thumbnail_url.*audio_quality.*track_max_quality.*bit_depth.*sample_rate", "download index quality persistence"),
        ("qt6/cache_manager.h", r"coverThumbnailUrl.*audioQuality.*trackMaxQuality.*bitDepth.*sampleRate", "cache entry quality/artwork fields"),
        ("qt6/cache_manager.cpp", r"cover_thumbnail_url.*audio_quality.*track_max_quality.*bit_depth.*sample_rate", "cache entry quality/artwork parsing"),
        ("qt6/tidal_sidecar.cpp", r"markFavoriteItems", "collection favorite tagging"),
        ("qt6/main_window.cpp", r"refreshFavoriteState", "favorite state sync"),
        ("qt6/main_window.cpp", r"addFavoriteAction.*Unfavorite.*Favorite", "favorite/unfavorite menu toggle"),
        ("qt6/main_window.cpp", r"rememberTracks.*shouldRememberTrackObject", "track cache avoids container pollution"),
        ("qt6/playback_controller.cpp", r"formatReady.*m_streamSampleRate.*m_streamBitDepth.*qualityChanged", "local playback quality reconstruction"),
        ("qt6/main_window.cpp", r"qualityChanged.*qualityLabelText", "local quality label rendering"),
        ("qt6/main_window.cpp", r"trackObjectForEntry.*cover_thumbnail_url.*audio_quality.*track_max_quality.*bit_depth.*sample_rate", "cache/download row restoration"),
        ("qt6/discord_rpc_service.cpp", r"cover_thumbnail_url.*cover_url.*large_image", "RPC thumbnail fallback"),
        ("qt6/mpris_service.cpp", r"cover_url.*mpris:artUrl", "MPRIS artwork propagation"),
        ("qt6/scrobble_service.cpp", r"track\.updateNowPlaying.*track\.scrobble", "Last.fm now playing and scrobble calls"),
        ("qt6/scrobble_service.cpp", r"submit-listens.*playing_now.*single", "ListenBrainz now playing and listen submissions"),
        ("qt6/scrobble_service.cpp", r"kMinimumScrobbleDurationSeconds.*30\.0.*kMaximumThresholdSeconds.*240\.0", "scrobble threshold constants"),
        ("qt6/scrobble_service.cpp", r"m_session\.listenedSeconds.*scrobbleThreshold", "scrobble uses accumulated listened time"),
        ("qt6/main_window.cpp", r"Scrobbling.*Last\.fm.*ListenBrainz", "scrobbling settings integration"),
        ("qt6/TIDAL_CPP_API_BRIEF.md", r"Normalized media-object contract", "documented contract"),
    ]
    for path, pattern, note in checks:
        require(path, pattern, note)

    forbid("qt6/main_window.cpp", r"menu\.addAction\(QStringLiteral\(\"Favorite\"\)", "hardcoded Favorite menu action bypassing contract helper")

    print(f"media contract smoke: {len(checks) + 1} checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"media contract smoke: {exc}", file=sys.stderr)
        raise SystemExit(1)
