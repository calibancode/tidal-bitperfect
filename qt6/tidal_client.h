#pragma once

#include <QDateTime>
#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QJsonValue>
#include <QNetworkAccessManager>
#include <QObject>
#include <QSet>
#include <QStringList>
#include <functional>

class QNetworkReply;

class TidalClient : public QObject {
    Q_OBJECT

public:
    using SuccessHandler = std::function<void(const QJsonObject&)>;
    using ErrorHandler = std::function<void(const QString&)>;

    explicit TidalClient(QObject* parent = nullptr);
    ~TidalClient() override;

    void start();
    int request(
        const QString& command,
        const QJsonObject& args,
        SuccessHandler onSuccess,
        ErrorHandler onError = {}
    );
    bool isRunning() const;

signals:
    void ready();
    void statusMessage(const QString& message);
    void loginLink(const QString& url, const QString& code, int expiresSeconds);
    void fatalError(const QString& message);

private:
    using ValueHandler = std::function<void(const QJsonValue&)>;
    using ObjectHandler = std::function<void(const QJsonObject&)>;
    using ArrayHandler = std::function<void(const QJsonArray&)>;

    enum class ApiBase {
        V1,
        V2,
        OpenApiV2,
    };

    struct Pending {
        SuccessHandler onSuccess;
        ErrorHandler onError;
    };

    struct StreamCandidate {
        QJsonObject track;
        QJsonObject stream;
        QString quality;
        QString url;
        int rank = -1;
        int bitDepth = 0;
        int sampleRate = 0;
    };

    void dispatch(int id, const QString& command, const QJsonObject& args);
    void complete(int id, const QJsonObject& result);
    void reject(int id, const QString& message);
    void rejectAll(const QString& message);

    void cmdLogin(int id);
    void cmdSearch(int id, const QJsonObject& args);
    void cmdUrl(int id, const QJsonObject& args);
    void cmdCollection(int id, const QJsonObject& args);
    void cmdHome(int id);
    void cmdLyrics(int id, const QJsonObject& args);
    void cmdRadio(int id, const QJsonObject& args);
    void cmdDetails(int id, const QJsonObject& args);
    void cmdFavorite(int id, const QJsonObject& args);
    void cmdStream(int id, const QJsonObject& args);
    void cmdPrefetch(int id, const QJsonObject& args);
    void cmdDownload(int id, const QJsonObject& args);

    bool ensureLoggedIn(int id);
    void loadSavedLogin(ObjectHandler onSuccess, ErrorHandler onError);
    void startDeviceLogin(ObjectHandler onSuccess, ErrorHandler onError);
    void pollDeviceLogin(
        const QString& deviceCode,
        int intervalSeconds,
        QDateTime expiresAt,
        ObjectHandler onSuccess,
        ErrorHandler onError
    );
    void processAuthToken(
        const QJsonObject& token,
        ObjectHandler onSuccess,
        ErrorHandler onError,
        bool saveCredentials
    );
    void fetchSessionContext(ObjectHandler onSuccess, ErrorHandler onError);
    void checkLogin(ObjectHandler onSuccess, ErrorHandler onError);
    void refreshToken(std::function<void(bool)> done);
    bool loadCredentials(QJsonObject* out) const;
    void saveCredentials() const;

    void apiRequest(
        const QString& method,
        const QString& path,
        const QJsonObject& params,
        const QJsonObject& form,
        ApiBase base,
        ValueHandler onSuccess,
        ErrorHandler onError,
        bool includeSession = true,
        bool allowRefresh = true
    );
    void authPost(
        const QString& url,
        const QJsonObject& form,
        ValueHandler onSuccess,
        ErrorHandler onError
    );
    void httpGetBytes(const QUrl& url, std::function<void(const QByteArray&)> onSuccess, ErrorHandler onError);
    QNetworkRequest makeRequest(const QUrl& url, bool includeAuth) const;

