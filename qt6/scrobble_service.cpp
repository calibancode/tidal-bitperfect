#include "scrobble_service.h"

#include "playback_controller.h"

#include <QCryptographicHash>
#include <QDateTime>
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QSettings>
#include <QUrlQuery>

#include <algorithm>
#include <cmath>
#include <functional>
#include <memory>
#include <utility>

namespace {

constexpr const char* kUserAgent = "tidal-bitperfect-qt6/0.1";
constexpr const char* kLastFmApiRoot = "https://ws.audioscrobbler.com/2.0/";
constexpr const char* kListenBrainzSubmitUrl = "https://api.listenbrainz.org/1/submit-listens";
constexpr double kMinimumScrobbleDurationSeconds = 30.0;
constexpr double kMaximumThresholdSeconds = 240.0;

qint64 nowSeconds() {
    return QDateTime::currentSecsSinceEpoch();
}

qint64 nowMs() {
    return QDateTime::currentMSecsSinceEpoch();
}

} // namespace

ScrobbleService::ScrobbleService(PlaybackController* playback, QSettings* settings, QObject* parent)
    : QObject(parent), m_playback(playback), m_settings(settings) {
    loadSettings();
    loadPending();
    attachPlayback();
}

bool ScrobbleService::lastFmReady() const {
    return m_lastFm.enabled
        && !m_lastFm.apiKey.isEmpty()
        && !m_lastFm.sharedSecret.isEmpty()
        && !m_lastFm.sessionKey.isEmpty();
}

bool ScrobbleService::listenBrainzReady() const {
    return m_listenBrainz.enabled && !m_listenBrainz.token.isEmpty();
}

void ScrobbleService::setLastFmConfig(bool enabled, const QString& apiKey, const QString& sharedSecret, const QString& sessionKey) {
    m_lastFm.enabled = enabled;
    m_lastFm.apiKey = apiKey.trimmed();
    m_lastFm.sharedSecret = sharedSecret.trimmed();
    m_lastFm.sessionKey = sessionKey.trimmed();
    saveSettings();
    emit configurationChanged();
    retryPending();
}

void ScrobbleService::setListenBrainzConfig(bool enabled, const QString& token) {
    m_listenBrainz.enabled = enabled;
    m_listenBrainz.token = token.trimmed();
    saveSettings();
    emit configurationChanged();
    retryPending();
}

void ScrobbleService::beginLastFmAuthorization() {
    if (m_lastFm.apiKey.isEmpty() || m_lastFm.sharedSecret.isEmpty()) {
        emit errorMessage(QStringLiteral("Last.fm API key and shared secret are required"));
        return;
    }
    QJsonObject params{
        {QStringLiteral("method"), QStringLiteral("auth.getToken")},
        {QStringLiteral("api_key"), m_lastFm.apiKey},
    };
    postLastFm(QStringLiteral("auth.getToken"), params, [this](bool ok, bool, const QString& message) {
        if (!ok) {
            emit errorMessage(QStringLiteral("Last.fm authorization failed: %1").arg(message));
            return;
        }
    });
}

void ScrobbleService::completeLastFmAuthorization() {
    if (m_pendingLastFmToken.isEmpty()) {
        emit errorMessage(QStringLiteral("Start Last.fm authorization first"));
        return;
    }
    QJsonObject params{
        {QStringLiteral("method"), QStringLiteral("auth.getSession")},
        {QStringLiteral("api_key"), m_lastFm.apiKey},
        {QStringLiteral("token"), m_pendingLastFmToken},
    };
    postLastFm(QStringLiteral("auth.getSession"), params, [this](bool ok, bool, const QString& message) {
        if (!ok) emit errorMessage(QStringLiteral("Last.fm authorization failed: %1").arg(message));
    });
}

void ScrobbleService::retryPending() {
    flushPending();
}

