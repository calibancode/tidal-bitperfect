#include "playback_controller.h"

#include "cache_manager.h"
#include "tidal_sidecar.h"

#include <QFile>
#include <QJsonValue>
#include <QtGlobal>

#include <utility>

namespace {

QString trackId(const QJsonObject& track) {
    return track.value(QStringLiteral("id")).toVariant().toString();
}

QString streamQuality(const QJsonObject& stream, const QJsonObject& track) {
    QString quality = stream.value(QStringLiteral("audio_quality")).toString(stream.value(QStringLiteral("track_max_quality")).toString());
    if (quality.isEmpty()) {
        quality = track.value(QStringLiteral("audio_quality")).toString(track.value(QStringLiteral("track_max_quality")).toString());
    }
    return quality;
}

} // namespace

PlaybackController::PlaybackController(TidalSidecar* sidecar, CacheManagerQt* cache, QObject* parent)
    : QObject(parent), m_sidecar(sidecar), m_cache(cache) {
    setupNativeSignals();
}

void PlaybackController::setRequireOnlineCallback(RequireOnlineCallback callback) {
    m_requireOnline = std::move(callback);
}

void PlaybackController::setOutputDevice(const QString& device) {
    const QString clean = device.trimmed();
    m_outputDevice = clean.isEmpty() ? QStringLiteral("default") : clean;
}

void PlaybackController::setVolume(int volumePercent) {
    m_volumePercent = qBound(0, volumePercent, 100);
    m_player.setVolume(m_volumePercent);
}

void PlaybackController::setOfflineMode(bool offline) {
    m_offlineMode = offline;
}

void PlaybackController::setGaplessEnabled(bool enabled) {
    m_gaplessEnabled = enabled;
    refreshLocalPrefetch();
}

void PlaybackController::updateTrackMetadata(const QJsonObject& track) {
    const QString id = trackId(track);
    if (id.isEmpty() || !m_tracks.contains(id)) return;
    m_tracks[id] = track;
    if (m_queue.contains(id)) emitQueueChanged();
    if (id == m_currentTrackId) emit trackMetadataUpdated(track);
}

bool PlaybackController::nativeAvailable() const {
    return m_player.available();
}

bool PlaybackController::busy() const {
    return m_player.busy();
}

QVector<QJsonObject> PlaybackController::queuedTracks() const {
    QVector<QJsonObject> tracks;
    tracks.reserve(m_queue.size());
    for (const QString& id : m_queue) {
        const QJsonObject track = m_tracks.value(id);
        if (!track.isEmpty()) tracks.push_back(track);
    }
    return tracks;
}

QJsonObject PlaybackController::queueTrack(int row) const {
    if (row < 0 || row >= m_queue.size()) return {};
    return m_tracks.value(m_queue.at(row));
}

QJsonObject PlaybackController::currentTrack() const {
    return m_currentTrackId.isEmpty() ? QJsonObject{} : m_tracks.value(m_currentTrackId);
}

bool PlaybackController::trackIsLocal(const QString& id) const {
    return m_cache && !id.isEmpty() && (m_cache->hasDownload(id) || m_cache->hasCachedAudio(id));
}

bool PlaybackController::trackIsDownloaded(const QString& id) const {
    return m_cache && !id.isEmpty() && m_cache->hasDownload(id);
}

void PlaybackController::appendQueue(const QJsonObject& track) {
    const QString id = trackId(track);
    if (id.isEmpty()) return;
    rememberTrack(track);
    m_queue.push_back(id);
    emitQueueChanged();
    maybePrefetchNext();
}

void PlaybackController::insertQueueNext(const QJsonObject& track) {
    const QString id = trackId(track);
    if (id.isEmpty()) return;
    rememberTrack(track);
    m_queue.prepend(id);
    emitQueueChanged();
    m_player.clearNextTrack();
    maybePrefetchNext();
}

void PlaybackController::clearQueue() {
    m_queue.clear();
    emitQueueChanged();
    m_player.clearNextTrack();
}

void PlaybackController::removeQueueRow(int row) {
    if (row < 0 || row >= m_queue.size()) return;
    const bool removedPrefetched = row == 0;
    m_queue.removeAt(row);
    emitQueueChanged();
    if (removedPrefetched) refreshLocalPrefetch();
}

void PlaybackController::moveQueueRowToNext(int row) {
    if (row <= 0 || row >= m_queue.size()) return;
    const QString id = m_queue.takeAt(row);
    m_queue.prepend(id);
    emitQueueChanged();
    refreshLocalPrefetch();
}

void PlaybackController::playQueueRow(int row) {
    if (row < 0 || row >= m_queue.size()) return;
    const QString id = m_queue.at(row);
    m_queue.removeAt(row);
    emitQueueChanged();
    m_player.clearNextTrack();
    m_userStopped = false;
    playTrack(m_tracks.value(id));
}

void PlaybackController::playNextQueued() {
    if (m_queue.isEmpty()) return;
    const QString id = m_queue.takeFirst();
    emitQueueChanged();
    m_player.clearNextTrack();
    m_userStopped = false;
    playTrack(m_tracks.value(id));
}

