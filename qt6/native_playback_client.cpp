#include "native_playback_client.h"

#include "runtime_paths.h"

namespace {

QByteArray encodeFieldValue(const QString& value) {
    constexpr char digits[] = "0123456789ABCDEF";
    const QByteArray raw = value.toUtf8();
    QByteArray out;
    for (const unsigned char c : raw) {
        if (c == '%' || c == '=' || c == '\n' || c == '\r' || c < 0x20 || c > 0x7e) {
            out.append('%');
            out.append(digits[(c >> 4) & 0x0f]);
            out.append(digits[c & 0x0f]);
        } else {
            out.append(static_cast<char>(c));
        }
    }
    return out;
}

int hexValue(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return 10 + c - 'a';
    if (c >= 'A' && c <= 'F') return 10 + c - 'A';
    return -1;
}

QString decodeFieldValue(const QByteArray& value) {
    QByteArray out;
    for (qsizetype i = 0; i < value.size(); ++i) {
        if (value.at(i) == '%' && i + 2 < value.size()) {
            const int hi = hexValue(value.at(i + 1));
            const int lo = hexValue(value.at(i + 2));
            if (hi >= 0 && lo >= 0) {
                out.append(static_cast<char>((hi << 4) | lo));
                i += 2;
                continue;
            }
        }
        out.append(value.at(i));
    }
    return QString::fromUtf8(out);
}

QByteArray encodePayload(const QString& type, const QList<QPair<QString, QString>>& fields) {
    QByteArray payload = type.toUtf8();
    payload.append('\n');
    for (const auto& field : fields) {
        payload.append(field.first.toUtf8());
        payload.append('=');
        payload.append(encodeFieldValue(field.second));
        payload.append('\n');
    }
    return payload;
}

enum class FrameStatus {
    Incomplete,
    Complete,
    Invalid,
};

FrameStatus takeFrame(QByteArray& buffer, QString& type, QMap<QString, QString>& fields) {
    const qsizetype headerEnd = buffer.indexOf('\n');
    if (headerEnd < 0) return FrameStatus::Incomplete;
    const QByteArray header = buffer.left(headerEnd);
    if (header.isEmpty()) return FrameStatus::Invalid;
    bool ok = false;
    const qsizetype payloadSize = header.toLongLong(&ok);
    if (!ok || payloadSize < 0) return FrameStatus::Invalid;
    const qsizetype payloadStart = headerEnd + 1;
    if (buffer.size() < payloadStart + payloadSize) return FrameStatus::Incomplete;

    const QByteArray payload = buffer.mid(payloadStart, payloadSize);
    buffer.remove(0, payloadStart + payloadSize);
    const qsizetype firstNewline = payload.indexOf('\n');
    const QByteArray rawType = firstNewline < 0 ? payload : payload.left(firstNewline);
    type = QString::fromUtf8(rawType);
    fields.clear();
    qsizetype start = firstNewline < 0 ? payload.size() : firstNewline + 1;
    while (start < payload.size()) {
        qsizetype end = payload.indexOf('\n', start);
        if (end < 0) end = payload.size();
        const QByteArray line = payload.mid(start, end - start);
        const qsizetype sep = line.indexOf('=');
        if (sep > 0) {
            fields.insert(QString::fromUtf8(line.left(sep)), decodeFieldValue(line.mid(sep + 1)));
        }
        start = end + 1;
    }
    return FrameStatus::Complete;
}

} // namespace

NativePlaybackClient::NativePlaybackClient(QObject* parent) : QObject(parent) {
    m_process.setProcessChannelMode(QProcess::SeparateChannels);
    connect(&m_process, &QProcess::readyReadStandardOutput, this, &NativePlaybackClient::onReadyReadStdout);
    connect(&m_process, &QProcess::readyReadStandardError, this, &NativePlaybackClient::onReadyReadStderr);
    connect(&m_process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, &NativePlaybackClient::onFinished);
}

NativePlaybackClient::~NativePlaybackClient() {
    shutdown();
}

void NativePlaybackClient::shutdown() {
    if (m_process.state() == QProcess::NotRunning) return;
    m_shuttingDown = true;
    m_suppressFinished = true;
    disconnect(&m_process, nullptr, this, nullptr);
    sendMessage(QStringLiteral("shutdown"));
    if (!m_process.waitForFinished(1500)) {
        m_process.terminate();
    }
    if (m_process.state() != QProcess::NotRunning && !m_process.waitForFinished(1000)) {
        m_process.kill();
        m_process.waitForFinished(1000);
    }
    m_busy = false;
    m_nextTrackSet = false;
    m_nextTrackId.clear();
    m_nextTrackPath.clear();
}

