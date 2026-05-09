#include "discord_rpc_service.h"

#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonValue>
#include <QRegularExpression>
#include <QSet>
#include <QtEndian>

#include <unistd.h>

namespace {
constexpr const char* kProjectUrl = "https://github.com/calibancode/tidal-bitperfect";

QString oneLine(QString text, int maxLength = 128) {
    text.replace(QLatin1Char('\n'), QLatin1Char(' '));
    text = text.simplified();
    if (text.size() <= maxLength) return text;
    return text.left(qMax(0, maxLength - 1)).trimmed() + QStringLiteral("…");
}

QString strippedLabel(QString text, const QString& prefix) {
    if (text.startsWith(prefix, Qt::CaseInsensitive)) text = text.mid(prefix.size());
    return text.trimmed();
}

QStringList artistNames(const QJsonObject& track) {
    QStringList names;
    for (const QJsonValue& value : track.value(QStringLiteral("artists")).toArray()) {
        if (value.isString()) names.push_back(value.toString());
        else if (value.isObject()) {
            const QString name = value.toObject().value(QStringLiteral("name")).toString();
            if (!name.isEmpty()) names.push_back(name);
        }
    }
    if (names.isEmpty()) {
        const QString display = track.value(QStringLiteral("artist_display")).toString(track.value(QStringLiteral("artist")).toString());
        if (!display.isEmpty()) names.push_back(display);
    }
    return names;
}

QString firstArtist(const QJsonObject& track) {
    const QStringList names = artistNames(track);
    return names.isEmpty() ? QStringLiteral("Unknown Artist") : names.join(QStringLiteral(", "));
}

QString qualityDisplayName(QString quality) {
    quality = quality.trimmed();
    if (quality == QStringLiteral("HI_RES_LOSSLESS")) return QStringLiteral("Max");
    if (quality == QStringLiteral("LOSSLESS")) return QStringLiteral("HiFi");
    if (quality == QStringLiteral("HIGH")) return QStringLiteral("High");
    if (quality == QStringLiteral("LOW")) return QStringLiteral("Low");
    return quality.replace(QLatin1Char('_'), QLatin1Char(' ')).trimmed();
}

void addUnique(QStringList& values, const QString& value) {
    if (!value.isEmpty() && !values.contains(value)) values.push_back(value);
}
}

DiscordRpcService::DiscordRpcService(QObject* parent) : QObject(parent) {
    connect(&m_socket, &QLocalSocket::readyRead, this, &DiscordRpcService::onReadyRead);
    connect(&m_socket, &QLocalSocket::disconnected, this, &DiscordRpcService::onDisconnected);
    connect(&m_socket, &QLocalSocket::errorOccurred, this, [this](QLocalSocket::LocalSocketError) {
        if (m_shouldRun && m_socket.state() == QLocalSocket::UnconnectedState) {
            m_ready = false;
            if (!m_retryTimer.isActive()) m_retryTimer.start();
        }
    });
    m_retryTimer.setInterval(10000);
    m_retryTimer.setSingleShot(false);
    connect(&m_retryTimer, &QTimer::timeout, this, &DiscordRpcService::retryConnect);
}

DiscordRpcService::~DiscordRpcService() {
    stopService();
}

void DiscordRpcService::setClientId(const QString& clientId) {
    const QString next = clientId.trimmed().isEmpty() ? QString::fromLatin1(kDefaultClientId) : clientId.trimmed();
    if (next == m_clientId) return;
    const bool restart = m_shouldRun;
    stopService();
    m_clientId = next;
    if (restart) start();
}

bool DiscordRpcService::start() {
    m_shouldRun = true;
    if (m_ready) return true;
    return connectIpc();
}

void DiscordRpcService::stopService() {
    m_shouldRun = false;
    m_retryTimer.stop();
    if (m_ready && !m_track.isEmpty()) clearActivity();
    m_ready = false;
    m_buffer.clear();
    if (m_socket.state() != QLocalSocket::UnconnectedState) {
        m_socket.disconnectFromServer();
        if (m_socket.state() != QLocalSocket::UnconnectedState && !m_socket.waitForDisconnected(500)) {
            m_socket.abort();
        }
    }
}

void DiscordRpcService::updateTrack(const QJsonObject& track, double durationSeconds) {
    m_track = track;
    m_positionSeconds = 0.0;
    m_durationSeconds = qMax(0.0, durationSeconds);
    m_playing = !track.isEmpty();
    updateActivity(true);
}