void PlaybackController::playTrack(const QJsonObject& track) {
    if (track.isEmpty()) return;
    const QString id = trackId(track);
    if (id.isEmpty()) return;

    const QString cached = localPathForTrack(id);
    if (cached.isEmpty() && m_offlineMode) {
        emit statusMessage(QStringLiteral("Track is not available in cache/downloads"));
        return;
    }
    if (m_player.busy() && !m_currentTrackId.isEmpty() && id != m_currentTrackId) {
        m_replacingPlayback = true;
        m_player.clearNextTrack();
        m_player.stop();
        cleanupCurrentTempMpd();
    }

    m_userStopped = false;
    m_paused = false;
    m_positionSeconds = 0.0;
    m_duration = 0.0;
    m_streamSampleRate = track.value(QStringLiteral("sample_rate")).toInt();
    m_streamBitDepth = track.value(QStringLiteral("bit_depth")).toInt();
    rememberTrack(track);
    m_currentTrackId = id;

    emit stateChanged();
    emit positionChanged(0.0, 0.0);
    emit qualityChanged(
        track.value(QStringLiteral("audio_quality")).toString(track.value(QStringLiteral("track_max_quality")).toString()),
        m_streamBitDepth,
        m_streamSampleRate
    );
    emit nowPlayingChanged(track);

    if (!cached.isEmpty()) {
        m_replacingPlayback = false;
        m_player.playFile(cached, m_outputDevice, m_volumePercent);
        emit stateChanged();
        maybePrefetchNext();
        return;
    }
    requestStreamAndPlay(track);
}

void PlaybackController::stop() {
    m_userStopped = true;
    m_paused = false;
    m_player.clearNextTrack();
    m_player.stop();
    emit stateChanged();
    emit statusMessage(QStringLiteral("Stopped"));
    emit activityCleared();
}

void PlaybackController::togglePause() {
    if (!m_player.busy()) {
        const QJsonObject current = currentTrack();
        if (!current.isEmpty()) playTrack(current);
        return;
    }
    m_paused = !m_paused;
    emit stateChanged();
    m_player.pauseToggle();
}

void PlaybackController::seek(double deltaSeconds) {
    m_player.seek(deltaSeconds);
}

void PlaybackController::seekTo(double seconds) {
    m_player.seekTo(seconds);
}

void PlaybackController::refreshLocalPrefetch() {
    m_player.clearNextTrack();
    maybePrefetchNext();
}

void PlaybackController::shutdown() {
    m_player.shutdown();
}

void PlaybackController::setupNativeSignals() {
    connect(&m_player, &NativePlaybackClient::statusMessage, this, [this](const QString& message) {
        if (message == QStringLiteral("Paused")) {
            m_paused = true;
            emit stateChanged();
        } else if (message == QStringLiteral("Playing")) {
            m_paused = false;
            emit stateChanged();
        }
        emit statusMessage(message);
    });
    connect(&m_player, &NativePlaybackClient::logMessage, this, &PlaybackController::logMessage);
    connect(&m_player, &NativePlaybackClient::errorMessage, this, [this](const QString& msg) {
        cleanupCurrentTempMpd();
        m_replacingPlayback = false;
        m_paused = false;
        emit stateChanged();
        emit activityCleared();
        emit playbackError(msg);
    });
    connect(&m_player, &NativePlaybackClient::position, this, [this](double pos, double durationSeconds) {
        m_positionSeconds = pos;
        m_duration = durationSeconds;
        emit positionChanged(pos, durationSeconds);
    });
    connect(&m_player, &NativePlaybackClient::formatReady, this, [this](const NativeAudioFormat& fmt) {
        if (m_streamSampleRate <= 0 && fmt.rate > 0) m_streamSampleRate = fmt.rate;
        if (m_streamBitDepth <= 0 && fmt.bits > 0) m_streamBitDepth = fmt.bits;
        if (fmt.duration > 0.0) m_duration = fmt.duration;
        emit nativeFormatReady(fmt);
        const QJsonObject track = currentTrack();
        emit qualityChanged(
            track.value(QStringLiteral("audio_quality")).toString(track.value(QStringLiteral("track_max_quality")).toString()),
            m_streamBitDepth,
            m_streamSampleRate
        );
        if (fmt.duration > 0.0) emit positionChanged(m_positionSeconds, m_duration);
    });
    connect(&m_player, &NativePlaybackClient::advanced, this, [this](const QString& id) {
        if (!m_queue.isEmpty() && m_queue.first() == id) m_queue.pop_front();
        m_currentTrackId = id;
        m_paused = false;
        m_positionSeconds = 0.0;
        m_duration = 0.0;
        const QJsonObject track = m_tracks.value(id);
        m_streamSampleRate = track.value(QStringLiteral("sample_rate")).toInt();
        m_streamBitDepth = track.value(QStringLiteral("bit_depth")).toInt();
        emit stateChanged();
        emitQueueChanged();
        emit positionChanged(0.0, 0.0);
        emit nowPlayingChanged(track);
        emit qualityChanged(
            track.value(QStringLiteral("audio_quality")).toString(track.value(QStringLiteral("track_max_quality")).toString()),
            m_streamBitDepth,
            m_streamSampleRate
        );
        maybePrefetchNext();
    });
    connect(&m_player, &NativePlaybackClient::finishedOk, this, [this]() {
        m_paused = false;
        emit stateChanged();
        if (m_replacingPlayback) {
            m_replacingPlayback = false;
            return;
        }
        cleanupCurrentTempMpd();
        if (m_userStopped) {
            m_userStopped = false;
            return;
        }
        if (!m_queue.isEmpty()) playNextQueued();
        else emit activityCleared();
    });
}