void ScrobbleService::loadSettings() {
    if (!m_settings) return;
    m_lastFm.enabled = m_settings->value(QStringLiteral("qt6/lastfm_enabled"), false).toBool();
    m_lastFm.apiKey = m_settings->value(QStringLiteral("qt6/lastfm_api_key")).toString().trimmed();
    m_lastFm.sharedSecret = m_settings->value(QStringLiteral("qt6/lastfm_shared_secret")).toString().trimmed();
    m_lastFm.sessionKey = m_settings->value(QStringLiteral("qt6/lastfm_session_key")).toString().trimmed();
    m_lastFm.userName = m_settings->value(QStringLiteral("qt6/lastfm_user_name")).toString().trimmed();
    m_listenBrainz.enabled = m_settings->value(QStringLiteral("qt6/listenbrainz_enabled"), false).toBool();
    m_listenBrainz.token = m_settings->value(QStringLiteral("qt6/listenbrainz_token")).toString().trimmed();
}

void ScrobbleService::saveSettings() {
    if (!m_settings) return;
    m_settings->setValue(QStringLiteral("qt6/lastfm_enabled"), m_lastFm.enabled);
    m_settings->setValue(QStringLiteral("qt6/lastfm_api_key"), m_lastFm.apiKey);
    m_settings->setValue(QStringLiteral("qt6/lastfm_shared_secret"), m_lastFm.sharedSecret);
    m_settings->setValue(QStringLiteral("qt6/lastfm_session_key"), m_lastFm.sessionKey);
    m_settings->setValue(QStringLiteral("qt6/lastfm_user_name"), m_lastFm.userName);
    m_settings->setValue(QStringLiteral("qt6/listenbrainz_enabled"), m_listenBrainz.enabled);
    m_settings->setValue(QStringLiteral("qt6/listenbrainz_token"), m_listenBrainz.token);
}

void ScrobbleService::loadPending() {
    if (!m_settings) return;
    const QByteArray raw = m_settings->value(QStringLiteral("qt6/scrobble_pending")).toByteArray();
    const QJsonArray items = QJsonDocument::fromJson(raw).array();
    for (const QJsonValue& value : items) {
        const QJsonObject obj = value.toObject();
        PendingScrobble pending;
        pending.provider = providerFromKey(obj.value(QStringLiteral("provider")).toString());
        pending.track = trackInfoFromJson(obj.value(QStringLiteral("track")).toObject());
        pending.startedAtSeconds = static_cast<qint64>(obj.value(QStringLiteral("started_at")).toDouble());
        pending.listenedSeconds = obj.value(QStringLiteral("listened_s")).toDouble();
        if (trackUsable(pending.track) && pending.startedAtSeconds > 0) m_pending.push_back(pending);
    }
}

void ScrobbleService::savePending() {
    if (!m_settings) return;
    QJsonArray items;
    for (const PendingScrobble& pending : std::as_const(m_pending)) {
        items.push_back(QJsonObject{
            {QStringLiteral("provider"), providerKey(pending.provider)},
            {QStringLiteral("track"), trackInfoToJson(pending.track)},
            {QStringLiteral("started_at"), static_cast<double>(pending.startedAtSeconds)},
            {QStringLiteral("listened_s"), pending.listenedSeconds},
        });
    }
    m_settings->setValue(QStringLiteral("qt6/scrobble_pending"), QJsonDocument(items).toJson(QJsonDocument::Compact));
    emit configurationChanged();
}

void ScrobbleService::attachPlayback() {
    if (!m_playback) return;
    connect(m_playback, &PlaybackController::playbackStateChanged, this, &ScrobbleService::handlePlaybackState);
}

void ScrobbleService::beginSession(const QJsonObject& track) {
    finishSession();
    const TrackInfo info = trackInfoFromObject(track);
    if (!trackUsable(info)) {
        resetSession();
        return;
    }
    m_session = {};
    m_session.active = true;
    m_session.track = info;
    m_session.startedAtSeconds = nowSeconds();
    m_session.lastUpdateMs = nowMs();
}