void DiscordRpcService::updateContext(const QString& qualityText, const QString& bitperfectText, bool localPlayback, bool offlineMode, int queueCount) {
    m_qualityText = qualityText;
    m_bitperfectText = bitperfectText;
    m_localPlayback = localPlayback;
    m_offlineMode = offlineMode;
    m_queueCount = qMax(0, queueCount);
    updateActivity();
}

void DiscordRpcService::updatePosition(double positionSeconds, double durationSeconds) {
    m_positionSeconds = qMax(0.0, positionSeconds);
    if (durationSeconds > 0.0) m_durationSeconds = durationSeconds;
}

void DiscordRpcService::notifySeeked(double positionSeconds, double durationSeconds) {
    updatePosition(positionSeconds, durationSeconds);
    updateActivity(true);
}

void DiscordRpcService::setPlaying(bool playing) {
    if (m_playing == playing) return;
    m_playing = playing;
    updateActivity(true);
}

void DiscordRpcService::clearActivity() {
    m_track = {};
    m_playing = false;
    m_positionSeconds = 0.0;
    m_durationSeconds = 0.0;
    if (!m_ready) return;
    QJsonObject args{{QStringLiteral("pid"), QCoreApplication::applicationPid()}, {QStringLiteral("activity"), QJsonValue::Null}};
    sendCommand(QStringLiteral("SET_ACTIVITY"), args);
}

QString DiscordRpcService::findIpcPath() const {
    QStringList roots;
    addUnique(roots, QString::fromLocal8Bit(qgetenv("XDG_RUNTIME_DIR")));
    addUnique(roots, QStringLiteral("/run/user/%1").arg(getuid()));
    addUnique(roots, QString::fromLocal8Bit(qgetenv("TMPDIR")));
    addUnique(roots, QStringLiteral("/tmp"));

    QStringList prefixes;
    for (const QString& root : roots) {
        addUnique(prefixes, root);
        addUnique(prefixes, QDir(root).filePath(QStringLiteral("snap.discord")));
        addUnique(prefixes, QDir(root).filePath(QStringLiteral("app/com.discordapp.Discord")));
        addUnique(prefixes, QDir(root).filePath(QStringLiteral("app/dev.vencord.Vesktop")));
    }
    for (const QString& prefix : prefixes) {
        for (int i = 0; i < 10; ++i) {
            const QString path = QDir(prefix).filePath(QStringLiteral("discord-ipc-%1").arg(i));
            if (QFileInfo::exists(path)) return path;
        }
    }
    return QString();
}

bool DiscordRpcService::connectIpc() {
    if (!m_shouldRun) return false;
    if (m_socket.state() != QLocalSocket::UnconnectedState) m_socket.abort();
    m_ready = false;
    m_buffer.clear();
    const QString path = findIpcPath();
    if (path.isEmpty()) {
        if (!m_retryTimer.isActive()) m_retryTimer.start();
        emit statusMessage(QStringLiteral("Discord is not running; Rich Presence will retry"));
        return false;
    }
    m_socket.connectToServer(path);
    if (!m_socket.waitForConnected(1000)) {
        if (!m_retryTimer.isActive()) m_retryTimer.start();
        emit errorMessage(QStringLiteral("Discord IPC connection failed: %1").arg(m_socket.errorString()));
        return false;
    }
    sendHandshake();
    return true;
}

void DiscordRpcService::retryConnect() {
    if (!m_shouldRun || m_ready) {
        m_retryTimer.stop();
        return;
    }
    connectIpc();
}

void DiscordRpcService::sendHandshake() {
    sendFrame(Handshake, QJsonObject{{QStringLiteral("v"), 1}, {QStringLiteral("client_id"), m_clientId}});
}

void DiscordRpcService::sendFrame(Opcode opcode, const QJsonObject& payload) {
    sendFrame(opcode, QJsonDocument(payload).toJson(QJsonDocument::Compact));
}

void DiscordRpcService::sendFrame(Opcode opcode, const QByteArray& payload) {
    if (m_socket.state() != QLocalSocket::ConnectedState) return;
    QByteArray frame;
    frame.resize(8);
    qToLittleEndian<quint32>(static_cast<quint32>(opcode), reinterpret_cast<uchar*>(frame.data()));
    qToLittleEndian<quint32>(static_cast<quint32>(payload.size()), reinterpret_cast<uchar*>(frame.data() + 4));
    frame.append(payload);
    m_socket.write(frame);
    m_socket.flush();
}