void PlaybackController::rememberTrack(const QJsonObject& track) {
    const QString id = trackId(track);
    if (!id.isEmpty()) m_tracks[id] = track;
}

void PlaybackController::emitQueueChanged() {
    emit queueChanged(queuedTracks());
}

void PlaybackController::requestStreamAndPlay(const QJsonObject& track) {
    if (!m_sidecar) return;
    if (m_requireOnline && !m_requireOnline(QStringLiteral("Streaming"))) {
        emit statusMessage(QStringLiteral("Track is not available in cache/downloads"));
        return;
    }
    const QString id = trackId(track);
    m_sidecar->request(QStringLiteral("stream"), {{QStringLiteral("track_id"), id}}, [this, track](const QJsonObject& result) {
        playStreamDescriptor(track, result);
    }, [this, id](const QString& error) {
        if (id == m_currentTrackId) emit streamError(error);
    });
}

void PlaybackController::playStreamDescriptor(const QJsonObject& track, const QJsonObject& stream) {
    const QString id = trackId(track);
    if (!id.isEmpty() && id != m_currentTrackId) {
        const QString staleMpd = stream.value(QStringLiteral("mpd_path")).toString();
        if (!staleMpd.isEmpty()) QFile::remove(staleMpd);
        return;
    }

    QJsonObject activeTrack = track;
    const QJsonObject resolvedTrack = stream.value(QStringLiteral("track")).toObject();
    if (!resolvedTrack.isEmpty()) {
        for (const QString& key : {
                 QStringLiteral("artist_id"),
                 QStringLiteral("artists"),
                 QStringLiteral("artist_display"),
                 QStringLiteral("album"),
                 QStringLiteral("album_id"),
                 QStringLiteral("cover_url"),
                 QStringLiteral("cover_thumbnail_url"),
                 QStringLiteral("audio_quality"),
                 QStringLiteral("track_max_quality"),
             }) {
            const QJsonValue existing = activeTrack.value(key);
            const QJsonValue resolved = resolvedTrack.value(key);
            if ((existing.isUndefined() || existing.isNull() || existing.toVariant().toString().isEmpty())
                && !resolved.isUndefined()
                && !resolved.isNull()) {
                activeTrack.insert(key, resolved);
            }
        }
        if (!id.isEmpty() && activeTrack != track) {
            rememberTrack(activeTrack);
            emit trackMetadataUpdated(activeTrack);
        }
    }

    cleanupCurrentTempMpd();
    m_currentTempMpd = stream.value(QStringLiteral("mpd_path")).toString();
    const QString input = stream.value(QStringLiteral("input")).toString();
    const bool protocol = stream.value(QStringLiteral("is_dash")).toBool(false);
    const int bits = stream.value(QStringLiteral("bit_depth")).toInt(16);
    const QString codec = bits >= 24 ? QStringLiteral("pcm_s32le") : QStringLiteral("pcm_s16le");
    m_duration = stream.value(QStringLiteral("duration_s")).toDouble();
    m_streamBitDepth = bits;
    m_streamSampleRate = stream.value(QStringLiteral("sample_rate")).toInt();
    const QString audioQuality = streamQuality(stream, activeTrack);

    emit qualityChanged(audioQuality, m_streamBitDepth, m_streamSampleRate);
    emit streamStarted(activeTrack, m_duration);
    emit positionChanged(0.0, m_duration);

    m_replacingPlayback = false;
    m_player.playFfmpeg(input, m_outputDevice, m_volumePercent, codec, m_duration, protocol);
    emit stateChanged();
    maybePrefetchNext();
}

void PlaybackController::maybePrefetchNext() {
    if (!m_gaplessEnabled || m_queue.isEmpty()) {
        m_player.clearNextTrack();
        return;
    }
    const QString id = m_queue.first();
    const QString path = localPathForTrack(id);
    if (!path.isEmpty()) m_player.setNextTrack(id, path);
    else m_player.clearNextTrack();
}

void PlaybackController::cleanupCurrentTempMpd() {
    if (m_currentTempMpd.isEmpty()) return;
    QFile::remove(m_currentTempMpd);
    m_currentTempMpd.clear();
}

QString PlaybackController::localPathForTrack(const QString& id) const {
    if (!m_cache || id.isEmpty()) return {};
    const QString download = m_cache->downloadPath(id);
    return download.isEmpty() ? m_cache->cachedAudioPath(id) : download;
}