void ScrobbleService::updateSessionTrack(const QJsonObject& track, double durationSeconds) {
    if (!m_session.active) return;
    const TrackInfo info = trackInfoFromObject(track);
    if (!info.id.isEmpty() && info.id != m_session.track.id) return;
    if (!info.title.isEmpty()) m_session.track.title = info.title;
    if (!info.artist.isEmpty()) m_session.track.artist = info.artist;
    if (!info.album.isEmpty()) m_session.track.album = info.album;
    if (durationSeconds > 0.0) m_session.track.durationSeconds = durationSeconds;
    else if (info.durationSeconds > 0.0) m_session.track.durationSeconds = info.durationSeconds;
}

void ScrobbleService::handlePlaybackState(const PlaybackState& state) {
    if (!state.hasTrack()) {
        finishSession();
        return;
    }
    if (!m_session.active || state.trackId != m_session.track.id) {
        finishSession();
        if (!state.busy) return;
        beginSession(state.track);
    }
    if (!m_session.active) return;

    updateSessionTrack(state.track, state.durationSeconds);
    updateListenedTime(state.positionSeconds);
    m_session.playing = state.playing();
    maybeSendNowPlaying();
    maybeSubmitScrobble();
    if (!state.busy) finishSession();
}

void ScrobbleService::finishSession() {
    if (!m_session.active) return;
    updateListenedTime(m_session.lastPositionSeconds);
    maybeSubmitScrobble();
    resetSession();
}

void ScrobbleService::resetSession() {
    m_session = {};
}

void ScrobbleService::updateListenedTime(double positionSeconds) {
    const qint64 currentMs = nowMs();
    if (m_session.playing && m_session.lastUpdateMs > 0) {
        const double elapsed = static_cast<double>(currentMs - m_session.lastUpdateMs) / 1000.0;
        const double positionDelta = positionSeconds - m_session.lastPositionSeconds;
        if (elapsed > 0.0 && positionDelta > -2.0) {
            m_session.listenedSeconds += qMin(elapsed, 10.0);
        }
    }
    m_session.lastUpdateMs = currentMs;
    m_session.lastPositionSeconds = qMax(0.0, positionSeconds);
}

void ScrobbleService::maybeSendNowPlaying() {
    if (!m_session.active || m_session.nowPlayingSent || !m_session.playing || !trackUsable(m_session.track)) return;
    bool sent = false;
    if (lastFmReady()) {
        submitNowPlaying(Provider::LastFm, m_session.track);
        sent = true;
    }
    if (listenBrainzReady()) {
        submitNowPlaying(Provider::ListenBrainz, m_session.track);
        sent = true;
    }
    m_session.nowPlayingSent = sent;
}

void ScrobbleService::maybeSubmitScrobble() {
    if (!m_session.active || m_session.scrobbled || !trackUsable(m_session.track)) return;
    const double duration = m_session.track.durationSeconds;
    if (duration <= kMinimumScrobbleDurationSeconds) return;
    if (m_session.listenedSeconds + 0.1 < scrobbleThreshold(duration)) return;

    struct ScrobbleSubmission {
        int remaining = 0;
        int accepted = 0;
        int queued = 0;
        QStringList failures;
    };

    const bool submitLastFm = lastFmReady();
    const bool submitListenBrainz = listenBrainzReady();
    auto submission = std::make_shared<ScrobbleSubmission>();
    submission->remaining = (submitLastFm ? 1 : 0) + (submitListenBrainz ? 1 : 0);
    if (submission->remaining <= 0) return;

    auto finish = [this, submission](bool ok, bool queued, bool, const QString& message) {
        if (ok) ++submission->accepted;
        else if (queued) ++submission->queued;
        else if (!message.isEmpty()) submission->failures.push_back(message);

        --submission->remaining;
        if (submission->remaining > 0) return;

        if (submission->accepted > 0) {
            emit statusMessage(QStringLiteral("Scrobbled"));
        } else if (submission->queued > 0) {
            emit statusMessage(QStringLiteral("Queued scrobble for retry"));
        } else if (!submission->failures.isEmpty()) {
            emit statusMessage(QStringLiteral("Scrobble failed: %1").arg(submission->failures.join(QStringLiteral("; "))));
        } else {
            emit statusMessage(QStringLiteral("Scrobble failed"));
        }
    };

    if (lastFmReady()) {
        submitScrobble(Provider::LastFm, m_session.track, m_session.startedAtSeconds, m_session.listenedSeconds, true, finish);
    }
    if (listenBrainzReady()) {
        submitScrobble(Provider::ListenBrainz, m_session.track, m_session.startedAtSeconds, m_session.listenedSeconds, true, finish);
    }
    m_session.scrobbled = true;
}

