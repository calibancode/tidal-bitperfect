#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QObject>
#include <QString>
#include <QUrl>
#include <QVector>

#include <functional>

class PlaybackController;
class QNetworkReply;
class QSettings;

class ScrobbleService : public QObject {
    Q_OBJECT

public:
    struct LastFmConfig {
        bool enabled = false;
        QString apiKey;
        QString sharedSecret;
        QString sessionKey;
        QString userName;
    };

    struct ListenBrainzConfig {
        bool enabled = false;
        QString token;
    };

    explicit ScrobbleService(PlaybackController* playback, QSettings* settings, QObject* parent = nullptr);

    LastFmConfig lastFmConfig() const { return m_lastFm; }
    ListenBrainzConfig listenBrainzConfig() const { return m_listenBrainz; }
    bool lastFmReady() const;
    bool listenBrainzReady() const;
    int pendingCount() const { return static_cast<int>(m_pending.size()); }

    void setLastFmConfig(bool enabled, const QString& apiKey, const QString& sharedSecret, const QString& sessionKey);
    void setListenBrainzConfig(bool enabled, const QString& token);

public slots:
    void beginLastFmAuthorization();
    void completeLastFmAuthorization();
    void retryPending();

signals:
    void statusMessage(const QString& message);
    void errorMessage(const QString& message);
    void lastFmAuthUrlReady(const QUrl& url);
    void lastFmSessionKeyReady(const QString& sessionKey, const QString& userName);
    void configurationChanged();

private:
    enum class Provider {
        LastFm,
        ListenBrainz,
    };

    struct TrackInfo {
        QString id;
        QString title;
        QString artist;
        QString album;
        double durationSeconds = 0.0;
    };

    struct Session {
        bool active = false;
        bool playing = false;
        bool nowPlayingSent = false;
        bool scrobbled = false;
        TrackInfo track;
        qint64 startedAtSeconds = 0;
        qint64 lastUpdateMs = 0;
        double lastPositionSeconds = 0.0;
        double listenedSeconds = 0.0;
    };

    struct PendingScrobble {
        Provider provider = Provider::LastFm;
        TrackInfo track;
        qint64 startedAtSeconds = 0;
        double listenedSeconds = 0.0;
    };

    void loadSettings();
    void saveSettings();
    void loadPending();
    void savePending();
    void attachPlayback();
    void beginSession(const QJsonObject& track);
    void updateSessionTrack(const QJsonObject& track, double durationSeconds = 0.0);
    void updatePlaybackState();
    void updatePosition(double positionSeconds, double durationSeconds);
    void finishSession();
    void resetSession();
    void updateListenedTime(double positionSeconds);
    void maybeSendNowPlaying();
    void maybeSubmitScrobble();
    void submitNowPlaying(Provider provider, const TrackInfo& track);
    void submitScrobble(Provider provider, const TrackInfo& track, qint64 startedAtSeconds, double listenedSeconds, bool cacheOnRetryableFailure);
    void enqueueScrobble(Provider provider, const TrackInfo& track, qint64 startedAtSeconds, double listenedSeconds);
    void flushPending();
    void postLastFm(const QString& method, const QJsonObject& params, const std::function<void(bool, bool, const QString&)>& done);
    void postListenBrainz(const QString& listenType, const TrackInfo& track, qint64 startedAtSeconds, double listenedSeconds, const std::function<void(bool, bool, const QString&)>& done);
    QJsonObject listenBrainzPayload(const QString& listenType, const TrackInfo& track, qint64 startedAtSeconds, double listenedSeconds) const;
    TrackInfo trackInfoFromObject(const QJsonObject& track) const;
    QJsonObject trackInfoToJson(const TrackInfo& track) const;
    TrackInfo trackInfoFromJson(const QJsonObject& obj) const;
    static QString providerKey(Provider provider);
    static Provider providerFromKey(const QString& key);
    static QString lastFmSignature(const QJsonObject& params, const QString& sharedSecret);
    static QByteArray formBody(const QJsonObject& params);
    static QString bestArtist(const QJsonObject& track);
    static bool trackUsable(const TrackInfo& track);
    static double scrobbleThreshold(double durationSeconds);

    PlaybackController* m_playback = nullptr;
    QSettings* m_settings = nullptr;
    QNetworkAccessManager m_network;
    LastFmConfig m_lastFm;
    ListenBrainzConfig m_listenBrainz;
    Session m_session;
    QVector<PendingScrobble> m_pending;
    QString m_pendingLastFmToken;
    bool m_flushingPending = false;
};
