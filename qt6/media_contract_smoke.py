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
        ("qt6/tidal_client_models.cpp", r"parseTrack.*artist_id.*artists.*artist_display.*album_id.*duration.*audio_quality.*track_max_quality.*cover_url.*cover_thumbnail_url", "normalized track contract"),
        ("qt6/tidal_client_models.cpp", r"parseAlbum.*album_id.*artist_id.*artists.*artist_display.*cover_url.*cover_thumbnail_url", "normalized album contract"),
        ("qt6/tidal_client_models.cpp", r"parsePlaylist.*id.*title.*creator.*cover_url", "normalized playlist contract"),
        ("qt6/tidal_client_models.cpp", r"parseArtist.*id.*name.*cover_url", "normalized artist contract"),
        ("qt6/tidal_client_endpoints.cpp", r"playableUrlFromEncodedManifest", "base64 JSON manifest URL extraction"),
        ("qt6/tidal_client_endpoints.cpp", r"hasDashManifest", "DASH manifest detection"),
        ("qt6/tidal_client_endpoints.cpp", r"streamDescriptorFromCandidate.*track.*duration_s.*track_max_quality.*audio_quality.*bit_depth.*sample_rate", "stream descriptor enrichment"),
        ("qt6/tidal_client.cpp", r"cmdPrefetch.*fetchStreamCandidates.*storeAudio", "queued stream prefetch into audio cache"),
        ("qt6/tidal_client.cpp", r"cmdDetails.*loadTrack", "track detail hydration endpoint"),
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
        ("qt6/browser_controller.cpp", r"BrowserController::loadHome.*BrowserController::search.*BrowserController::loadUrl.*BrowserController::refreshCollection", "browser request controller"),
        ("qt6/browser_controller.cpp", r"BrowserController::loadContainerDetails.*showLoadingPlaceholder.*details.*detailLoaded", "browser lazy detail controller"),
        ("CMakeLists.txt", r"qt6/browser_controller\.cpp", "browser controller build integration"),
        ("qt6/lyrics_controller.cpp", r"LyricsController::loadLyrics.*lyrics.*timed_lines.*updatePosition", "lyrics loading and timed state controller"),
        ("qt6/lyrics_controller.cpp", r"eventFilter.*holdAutoScroll.*seekToLyricItem.*seekRequested", "lyrics manual scroll hold and seek signal"),
        ("CMakeLists.txt", r"qt6/lyrics_controller\.cpp", "lyrics controller build integration"),
        ("qt6/settings_dialog.cpp", r"SettingsDialog::buildPlaybackTab.*SettingsDialog::buildStorageTab.*SettingsDialog::buildIntegrationsTab.*SettingsDialog::buildHealthTab", "settings dialog tabs extracted"),
        ("qt6/settings_dialog.cpp", r"applyScrobbleConfig.*beginLastFmAuthorization.*completeLastFmAuthorization", "settings dialog scrobble controls"),
        ("CMakeLists.txt", r"qt6/settings_dialog\.cpp", "settings dialog build integration"),
        ("qt6/native_playback_client.cpp", r"encodePayload.*sendMessage.*takeFrame.*handleMessage", "Qt framed native IPC"),
        ("qt6/native_playback_client.cpp", r"smooth_transition", "stream transition smoothing IPC flag"),
        ("qt6/native_playback_client.cpp", r"m_nextTrackId == trackId.*m_nextTrackPath == path", "duplicate native next-track commands are suppressed"),
        ("native/daemon_protocol.cpp", r"encode_payload.*poll_messages.*read_ipc_message_blocking", "native framed daemon IPC"),
        ("native/daemon_protocol.cpp", r"header_end \+ 1.*message = \{\}.*return true", "native IPC parser resynchronizes after invalid headers"),
        ("qt6/native_playback_client.cpp", r"DONE.*wasBusy.*finishedOk", "daemon stays alive after DONE"),
        ("native/playback_state.cpp", r"play_queued_flac.*AlsaPcm&.*same_pcm_format.*ADVANCED", "same-format queued FLAC handoff"),
        ("native/playback_state.cpp", r"CommandReader commands.*playback_loop<short>.*play_queued_flac\(pcm, fmt, state, apply_software_volume, commands\)", "local playback keeps one IPC reader across queued handoff"),
        ("native/playback_state.cpp", r"prepare_next_track.*next_input.*play_queued_flac", "queued FLAC pre-open before handoff"),
        ("native/playback_state.cpp", r"stream_remaining_frames.*queued_stream_handoff_ready.*send_signal\(SIGTERM\)", "streamed gapless handoff follows PCM frame timeline before ffmpeg EOF"),
        ("native/playback_state.cpp", r"smooth_transition_start.*smooth_next_transition.*last_frame", "optional streamed handoff de-click smoothing"),
        ("native/playback_state.cpp", r"read_prefill.*next_prefill.*write_frames", "queued FLAC first block is pre-read for handoff"),
        ("native/playback_state.cpp", r"state\.paused.*prefill_frames.*sf_seek", "prefilled queued FLAC is rewound if handoff starts paused"),
        ("native/playback_state.cpp", r"after_first_write.*ADVANCED", "queued handoff writes audio before UI advance notifications"),
        ("native/alsa_output.cpp", r"snd_pcm_hw_params_get_channels.*snd_pcm_hw_params_get_rate.*snd_pcm_hw_params_get_format.*applied_format_", "actual ALSA hw params reporting"),
        ("native/alsa_output.cpp", r"emit_position\(SNDFILE\* file.*emit_position_values\(pos_s, fmt\.duration_s\)", "local FLAC positions use framed seconds/duration fields"),
        ("qt6/playback_controller.cpp", r"sourceBits.*m_outputFormat\.sampleRate.*m_outputFormat\.bitDepth", "PlaybackState separates source and output formats"),
        ("qt6/playback_controller.cpp", r"maybePrefetchNext.*QStringLiteral\(\"prefetch\"\).*m_prefetchToken.*setNextTrack", "queue-head prefetch delivered to native handoff"),
        ("qt6/playback_controller.cpp", r"native next format mismatch.*deleteCachedAudio", "stale prefetched audio is cleared after native handoff mismatch"),
        ("qt6/tidal_client.cpp", r"target_bit_depth.*target_sample_rate.*cache_reason.*cache_priority.*targetSampleFormat", "prefetch preserves native PCM format compatibility and cache intent"),
        ("qt6/cache_manager.cpp", r"audioEvictionScore.*reasonWeight.*playCount", "smart cache eviction scoring"),
        ("qt6/cache_manager.cpp", r"markAudioUsed.*last_used.*play_count.*last_played", "smart cache usage metadata"),
        ("qt6/cache_manager.cpp", r"enforceAudioLimit.*audioEvictionScore.*enforceCoverLimit.*enforceLimits", "smart cache size limit pruning"),
        ("qt6/native_gapless_smoke.py", r"ffmpeg->file same-format handoff.*ffmpeg->file mismatch fallback.*split seek frame tail resync", "dynamic native gapless smoke cases"),
        ("CMakeLists.txt", r"add_custom_target\(smoke.*media_contract_smoke\.py.*native_gapless_smoke\.py", "combined smoke target"),
        ("qt6/settings_dialog.cpp", r"Gapless playback.*Prefetches the next queued stream into the audio cache.*Soften streamed transitions", "playback transition settings"),
        ("qt6/settings_dialog.cpp", r"Cache Policy.*Audio cache.*Cover cache.*Mode.*Audio limit.*Cover limit", "cache policy settings"),
        ("qt6/playback_controller.cpp", r"setCacheMode.*conservative.*aggressive.*cache_reason.*cache_mode", "playback cache policy mode drives prefetch intent"),
        ("qt6/playback_state.h", r"struct PlaybackState.*trackId.*positionSeconds.*durationSeconds.*streamFormat.*outputFormat", "semantic playback state model"),
        ("qt6/playback_controller.cpp", r"buildPlaybackState.*emitPlaybackState.*playbackStateChanged", "playback controller state projection"),
        ("qt6/main_window.cpp", r"playbackStateChanged.*handlePlaybackState", "main window playback state observer"),
        ("qt6/main_window.cpp", r"artworkUrl.*cover_url.*cover_thumbnail_url.*loadCover.*coverBytes.*storeCoverBytes", "now playing artwork cache and thumbnail fallback"),
        ("qt6/main_window.cpp", r"m_time->setAlignment\(Qt::AlignHCenter \| Qt::AlignVCenter\).*setStyleHint\(QFont::Monospace\).*timeWrap->setMinimumHeight\(32\)", "prominent timestamp readout"),
        ("qt6/scrobble_service.cpp", r"playbackStateChanged.*handlePlaybackState", "scrobble playback state observer"),
        ("qt6/discord_rpc_service.cpp", r"cover_url.*cover_thumbnail_url.*large_image", "RPC cover art fallback"),
        ("qt6/discord_rpc_service.cpp", r"sameTrack.*m_positionSeconds = 0\.0.*changed.*if \(!changed\) return", "RPC timestamp anchor avoids per-position refresh jitter"),
        ("qt6/mpris_service.cpp", r"cover_url.*mpris:artUrl", "MPRIS artwork propagation"),
        ("qt6/scrobble_service.cpp", r"track\.updateNowPlaying.*track\.scrobble", "Last.fm now playing and scrobble calls"),
        ("qt6/scrobble_service.cpp", r"submit-listens.*playing_now.*single", "ListenBrainz now playing and listen submissions"),
        ("qt6/scrobble_service.cpp", r"kMinimumScrobbleDurationSeconds.*30\.0.*kMaximumThresholdSeconds.*240\.0", "scrobble threshold constants"),
        ("qt6/scrobble_service.cpp", r"m_session\.listenedSeconds.*scrobbleThreshold", "scrobble uses accumulated listened time"),
        ("qt6/scrobble_service.cpp", r"ScrobbleSubmission.*Scrobbled.*Queued scrobble for retry.*Scrobble failed", "provider-aggregated scrobble status"),
        ("qt6/settings_dialog.cpp", r"Scrobbling.*Last\.fm.*ListenBrainz", "scrobbling settings integration"),
    ]
    for path, pattern, note in checks:
        require(path, pattern, note)

    forbid("qt6/main_window.cpp", r"menu\.addAction\(QStringLiteral\(\"Favorite\"\)", "hardcoded Favorite menu action bypassing contract helper")
    forbid("qt6/main_window.cpp", r"void MainWindow::(populateTree|makeItem|addChildren|showLoadingPlaceholder)", "browser tree/detail helpers remaining in MainWindow")
    forbid("qt6/main_window.cpp", r"void MainWindow::(loadLyrics|updateLyrics|seekToLyricItem|holdLyricsAutoScroll|scrollLyricsToLine)", "lyrics controller helpers remaining in MainWindow")
    forbid("qt6/main_window.cpp", r"auto\* (playbackTab|storageTab|integrationsTab|healthTab)|Scrobbling.*ListenBrainz", "settings dialog UI remaining in MainWindow")
    forbid("qt6/main_window.cpp", r"PlaybackController::(nowPlayingChanged|trackMetadataUpdated|streamStarted|positionChanged|nativeFormatReady|qualityChanged|stateChanged|activityCleared)", "main window granular playback signal observer")
    forbid("qt6/scrobble_service.cpp", r"PlaybackController::(nowPlayingChanged|streamStarted|trackMetadataUpdated|positionChanged|stateChanged|activityCleared)", "scrobble granular playback signal observer")
    forbid("qt6/native_playback_client.cpp", r"QStringList\{.*play_|join\('\\t'\)|send\(QStringLiteral\(\"stop|send\(QStringLiteral\(\"shutdown", "legacy tab/newline native commands")
    forbid("qt6/native_playback_client.cpp", r"DONE.*sendMessage\(QStringLiteral\(\"shutdown", "DONE-triggered daemon shutdown")
    forbid("native/daemon_protocol.cpp", r"std::getline\(std::cin|split_tabs", "legacy newline/tab daemon commands")
    forbid("native/playback_state.cpp", r"next\\t|handle_next_command_line|poll_lines", "legacy newline/tab playback commands")

    print(f"media contract smoke: {len(checks) + 10} checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"media contract smoke: {exc}", file=sys.stderr)
        raise SystemExit(1)