void ScrobbleService::submitNowPlaying(Provider provider, const TrackInfo& track) {
    if (provider == Provider::LastFm) {
        QJsonObject params{
            {QStringLiteral("method"), QStringLiteral("track.updateNowPlaying")},
            {QStringLiteral("api_key"), m_lastFm.apiKey},
            {QStringLiteral("sk"), m_lastFm.sessionKey},
            {QStringLiteral("artist"), track.artist},
            {QStringLiteral("track"), track.title},
        };
        if (!track.album.isEmpty()) params.insert(QStringLiteral("album"), track.album);
        if (track.durationSeconds > 0.0) params.insert(QStringLiteral("duration"), QString::number(static_cast<int>(std::llround(track.durationSeconds))));
        postLastFm(QStringLiteral("track.updateNowPlaying"), params, [this](bool ok, bool, const QString& message) {
            if (!ok) emit statusMessage(QStringLiteral("Last.fm now playing failed: %1").arg(message));
        });
        return;
    }

    postListenBrainz(QStringLiteral("playing_now"), track, 0, 0.0, [this](bool ok, bool, const QString& message) {
        if (!ok) emit statusMessage(QStringLiteral("ListenBrainz now playing failed: %1").arg(message));
    });
}

void ScrobbleService::submitScrobble(Provider provider, const TrackInfo& track, qint64 startedAtSeconds, double listenedSeconds, bool cacheOnRetryableFailure, ScrobbleResultHandler done) {
    if (provider == Provider::LastFm) {
        QJsonObject params{
            {QStringLiteral("method"), QStringLiteral("track.scrobble")},
            {QStringLiteral("api_key"), m_lastFm.apiKey},
            {QStringLiteral("sk"), m_lastFm.sessionKey},
            {QStringLiteral("artist"), track.artist},
            {QStringLiteral("track"), track.title},
            {QStringLiteral("timestamp"), QString::number(startedAtSeconds)},
        };
        if (!track.album.isEmpty()) params.insert(QStringLiteral("album"), track.album);
        if (track.durationSeconds > 0.0) params.insert(QStringLiteral("duration"), QString::number(static_cast<int>(std::llround(track.durationSeconds))));
        postLastFm(QStringLiteral("track.scrobble"), params, [this, provider, track, startedAtSeconds, listenedSeconds, cacheOnRetryableFailure, done](bool ok, bool retryable, const QString& message) {
            if (ok) {
                if (done) done(true, false, false, QString());
                else emit statusMessage(QStringLiteral("Scrobbled"));
                return;
            }
            if (retryable && cacheOnRetryableFailure) {
                enqueueScrobble(provider, track, startedAtSeconds, listenedSeconds, !done);
                if (done) done(false, true, true, message);
                return;
            }
            const QString failure = QStringLiteral("%1 failed: %2").arg(providerDisplayName(provider), message);
            if (done) done(false, false, retryable, failure);
            else emit statusMessage(QStringLiteral("Scrobble failed: %1").arg(failure));
        });
        return;
    }

    postListenBrainz(QStringLiteral("single"), track, startedAtSeconds, listenedSeconds, [this, provider, track, startedAtSeconds, listenedSeconds, cacheOnRetryableFailure, done](bool ok, bool retryable, const QString& message) {
        if (ok) {
            if (done) done(true, false, false, QString());
            else emit statusMessage(QStringLiteral("Scrobbled"));
            return;
        }
        if (retryable && cacheOnRetryableFailure) {
            enqueueScrobble(provider, track, startedAtSeconds, listenedSeconds, !done);
            if (done) done(false, true, true, message);
            return;
        }
        const QString failure = QStringLiteral("%1 failed: %2").arg(providerDisplayName(provider), message);
        if (done) done(false, false, retryable, failure);
        else emit statusMessage(QStringLiteral("Scrobble failed: %1").arg(failure));
    });
}

