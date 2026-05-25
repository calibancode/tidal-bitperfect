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
        # TIDAL/media normalization: downstream UI, cache rows, RPC, and playback all
        # assume these stable keys exist regardless of the upstream response shape.
        ("qt6/tidal_client_models.cpp", r"parseTrack.*artist_id.*artists.*artist_display.*album_id.*duration.*audio_quality.*track_max_quality.*cover_url.*cover_thumbnail_url", "normalized track contract"),
        ("qt6/tidal_client_models.cpp", r"parseAlbum.*album_id.*artist_id.*artists.*artist_display.*cover_url.*cover_thumbnail_url", "normalized album contract"),
        ("qt6/tidal_client_models.cpp", r"parsePlaylist.*id.*title.*creator.*cover_url", "normalized playlist contract"),
        ("qt6/tidal_client_models.cpp", r"parseArtist.*id.*name.*cover_url", "normalized artist contract"),
        ("qt6/tidal_client_models.cpp", r"mixImageString.*mixImages.*detailMixImages.*parseMix.*titleTextInfo.*subtitleTextInfo.*cover_thumbnail_url", "normalized home mix contract"),
        ("qt6/tidal_client_models.cpp", r"homeItemsFromValue.*typeHint.*childTypeHintFromModule.*homeItemFromObject.*rawMediaType.*QStringLiteral\(\"mix\"\).*looksLikeMix", "home feed list-module mix extraction"),
        ("qt6/tidal_client_endpoints.cpp", r"playableUrlFromEncodedManifest", "base64 JSON manifest URL extraction"),
        ("qt6/tidal_client_endpoints.cpp", r"hasDashManifest", "DASH manifest detection"),
        ("qt6/tidal_client_endpoints.cpp", r"loadMix.*MIX_HEADER.*TRACK_LIST.*pagedList.*appendTracks.*homeItemsFromValue", "mix details parse header separately from track list"),
        ("qt6/tidal_client_endpoints.cpp", r"streamDescriptorFromCandidate.*track.*duration_s.*track_max_quality.*audio_quality.*bit_depth.*sample_rate", "stream descriptor enrichment"),
        ("qt6/tidal_client.cpp", r"cmdPrefetch.*fetchStreamCandidates.*storeAudio", "queued stream prefetch into audio cache"),
        ("qt6/tidal_client.cpp", r"cmdDetails.*loadTrack", "track detail hydration endpoint"),

        # Cache/download metadata: older cache entries and local playback paths need
        # enough metadata to render art, quality, albums, and favorite state.
        ("qt6/tidal_client_endpoints.cpp", r"audioDir.*audioPath.*storeAudio.*QStringLiteral\(\"audio\"\)", "audio cache write path"),
        ("qt6/tidal_client_endpoints.cpp", r"storeDownload.*cover_thumbnail_url.*audio_quality.*track_max_quality.*bit_depth.*sample_rate", "download index quality persistence"),
        ("qt6/cache_manager.h", r"coverThumbnailUrl.*audioQuality.*trackMaxQuality.*bitDepth.*sampleRate", "cache entry quality/artwork fields"),
        ("qt6/cache_manager.cpp", r"cover_thumbnail_url.*audio_quality.*track_max_quality.*bit_depth.*sample_rate", "cache entry quality/artwork parsing"),
        ("qt6/cache_manager.cpp", r"QCryptographicHash::Sha1.*coverBytes.*storeCoverBytes", "Python-compatible disk cover cache"),
        ("qt6/cache_manager.cpp", r"deleteCachedAudio.*audio\.remove.*saveIndex", "stale audio-cache entry removal"),
        ("qt6/tidal_client.cpp", r"markFavoriteItems", "collection favorite tagging"),
        ("qt6/main_window.cpp", r"refreshFavoriteState", "favorite state sync"),
        ("qt6/main_window.cpp", r"addFavoriteAction.*Unfavorite.*Favorite", "favorite/unfavorite menu toggle"),
        ("qt6/main_window.cpp", r"rememberTracks.*shouldRememberTrackObject", "track cache avoids container pollution"),
        ("qt6/playback_controller.cpp", r"formatReady.*m_streamSampleRate.*m_streamBitDepth.*qualityChanged", "local playback quality reconstruction"),
        ("qt6/main_window.cpp", r"handlePlaybackState.*qualityLabelText", "local quality label rendering"),
        ("qt6/main_window.cpp", r"trackObjectForEntry.*cover_thumbnail_url.*audio_quality.*track_max_quality.*bit_depth.*sample_rate", "cache/download row restoration"),
        ("qt6/main_window.cpp", r"hydrateTrackDetails.*details.*openTrackAlbum.*Open album.*hydrateTrackDetails", "cached track detail hydration before album open"),

        # Qt/native daemon boundary: keep the framed protocol and exact format
        # reporting from regressing back to the old tab/newline helper behavior.
        ("qt6/native_playback_client.cpp", r"encodePayload.*sendMessage.*takeFrame.*handleMessage", "Qt framed native IPC"),
        ("native/daemon_protocol.cpp", r"encode_payload.*poll_messages.*read_ipc_message_blocking", "native framed daemon IPC"),
        ("native/daemon_protocol.cpp", r"header_end \+ 1.*message = \{\}.*return true", "native IPC parser resynchronizes after invalid headers"),
        ("qt6/native_playback_client.cpp", r"DONE.*wasBusy.*finishedOk", "daemon stays alive after DONE"),
        ("native/alsa_output.cpp", r"snd_pcm_hw_params_get_channels.*snd_pcm_hw_params_get_rate.*snd_pcm_hw_params_get_format.*applied_format_", "actual ALSA hw params reporting"),
        ("native/alsa_output.cpp", r"emit_position\(SNDFILE\* file.*emit_position_values\(pos_s, fmt\.duration_s\)", "local FLAC positions use framed seconds/duration fields"),
        ("qt6/playback_controller.cpp", r"sourceBits.*m_outputFormat\.sampleRate.*m_outputFormat\.bitDepth", "PlaybackState separates source and output formats"),
        ("qt6/playback_controller.cpp", r"maybePrefetchNext.*QStringLiteral\(\"prefetch\"\).*m_prefetchToken.*setNextTrack", "queue-head prefetch delivered to native handoff"),
        ("qt6/playback_controller.cpp", r"native next format mismatch.*deleteCachedAudio", "stale prefetched audio is cleared after native handoff mismatch"),
        ("qt6/tidal_client.cpp", r"target_bit_depth.*target_sample_rate.*cache_reason.*cache_priority.*targetSampleFormat", "prefetch preserves native PCM format compatibility and cache intent"),

        # Smart cache policy is user-visible and data-destructive when wrong, so
        # keep a small guard that pruning still considers intent and recency.
        ("qt6/cache_manager.cpp", r"audioEvictionScore.*reasonWeight.*playCount", "smart cache eviction scoring"),
        ("qt6/cache_manager.cpp", r"markAudioUsed.*last_used.*play_count.*last_played", "smart cache usage metadata"),
        ("qt6/cache_manager.cpp", r"enforceAudioLimit.*audioEvictionScore.*enforceCoverLimit.*enforceLimits", "smart cache size limit pruning"),
    ]
    for path, pattern, note in checks:
        require(path, pattern, note)

    forbidden = [
        ("qt6/main_window.cpp", r"menu\.addAction\(QStringLiteral\(\"Favorite\"\)", "hardcoded Favorite menu action bypassing contract helper"),
        ("qt6/native_playback_client.cpp", r"QStringList\{.*play_|join\('\\t'\)|send\(QStringLiteral\(\"stop|send\(QStringLiteral\(\"shutdown", "legacy tab/newline native commands"),
        ("qt6/native_playback_client.cpp", r"DONE.*sendMessage\(QStringLiteral\(\"shutdown", "DONE-triggered daemon shutdown"),
        ("native/daemon_protocol.cpp", r"std::getline\(std::cin|split_tabs", "legacy newline/tab daemon commands"),
        ("native/playback_state.cpp", r"next\\t|handle_next_command_line|poll_lines", "legacy newline/tab playback commands"),
    ]
    for path, pattern, note in forbidden:
        forbid(path, pattern, note)

    print(f"media contract smoke: {len(checks) + len(forbidden)} checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"media contract smoke: {exc}", file=sys.stderr)
        raise SystemExit(1)
