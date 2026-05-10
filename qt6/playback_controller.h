#pragma once

#include "native_playback_client.h"
#include "playback_state.h"

#include <QJsonObject>
#include <QMap>
#include <QObject>
#include <QString>
#include <QVector>
#include <QtGlobal>

#include <functional>

class CacheManagerQt;
class TidalClient;

class PlaybackController : public QObject {
    Q_OBJECT

public:
    using RequireOnlineCallback = std::function<bool(const QString& action)>;

    explicit PlaybackController(TidalClient* tidal, CacheManagerQt* cache, QObject* parent = nullptr);

    void setRequireOnlineCallback(RequireOnlineCallback callback);
    void setOutputDevice(const QString& device);
    void setVolume(int volumePercent);
    void setOfflineMode(bool offline);
    void setGaplessEnabled(bool enabled);
    void setStreamTransitionSmoothing(bool enabled);
    void setAudioCacheEnabled(bool enabled);
    void setAudioCacheLimitBytes(qint64 bytes);
    void setCacheMode(const QString& mode);
    void updateTrackMetadata(const QJsonObject& track);

    bool nativeAvailable() const;
    bool busy() const { return m_state.busy; }
    bool paused() const { return m_paused; }
    bool gaplessEnabled() const { return m_gaplessEnabled; }
    bool streamTransitionSmoothing() const { return m_streamTransitionSmoothing; }
    bool audioCacheEnabled() const { return m_audioCacheEnabled; }
    qint64 audioCacheLimitBytes() const { return m_audioCacheLimitBytes; }
    QString cacheMode() const { return m_cacheMode; }
    bool queueEmpty() const { return m_queue.isEmpty(); }
    int queueSize() const { return static_cast<int>(m_queue.size()); }
    QVector<QJsonObject> queuedTracks() const;
    QJsonObject queueTrack(int row) const;
    PlaybackState playbackState() const { return m_state; }
    QString currentTrackId() const { return m_currentTrackId; }
    QJsonObject currentTrack() const;
    double positionSeconds() const { return m_positionSeconds; }
    double duration() const { return m_duration; }
    int streamSampleRate() const { return m_streamSampleRate; }
    int streamBitDepth() const { return m_streamBitDepth; }
    bool trackIsLocal(const QString& id) const;
    bool trackIsDownloaded(const QString& id) const;

public slots:
    void appendQueue(const QJsonObject& track);
    void insertQueueNext(const QJsonObject& track);
    void clearQueue();
    void removeQueueRow(int row);
    void moveQueueRowToNext(int row);
    void playQueueRow(int row);
    void playNextQueued();
    void playTrack(const QJsonObject& track);
    void stop();
    void togglePause();
    void seek(double deltaSeconds);
    void seekTo(double seconds);
    void refreshLocalPrefetch();
    void shutdown();

signals:
    void statusMessage(const QString& message);
    void logMessage(const QString& message);
    void streamError(const QString& message);
    void playbackError(const QString& message);
    void queueChanged(const QVector<QJsonObject>& queue);
    void playbackStateChanged(const PlaybackState& state);
    void nowPlayingChanged(const QJsonObject& track);
    void trackMetadataUpdated(const QJsonObject& track);
    void streamStarted(const QJsonObject& track, double durationSeconds);
    void positionChanged(double positionSeconds, double durationSeconds);
    void nativeFormatReady(const NativeAudioFormat& format);
    void qualityChanged(const QString& audioQuality, int bitDepth, int sampleRate);
    void stateChanged();
    void activityCleared();

private:
    void setupNativeSignals();
    void rememberTrack(const QJsonObject& track);
    void emitQueueChanged();
    void emitPlaybackState();
    PlaybackState buildPlaybackState() const;
    void requestStreamAndPlay(const QJsonObject& track);
    void playStreamDescriptor(const QJsonObject& track, const QJsonObject& stream);
    void maybePrefetchNext();
    void invalidatePrefetch();
    void cleanupCurrentTempMpd();
    bool cachedAudioMatchesCurrentFormat(const QString& id) const;
    bool sameAlbumAsCurrent(const QJsonObject& track) const;
    QString localPathForTrack(const QString& id) const;

    TidalClient* m_tidal = nullptr;
    CacheManagerQt* m_cache = nullptr;
    NativePlaybackClient m_player;
    RequireOnlineCallback m_requireOnline;
    QMap<QString, QJsonObject> m_tracks;
    QVector<QString> m_queue;
    QString m_currentTrackId;
    QString m_currentTempMpd;
    QString m_prefetchTrackId;
    QString m_cacheMode = QStringLiteral("balanced");
    QString m_outputDevice = QStringLiteral("default");
    QString m_streamAudioQuality;
    AudioFormat m_outputFormat;
    PlaybackState m_state;
    int m_volumePercent = 100;
    quint64 m_prefetchToken = 0;
    double m_duration = 0.0;
    double m_positionSeconds = 0.0;
    int m_streamSampleRate = 0;
    int m_streamBitDepth = 0;
    qint64 m_audioCacheLimitBytes = 0;
    bool m_gaplessEnabled = true;
    bool m_streamTransitionSmoothing = false;
    bool m_audioCacheEnabled = true;
    bool m_offlineMode = false;
    bool m_paused = false;
    bool m_buffering = false;
    bool m_userStopped = false;
    bool m_replacingPlayback = false;
};