    void loadTrack(const QString& trackId, ObjectHandler onSuccess, ErrorHandler onError);
    void loadAlbum(const QString& albumId, bool includeTracks, ObjectHandler onSuccess, ErrorHandler onError);
    void loadPlaylist(const QString& playlistId, bool includeTracks, ObjectHandler onSuccess, ErrorHandler onError);
    void loadArtist(const QString& artistId, bool includeDetails, ObjectHandler onSuccess, ErrorHandler onError);
    void loadMix(const QString& mixId, ObjectHandler onSuccess, ErrorHandler onError);
    void fetchTracksByIds(const QStringList& ids, ArrayHandler onSuccess, ErrorHandler onError);
    void fetchStreamCandidates(
        const QString& trackId,
        const QStringList& qualities,
        QVector<StreamCandidate> candidates,
        ObjectHandler onSuccess,
        ErrorHandler onError
    );
    void loadStreamForQuality(
        const QString& trackId,
        const QString& quality,
        std::function<void(const StreamCandidate&)> onSuccess,
        ErrorHandler onError
    );
    void downloadDirectFlac(
        const QString& url,
        const QString& trackId,
        const QJsonObject& meta,
        ObjectHandler onSuccess,
        ErrorHandler onError
    );
    void transcodeToFlac(
        const QString& input,
        bool protocolWhitelist,
        const QString& trackId,
        const QJsonObject& meta,
        const QString& mpdPath,
        ObjectHandler onSuccess,
        ErrorHandler onError
    );
    void transcodeToFlacTemp(
        const QString& input,
        bool protocolWhitelist,
        const QString& mpdPath,
        const QString& sampleFormat,
        int sampleRate,
        std::function<void(const QString&)> onSuccess,
        ErrorHandler onError
    );

    QJsonObject parseTrack(const QJsonObject& obj, const QJsonObject& albumOverride = {}) const;
    QJsonObject parseAlbum(const QJsonObject& obj, bool includeEmptyTracks = true) const;
    QJsonObject parsePlaylist(const QJsonObject& obj, bool includeEmptyTracks = true) const;
    QJsonObject parseArtist(const QJsonObject& obj, bool includeEmptyDetails = true) const;
    QJsonObject parseMix(const QJsonObject& obj) const;
    QJsonArray parseTracksArray(const QJsonValue& value, const QJsonObject& albumOverride = {}) const;
    QJsonArray parseAlbumsArray(const QJsonValue& value) const;
    QJsonArray parsePlaylistsArray(const QJsonValue& value) const;
    QJsonArray parseArtistsArray(const QJsonValue& value) const;
    QJsonArray arrayFromPayload(const QJsonValue& value) const;
    QJsonObject itemObject(const QJsonValue& value) const;
    QJsonObject streamDescriptorFromCandidate(const StreamCandidate& candidate, ErrorHandler onError) const;
    QJsonArray homeItemsFromValue(const QJsonValue& value, int limit) const;
    QJsonObject homeItemFromObject(const QJsonObject& obj) const;
    QJsonArray parseTimedLyrics(const QString& subtitles) const;

    QString apiBaseUrl(ApiBase base) const;
    QString imageUrl(const QString& imageId, const QString& fallback = QString(), const QString& size = QStringLiteral("origin")) const;
    QString artistDisplay(const QJsonObject& obj) const;
    QStringList artistNames(const QJsonObject& obj) const;
    QString mediaTypeKey(const QString& kind) const;
    QString searchTypesForKind(const QString& kind) const;
    QString favoritePathForKind(const QString& kind) const;
    QString favoriteFormKeyForKind(const QString& kind) const;
    QString credentialsPath() const;
    QString audioDir() const;
    QString downloadsDir() const;
    QString safeFilenamePart(const QString& text, const QString& fallback) const;
    QString audioPath(const QString& trackId) const;
    QString downloadPath(const QString& trackId, const QJsonObject& meta) const;
    QString storeAudio(const QString& tempPath, const QString& trackId, const QJsonObject& meta);
    QString storeDownload(const QString& tempPath, const QString& trackId, const QJsonObject& meta);
    QString writeTempFile(const QByteArray& bytes, const QString& prefix, const QString& suffix) const;
    QString parseTrackMaxQuality(const QJsonObject& track) const;
    QString plainLyricsText(const QString& lyrics, const QString& subtitles) const;
    int qualityRank(const QString& quality) const;
    int streamScore(const QJsonObject& stream) const;
    double jsonNumber(const QJsonObject& obj, const QString& key, double fallback = 0.0) const;
    QString clientId() const;
    QString clientSecret() const;
    QByteArray formBody(const QJsonObject& form) const;

    QNetworkAccessManager m_network;
    int m_nextId = 1;
    bool m_started = false;
    bool m_refreshingToken = false;
    QHash<int, Pending> m_pending;

    QString m_tokenType = QStringLiteral("Bearer");
    QString m_accessToken;
    QString m_refreshToken;
    QDateTime m_expiryTime;
    QString m_sessionId;
    QString m_countryCode;
    QString m_userId;
    QString m_locale = QStringLiteral("en_US");
    QString m_quality = QStringLiteral("HI_RES_LOSSLESS");
};