QString NativePlaybackClient::helperPath() const {
    return RuntimePaths::nativePlayerPath();
}

bool NativePlaybackClient::available() const {
    return qgetenv("TIDAL_DISABLE_NATIVE_PLAYER") != "1" && !helperPath().isEmpty();
}

bool NativePlaybackClient::startDaemon() {
    if (m_process.state() != QProcess::NotRunning) {
        return true;
    }
    m_shuttingDown = false;
    m_suppressFinished = false;
    m_seenDone = false;
    m_seenError = false;
    m_buffer.clear();
    m_nextTrackSet = false;
    m_nextTrackId.clear();
    m_nextTrackPath.clear();
    const QString helper = helperPath();
    if (helper.isEmpty()) {
        emit errorMessage(QStringLiteral("native player is not available"));
        return false;
    }
    m_process.start(helper, {QStringLiteral("--daemon")});
    if (!m_process.waitForStarted(3000)) {
        emit errorMessage(QStringLiteral("failed to start native player: %1").arg(m_process.errorString()));
        return false;
    }
    return true;
}

void NativePlaybackClient::restartDaemon() {
    if (m_process.state() == QProcess::NotRunning) return;
    m_suppressFinished = true;
    sendMessage(QStringLiteral("shutdown"));
    if (!m_process.waitForFinished(1200)) {
        m_process.kill();
        m_process.waitForFinished(1000);
    }
    m_suppressFinished = false;
    m_seenDone = false;
    m_seenError = false;
    m_busy = false;
    m_buffer.clear();
    m_nextTrackSet = false;
    m_nextTrackId.clear();
    m_nextTrackPath.clear();
}

void NativePlaybackClient::playFile(const QString& path, const QString& device, int volumePercent) {
    if (m_process.state() != QProcess::NotRunning && m_busy) {
        restartDaemon();
    }
    if (!startDaemon()) return;
    m_busy = true;
    m_seenDone = false;
    m_seenError = false;
    sendMessage(QStringLiteral("play_file"), {
        {QStringLiteral("path"), path},
        {QStringLiteral("device"), device},
        {QStringLiteral("volume"), QString::number(volumePercent)}
    });
}

void NativePlaybackClient::playFfmpeg(
    const QString& input,
    const QString& device,
    int volumePercent,
    const QString& codec,
    double duration,
    bool protocolWhitelist,
    bool smoothTransition
) {
    if (m_process.state() != QProcess::NotRunning && m_busy) {
        restartDaemon();
    }
    if (!startDaemon()) return;
    m_busy = true;
    m_seenDone = false;
    m_seenError = false;
    sendMessage(QStringLiteral("play_ffmpeg"), {
        {QStringLiteral("input"), input},
        {QStringLiteral("device"), device},
        {QStringLiteral("volume"), QString::number(volumePercent)},
        {QStringLiteral("codec"), codec},
        {QStringLiteral("duration"), QString::number(qMax(0.0, duration), 'f', 3)},
        {QStringLiteral("protocol"), protocolWhitelist ? QStringLiteral("1") : QStringLiteral("0")},
        {QStringLiteral("smooth_transition"), smoothTransition ? QStringLiteral("1") : QStringLiteral("0")}
    });
}

void NativePlaybackClient::setNextTrack(const QString& trackId, const QString& path) {
    if (m_process.state() == QProcess::NotRunning || trackId.isEmpty() || path.isEmpty()) {
        return;
    }
    if (m_nextTrackSet && m_nextTrackId == trackId && m_nextTrackPath == path) {
        return;
    }
    m_nextTrackSet = true;
    m_nextTrackId = trackId;
    m_nextTrackPath = path;
    sendMessage(QStringLiteral("next"), {{QStringLiteral("track_id"), trackId}, {QStringLiteral("path"), path}});
}

void NativePlaybackClient::clearNextTrack() {
    if (!m_nextTrackSet) {
        return;
    }
    m_nextTrackSet = false;
    m_nextTrackId.clear();
    m_nextTrackPath.clear();
    sendMessage(QStringLiteral("clear_next"));
}

void NativePlaybackClient::stop() {
    sendMessage(QStringLiteral("stop"));
}

void NativePlaybackClient::pauseToggle() {
    sendMessage(QStringLiteral("pause_toggle"));
}

