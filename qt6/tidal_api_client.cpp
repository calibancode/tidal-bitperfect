#include "tidal_api_client.h"

#include <QJsonDocument>
#include <QNetworkReply>
#include <QUrlQuery>

TidalApiClient::TidalApiClient(QObject* parent) : QObject(parent) {}

void TidalApiClient::setAccessToken(const QString& tokenType, const QString& accessToken) {
    m_tokenType = tokenType.isEmpty() ? QStringLiteral("Bearer") : tokenType;
    m_accessToken = accessToken;
}

void TidalApiClient::get(const QString& apiPath, const QJsonObject& params) {
    QNetworkReply* reply = m_network.get(makeRequest(apiPath, params));
    connect(reply, &QNetworkReply::finished, this, [this, reply, apiPath]() {
        const QByteArray body = reply->readAll();
        if (reply->error() != QNetworkReply::NoError) {
            emit error(apiPath, reply->errorString());
            reply->deleteLater();
            return;
        }
        const QJsonDocument doc = QJsonDocument::fromJson(body);
        emit response(apiPath, doc.isObject() ? doc.object() : QJsonObject{});
        reply->deleteLater();
    });
}

QNetworkRequest TidalApiClient::makeRequest(const QString& apiPath, const QJsonObject& params) const {
    QUrl url(m_baseUrl + QStringLiteral("/") + apiPath);
    QUrlQuery query;
    for (auto it = params.begin(); it != params.end(); ++it) {
        query.addQueryItem(it.key(), it.value().toVariant().toString());
    }
    if (!query.isEmpty()) {
        url.setQuery(query);
    }
    QNetworkRequest req(url);
    req.setHeader(QNetworkRequest::UserAgentHeader, QStringLiteral("tidal-bitperfect-qt6/0.1"));
    if (!m_accessToken.isEmpty()) {
        req.setRawHeader("Authorization", QStringLiteral("%1 %2").arg(m_tokenType, m_accessToken).toUtf8());
    }
    return req;
}