void ScrobbleService::enqueueScrobble(Provider provider, const TrackInfo& track, qint64 startedAtSeconds, double listenedSeconds, bool announce) {
    PendingScrobble pending;
    pending.provider = provider;
    pending.track = track;
    pending.startedAtSeconds = startedAtSeconds;
    pending.listenedSeconds = listenedSeconds;
    m_pending.push_back(pending);
    savePending();
    if (announce) emit statusMessage(QStringLiteral("Queued scrobble for retry"));
}

void ScrobbleService::flushPending() {
    if (m_flushingPending || m_pending.isEmpty()) return;
    int index = -1;
    for (int i = 0; i < m_pending.size(); ++i) {
        if ((m_pending.at(i).provider == Provider::LastFm && lastFmReady())
            || (m_pending.at(i).provider == Provider::ListenBrainz && listenBrainzReady())) {
            index = i;
            break;
        }
    }
    if (index < 0) return;
    m_flushingPending = true;
    const PendingScrobble pending = m_pending.at(index);
    auto done = [this, index](bool ok, bool retryable, const QString& message) {
        m_flushingPending = false;
        if (ok) {
            if (index >= 0 && index < m_pending.size()) m_pending.removeAt(index);
            savePending();
            flushPending();
            return;
        }
        if (!retryable) {
            if (index >= 0 && index < m_pending.size()) m_pending.removeAt(index);
            savePending();
            emit statusMessage(QStringLiteral("Dropped stale scrobble: %1").arg(message));
            flushPending();
        }
    };
    if (pending.provider == Provider::LastFm) {
        QJsonObject params{
            {QStringLiteral("method"), QStringLiteral("track.scrobble")},
            {QStringLiteral("api_key"), m_lastFm.apiKey},
            {QStringLiteral("sk"), m_lastFm.sessionKey},
            {QStringLiteral("artist"), pending.track.artist},
            {QStringLiteral("track"), pending.track.title},
            {QStringLiteral("timestamp"), QString::number(pending.startedAtSeconds)},
        };
        if (!pending.track.album.isEmpty()) params.insert(QStringLiteral("album"), pending.track.album);
        if (pending.track.durationSeconds > 0.0) params.insert(QStringLiteral("duration"), QString::number(static_cast<int>(std::llround(pending.track.durationSeconds))));
        postLastFm(QStringLiteral("track.scrobble"), params, done);
    } else {
        postListenBrainz(QStringLiteral("single"), pending.track, pending.startedAtSeconds, pending.listenedSeconds, done);
    }
}