void DiscordRpcService::sendCommand(const QString& command, const QJsonObject& args) {
    QJsonObject payload{
        {QStringLiteral("cmd"), command},
        {QStringLiteral("args"), args},
        {QStringLiteral("nonce"), QString::number(m_nonce++)},
    };
    sendFrame(Frame, payload);
}

void DiscordRpcService::onReadyRead() {
    m_buffer += m_socket.readAll();
    while (m_buffer.size() >= 8) {
        const quint32 opcodeValue = qFromLittleEndian<quint32>(reinterpret_cast<const uchar*>(m_buffer.constData()));
        const quint32 length = qFromLittleEndian<quint32>(reinterpret_cast<const uchar*>(m_buffer.constData() + 4));
        if (m_buffer.size() < static_cast<int>(8 + length)) return;
        const QByteArray payload = m_buffer.mid(8, length);
        m_buffer.remove(0, 8 + length);
        handleFrame(static_cast<Opcode>(opcodeValue), payload);
    }
}

void DiscordRpcService::onDisconnected() {
    const bool wasReady = m_ready;
    m_ready = false;
    m_buffer.clear();
    if (m_shouldRun) {
        if (wasReady) emit statusMessage(QStringLiteral("Discord Rich Presence disconnected; retrying"));
        if (!m_retryTimer.isActive()) m_retryTimer.start();
    }
}

void DiscordRpcService::handleFrame(Opcode opcode, const QByteArray& payload) {
    if (opcode == Ping) {
        sendFrame(Pong, payload);
        return;
    }
    if (opcode == Close) {
        m_ready = false;
        emit errorMessage(QStringLiteral("Discord closed the Rich Presence connection"));
        m_socket.abort();
        if (m_shouldRun && !m_retryTimer.isActive()) m_retryTimer.start();
        return;
    }
    if (opcode != Frame) return;
    const QJsonDocument doc = QJsonDocument::fromJson(payload);
    if (!doc.isObject()) return;
    const QJsonObject obj = doc.object();
    const QString event = obj.value(QStringLiteral("evt")).toString();
    if (event == QStringLiteral("READY")) {
        m_ready = true;
        m_retryTimer.stop();
        emit statusMessage(QStringLiteral("Discord Rich Presence connected"));
        updateActivity(true);
        return;
    }
    if (event == QStringLiteral("ERROR")) {
        const QJsonObject data = obj.value(QStringLiteral("data")).toObject();
        emit errorMessage(QStringLiteral("Discord RPC error: %1").arg(data.value(QStringLiteral("message")).toString(QStringLiteral("unknown"))));
    }
}

void DiscordRpcService::updateActivity(bool force) {
    Q_UNUSED(force);
    if (!m_shouldRun) return;
    if (!m_ready) {
        if (m_socket.state() == QLocalSocket::UnconnectedState && !m_retryTimer.isActive()) connectIpc();
        return;
    }
    if (m_track.isEmpty()) {
        clearActivity();
        return;
    }
    sendCommand(QStringLiteral("SET_ACTIVITY"), QJsonObject{{QStringLiteral("pid"), QCoreApplication::applicationPid()}, {QStringLiteral("activity"), buildActivity()}});
}

