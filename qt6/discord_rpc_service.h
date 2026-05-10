#pragma once

#include <QJsonObject>
#include <QObject>
#include <QString>
#include <QTimer>
#include <QLocalSocket>

class DiscordRpcService : public QObject {
    Q_OBJECT

public:
    static constexpr const char* kDefaultClientId = "1465929585698017426";

    explicit DiscordRpcService(QObject* parent = nullptr);
    ~DiscordRpcService() override;

    void setClientId(const QString& clientId);
    QString clientId() const { return m_clientId; }
    bool start();
    void stopService();
    bool running() const { return m_shouldRun; }
    bool connected() const { return m_ready; }

    void updateTrack(const QJsonObject& track, double durationSeconds = 0.0);
    void updateContext(const QString& qualityText, const QString& bitperfectText, bool localPlayback, bool offlineMode, int queueCount);
    void updatePosition(double positionSeconds, double durationSeconds);
    void notifySeeked(double positionSeconds, double durationSeconds);
    void setPlaying(bool playing);
    void clearActivity();

signals:
    void statusMessage(const QString& message);
    void errorMessage(const QString& message);

private slots:
    void onReadyRead();
    void onDisconnected();
    void retryConnect();

private:
    enum Opcode : quint32 {
        Handshake = 0,
        Frame = 1,
        Close = 2,
        Ping = 3,
        Pong = 4,
    };

    QString findIpcPath() const;
    bool connectIpc();
    void sendHandshake();
    void sendFrame(Opcode opcode, const QJsonObject& payload);
    void sendFrame(Opcode opcode, const QByteArray& payload);
    void sendCommand(const QString& command, const QJsonObject& args);
    void handleFrame(Opcode opcode, const QByteArray& payload);
    void updateActivity(bool force = false);
    void suspendActivity(bool force = false);
    void sendNullActivity();
    QJsonObject buildActivity() const;
    QString trackUrl() const;
    QString albumUrl() const;
    QString artistUrl() const;
    QString qualitySummary() const;

    QLocalSocket m_socket;
    QTimer m_retryTimer;
    QByteArray m_buffer;
    QString m_clientId = QString::fromLatin1(kDefaultClientId);
    QJsonObject m_track;
    QString m_qualityText;
    QString m_bitperfectText;
    double m_positionSeconds = 0.0;
    double m_durationSeconds = 0.0;
    int m_queueCount = 0;
    int m_nonce = 1;
    bool m_shouldRun = false;
    bool m_ready = false;
    bool m_playing = false;
    bool m_activitySuspended = false;
    bool m_localPlayback = false;
    bool m_offlineMode = false;
};