void ScrobbleService::postLastFm(const QString& method, const QJsonObject& params, const std::function<void(bool, bool, const QString&)>& done) {
    QJsonObject signedParams = params;
    signedParams.insert(QStringLiteral("api_sig"), lastFmSignature(signedParams, m_lastFm.sharedSecret));
    signedParams.insert(QStringLiteral("format"), QStringLiteral("json"));
    QNetworkRequest req{QUrl(QString::fromLatin1(kLastFmApiRoot))};
    req.setHeader(QNetworkRequest::UserAgentHeader, QString::fromLatin1(kUserAgent));
    req.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/x-www-form-urlencoded"));
    QNetworkReply* reply = m_network.post(req, formBody(signedParams));
    connect(reply, &QNetworkReply::finished, this, [this, reply, method, done]() {
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        const QJsonObject body = QJsonDocument::fromJson(reply->readAll()).object();
        const int errorCode = body.value(QStringLiteral("error")).toInt();
        const QString message = body.value(QStringLiteral("message")).toString(reply->errorString());
        reply->deleteLater();

        if (status >= 200 && status < 300 && errorCode == 0) {
            if (method == QStringLiteral("auth.getToken")) {
                m_pendingLastFmToken = body.value(QStringLiteral("token")).toString();
                if (m_pendingLastFmToken.isEmpty()) {
                    done(false, false, QStringLiteral("missing auth token"));
                    return;
                }
                emit lastFmAuthUrlReady(QUrl(QStringLiteral("https://www.last.fm/api/auth/?api_key=%1&token=%2").arg(m_lastFm.apiKey, m_pendingLastFmToken)));
                emit statusMessage(QStringLiteral("Last.fm authorization opened"));
            } else if (method == QStringLiteral("auth.getSession")) {
                const QJsonObject session = body.value(QStringLiteral("session")).toObject();
                m_lastFm.sessionKey = session.value(QStringLiteral("key")).toString();
                m_lastFm.userName = session.value(QStringLiteral("name")).toString();
                m_pendingLastFmToken.clear();
                saveSettings();
                emit lastFmSessionKeyReady(m_lastFm.sessionKey, m_lastFm.userName);
                emit statusMessage(m_lastFm.userName.isEmpty()
                    ? QStringLiteral("Last.fm authorized")
                    : QStringLiteral("Last.fm authorized as %1").arg(m_lastFm.userName));
                emit configurationChanged();
                retryPending();
            }
            done(true, false, QString());
            return;
        }

        const bool retryable = errorCode == 11 || errorCode == 16 || status == 0 || status >= 500;
        done(false, retryable, message.isEmpty() ? QStringLiteral("HTTP %1").arg(status) : message);
    });
}

void ScrobbleService::postListenBrainz(const QString& listenType, const TrackInfo& track, qint64 startedAtSeconds, double listenedSeconds, const std::function<void(bool, bool, const QString&)>& done) {
    QNetworkRequest req{QUrl(QString::fromLatin1(kListenBrainzSubmitUrl))};
    req.setHeader(QNetworkRequest::UserAgentHeader, QString::fromLatin1(kUserAgent));
    req.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    req.setRawHeader("Authorization", QStringLiteral("Token %1").arg(m_listenBrainz.token).toUtf8());
    QNetworkReply* reply = m_network.post(req, QJsonDocument(listenBrainzPayload(listenType, track, startedAtSeconds, listenedSeconds)).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [reply, done]() {
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        const QJsonObject body = QJsonDocument::fromJson(reply->readAll()).object();
        QString message = body.value(QStringLiteral("error")).toString(body.value(QStringLiteral("message")).toString(reply->errorString()));
        reply->deleteLater();
        if (status >= 200 && status < 300) {
            done(true, false, QString());
            return;
        }
        const bool retryable = status == 0 || status == 429 || status >= 500;
        if (message.isEmpty()) message = QStringLiteral("HTTP %1").arg(status);
        done(false, retryable, message);
    });
}

QJsonObject ScrobbleService::listenBrainzPayload(const QString& listenType, const TrackInfo& track, qint64 startedAtSeconds, double listenedSeconds) const {
    QJsonObject additional{
        {QStringLiteral("media_player"), QStringLiteral("TIDAL Bitperfect Qt6")},
        {QStringLiteral("submission_client"), QStringLiteral("TIDAL Bitperfect Qt6")},
        {QStringLiteral("music_service"), QStringLiteral("tidal.com")},
    };
    if (!track.id.isEmpty()) additional.insert(QStringLiteral("origin_url"), QStringLiteral("https://tidal.com/track/%1").arg(track.id));
    if (track.durationSeconds > 0.0) additional.insert(QStringLiteral("duration"), static_cast<int>(std::llround(track.durationSeconds)));
    if (listenedSeconds > 0.0) additional.insert(QStringLiteral("duration_played"), static_cast<int>(std::llround(listenedSeconds)));

    QJsonObject metadata{
        {QStringLiteral("artist_name"), track.artist},
        {QStringLiteral("track_name"), track.title},
        {QStringLiteral("additional_info"), additional},
    };
    if (!track.album.isEmpty()) metadata.insert(QStringLiteral("release_name"), track.album);
    QJsonObject listen{{QStringLiteral("track_metadata"), metadata}};
    if (listenType != QStringLiteral("playing_now")) listen.insert(QStringLiteral("listened_at"), static_cast<double>(startedAtSeconds));
    return QJsonObject{
        {QStringLiteral("listen_type"), listenType},
        {QStringLiteral("payload"), QJsonArray{listen}},
    };
}

