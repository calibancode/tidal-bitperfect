#include "tidal_sidecar.h"

#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QTimer>
#include <QTimeZone>

bool TidalSidecar::ensureLoggedIn(int id) {
    if (!m_accessToken.isEmpty() && !m_userId.isEmpty()) return true;
    reject(id, QStringLiteral("not logged in"));
    return false;
}

void TidalSidecar::loadSavedLogin(ObjectHandler onSuccess, ErrorHandler onError) {
    QJsonObject creds;
    if (!loadCredentials(&creds)) {
        onError(QStringLiteral("no saved credentials"));
        return;
    }
    m_tokenType = creds.value(QStringLiteral("token_type")).toString(QStringLiteral("Bearer"));
    m_accessToken = creds.value(QStringLiteral("access_token")).toString();
    m_refreshToken = creds.value(QStringLiteral("refresh_token")).toString();
    const QJsonValue expiry = creds.value(QStringLiteral("expiry_time"));
    if (expiry.isDouble()) m_expiryTime = QDateTime::fromSecsSinceEpoch(static_cast<qint64>(expiry.toDouble()), QTimeZone::UTC);
    if (m_accessToken.isEmpty()) {
        onError(QStringLiteral("saved credentials are incomplete"));
        return;
    }
    fetchSessionContext([this, onSuccess, onError](const QJsonObject& session) {
        checkLogin([onSuccess, session](const QJsonObject&) { onSuccess(session); }, onError);
    }, onError);
}

void TidalSidecar::startDeviceLogin(ObjectHandler onSuccess, ErrorHandler onError) {
    authPost(QStringLiteral("https://auth.tidal.com/v1/oauth2/device_authorization"),
        {{QStringLiteral("client_id"), clientId()}, {QStringLiteral("scope"), QStringLiteral("r_usr w_usr w_sub")}},
        [this, onSuccess, onError](const QJsonValue& value) {
            const QJsonObject obj = value.toObject();
            const QString url = obj.value(QStringLiteral("verificationUriComplete")).toString();
            const QString code = obj.value(QStringLiteral("userCode")).toString();
            const QString deviceCode = obj.value(QStringLiteral("deviceCode")).toString();
            const int expires = obj.value(QStringLiteral("expiresIn")).toInt(300);
            const int interval = qMax(1, obj.value(QStringLiteral("interval")).toInt(5));
            if (url.isEmpty() || deviceCode.isEmpty()) {
                onError(QStringLiteral("TIDAL login did not return a device code"));
                return;
            }
            emit loginLink(url, code, expires);
            pollDeviceLogin(deviceCode, interval, QDateTime::currentDateTimeUtc().addSecs(expires), onSuccess, onError);
        },
        onError
    );
}

void TidalSidecar::pollDeviceLogin(
    const QString& deviceCode,
    int intervalSeconds,
    QDateTime expiresAt,
    ObjectHandler onSuccess,
    ErrorHandler onError
) {
    if (QDateTime::currentDateTimeUtc() >= expiresAt) {
        onError(QStringLiteral("TIDAL login expired"));
        return;
    }
    authPost(QStringLiteral("https://auth.tidal.com/v1/oauth2/token"),
        {
            {QStringLiteral("client_id"), clientId()},
            {QStringLiteral("client_secret"), clientSecret()},
            {QStringLiteral("device_code"), deviceCode},
            {QStringLiteral("grant_type"), QStringLiteral("urn:ietf:params:oauth:grant-type:device_code")},
            {QStringLiteral("scope"), QStringLiteral("r_usr w_usr w_sub")},
        },
        [this, onSuccess, onError](const QJsonValue& value) {
            processAuthToken(value.toObject(), onSuccess, onError, true);
        },
        [this, deviceCode, intervalSeconds, expiresAt, onSuccess, onError](const QString& error) {
            if (error.contains(QStringLiteral("authorization_pending"), Qt::CaseInsensitive)
                || error.contains(QStringLiteral("slow_down"), Qt::CaseInsensitive)
                || error.contains(QStringLiteral("HTTP 400"), Qt::CaseInsensitive)) {
                QTimer::singleShot(intervalSeconds * 1000, this, [this, deviceCode, intervalSeconds, expiresAt, onSuccess, onError]() {
                    pollDeviceLogin(deviceCode, intervalSeconds, expiresAt, onSuccess, onError);
                });
                return;
            }
            onError(error);
        }
    );
}