QJsonObject DiscordRpcService::buildActivity() const {
    const QString title = oneLine(m_track.value(QStringLiteral("title")).toString(QStringLiteral("Unknown Track")));
    const QString artist = oneLine(firstArtist(m_track));
    QString album = oneLine(m_track.value(QStringLiteral("album")).toString(QStringLiteral("Unknown Album")));
    if (album.isEmpty()) album = QStringLiteral("Unknown Album");
    QString state = QStringLiteral("%1 • %2").arg(artist, album);
    if (!m_playing) state += QStringLiteral(" (Paused)");
    state = oneLine(state);

    QJsonObject assets;
    QString coverUrl = m_track.value(QStringLiteral("cover_thumbnail_url")).toString();
    if (coverUrl.isEmpty()) coverUrl = m_track.value(QStringLiteral("cover_url")).toString();
    assets.insert(QStringLiteral("large_image"), coverUrl.isEmpty() ? QStringLiteral("tidal_logo") : coverUrl);
    const QString quality = qualitySummary();
    assets.insert(QStringLiteral("large_text"), oneLine(quality.isEmpty() ? album : quality));
    const QString albumLink = albumUrl();
    const QString trackLink = trackUrl();
    const QString artistLink = artistUrl();
    assets.insert(QStringLiteral("large_url"), QString::fromLatin1(kProjectUrl));

    QJsonObject activity{
        {QStringLiteral("type"), 2},
        {QStringLiteral("name"), artist},
        {QStringLiteral("details"), title},
        {QStringLiteral("state"), state},
        {QStringLiteral("assets"), assets},
    };
    if (!trackLink.isEmpty()) activity.insert(QStringLiteral("details_url"), trackLink);
    const QString stateLink = albumLink.isEmpty() ? artistLink : albumLink;
    if (!stateLink.isEmpty()) activity.insert(QStringLiteral("state_url"), stateLink);

    if (m_playing && m_durationSeconds > 0.0) {
        const qint64 now = QDateTime::currentSecsSinceEpoch();
        const qint64 pos = qMax<qint64>(0, static_cast<qint64>(m_positionSeconds));
        const qint64 remaining = qMax<qint64>(0, static_cast<qint64>(m_durationSeconds - m_positionSeconds));
        activity.insert(QStringLiteral("timestamps"), QJsonObject{{QStringLiteral("start"), now - pos}, {QStringLiteral("end"), now + remaining}});
    }

    QJsonArray buttons;
    if (!trackLink.isEmpty()) buttons.push_back(QJsonObject{{QStringLiteral("label"), QStringLiteral("Open Track")}, {QStringLiteral("url"), trackLink}});
    if (!albumLink.isEmpty()) buttons.push_back(QJsonObject{{QStringLiteral("label"), QStringLiteral("Open Album")}, {QStringLiteral("url"), albumLink}});
    else if (!artistLink.isEmpty()) buttons.push_back(QJsonObject{{QStringLiteral("label"), QStringLiteral("Open Artist")}, {QStringLiteral("url"), artistLink}});
    if (!buttons.isEmpty()) activity.insert(QStringLiteral("buttons"), buttons);
    return activity;
}

QString DiscordRpcService::trackUrl() const {
    const QString id = m_track.value(QStringLiteral("id")).toVariant().toString();
    return id.isEmpty() ? QString() : QStringLiteral("https://tidal.com/track/%1").arg(id);
}

QString DiscordRpcService::albumUrl() const {
    const QString id = m_track.value(QStringLiteral("album_id")).toVariant().toString();
    return id.isEmpty() ? QString() : QStringLiteral("https://tidal.com/album/%1").arg(id);
}

QString DiscordRpcService::artistUrl() const {
    const QString id = m_track.value(QStringLiteral("artist_id")).toVariant().toString();
    return id.isEmpty() ? QString() : QStringLiteral("https://tidal.com/artist/%1").arg(id);
}

QString DiscordRpcService::qualitySummary() const {
    const QString quality = strippedLabel(m_qualityText, QStringLiteral("Quality:"));
    QStringList parts;
    if (quality.isEmpty() || quality == QStringLiteral("—")) return QString();

    const QRegularExpression specRe(QStringLiteral("(\\d+)\\s*-?bit\\s*/\\s*(\\d+(?:\\.\\d+)?)\\s*(?:k?Hz)?"), QRegularExpression::CaseInsensitiveOption);
    const QRegularExpressionMatch match = specRe.match(quality);
    const QString qualityName = (match.hasMatch() ? quality.left(match.capturedStart()) : quality).trimmed();
    if (!qualityName.isEmpty()) parts.push_back(qualityDisplayName(qualityName));

    if (match.hasMatch()) {
        const int bitDepth = match.captured(1).toInt();
        double sampleRate = match.captured(2).toDouble();
        if (sampleRate >= 1000.0) sampleRate /= 1000.0;
        if (parts.isEmpty()) {
            if (bitDepth >= 24 || sampleRate >= 48.0) parts.push_back(QStringLiteral("HiFi+"));
            else if (bitDepth == 16 && qAbs(sampleRate - 44.1) < 0.1) parts.push_back(QStringLiteral("HiFi"));
        }
        parts.push_back(QStringLiteral("%1bit/%2kHz").arg(bitDepth).arg(sampleRate, 0, 'f', 1));
    }

    return parts.join(QStringLiteral(" • "));
}
