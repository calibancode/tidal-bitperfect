#include "tidal_sidecar.h"

#include <QJsonDocument>
#include <QNetworkReply>
#include <QUrlQuery>

namespace {
constexpr int kDefaultLimit = 1000;
} // namespace

void TidalSidecar::apiRequest(
    const QString& method,
    const QString& path,
    const QJsonObject& params,
    const QJsonObject& form,
    ApiBase base,
    ValueHandler onSuccess,
    ErrorHandler onError,
    bool includeSession,
    bool allowRefresh
) {
    QUrl url(path.startsWith(QStringLiteral("http")) ? path : apiBaseUrl(base) + path);
    QUrlQuery query(url);
    if (includeSession) {
        if (!m_sessionId.isEmpty()) query.addQueryItem(QStringLiteral("sessionId"), m_sessionId);
        if (!m_countryCode.isEmpty()) query.addQueryItem(QStringLiteral("countryCode"), m_countryCode);
        query.addQueryItem(QStringLiteral("limit"), QString::number(kDefaultLimit));
    }
    for (auto it = params.begin(); it != params.end(); ++it) {
        if (it.value().isNull() || it.value().isUndefined()) continue;
        query.removeAllQueryItems(it.key());
        query.addQueryItem(it.key(), it.value().toVariant().toString());
    }
    url.setQuery(query);

    QNetworkRequest req = makeRequest(url, true);
    QByteArray body;
    const QString upper = method.toUpper();
    if (!form.isEmpty()) {
        body = formBody(form);
        req.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/x-www-form-urlencoded"));
    }
    QNetworkReply* reply = nullptr;
    if (upper == QStringLiteral("GET")) reply = m_network.get(req);
    else if (upper == QStringLiteral("POST")) reply = m_network.post(req, body);
    else if (upper == QStringLiteral("PUT")) reply = m_network.put(req, body);
    else if (upper == QStringLiteral("DELETE")) reply = m_network.deleteResource(req);
    else {
        onError(QStringLiteral("unsupported HTTP method: %1").arg(method));
        return;
    }

    connect(reply, &QNetworkReply::finished, this, [this, reply, method, path, params, form, base, onSuccess, onError, includeSession, allowRefresh]() {
        const QByteArray raw = reply->readAll();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        const bool ok = reply->error() == QNetworkReply::NoError && status >= 200 && status < 300;
        QJsonParseError parseError;
        const QJsonDocument doc = QJsonDocument::fromJson(raw, &parseError);
        const QJsonValue value = doc.isObject() ? QJsonValue(doc.object()) : (doc.isArray() ? QJsonValue(doc.array()) : QJsonValue());
        if (ok) {
            reply->deleteLater();
            onSuccess(value);
            return;
        }
        const QString responseText = QString::fromUtf8(raw);
        const bool expired = responseText.contains(QStringLiteral("token"), Qt::CaseInsensitive)
            && responseText.contains(QStringLiteral("expired"), Qt::CaseInsensitive);
        reply->deleteLater();
        if (allowRefresh && expired && !m_refreshToken.isEmpty()) {
            refreshToken([this, method, path, params, form, base, onSuccess, onError, includeSession](bool refreshed) {
                if (!refreshed) {
                    onError(QStringLiteral("TIDAL token expired and refresh failed"));
                    return;
                }
                apiRequest(method, path, params, form, base, onSuccess, onError, includeSession, false);
            });
            return;
        }
        QString message = QStringLiteral("TIDAL request failed");
        if (status > 0) message += QStringLiteral(" (HTTP %1)").arg(status);
        if (!responseText.trimmed().isEmpty()) message += QStringLiteral(": %1").arg(responseText.trimmed().left(500));
        else message += QStringLiteral(": %1").arg(reply->errorString());
        onError(message);
    });
}

void TidalSidecar::authPost(
    const QString& url,
    const QJsonObject& form,
    ValueHandler onSuccess,
    ErrorHandler onError
) {
    QNetworkRequest req{QUrl(url)};
    req.setHeader(QNetworkRequest::UserAgentHeader, QStringLiteral("tidal-bitperfect-qt6/0.1"));
    req.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/x-www-form-urlencoded"));
    QNetworkReply* reply = m_network.post(req, formBody(form));
    connect(reply, &QNetworkReply::finished, this, [reply, onSuccess, onError]() {
        const QByteArray raw = reply->readAll();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        QJsonParseError parseError;
        const QJsonDocument doc = QJsonDocument::fromJson(raw, &parseError);
        const QJsonValue value = doc.isObject() ? QJsonValue(doc.object()) : (doc.isArray() ? QJsonValue(doc.array()) : QJsonValue());
        const bool ok = reply->error() == QNetworkReply::NoError && status >= 200 && status < 300;
        if (ok) {
            reply->deleteLater();
            onSuccess(value);
            return;
        }
        const QString responseText = QString::fromUtf8(raw).trimmed();
        QString message = QStringLiteral("TIDAL auth failed");
        if (status > 0) message += QStringLiteral(" (HTTP %1)").arg(status);
        if (!responseText.isEmpty()) message += QStringLiteral(": %1").arg(responseText.left(500));
        else message += QStringLiteral(": %1").arg(reply->errorString());
        reply->deleteLater();
        onError(message);
    });
}

void TidalSidecar::httpGetBytes(const QUrl& url, std::function<void(const QByteArray&)> onSuccess, ErrorHandler onError) {
    QNetworkReply* reply = m_network.get(makeRequest(url, false));
    connect(reply, &QNetworkReply::finished, this, [reply, onSuccess, onError]() {
        const QByteArray bytes = reply->readAll();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (reply->error() == QNetworkReply::NoError && status >= 200 && status < 300) {
            reply->deleteLater();
            onSuccess(bytes);
            return;
        }
        const QString message = QStringLiteral("download failed%1: %2")
            .arg(status > 0 ? QStringLiteral(" (HTTP %1)").arg(status) : QString())
            .arg(reply->errorString());
        reply->deleteLater();
        onError(message);
    });
}

QNetworkRequest TidalSidecar::makeRequest(const QUrl& url, bool includeAuth) const {
    QNetworkRequest req(url);
    req.setHeader(QNetworkRequest::UserAgentHeader, QStringLiteral("tidal-bitperfect-qt6/0.1"));
    req.setRawHeader("x-tidal-client-version", "2025.7.16");
    req.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::NoLessSafeRedirectPolicy);
    if (includeAuth && !m_accessToken.isEmpty()) {
        req.setRawHeader("authorization", QStringLiteral("%1 %2").arg(m_tokenType, m_accessToken).toUtf8());
    }
    return req;
}

QString TidalSidecar::apiBaseUrl(ApiBase base) const {
    switch (base) {
    case ApiBase::V2:
        return QStringLiteral("https://api.tidal.com/v2/");
    case ApiBase::OpenApiV2:
        return QStringLiteral("https://openapi.tidal.com/v2/");
    case ApiBase::V1:
    default:
        return QStringLiteral("https://api.tidal.com/v1/");
    }
}

QByteArray TidalSidecar::formBody(const QJsonObject& form) const {
    QUrlQuery query;
    for (auto it = form.begin(); it != form.end(); ++it) {
        if (it.value().isNull() || it.value().isUndefined()) continue;
        query.addQueryItem(it.key(), it.value().toVariant().toString());
    }
    return query.toString(QUrl::FullyEncoded).toUtf8();
}