void TidalSidecar::processAuthToken(
    const QJsonObject& token,
    ObjectHandler onSuccess,
    ErrorHandler onError,
    bool save
) {
    m_accessToken = token.value(QStringLiteral("access_token")).toString();
    m_refreshToken = token.value(QStringLiteral("refresh_token")).toString(m_refreshToken);
    m_tokenType = token.value(QStringLiteral("token_type")).toString(QStringLiteral("Bearer"));
    const int expires = token.value(QStringLiteral("expires_in")).toInt(0);
    if (expires > 0) m_expiryTime = QDateTime::currentDateTimeUtc().addSecs(expires);
    if (m_accessToken.isEmpty()) {
        onError(QStringLiteral("TIDAL login did not return an access token"));
        return;
    }
    fetchSessionContext([this, onSuccess, onError, save](const QJsonObject& session) {
        if (save) saveCredentials();
        checkLogin([onSuccess, session](const QJsonObject&) { onSuccess(session); }, onError);
    }, onError);
}

void TidalSidecar::fetchSessionContext(ObjectHandler onSuccess, ErrorHandler onError) {
    apiRequest(QStringLiteral("GET"), QStringLiteral("sessions"), {}, {}, ApiBase::V1,
        [this, onSuccess](const QJsonValue& value) {
            const QJsonObject obj = value.toObject();
            m_sessionId = obj.value(QStringLiteral("sessionId")).toString();
            m_countryCode = obj.value(QStringLiteral("countryCode")).toString();
            m_userId = obj.value(QStringLiteral("userId")).toVariant().toString();
            if (m_locale.isEmpty()) m_locale = QStringLiteral("en_US");
            onSuccess(obj);
        },
        onError,
        false
    );
}

void TidalSidecar::checkLogin(ObjectHandler onSuccess, ErrorHandler onError) {
    if (m_userId.isEmpty()) {
        onError(QStringLiteral("TIDAL session has no user id"));
        return;
    }
    apiRequest(QStringLiteral("GET"), QStringLiteral("users/%1/subscription").arg(m_userId), {}, {}, ApiBase::V1,
        [onSuccess](const QJsonValue& value) { onSuccess(value.toObject()); },
        onError
    );
}

void TidalSidecar::refreshToken(std::function<void(bool)> done) {
    if (m_refreshingToken || m_refreshToken.isEmpty()) {
        done(false);
        return;
    }
    m_refreshingToken = true;
    authPost(QStringLiteral("https://auth.tidal.com/v1/oauth2/token"),
        {{QStringLiteral("grant_type"), QStringLiteral("refresh_token")}, {QStringLiteral("refresh_token"), m_refreshToken}, {QStringLiteral("client_id"), clientId()}, {QStringLiteral("client_secret"), clientSecret()}},
        [this, done](const QJsonValue& value) {
            const QJsonObject token = value.toObject();
            m_accessToken = token.value(QStringLiteral("access_token")).toString(m_accessToken);
            m_tokenType = token.value(QStringLiteral("token_type")).toString(m_tokenType);
            const int expires = token.value(QStringLiteral("expires_in")).toInt(0);
            if (expires > 0) m_expiryTime = QDateTime::currentDateTimeUtc().addSecs(expires);
            saveCredentials();
            m_refreshingToken = false;
            done(!m_accessToken.isEmpty());
        },
        [this, done](const QString&) {
            m_refreshingToken = false;
            done(false);
        }
    );
}

bool TidalSidecar::loadCredentials(QJsonObject* out) const {
    QFile file(credentialsPath());
    if (!file.open(QIODevice::ReadOnly)) return false;
    const QJsonDocument doc = QJsonDocument::fromJson(file.readAll());
    if (!doc.isObject()) return false;
    *out = doc.object();
    return true;
}

void TidalSidecar::saveCredentials() const {
    QDir().mkpath(QFileInfo(credentialsPath()).absolutePath());
    QFile file(credentialsPath());
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) return;
    QJsonObject obj{
        {QStringLiteral("token_type"), m_tokenType},
        {QStringLiteral("access_token"), m_accessToken},
        {QStringLiteral("refresh_token"), m_refreshToken},
        {QStringLiteral("expiry_time"), static_cast<double>(m_expiryTime.toSecsSinceEpoch())},
    };
    file.write(QJsonDocument(obj).toJson(QJsonDocument::Indented));
    file.setPermissions(QFile::ReadOwner | QFile::WriteOwner);
}

QString TidalSidecar::credentialsPath() const {
    return QDir::home().filePath(QStringLiteral(".config/tidal/credentials.json"));
}

QString TidalSidecar::clientId() const {
    const QByteArray first = QByteArray::fromBase64("WmxneVNuaGtiVzUw");
    const QByteArray second = QByteArray::fromBase64("V2xkTE1HbDRWQT09");
    return QString::fromUtf8(QByteArray::fromBase64(first + second));
}

QString TidalSidecar::clientSecret() const {
    const QByteArray first = QByteArray::fromBase64("TVU1dU9VRm1SRUZxZUhKblNrWktZa3RPVjB4bFFY");
    const QByteArray second = QByteArray::fromBase64("bExSMVpIYlVsT2RWaFFVRXhJVmxoQmRuaEJaejA9");
    return QString::fromUtf8(QByteArray::fromBase64(first + second));
}
