#pragma once

#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QObject>

class TidalApiClient : public QObject {
    Q_OBJECT

public:
    explicit TidalApiClient(QObject* parent = nullptr);

    void setAccessToken(const QString& tokenType, const QString& accessToken);
    void get(const QString& apiPath, const QJsonObject& params = {});

signals:
    void response(const QString& apiPath, const QJsonObject& payload);
    void error(const QString& apiPath, const QString& message);

private:
    QNetworkRequest makeRequest(const QString& apiPath, const QJsonObject& params) const;

    QNetworkAccessManager m_network;
    QString m_tokenType = QStringLiteral("Bearer");
    QString m_accessToken;
    QString m_baseUrl = QStringLiteral("https://api.tidal.com/v1");
};