void NativePlaybackClient::seek(double deltaSeconds) {
    sendMessage(QStringLiteral("seek"), {{QStringLiteral("seconds"), QString::number(deltaSeconds, 'f', 3)}});
}

void NativePlaybackClient::seekTo(double seconds) {
    sendMessage(QStringLiteral("seek_to"), {{QStringLiteral("seconds"), QString::number(seconds, 'f', 3)}});
}

void NativePlaybackClient::setVolume(int percent) {
    sendMessage(QStringLiteral("set_volume"), {{QStringLiteral("percent"), QString::number(percent)}});
}

void NativePlaybackClient::sendMessage(const QString& type, const QList<QPair<QString, QString>>& fields) {
    if (m_process.state() == QProcess::NotRunning) {
        return;
    }
    const QByteArray payload = encodePayload(type, fields);
    m_process.write(QByteArray::number(payload.size()) + '\n' + payload);
}

void NativePlaybackClient::onReadyReadStdout() {
    m_buffer += m_process.readAllStandardOutput();
    while (true) {
        QString type;
        QMap<QString, QString> fields;
        const FrameStatus status = takeFrame(m_buffer, type, fields);
        if (status == FrameStatus::Incomplete) {
            return;
        }
        if (status == FrameStatus::Invalid) {
            m_buffer.clear();
            m_seenError = true;
            m_busy = false;
            emit errorMessage(QStringLiteral("invalid native player IPC frame"));
            return;
        }
        handleMessage(type, fields);
    }
}

void NativePlaybackClient::onReadyReadStderr() {
    const QString text = QString::fromUtf8(m_process.readAllStandardError()).trimmed();
    if (!text.isEmpty()) {
        emit logMessage(QStringLiteral("native stderr: %1").arg(text));
    }
}

void NativePlaybackClient::onFinished(int exitCode, QProcess::ExitStatus status) {
    Q_UNUSED(status);
    m_busy = false;
    if (m_suppressFinished || m_shuttingDown) {
        return;
    }
    if (!m_seenError && !m_seenDone && exitCode == 0) {
        emit finishedOk();
    } else if (!m_seenError && !m_seenDone) {
        emit errorMessage(QStringLiteral("native player exited (%1)").arg(exitCode));
    }
}

void NativePlaybackClient::handleMessage(const QString& type, const QMap<QString, QString>& fields) {
    if (type.isEmpty() || type == QStringLiteral("READY")) {
        return;
    }
    if (type == QStringLiteral("DONE")) {
        m_seenDone = true;
        const bool wasBusy = m_busy;
        m_busy = false;
        m_nextTrackSet = false;
        m_nextTrackId.clear();
        m_nextTrackPath.clear();
        if (wasBusy && !m_seenError && !m_suppressFinished) emit finishedOk();
        return;
    }
    if (type == QStringLiteral("BYE")) {
        return;
    }
    if (type == QStringLiteral("FORMAT")) {
        NativeAudioFormat format;
        format.channels = fields.value(QStringLiteral("channels")).toInt();
        format.rate = fields.value(QStringLiteral("rate")).toInt();
        format.bits = fields.value(QStringLiteral("bits")).toInt();
        format.duration = fields.value(QStringLiteral("duration")).toDouble();
        format.sourceChannels = fields.value(QStringLiteral("source_channels")).toInt();
        format.sourceRate = fields.value(QStringLiteral("source_rate")).toInt();
        format.sourceBits = fields.value(QStringLiteral("source_bits")).toInt();
        emit formatReady(format);
        return;
    }
    if (type == QStringLiteral("POSITION")) {
        emit position(fields.value(QStringLiteral("seconds")).toDouble(), fields.value(QStringLiteral("duration")).toDouble());
        return;
    }
    if (type == QStringLiteral("STATUS")) {
        emit statusMessage(fields.value(QStringLiteral("message")));
        return;
    }
    if (type == QStringLiteral("LOG")) {
        emit logMessage(fields.value(QStringLiteral("message")));
        return;
    }
    if (type == QStringLiteral("ADVANCED")) {
        m_nextTrackSet = false;
        m_nextTrackId.clear();
        m_nextTrackPath.clear();
        emit advanced(fields.value(QStringLiteral("track_id"), fields.value(QStringLiteral("message"))));
        return;
    }
    if (type == QStringLiteral("ERROR")) {
        m_seenError = true;
        m_busy = false;
        m_nextTrackSet = false;
        m_nextTrackId.clear();
        m_nextTrackPath.clear();
        emit errorMessage(fields.value(QStringLiteral("message")));
    }
}