ScrobbleService::TrackInfo ScrobbleService::trackInfoFromObject(const QJsonObject& track) const {
    TrackInfo info;
    info.id = track.value(QStringLiteral("id")).toVariant().toString();
    info.title = track.value(QStringLiteral("title")).toString().trimmed();
    info.artist = bestArtist(track).trimmed();
    info.album = track.value(QStringLiteral("album")).toString().trimmed();
    info.durationSeconds = track.value(QStringLiteral("duration_s")).toDouble(track.value(QStringLiteral("duration")).toDouble());
    return info;
}

QJsonObject ScrobbleService::trackInfoToJson(const TrackInfo& track) const {
    return QJsonObject{
        {QStringLiteral("id"), track.id},
        {QStringLiteral("title"), track.title},
        {QStringLiteral("artist"), track.artist},
        {QStringLiteral("album"), track.album},
        {QStringLiteral("duration_s"), track.durationSeconds},
    };
}

ScrobbleService::TrackInfo ScrobbleService::trackInfoFromJson(const QJsonObject& obj) const {
    TrackInfo track;
    track.id = obj.value(QStringLiteral("id")).toString();
    track.title = obj.value(QStringLiteral("title")).toString();
    track.artist = obj.value(QStringLiteral("artist")).toString();
    track.album = obj.value(QStringLiteral("album")).toString();
    track.durationSeconds = obj.value(QStringLiteral("duration_s")).toDouble();
    return track;
}

QString ScrobbleService::providerKey(Provider provider) {
    return provider == Provider::LastFm ? QStringLiteral("lastfm") : QStringLiteral("listenbrainz");
}

QString ScrobbleService::providerDisplayName(Provider provider) {
    return provider == Provider::LastFm ? QStringLiteral("Last.fm") : QStringLiteral("ListenBrainz");
}

ScrobbleService::Provider ScrobbleService::providerFromKey(const QString& key) {
    return key == QStringLiteral("listenbrainz") ? Provider::ListenBrainz : Provider::LastFm;
}

QString ScrobbleService::lastFmSignature(const QJsonObject& params, const QString& sharedSecret) {
    QStringList keys = params.keys();
    keys.removeAll(QStringLiteral("format"));
    keys.removeAll(QStringLiteral("callback"));
    keys.sort();
    QString signature;
    for (const QString& key : keys) signature += key + params.value(key).toVariant().toString();
    signature += sharedSecret;
    return QString::fromLatin1(QCryptographicHash::hash(signature.toUtf8(), QCryptographicHash::Md5).toHex());
}

QByteArray ScrobbleService::formBody(const QJsonObject& params) {
    QUrlQuery query;
    for (const QString& key : params.keys()) query.addQueryItem(key, params.value(key).toVariant().toString());
    return query.toString(QUrl::FullyEncoded).toUtf8();
}

QString ScrobbleService::bestArtist(const QJsonObject& track) {
    QString artist = track.value(QStringLiteral("artist_display")).toString();
    if (!artist.isEmpty()) return artist;
    artist = track.value(QStringLiteral("artist")).toString();
    if (!artist.isEmpty()) return artist;
    const QJsonArray artists = track.value(QStringLiteral("artists")).toArray();
    QStringList names;
    for (const QJsonValue& value : artists) {
        const QJsonObject obj = value.toObject();
        const QString name = obj.value(QStringLiteral("name")).toString();
        if (!name.isEmpty()) names << name;
    }
    return names.join(QStringLiteral(", "));
}

bool ScrobbleService::trackUsable(const TrackInfo& track) {
    return !track.title.isEmpty() && !track.artist.isEmpty();
}

double ScrobbleService::scrobbleThreshold(double durationSeconds) {
    return qMin(durationSeconds / 2.0, kMaximumThresholdSeconds);
}
