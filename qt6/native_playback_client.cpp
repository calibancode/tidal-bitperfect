#include "native_playback_client.h"

#include "runtime_paths.h"

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
    m_process.write("shutdown\n");
    if (!m_process.waitForFinished(1500)) {
        m_process.terminate();
    }
    if (m_process.state() != QProcess::NotRunning && !m_process.waitForFinished(1000)) {
        m_process.kill();
        m_process.waitForFinished(1000);
    }
    m_busy = false;
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
    m_seenDone = false;
    m_seenError = false;
    m_buffer.clear();
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
    m_process.write("shutdown\n");
    if (!m_process.waitForFinished(1200)) {
        m_process.kill();
        m_process.waitForFinished(1000);
    }
    m_suppressFinished = false;
    m_seenDone = false;
    m_seenError = false;
    m_busy = false;
    m_buffer.clear();
}

void NativePlaybackClient::playFile(const QString& path, const QString& device, int volumePercent) {
    if (m_process.state() != QProcess::NotRunning && (m_busy || m_seenDone)) {
        restartDaemon();
    }
    if (!startDaemon()) return;
    m_busy = true;
    m_seenDone = false;
    m_seenError = false;
    send(QStringList{QStringLiteral("play_file"), path, device, QString::number(volumePercent)}.join('\t'));
}

void NativePlaybackClient::playFfmpeg(
    const QString& input,
    const QString& device,
    int volumePercent,
    const QString& codec,
    double duration,
    bool protocolWhitelist
) {
    if (m_process.state() != QProcess::NotRunning && (m_busy || m_seenDone)) {
        restartDaemon();
    }
    if (!startDaemon()) return;
    m_busy = true;
    m_seenDone = false;
    m_seenError = false;
    send(QStringList{
        QStringLiteral("play_ffmpeg"),
        input,
        device,
        QString::number(volumePercent),
        codec,
        QString::number(qMax(0.0, duration), 'f', 3),
        protocolWhitelist ? QStringLiteral("1") : QStringLiteral("0")
    }.join('\t'));
}

void NativePlaybackClient::setNextTrack(const QString& trackId, const QString& path) {
    send(QStringLiteral("next\t%1\t%2").arg(trackId, path));
}

void NativePlaybackClient::clearNextTrack() {
    send(QStringLiteral("clear_next"));
}

void NativePlaybackClient::stop() {
    send(QStringLiteral("stop"));
}

void NativePlaybackClient::pauseToggle() {
    send(QStringLiteral("pause_toggle"));
}

void NativePlaybackClient::seek(double deltaSeconds) {
    send(QStringLiteral("seek %1").arg(deltaSeconds, 0, 'f', 3));
}

void NativePlaybackClient::seekTo(double seconds) {
    send(QStringLiteral("seek_to %1").arg(seconds, 0, 'f', 3));
}

void NativePlaybackClient::setVolume(int percent) {
    send(QStringLiteral("set_volume %1").arg(percent));
}

void NativePlaybackClient::send(const QString& line) {
    if (m_process.state() == QProcess::NotRunning) {
        return;
    }
    m_process.write(line.toUtf8() + '\n');
}

void NativePlaybackClient::onReadyReadStdout() {
    m_buffer += m_process.readAllStandardOutput();
    while (true) {
        const int nl = m_buffer.indexOf('\n');
        if (nl < 0) {
            return;
        }
        const QByteArray line = m_buffer.left(nl);
        m_buffer.remove(0, nl + 1);
        handleLine(line);
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
    if (!m_seenError && (m_seenDone || exitCode == 0)) {
        emit finishedOk();
    } else if (!m_seenError) {
        emit errorMessage(QStringLiteral("native player exited (%1)").arg(exitCode));
    }
}

void NativePlaybackClient::handleLine(const QByteArray& rawLine) {
    const QString line = QString::fromUtf8(rawLine).trimmed();
    if (line.isEmpty() || line == QStringLiteral("READY")) {
        return;
    }
    if (line == QStringLiteral("DONE")) {
        m_seenDone = true;
        m_busy = false;
        send(QStringLiteral("shutdown"));
        return;
    }
    if (line == QStringLiteral("BYE")) {
        return;
    }
    if (line.startsWith(QStringLiteral("FORMAT "))) {
        const QStringList parts = line.split(' ', Qt::SkipEmptyParts);
        if (parts.size() >= 5) {
            NativeAudioFormat format;
            format.channels = parts[1].toInt();
            format.rate = parts[2].toInt();
            format.bits = parts[3].toInt();
            format.duration = parts[4].toDouble();
            emit formatReady(format);
        }
        return;
    }
    if (line.startsWith(QStringLiteral("POSITION "))) {
        const QStringList parts = line.split(' ', Qt::SkipEmptyParts);
        if (parts.size() >= 3) {
            emit position(parts[1].toDouble(), parts[2].toDouble());
        }
        return;
    }
    if (line.startsWith(QStringLiteral("STATUS "))) {
        emit statusMessage(line.mid(7));
        return;
    }
    if (line.startsWith(QStringLiteral("LOG "))) {
        emit logMessage(line.mid(4));
        return;
    }
    if (line.startsWith(QStringLiteral("ADVANCED "))) {
        emit advanced(line.mid(9));
        return;
    }
    if (line.startsWith(QStringLiteral("ERROR "))) {
        m_seenError = true;
        m_busy = false;
        emit errorMessage(line.mid(6));
    }
}
